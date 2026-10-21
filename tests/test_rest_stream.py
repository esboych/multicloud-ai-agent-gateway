"""Tests for the REST :stream endpoint - SSE serialization end-to-end.

The Router stub in conftest.py only implements invoke(). For streaming we
plug a tiny per-test backend stub that yields scripted AgentEvents.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentSpec,
    StreamDone,
    StreamError,
    TextChunk,
    ToolFinished,
    ToolStarted,
)
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


class _StreamingStub:
    backend_id = "foundry"

    def __init__(self, scripted: list[AgentEvent]) -> None:
        self._scripted = scripted

    async def invoke(self, *args: Any, **kwargs: Any) -> AgentReply:
        raise NotImplementedError

    async def stream(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        for event in self._scripted:
            yield event

    async def aclose(self) -> None:
        return None


def _client_with(events: list[AgentEvent]) -> TestClient:
    backend = _StreamingStub(events)
    registry = AgentRegistry(
        [AgentSpec(name="A", backend_id="foundry", remote_id="A")]
    )
    router = Router(registry, {"foundry": backend})
    return TestClient(build_rest_app(router=router, gateway_version="test"))


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Split raw SSE wire bytes into (event_type, parsed_data) tuples."""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        event_line = next((l for l in block.splitlines() if l.startswith("event:")), None)
        data_line = next((l for l in block.splitlines() if l.startswith("data:")), None)
        if event_line is None or data_line is None:
            continue
        event = event_line[len("event:"):].strip()
        data = json.loads(data_line[len("data:"):].strip())
        out.append((event, data))
    return out


def test_stream_emits_chunks_and_done() -> None:
    client = _client_with(
        [
            TextChunk(text="Hel"),
            TextChunk(text="lo"),
            StreamDone(conversation_id="c1", usage={"total_tokens": 5}),
        ]
    )
    resp = client.post("/v1/agents/A:stream", json={"prompt": "hi"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)

    assert [e for e, _ in events] == ["chunk", "chunk", "done"]
    assert events[0][1] == {"text": "Hel"}
    assert events[1][1] == {"text": "lo"}
    assert events[2][1] == {
        "conversation_id": "c1",
        "usage": {"total_tokens": 5},
        "finish_reason": "completed",
    }


def test_stream_emits_tool_events() -> None:
    client = _client_with(
        [
            ToolStarted(name="runX", arguments={"a": 1}),
            ToolFinished(name="runX", result={"ok": True}),
            TextChunk(text="done"),
            StreamDone(conversation_id="c2"),
        ]
    )
    events = _parse_sse(client.post("/v1/agents/A:stream", json={"prompt": "x"}).text)

    assert [e for e, _ in events] == ["tool_started", "tool_finished", "chunk", "done"]
    assert events[0][1] == {"name": "runX", "arguments": {"a": 1}}
    assert events[1][1] == {
        "name": "runX",
        "result": {"ok": True},
        "error": None,
        "replayed": False,
    }


def test_stream_emits_error_event() -> None:
    client = _client_with(
        [
            StreamError(message="kaboom", error_type="RuntimeError"),
        ]
    )
    events = _parse_sse(client.post("/v1/agents/A:stream", json={"prompt": "x"}).text)

    assert [e for e, _ in events] == ["error"]
    assert events[0][1] == {"message": "kaboom", "error_type": "RuntimeError"}


def test_stream_unknown_agent_returns_404_before_stream_starts() -> None:
    client = _client_with([TextChunk(text="never")])
    resp = client.post("/v1/agents/Nope:stream", json={"prompt": "x"})
    assert resp.status_code == 404
    assert "Nope" in resp.json()["detail"]


def test_stream_validates_prompt() -> None:
    client = _client_with([])
    resp = client.post("/v1/agents/A:stream", json={"prompt": ""})
    assert resp.status_code == 422
