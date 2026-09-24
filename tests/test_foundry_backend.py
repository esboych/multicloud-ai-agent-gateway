from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from multicloud_ai_agent_gateway.backends.foundry import FoundryBackend, run_tool_loop
from multicloud_ai_agent_gateway.core.backend import (
    NoDispatcherConfigured,
    StreamDone,
    StreamError,
    TextChunk,
    ToolBudgetExceeded,
    ToolFinished,
    ToolStarted,
)


def _completed(text: str = "done", id_: str = "resp_final") -> dict[str, Any]:
    return {
        "id": id_,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "output_text": text,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _function_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": "resp_tool",
        "status": "requires_action",
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(args),
            }
        ],
        "output_text": None,
    }


def test_loop_returns_immediately_when_initial_response_is_completed() -> None:
    initial = _completed("hi")
    calls: list[tuple[list[dict[str, Any]], str | None]] = []

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        calls.append((outputs, prev))
        raise AssertionError("respond_fn should not be called when no tools were requested")

    reply = run_tool_loop(initial, respond_fn=respond_fn, dispatcher=None, max_rounds=5)
    assert reply.output_text == "hi"
    assert reply.conversation_id == "resp_final"
    assert reply.tool_events == []
    assert calls == []


def test_loop_dispatches_function_call_and_continues() -> None:
    initial = _function_call("doStuff", {"x": 1})
    follow_up = _completed("after tool", id_="resp_follow")

    received_outputs: list[list[dict[str, Any]]] = []

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        received_outputs.append(outputs)
        assert prev == "resp_tool"
        return follow_up

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert name == "doStuff"
        assert args == {"x": 1}
        return {"answer": 42}

    reply = run_tool_loop(initial, respond_fn=respond_fn, dispatcher=dispatcher, max_rounds=5)

    assert reply.output_text == "after tool"
    assert reply.conversation_id == "resp_follow"
    assert len(reply.tool_events) == 1
    assert reply.tool_events[0].name == "doStuff"
    assert reply.tool_events[0].result == {"answer": 42}
    assert reply.tool_events[0].error is None

    assert len(received_outputs) == 1
    [first] = received_outputs[0]
    assert first["type"] == "function_call_output"
    assert first["call_id"] == "call_1"
    assert json.loads(first["output"]) == {"answer": 42}


def test_loop_records_dispatcher_exception_as_error_and_continues() -> None:
    initial = _function_call("bad", {})
    follow_up = _completed("recovered")

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        return follow_up

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    reply = run_tool_loop(initial, respond_fn=respond_fn, dispatcher=dispatcher, max_rounds=5)

    assert reply.output_text == "recovered"
    assert len(reply.tool_events) == 1
    event = reply.tool_events[0]
    assert event.error is not None
    assert "RuntimeError" in event.error
    assert event.result == {"error": event.error}


def test_loop_raises_when_no_dispatcher_configured() -> None:
    initial = _function_call("any", {})

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        raise AssertionError("respond_fn should not be called")

    with pytest.raises(NoDispatcherConfigured) as info:
        run_tool_loop(initial, respond_fn=respond_fn, dispatcher=None, max_rounds=5)
    assert info.value.function_name == "any"


def test_loop_enforces_max_rounds() -> None:
    initial = _function_call("loopy", {})
    forever = _function_call("loopy", {}, call_id="call_n")

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        return forever

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"keep": "going"}

    with pytest.raises(ToolBudgetExceeded) as info:
        run_tool_loop(initial, respond_fn=respond_fn, dispatcher=dispatcher, max_rounds=3)
    assert info.value.rounds == 3


def test_loop_handles_string_arguments_that_are_not_json() -> None:
    initial = {
        "id": "r1",
        "status": "requires_action",
        "output": [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "f",
                "arguments": "not-json",
            }
        ],
    }

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        return _completed("ok")

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert args == {}
        return {"ack": True}

    reply = run_tool_loop(initial, respond_fn=respond_fn, dispatcher=dispatcher, max_rounds=2)
    assert reply.tool_events[0].arguments == {}
    assert reply.output_text == "ok"


def test_loop_ignores_non_function_call_output_items() -> None:
    initial = {
        "id": "r1",
        "status": "requires_action",
        "output": [
            {"type": "reasoning", "summary": "thinking..."},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "f",
                "arguments": "{}",
            },
            {"type": "message", "content": []},
        ],
    }

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        assert len(outputs) == 1
        return _completed("after")

    reply = run_tool_loop(
        initial, respond_fn=respond_fn, dispatcher=lambda n, a: {"ok": True}, max_rounds=3
    )
    assert reply.output_text == "after"
    assert len(reply.tool_events) == 1


async def test_foundry_invoke_retries_rate_limit_with_fallback_agent() -> None:
    backend = FoundryBackend(
        "https://example.test/project",
        dispatcher=None,
        fallback_agent_names={"PrimaryAgent": "FallbackAgent"},
    )
    backend._ensure_started = AsyncMock()  # type: ignore[method-assign]
    backend._project = _FakeProject(
        [
            _FakeCreateStep("PrimaryAgent", RuntimeError("rate limit exceeded")),
            _FakeCreateStep("FallbackAgent", _completed("fallback answer")),
        ]
    )

    reply = await backend.invoke("PrimaryAgent", "hello")

    assert reply.output_text == "fallback answer"
    assert reply.tool_events[0].name == "model_fallback"
    assert reply.tool_events[0].arguments["primary_agent"] == "PrimaryAgent"
    assert reply.tool_events[0].arguments["fallback_agent"] == "FallbackAgent"


async def test_foundry_stream_retries_rate_limit_with_fallback_agent() -> None:
    backend = FoundryBackend(
        "https://example.test/project",
        dispatcher=None,
        fallback_agent_names={"PrimaryAgent": "FallbackAgent"},
    )
    backend._ensure_started = AsyncMock()  # type: ignore[method-assign]
    backend._project = _FakeProject(
        [
            _FakeCreateStep("PrimaryAgent", _RaisingStream("rate limit exceeded")),
            _FakeCreateStep(
                "FallbackAgent",
                iter(
                    [
                        {"type": "response.output_text.delta", "delta": "fallback"},
                        {
                            "type": "response.completed",
                            "response": _completed("fallback answer", id_="resp_fb"),
                        },
                    ]
                ),
            ),
        ]
    )

    events = [event async for event in backend.stream("PrimaryAgent", "hello")]

    assert isinstance(events[0], ToolStarted)
    assert events[0].name == "model_fallback"
    assert isinstance(events[1], ToolFinished)
    assert events[1].result is not None
    assert events[1].result["fallback_agent"] == "FallbackAgent"
    assert isinstance(events[2], TextChunk)
    assert events[2].text == "fallback"
    assert isinstance(events[3], StreamDone)
    assert events[3].conversation_id == "resp_fb"


class _FakeCreateStep:
    def __init__(self, expected_agent: str, result: Any) -> None:
        self.expected_agent = expected_agent
        self.result = result


class _FakeProject:
    def __init__(self, steps: list[_FakeCreateStep]) -> None:
        self._openai = _FakeOpenAI(steps)

    def get_openai_client(self) -> "_FakeOpenAI":
        return self._openai


class _FakeOpenAI:
    def __init__(self, steps: list[_FakeCreateStep]) -> None:
        self.responses = _FakeResponses(steps)


class _FakeResponses:
    def __init__(self, steps: list[_FakeCreateStep]) -> None:
        self._steps = list(steps)

    def create(self, **kwargs: Any) -> Any:
        assert self._steps
        step = self._steps.pop(0)
        agent = kwargs["extra_body"]["agent_reference"]["name"]
        assert agent == step.expected_agent
        if isinstance(step.result, Exception):
            raise step.result
        return step.result


class _RaisingStream:
    def __init__(self, message: str) -> None:
        self.message = message

    def __iter__(self):
        raise RuntimeError(self.message)


def test_loop_returns_completed_reply_on_last_allowed_round() -> None:
    """max_rounds counts tool rounds; the answer to the last one is still a result."""
    initial = {
        "id": "r1",
        "status": "requires_action",
        "output": [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"}
        ],
    }
    calls: list[str] = []

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"ok": True}

    def respond_fn(outputs: list[dict[str, Any]], prev: str | None) -> dict[str, Any]:
        return _completed("done after tool", id_="r2")

    reply = run_tool_loop(initial, respond_fn=respond_fn, dispatcher=dispatcher, max_rounds=1)

    assert calls == ["f"]
    assert reply.output_text == "done after tool"
    assert reply.conversation_id == "r2"
    assert len(reply.tool_events) == 1


class _PartialThenRaisingStream:
    """Yields one text delta, then fails like a mid-stream rate limit."""

    def __init__(self, text: str, message: str) -> None:
        self.text = text
        self.message = message

    def __iter__(self):
        yield {"type": "response.output_text.delta", "delta": self.text}
        raise RuntimeError(self.message)


async def test_foundry_stream_does_not_fall_back_after_output_started() -> None:
    backend = FoundryBackend(
        "https://example.test/project",
        dispatcher=None,
        fallback_agent_names={"PrimaryAgent": "FallbackAgent"},
    )
    backend._ensure_started = AsyncMock()  # type: ignore[method-assign]
    # Only one step: a second create() (the fallback) would fail the fake.
    backend._project = _FakeProject(
        [
            _FakeCreateStep(
                "PrimaryAgent", _PartialThenRaisingStream("partial", "rate limit exceeded")
            ),
        ]
    )

    events = [event async for event in backend.stream("PrimaryAgent", "hello")]

    assert isinstance(events[0], TextChunk)
    assert events[0].text == "partial"
    assert not any(isinstance(event, ToolStarted) for event in events)
    assert isinstance(events[-1], StreamError)
    assert events[-1].error_type == "RuntimeError"
