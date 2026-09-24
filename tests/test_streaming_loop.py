"""Tests for run_streaming_tool_loop, the pure tool-loop streamer.

Mirrors test_foundry_backend.py but exercises the streaming path. Uses lists
of dicts as fake OpenAI-Responses streaming events so no SDK is involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from multicloud_ai_agent_gateway.backends.foundry import run_streaming_tool_loop
from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    StreamDone,
    StreamError,
    TextChunk,
    ToolFinished,
    ToolStarted,
)


def _delta(text: str) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "delta": text}


def _completed(
    *,
    id_: str,
    status: str = "completed",
    output_items: list[dict[str, Any]] | None = None,
    output_text: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": id_,
            "status": status,
            "output": output_items or [],
            "output_text": output_text,
            "usage": usage,
        },
    }


def _function_call_item(
    name: str, args: dict[str, Any], call_id: str = "call_1"
) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _capture() -> tuple[list[AgentEvent], callable]:
    captured: list[AgentEvent] = []

    def emit(event: AgentEvent) -> None:
        captured.append(event)

    return captured, emit


def test_streams_text_deltas_and_emits_done() -> None:
    initial = iter(
        [
            _delta("Hel"),
            _delta("lo"),
            _delta(" world"),
            _completed(id_="resp_1", output_text="Hello world"),
        ]
    )

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=initial,
        respond_stream_fn=lambda outputs, prev: pytest.fail("no tools expected"),
        dispatcher=None,
        max_rounds=5,
        emit=emit,
    )

    chunks = [e for e in captured if isinstance(e, TextChunk)]
    assert [c.text for c in chunks] == ["Hel", "lo", " world"]
    assert isinstance(captured[-1], StreamDone)
    assert captured[-1].conversation_id == "resp_1"
    assert captured[-1].finish_reason == "completed"


def test_dispatches_function_call_then_streams_continuation() -> None:
    initial = iter(
        [
            _completed(
                id_="resp_tool",
                status="requires_action",
                output_items=[_function_call_item("doStuff", {"x": 1})],
            )
        ]
    )
    follow_up = iter(
        [
            _delta("ok"),
            _completed(id_="resp_final", output_text="ok"),
        ]
    )

    received: list[tuple[list[dict[str, Any]], str | None]] = []

    def respond_stream_fn(outputs, previous_id):
        received.append((outputs, previous_id))
        return follow_up

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert name == "doStuff" and args == {"x": 1}
        return {"answer": 42}

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=initial,
        respond_stream_fn=respond_stream_fn,
        dispatcher=dispatcher,
        max_rounds=5,
        emit=emit,
    )

    types = [type(e).__name__ for e in captured]
    assert types == ["ToolStarted", "ToolFinished", "TextChunk", "StreamDone"]

    assert isinstance(captured[0], ToolStarted)
    assert captured[0].name == "doStuff"
    assert isinstance(captured[1], ToolFinished)
    assert captured[1].result == {"answer": 42}
    assert captured[1].error is None
    assert isinstance(captured[2], TextChunk) and captured[2].text == "ok"
    assert isinstance(captured[3], StreamDone) and captured[3].conversation_id == "resp_final"

    assert len(received) == 1
    [outputs], previous_id = received[0]
    assert previous_id == "resp_tool"
    assert outputs["call_id"] == "call_1"
    assert json.loads(outputs["output"]) == {"answer": 42}


def test_dispatcher_exception_emitted_as_tool_finished_error_then_continues() -> None:
    initial = iter(
        [
            _completed(
                id_="r1",
                status="requires_action",
                output_items=[_function_call_item("bad", {})],
            )
        ]
    )
    follow_up = iter([_completed(id_="r2", output_text="recovered")])

    def dispatcher(name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=initial,
        respond_stream_fn=lambda outputs, prev: follow_up,
        dispatcher=dispatcher,
        max_rounds=5,
        emit=emit,
    )

    finished = [e for e in captured if isinstance(e, ToolFinished)]
    assert len(finished) == 1
    assert finished[0].error is not None and "RuntimeError" in finished[0].error
    assert isinstance(captured[-1], StreamDone)


def test_no_dispatcher_emits_streamerror_and_stops() -> None:
    initial = iter(
        [
            _completed(
                id_="r1",
                status="requires_action",
                output_items=[_function_call_item("any", {})],
            )
        ]
    )

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=initial,
        respond_stream_fn=lambda outputs, prev: pytest.fail("should not be called"),
        dispatcher=None,
        max_rounds=5,
        emit=emit,
    )

    types = [type(e).__name__ for e in captured]
    assert types == ["ToolStarted", "ToolFinished", "StreamError"]
    assert isinstance(captured[-1], StreamError)
    assert captured[-1].error_type == "NoDispatcherConfigured"


def test_max_rounds_exceeded_emits_streamerror() -> None:
    forever = lambda: iter(
        [
            _completed(
                id_="r",
                status="requires_action",
                output_items=[_function_call_item("loopy", {}, call_id="c1")],
            )
        ]
    )

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=forever(),
        respond_stream_fn=lambda outputs, prev: forever(),
        dispatcher=lambda name, args: {"again": True},
        max_rounds=3,
        emit=emit,
    )

    assert isinstance(captured[-1], StreamError)
    assert captured[-1].error_type == "ToolBudgetExceeded"


def test_stream_without_completed_event_emits_protocol_error() -> None:
    initial = iter([_delta("partial"), _delta(" text")])  # never completes

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=initial,
        respond_stream_fn=lambda outputs, prev: pytest.fail("should not be called"),
        dispatcher=None,
        max_rounds=5,
        emit=emit,
    )

    chunks = [e for e in captured if isinstance(e, TextChunk)]
    assert [c.text for c in chunks] == ["partial", " text"]
    assert isinstance(captured[-1], StreamError)
    assert captured[-1].error_type == "StreamProtocolError"


def test_last_allowed_round_completion_is_streamed() -> None:
    """max_rounds counts tool rounds; the continuation after the last one is
    still drained and, if completed, delivered as the answer."""
    initial = iter(
        [
            _completed(
                id_="r1",
                status="requires_action",
                output_items=[_function_call_item("f", {}, call_id="c1")],
            )
        ]
    )

    def final(outputs: list[dict[str, Any]], prev: str | None):
        return iter([_delta("final"), _completed(id_="r2", output_text="final")])

    captured, emit = _capture()
    run_streaming_tool_loop(
        initial_stream=initial,
        respond_stream_fn=final,
        dispatcher=lambda name, args: {"ok": True},
        max_rounds=1,
        emit=emit,
    )

    types = [type(e).__name__ for e in captured]
    assert types == ["ToolStarted", "ToolFinished", "TextChunk", "StreamDone"]
    assert isinstance(captured[-1], StreamDone)
    assert captured[-1].conversation_id == "r2"
