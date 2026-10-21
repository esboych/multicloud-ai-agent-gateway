"""Concrete DirectBackend handlers.

Each builder here returns a closure that captures the boto3 / Azure SDK
clients it needs and exposes the ``(prompt, conversation_id) -> AgentReply``
signature that ``DirectBackend`` calls. Handlers are deterministic --
they do not look up a model, they just call a cloud API.

Currently registered:

  - demo_echo:
      Zero-dependency handler used by the local quickstart and the test
      suite. Echoes the prompt back; touches no cloud API.

  - aws_cost_mtd_by_service:
      AWS Cost Explorer 'GetCostAndUsage' for the current calendar month
      to date, grouped by SERVICE dimension. Auth via AwsBridgeCredentials
      (same workload identity path AgentCoreBackend uses).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from multicloud_ai_agent_gateway.auth.aws_bridge import AwsBridgeCredentials
from multicloud_ai_agent_gateway.backends.direct import DirectHandler
from multicloud_ai_agent_gateway.core.backend import AgentReply, ToolEvent

log = logging.getLogger(__name__)


def build_demo_echo_handler(*, delay_ms: int = 0) -> DirectHandler:
    """Return a handler that echoes the prompt without touching any cloud.

    Exists so ``python -m multicloud_ai_agent_gateway`` has something to serve
    on a laptop with no cloud credentials, and so the full request/response
    shape (``output_text``, ``tool_events``, ``usage``, ``routing``) can be
    exercised end to end in tests. ``delay_ms`` sleeps (without blocking the
    event loop) to stand in for a slow backend in load tests.
    """
    delay_seconds = max(0, int(delay_ms)) / 1000.0

    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        words = prompt.split()
        tool_event = ToolEvent(
            name="demo_echo",
            arguments={"prompt": prompt},
            result={"word_count": len(words)},
        )
        # Mint a conversation id on the first turn so the demo also exercises
        # the conversation store (sticky AutoRouter, /v1/conversations/{id}).
        return AgentReply(
            output_text=f"DemoAgent received {len(words)} word(s): {prompt}",
            conversation_id=conversation_id or f"demo-{uuid.uuid4().hex[:12]}",
            tool_events=[tool_event],
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            finish_reason="completed",
        )

    return handler


def build_aws_cost_mtd_by_service_handler(
    bridge: AwsBridgeCredentials,
    *,
    region: str = "us-east-1",
) -> DirectHandler:
    """Return a handler that fetches current-month AWS cost by service.

    The handler is *intentionally* parameter-free w.r.t. the prompt: any
    prompt routed here gets MTD-by-service. Variations (last 7 days, by
    region, etc.) need a different agent / handler -- we trade flexibility
    for predictability and zero LLM tokens.
    """

    async def handler(prompt: str, conversation_id: str | None) -> AgentReply:
        # Auth lock + AAD token exchange stay on the main loop -- the
        # bridge uses an asyncio.Lock that is loop-bound. boto3 itself is
        # blocking, so we hand the actual CE call off to a worker thread.
        creds = await bridge.get_credentials()
        cost_groups, period = await asyncio.to_thread(
            _call_cost_explorer_sync, creds, region
        )
        markdown = _render_markdown(cost_groups, period)
        # We surface the raw CE response as a ToolEvent so downstream UIs
        # (and any analytics) can reconstruct the structured data without
        # re-parsing markdown.
        tool_event = ToolEvent(
            name="aws_cost_mtd_by_service",
            arguments={"region": region, "period": period},
            result={"cost_groups": cost_groups, "period": period},
        )
        return AgentReply(
            output_text=markdown,
            conversation_id=None,  # stateless: no conversation to continue
            tool_events=[tool_event],
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            finish_reason="completed",
        )

    return handler


def _call_cost_explorer_sync(
    creds: dict[str, Any], region: str
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Synchronous Cost Explorer call. Run via asyncio.to_thread.

    Takes already-resolved AWS credentials (a dict from
    ``AwsBridgeCredentials.get_credentials()``) so this function does not
    need an event loop -- it's pure boto3.
    """
    # Lazy import: boto3 is already a dep but keep startup cost off the hot path.
    import boto3

    client = boto3.client(
        "ce",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    today = datetime.now(timezone.utc).date()
    period_start = today.replace(day=1)
    # Cost Explorer treats End as exclusive; passing today gives MTD.
    period = {"Start": period_start.isoformat(), "End": today.isoformat()}

    # If today is the 1st of the month, Start == End -> CE returns 400.
    # In that case widen to 'yesterday..today' so MTD page on day-1 still
    # works (it'll show the carry-over from the previous month's last day
    # only, which is the honest answer).
    if period_start == today:
        from datetime import timedelta as _td

        period = {
            "Start": (today - _td(days=1)).isoformat(),
            "End": today.isoformat(),
        }

    response = client.get_cost_and_usage(
        TimePeriod=period,
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    groups = []
    for bucket in response.get("ResultsByTime", []):
        for group in bucket.get("Groups", []):
            service = group["Keys"][0] if group.get("Keys") else "(unknown)"
            amount_str = group["Metrics"]["UnblendedCost"]["Amount"]
            unit = group["Metrics"]["UnblendedCost"]["Unit"]
            groups.append(
                {
                    "service": service,
                    "amount": float(Decimal(amount_str)),
                    "unit": unit,
                }
            )
    # Highest-cost services first; that's what a human reading the report wants.
    groups.sort(key=lambda g: g["amount"], reverse=True)
    return groups, period


def _render_markdown(
    cost_groups: list[dict[str, Any]], period: dict[str, str]
) -> str:
    """Format CE response as a compact markdown block."""
    if not cost_groups:
        return (
            f"No AWS spend recorded between {period['Start']} and {period['End']}.\n"
            "(Source: AWS Cost Explorer, classic deterministic agent, no LLM tokens used.)"
        )

    total = sum(g["amount"] for g in cost_groups)
    unit = cost_groups[0]["unit"]

    lines = [
        f"### AWS costs {period['Start']} → {period['End']}",
        "",
        f"Total: **{total:,.2f} {unit}** across {len(cost_groups)} services.",
        "",
        "| Service | Spend |",
        "| --- | ---: |",
    ]
    for group in cost_groups[:15]:
        lines.append(f"| {group['service']} | {group['amount']:,.2f} {group['unit']} |")
    if len(cost_groups) > 15:
        rest = sum(g["amount"] for g in cost_groups[15:])
        lines.append(f"| _other ({len(cost_groups) - 15} services)_ | {rest:,.2f} {unit} |")
    lines += [
        "",
        "_Source: AWS Cost Explorer, classic deterministic agent (no LLM tokens used)._",
    ]
    return "\n".join(lines)


__all__: list[str] = ["build_aws_cost_mtd_by_service_handler"]
