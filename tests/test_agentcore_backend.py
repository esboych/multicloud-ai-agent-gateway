from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from multicloud_ai_agent_gateway.backends.agentcore import (
    AgentCoreBackend,
    detect_agent_error,
    parse_invoke_response,
)
from multicloud_ai_agent_gateway.core.backend import AgentReturnedError


@dataclass
class _FakeAgentCoreClient:
    response: dict[str, Any]
    received: list[dict[str, Any]] = field(default_factory=list)

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.received.append(kwargs)
        return self.response


def _backend_for(client: _FakeAgentCoreClient) -> AgentCoreBackend:
    async def factory() -> Any:
        return client

    return AgentCoreBackend(client_factory=factory)


# ---------------------------------------------------------------------------
# parse_invoke_response: pure transform
# ---------------------------------------------------------------------------


def test_parse_extracts_output_text_from_json_dict() -> None:
    response = {
        "response": json.dumps({"output_text": "hello world"}).encode(),
        "contentType": "application/json",
        "runtimeSessionId": "sess-1",
    }
    reply = parse_invoke_response(response)
    assert reply.output_text == "hello world"
    assert reply.conversation_id == "sess-1"
    assert reply.tool_events == []
    assert reply.finish_reason == "completed"


def test_parse_falls_back_to_other_known_keys() -> None:
    for key in ("text", "result", "output", "message"):
        body = json.dumps({key: f"value-{key}"}).encode()
        reply = parse_invoke_response({"response": body, "contentType": "application/json"})
        assert reply.output_text == f"value-{key}"


def test_parse_serializes_unknown_json_shape() -> None:
    body = json.dumps({"completion": "ignored", "extra": 1}).encode()
    reply = parse_invoke_response({"response": body, "contentType": "application/json"})
    parsed = json.loads(reply.output_text)
    assert parsed == {"completion": "ignored", "extra": 1}


def test_parse_returns_raw_text_for_non_json_content_type() -> None:
    reply = parse_invoke_response(
        {"response": b"plain text reply", "contentType": "text/plain"}
    )
    assert reply.output_text == "plain text reply"


def test_parse_returns_raw_text_when_json_content_type_but_invalid_json() -> None:
    reply = parse_invoke_response(
        {"response": b"{not json", "contentType": "application/json"}
    )
    assert reply.output_text == "{not json"


def test_parse_handles_streaming_body_like_object() -> None:
    body = io.BytesIO(json.dumps({"output_text": "streamed"}).encode())
    reply = parse_invoke_response({"response": body, "contentType": "application/json"})
    assert reply.output_text == "streamed"


def test_parse_empty_body() -> None:
    reply = parse_invoke_response({"response": None})
    assert reply.output_text == ""


# ---------------------------------------------------------------------------
# AgentCoreBackend invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_sends_payload_and_returns_reply() -> None:
    client = _FakeAgentCoreClient(
        response={
            "response": json.dumps({"output_text": "ok"}).encode(),
            "contentType": "application/json",
            "runtimeSessionId": "sess-7",
        }
    )
    backend = _backend_for(client)
    reply = await backend.invoke(
        "arn:aws:bedrock-agentcore:us-west-2:1:runtime/abc",
        "hello",
    )

    assert reply.output_text == "ok"
    assert reply.conversation_id == "sess-7"
    assert len(client.received) == 1
    call = client.received[0]
    assert call["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:us-west-2:1:runtime/abc"
    assert json.loads(call["payload"]) == {"prompt": "hello"}
    assert call["contentType"] == "application/json"
    assert call["accept"] == "application/json"
    assert "runtimeSessionId" not in call


@pytest.mark.asyncio
async def test_invoke_passes_conversation_id_as_runtime_session_id() -> None:
    client = _FakeAgentCoreClient(
        response={
            "response": json.dumps({"output_text": "next"}).encode(),
            "contentType": "application/json",
            "runtimeSessionId": "sess-from-server",
        }
    )
    backend = _backend_for(client)
    await backend.invoke("arn:1", "more", conversation_id="sess-prev")

    assert client.received[0]["runtimeSessionId"] == "sess-prev"


@pytest.mark.asyncio
async def test_invoke_falls_back_to_input_conversation_id_when_response_omits_it() -> None:
    client = _FakeAgentCoreClient(
        response={
            "response": json.dumps({"output_text": "x"}).encode(),
            "contentType": "application/json",
        }
    )
    backend = _backend_for(client)
    reply = await backend.invoke("arn:1", "p", conversation_id="caller-supplied")
    assert reply.conversation_id == "caller-supplied"


@pytest.mark.asyncio
async def test_invoke_rejects_empty_remote_id() -> None:
    client = _FakeAgentCoreClient(response={"response": b""})
    backend = _backend_for(client)
    with pytest.raises(ValueError, match="runtime ARN"):
        await backend.invoke("", "prompt")


@pytest.mark.asyncio
async def test_constructor_requires_bridge_or_factory() -> None:
    with pytest.raises(ValueError, match="bridge or client_factory"):
        AgentCoreBackend()


# ---------------------------------------------------------------------------
# detect_agent_error: server-side error payloads inside a 200 reply
# ---------------------------------------------------------------------------


def test_detect_returns_none_for_normal_response() -> None:
    assert detect_agent_error({"output_text": "all good"}, "all good") is None


def test_detect_handles_aws_typed_error() -> None:
    # AWS APIs serialize errors with __type and message at the top level.
    detected = detect_agent_error(
        {"__type": "ThrottlingException", "message": "Rate exceeded"},
        '{"__type": "ThrottlingException", "message": "Rate exceeded"}',
    )
    assert detected == ("ThrottlingException", "Rate exceeded")


def test_detect_strips_namespace_prefix_from_typed_error() -> None:
    detected = detect_agent_error(
        {"__type": "com.amazonaws.bedrock#AccessDeniedException", "message": "no"},
        "{}",
    )
    assert detected == ("AccessDeniedException", "no")


def test_detect_handles_lambda_style_error_keys() -> None:
    detected = detect_agent_error(
        {"errorType": "ValidationException", "errorMessage": "bad input"},
        "{}",
    )
    assert detected == ("ValidationException", "bad input")


def test_detect_handles_text_starting_with_exception_name() -> None:
    # Agent runtime caught the exception and stuffed it into a message field;
    # the chosen text starts with the exception name. The detector strips the
    # prefix so the message is just the human-readable part.
    text = "ThrottlingException: Too many tokens per day, please wait before trying again."
    detected = detect_agent_error({"message": text}, text)
    assert detected == (
        "ThrottlingException",
        "Too many tokens per day, please wait before trying again.",
    )


def test_detect_does_not_misclassify_long_response_mentioning_exceptions() -> None:
    # A real FinOps explanation that happens to discuss exceptions in its body.
    long_text = (
        "Found 3 cost anomalies. The first is a stale ResourceNotFoundException in "
        "container insights logs that you can safely ignore. " * 20
    )
    assert detect_agent_error({"output_text": long_text}, long_text) is None


def test_detect_does_not_misclassify_response_starting_with_capitalized_word() -> None:
    text = "Service reports 5% cost growth this month versus last."
    assert detect_agent_error({"output_text": text}, text) is None


# ---------------------------------------------------------------------------
# parse_invoke_response error-raising integration
# ---------------------------------------------------------------------------


def test_parse_raises_agent_returned_error_for_typed_payload() -> None:
    body = json.dumps(
        {"__type": "ThrottlingException", "message": "Too many tokens per day"}
    ).encode()
    with pytest.raises(AgentReturnedError) as info:
        parse_invoke_response({"response": body, "contentType": "application/json"})
    assert info.value.error_type == "ThrottlingException"
    assert "Too many tokens" in str(info.value)


def test_parse_raises_agent_returned_error_for_message_field_text() -> None:
    body = json.dumps(
        {"message": "ThrottlingException: Too many tokens per day, please wait before trying again."}
    ).encode()
    with pytest.raises(AgentReturnedError) as info:
        parse_invoke_response({"response": body, "contentType": "application/json"})
    assert info.value.error_type == "ThrottlingException"


def test_parse_raises_for_plain_text_error_response() -> None:
    # The text heuristic intentionally fires regardless of content type: if the
    # agent returned a short string starting with '<Word>Exception: ...' or
    # '<Word>Error: ...' it almost certainly forwarded a server-side error.
    body = b"ThrottlingException: this is just text"
    with pytest.raises(AgentReturnedError) as info:
        parse_invoke_response({"response": body, "contentType": "text/plain"})
    assert info.value.error_type == "ThrottlingException"


@pytest.mark.asyncio
async def test_invoke_propagates_agent_returned_error() -> None:
    client = _FakeAgentCoreClient(
        response={
            "response": json.dumps(
                {"__type": "ThrottlingException", "message": "Rate exceeded"}
            ).encode(),
            "contentType": "application/json",
        }
    )
    backend = _backend_for(client)
    with pytest.raises(AgentReturnedError):
        await backend.invoke("arn:1", "p")
