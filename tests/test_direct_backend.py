"""Tests for DirectBackend + the AWS classic FinOps handler.

DirectBackend dispatches in-process to async handlers keyed by ``remote_id``.
The AWS classic handler reads workload-identity credentials from
``AwsBridgeCredentials`` and calls Cost Explorer via boto3. Both are unit
tested here without hitting AWS by stubbing the bridge and the boto3 client.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from multicloud_ai_agent_gateway.backends.direct import DirectBackend
from multicloud_ai_agent_gateway.backends.direct_handlers import (
    _render_markdown,
    build_aws_cost_mtd_by_service_handler,
)
from multicloud_ai_agent_gateway.core.backend import (
    AgentReply,
    StreamDone,
    TextChunk,
    UnknownAgent,
)

# ---------------------------------------------------------------------------
# DirectBackend dispatch + streaming shape
# ---------------------------------------------------------------------------


async def test_direct_backend_invokes_handler_by_remote_id() -> None:
    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        return AgentReply(
            output_text=f"got:{prompt}",
            conversation_id=None,
            usage={"total_tokens": 0},
        )

    backend = DirectBackend({"my_handler": handler})
    reply = await backend.invoke("my_handler", "hello")
    assert reply.output_text == "got:hello"
    assert reply.usage == {"total_tokens": 0}


async def test_direct_backend_unknown_remote_id_raises() -> None:
    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        return AgentReply(output_text="x", conversation_id=None)

    backend = DirectBackend({"only": handler})
    with pytest.raises(UnknownAgent):
        await backend.invoke("not_registered", "hi")


async def test_direct_backend_stream_emits_chunk_then_done() -> None:
    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        return AgentReply(
            output_text="hello world",
            conversation_id=None,
            usage={"total_tokens": 0},
            finish_reason="completed",
        )

    backend = DirectBackend({"h": handler})
    events = [event async for event in backend.stream("h", "ping")]
    assert len(events) == 2
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "hello world"
    assert isinstance(events[1], StreamDone)
    assert events[1].finish_reason == "completed"


def test_direct_backend_requires_at_least_one_handler() -> None:
    with pytest.raises(ValueError):
        DirectBackend({})


# ---------------------------------------------------------------------------
# AWS cost handler: markdown rendering
# ---------------------------------------------------------------------------


def test_render_markdown_handles_empty_response() -> None:
    out = _render_markdown([], {"Start": "2026-05-01", "End": "2026-05-12"})
    assert "No AWS spend recorded" in out
    assert "2026-05-01" in out
    assert "no LLM tokens used" in out


def test_render_markdown_sorts_services_by_spend_desc() -> None:
    groups = [
        {"service": "Cheap", "amount": 1.0, "unit": "USD"},
        {"service": "Big", "amount": 100.0, "unit": "USD"},
        {"service": "Medium", "amount": 50.0, "unit": "USD"},
    ]
    out = _render_markdown(
        sorted(groups, key=lambda g: g["amount"], reverse=True),
        {"Start": "2026-05-01", "End": "2026-05-12"},
    )
    # Big comes before Medium comes before Cheap.
    assert out.index("Big") < out.index("Medium") < out.index("Cheap")
    assert "Total: **151.00 USD**" in out


def test_render_markdown_collapses_long_tail() -> None:
    """More than 15 services -> the rest are rolled up into one 'other' row."""
    groups = [
        {"service": f"Svc{n}", "amount": 100.0 - n, "unit": "USD"}
        for n in range(20)
    ]
    out = _render_markdown(groups, {"Start": "2026-05-01", "End": "2026-05-12"})
    assert "_other (5 services)_" in out


# ---------------------------------------------------------------------------
# AWS cost handler: end-to-end with stubbed bridge + boto3
# ---------------------------------------------------------------------------


class _StubBridge:
    """Async stub mimicking AwsBridgeCredentials.get_credentials shape."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_credentials(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "AccessKeyId": "AKIA-STUB",
            "SecretAccessKey": "secret-stub",
            "SessionToken": "session-stub",
            "Expiration": datetime.now(timezone.utc),
        }


def _ce_response(amount: float = 42.0) -> dict[str, Any]:
    return {
        "ResultsByTime": [
            {
                "Groups": [
                    {
                        "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                        "Metrics": {
                            "UnblendedCost": {
                                "Amount": f"{amount:.2f}",
                                "Unit": "USD",
                            }
                        },
                    },
                    {
                        "Keys": ["Amazon Simple Storage Service"],
                        "Metrics": {
                            "UnblendedCost": {
                                "Amount": "3.14",
                                "Unit": "USD",
                            }
                        },
                    },
                ]
            }
        ]
    }


async def test_aws_classic_handler_returns_reply_with_markdown_and_tool_event() -> None:
    bridge = _StubBridge()
    handler = build_aws_cost_mtd_by_service_handler(bridge, region="us-east-1")  # type: ignore[arg-type]

    fake_client = type("FakeCE", (), {"get_cost_and_usage": lambda self, **kw: _ce_response()})()
    with patch("boto3.client", return_value=fake_client) as mock_boto:
        reply = await handler("Current AWS costs", None)

    # Bridge must be hit once.
    assert bridge.calls == 1
    # boto3.client called once with ce + region + creds.
    mock_boto.assert_called_once()
    call_kwargs = mock_boto.call_args.kwargs
    assert mock_boto.call_args.args == ("ce",) or call_kwargs.get("service_name") == "ce"
    assert call_kwargs["region_name"] == "us-east-1"
    assert call_kwargs["aws_access_key_id"] == "AKIA-STUB"
    # Cost groups land in the markdown ordered by spend desc.
    assert reply.output_text.index("Compute") < reply.output_text.index("Storage")
    # Zero tokens accounting -- this is the whole point of the agent.
    assert reply.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    # Structured data preserved as a tool event for downstream analytics.
    assert reply.tool_events
    event = reply.tool_events[0]
    assert event.name == "aws_cost_mtd_by_service"
    assert event.result is not None
    assert len(event.result["cost_groups"]) == 2


async def test_aws_classic_handler_period_is_current_month_to_date() -> None:
    bridge = _StubBridge()
    handler = build_aws_cost_mtd_by_service_handler(bridge)  # type: ignore[arg-type]

    captured: dict[str, Any] = {}

    class _CapturingClient:
        def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return _ce_response()

    with patch("boto3.client", return_value=_CapturingClient()):
        await handler("anything", None)

    period = captured["TimePeriod"]
    today = date.today()
    expected_start = today.replace(day=1)
    # Day-1 edge case is handled by widening the window; otherwise MTD applies.
    if expected_start == today:
        assert period["End"] == today.isoformat()
    else:
        assert period["Start"] == expected_start.isoformat()
        assert period["End"] == today.isoformat()
    assert captured["Granularity"] == "MONTHLY"
    assert captured["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
