"""Tests for the gateway's retry-on-AgentReturnedError feature.

The Hybrid router's job is to *pick* the right agent; this layer's job is
to make sure that if the picked agent fails (e.g. Bedrock throttling), we
transparently fall back to the BM25 runner-up before declaring failure.

We cover both REST paths:
  - :stream -- a routing-retry SSE event is emitted, then the new agent's
    stream is forwarded. If a chunk has already gone over the wire we
    must NOT retry (can't rewind half a message).
  - :invoke -- the response carries the actually-used agent in
    routing.selected_agent and method gets a '+retry' suffix.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentReturnedError,
    AgentSpec,
    StreamDone,
    StreamError,
    TextChunk,
)
from multicloud_ai_agent_gateway.core.bm25 import BM25Classifier
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


class _ScriptedBackend:
    """Backend stub that returns different behaviours per remote_id.

    ``scripts`` is a {remote_id -> list[AgentEvent] | Exception | AgentReply}
    map. Exceptions are raised during invoke/stream; AgentReply is returned
    by invoke directly; lists of events are streamed.
    """

    backend_id = "foundry"

    def __init__(self, scripts: dict[str, Any]) -> None:
        self._scripts = scripts
        self.invoke_calls: list[str] = []
        self.stream_calls: list[str] = []

    async def invoke(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        self.invoke_calls.append(remote_id)
        script = self._scripts[remote_id]
        if isinstance(script, Exception):
            raise script
        if isinstance(script, AgentReply):
            return script
        raise AssertionError(f"No invoke script for {remote_id}")

    async def stream(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self.stream_calls.append(remote_id)
        script = self._scripts[remote_id]
        if isinstance(script, Exception):
            raise script
        for event in script:
            yield event

    async def aclose(self) -> None:
        return None


def _registry_three() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentSpec(
                name="AgentA",
                backend_id="foundry",
                remote_id="AgentA",
                tags=("alpha", "first"),
                example_prompts=("do alpha thing",),
            ),
            AgentSpec(
                name="AgentB",
                backend_id="foundry",
                remote_id="AgentB",
                tags=("alpha", "second"),
                example_prompts=("alpha runner-up",),
            ),
            AgentSpec(
                name="AgentC",
                backend_id="foundry",
                remote_id="AgentC",
                tags=("alpha", "third"),
                example_prompts=("alpha tail",),
            ),
        ]
    )


def _router_with_bm25(backend: _ScriptedBackend) -> Router:
    registry = _registry_three()
    return Router(registry, {"foundry": backend}, classifier=BM25Classifier(registry.list()))


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
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


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


def test_stream_retries_when_first_agent_errors_before_any_chunk() -> None:
    """First agent raises AgentReturnedError immediately -> retry runner-up.

    Retry needs BM25 candidates, which only exist on the AutoRouter path
    (a direct ``/v1/agents/X:stream`` is treated as 'caller explicitly
    wanted X, do not improvise').
    """
    # Both runner-ups carry a complete script so the test doesn't depend on
    # the lexicographic / score tie-break order BM25 produces internally.
    happy_path = [TextChunk(text="hello from runner-up"), StreamDone(conversation_id="c1")]
    backend = _ScriptedBackend(
        {
            "AgentA": AgentReturnedError("ThrottlingException", "rate limited"),
            "AgentB": list(happy_path),
            "AgentC": list(happy_path),
        }
    )
    router = _router_with_bm25(backend)
    client = TestClient(build_rest_app(router=router, gateway_version="t"))

    resp = client.post("/v1/agents/AutoRouter:stream", json={"prompt": "alpha"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]

    # We should see: a routing-retry, then the runner-up's chunk + done.
    assert "routing-retry" in kinds, f"got {kinds!r}"
    assert "chunk" in kinds
    assert kinds[-1] == "done", f"got {kinds!r}; raw={resp.text!r}"
    # The error must NOT leak to the client when we successfully fell back.
    assert "error" not in kinds
    # The first attempt was the BM25 top pick; a runner-up actually answered.
    assert len(backend.stream_calls) >= 2


def test_stream_does_not_retry_after_a_chunk_has_been_emitted() -> None:
    """Safety: once a chunk reached the client we cannot rewind, so emit
    the error and stop -- never start a second agent half-way."""
    backend = _ScriptedBackend(
        {
            "AgentA": [
                TextChunk(text="partial answer"),
                StreamError(message="boom mid-stream", error_type="MidStreamError"),
            ],
            "AgentB": [TextChunk(text="should-not-appear"), StreamDone(conversation_id="c2")],
            "AgentC": [TextChunk(text="never")],
        }
    )
    router = _router_with_bm25(backend)
    client = TestClient(build_rest_app(router=router, gateway_version="t"))

    resp = client.post("/v1/agents/AutoRouter:stream", json={"prompt": "alpha"})
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]

    # Sequence: chunk (partial), error -- and no retry, no second chunk.
    assert "chunk" in kinds
    assert "error" in kinds
    assert "routing-retry" not in kinds
    # Only the BM25-picked agent was streamed, never any runner-up.
    assert len(backend.stream_calls) == 1


def test_stream_emits_error_when_all_candidates_fail() -> None:
    err = AgentReturnedError("ThrottlingException", "global throttling")
    backend = _ScriptedBackend({"AgentA": err, "AgentB": err, "AgentC": err})
    router = _router_with_bm25(backend)
    client = TestClient(build_rest_app(router=router, gateway_version="t"))

    resp = client.post("/v1/agents/AutoRouter:stream", json={"prompt": "alpha"})
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]

    # We should see routing-retry events between attempts, then a final error.
    assert kinds.count("routing-retry") >= 1
    assert kinds[-1] == "error"


# ---------------------------------------------------------------------------
# Invoke (non-streaming) path
# ---------------------------------------------------------------------------


def test_invoke_retries_first_failure_and_returns_runner_up_reply() -> None:
    backend = _ScriptedBackend(
        {
            "AgentA": AgentReturnedError("ThrottlingException", "rate limited"),
            "AgentB": AgentReply(
                output_text="answered by B",
                conversation_id="resp_b",
                usage={"total_tokens": 7},
            ),
        }
    )
    router = _router_with_bm25(backend)
    client = TestClient(build_rest_app(router=router, gateway_version="t"))

    resp = client.post("/v1/agents/AutoRouter:invoke", json={"prompt": "alpha"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["output_text"] == "answered by B"
    # routing.selected_agent now reflects whoever actually served the reply.
    assert body["routing"]["selected_agent"] == "AgentB"
    # And method carries the +retry marker so callers can tell.
    assert body["routing"]["method"].endswith("+retry")
    # Both candidates were touched.
    assert backend.invoke_calls[0] == "AgentA"
    assert "AgentB" in backend.invoke_calls


def test_invoke_returns_502_when_all_candidates_throttled() -> None:
    err = AgentReturnedError("ThrottlingException", "all throttled")
    backend = _ScriptedBackend({"AgentA": err, "AgentB": err, "AgentC": err})
    router = _router_with_bm25(backend)
    client = TestClient(build_rest_app(router=router, gateway_version="t"))

    resp = client.post("/v1/agents/AutoRouter:invoke", json={"prompt": "alpha"})
    assert resp.status_code == 502
    assert "ThrottlingException" in resp.json()["detail"]
