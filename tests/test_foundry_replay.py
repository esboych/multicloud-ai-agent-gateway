"""Foundry model fallback: tool-result replay and stream hold-back."""

from __future__ import annotations

from typing import Any, Iterable
from unittest.mock import AsyncMock

from multicloud_ai_agent_gateway.backends.foundry import (
    FoundryBackend,
    ReplayedToolResult,
    ToolResultRecorder,
    _OutputTracker,
)
from multicloud_ai_agent_gateway.core.backend import (
    StreamDone,
    StreamError,
    TextChunk,
    ToolFinished,
    ToolStarted,
)


def _requires_action(call_id: str, name: str, arguments: str, *, id_: str) -> dict[str, Any]:
    return {
        "id": id_,
        "status": "requires_action",
        "output": [{"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}],
        "output_text": None,
        "usage": None,
    }


def _completed(text: str, *, id_: str) -> dict[str, Any]:
    return {"id": id_, "status": "completed", "output": [], "output_text": text, "usage": {"total_tokens": 1}}


class _Step:
    def __init__(self, expected_agent: str, result: Any) -> None:
        self.expected_agent = expected_agent
        self.result = result


class _Responses:
    def __init__(self, steps: list[_Step]) -> None:
        self._steps = list(steps)

    def create(self, **kwargs: Any) -> Any:
        assert self._steps, "unexpected extra create() call"
        step = self._steps.pop(0)
        assert kwargs["extra_body"]["agent_reference"]["name"] == step.expected_agent
        if isinstance(step.result, Exception):
            raise step.result
        return step.result


class _Project:
    def __init__(self, steps: list[_Step]) -> None:
        self._openai = type("OpenAI", (), {"responses": _Responses(steps)})()

    def get_openai_client(self) -> Any:
        return self._openai


class _RaisingStream:
    def __init__(self, message: str) -> None:
        self._message = message

    def __iter__(self):
        raise RuntimeError(self._message)


class _DeltasThenRaise:
    def __init__(self, deltas: list[str], message: str) -> None:
        self._deltas = deltas
        self._message = message

    def __iter__(self):
        for delta in self._deltas:
            yield {"type": "response.output_text.delta", "delta": delta}
        raise RuntimeError(self._message)


def _stream_of(*events: dict[str, Any]) -> Iterable[Any]:
    return iter(list(events))


class _CountingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        return {"rows": len(self.calls)}


def _backend(dispatcher: Any, steps: list[_Step], **kwargs: Any) -> FoundryBackend:
    backend = FoundryBackend(
        "https://example.test/project",
        dispatcher=dispatcher,
        fallback_agent_names={"Primary": "Fallback"},
        **kwargs,
    )
    backend._ensure_started = AsyncMock()  # type: ignore[method-assign]
    backend._project = _Project(steps)
    return backend


async def test_invoke_fallback_replays_tool_results() -> None:
    dispatcher = _CountingDispatcher()
    backend = _backend(
        dispatcher,
        [
            _Step("Primary", _requires_action("c1", "lookup", '{"q": 1}', id_="r1")),
            _Step("Primary", RuntimeError("rate limit exceeded")),
            _Step("Fallback", _requires_action("c2", "lookup", '{"q": 1}', id_="r2")),
            _Step("Fallback", _completed("fallback answer", id_="r3")),
        ],
    )
    reply = await backend.invoke("Primary", "hello")

    assert reply.output_text == "fallback answer"
    assert dispatcher.calls == [("lookup", {"q": 1})]  # executed once, replayed once
    assert reply.tool_events[0].name == "model_fallback"
    assert reply.tool_events[0].result["cached_tool_results"] == 1
    assert reply.tool_events[1].name == "lookup"
    assert reply.tool_events[1].replayed is True
    assert reply.tool_events[1].result == {"rows": 1}


async def test_invoke_fallback_executes_new_tools_for_real() -> None:
    dispatcher = _CountingDispatcher()
    backend = _backend(
        dispatcher,
        [
            _Step("Primary", _requires_action("c1", "lookup", '{"q": 1}', id_="r1")),
            _Step("Primary", RuntimeError("rate limit exceeded")),
            _Step("Fallback", _requires_action("c2", "lookup", '{"q": 2}', id_="r2")),
            _Step("Fallback", _completed("done", id_="r3")),
        ],
    )
    reply = await backend.invoke("Primary", "hello")
    assert dispatcher.calls == [("lookup", {"q": 1}), ("lookup", {"q": 2})]
    assert reply.tool_events[1].replayed is False


async def test_replay_can_be_disabled() -> None:
    dispatcher = _CountingDispatcher()
    backend = _backend(
        dispatcher,
        [
            _Step("Primary", _requires_action("c1", "lookup", '{"q": 1}', id_="r1")),
            _Step("Primary", RuntimeError("rate limit exceeded")),
            _Step("Fallback", _requires_action("c2", "lookup", '{"q": 1}', id_="r2")),
            _Step("Fallback", _completed("done", id_="r3")),
        ],
        replay_tool_results_on_fallback=False,
    )
    reply = await backend.invoke("Primary", "hello")
    assert len(dispatcher.calls) == 2
    assert reply.tool_events[0].result["cached_tool_results"] == 0
    assert reply.tool_events[1].replayed is False


async def test_stream_fallback_replays_tool_results() -> None:
    dispatcher = _CountingDispatcher()
    backend = _backend(
        dispatcher,
        [
            _Step(
                "Primary",
                _stream_of(
                    {
                        "type": "response.completed",
                        "response": _requires_action("c1", "lookup", '{"q": 1}', id_="r1"),
                    }
                ),
            ),
            _Step("Primary", _RaisingStream("rate limit exceeded")),
            _Step(
                "Fallback",
                _stream_of(
                    {
                        "type": "response.completed",
                        "response": _requires_action("c2", "lookup", '{"q": 1}', id_="r2"),
                    }
                ),
            ),
            _Step(
                "Fallback",
                _stream_of(
                    {"type": "response.output_text.delta", "delta": "fallback"},
                    {"type": "response.completed", "response": _completed("fallback", id_="r3")},
                ),
            ),
        ],
    )
    events = [event async for event in backend.stream("Primary", "hello")]

    # Primary's tool events reached the client (they are not text), then the
    # fallback announcement, then the replayed tool and the answer.
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "ToolStarted", "ToolFinished",  # primary executed lookup for real
        "ToolStarted", "ToolFinished",  # model_fallback marker
        "ToolStarted", "ToolFinished",  # fallback: replayed lookup
        "TextChunk", "StreamDone",
    ]
    assert dispatcher.calls == [("lookup", {"q": 1})]
    assert isinstance(events[1], ToolFinished) and events[1].replayed is False
    assert isinstance(events[5], ToolFinished) and events[5].replayed is True
    assert isinstance(events[6], TextChunk) and events[6].text == "fallback"


async def test_stream_holdback_lets_early_rate_limit_switch_models() -> None:
    backend = _backend(
        None,
        [
            _Step("Primary", _DeltasThenRaise(["par", "tial"], "rate limit exceeded")),
            _Step(
                "Fallback",
                _stream_of(
                    {"type": "response.output_text.delta", "delta": "fallback answer"},
                    {"type": "response.completed", "response": _completed("fallback answer", id_="r9")},
                ),
            ),
        ],
        stream_fallback_holdback_chars=32,
    )
    events = [event async for event in backend.stream("Primary", "hello")]
    assert [type(e).__name__ for e in events] == ["ToolStarted", "ToolFinished", "TextChunk", "StreamDone"]
    assert isinstance(events[0], ToolStarted) and events[0].name == "model_fallback"
    assert isinstance(events[2], TextChunk) and events[2].text == "fallback answer"
    assert isinstance(events[3], StreamDone) and events[3].conversation_id == "r9"


async def test_stream_holdback_releases_once_threshold_reached() -> None:
    backend = _backend(
        None,
        [_Step("Primary", _DeltasThenRaise(["0123456789", "0123456789ABC"], "rate limit exceeded"))],
        stream_fallback_holdback_chars=20,
    )
    events = [event async for event in backend.stream("Primary", "hello")]
    # 23 chars crossed the threshold: text was released, so no fallback.
    assert [type(e).__name__ for e in events] == ["TextChunk", "TextChunk", "StreamError"]
    assert isinstance(events[-1], StreamError)


async def test_stream_holdback_flushes_short_answers_on_completion() -> None:
    backend = _backend(
        None,
        [
            _Step(
                "Primary",
                _stream_of(
                    {"type": "response.output_text.delta", "delta": "short"},
                    {"type": "response.completed", "response": _completed("short", id_="r1")},
                ),
            )
        ],
        stream_fallback_holdback_chars=1000,
    )
    events = [event async for event in backend.stream("Primary", "hello")]
    assert [type(e).__name__ for e in events] == ["TextChunk", "StreamDone"]
    assert isinstance(events[0], TextChunk) and events[0].text == "short"


def test_output_tracker_orders_tool_events_after_held_text() -> None:
    seen: list[Any] = []
    tracker = _OutputTracker(seen.append, holdback_chars=100)
    tracker(TextChunk(text="a"))
    tracker(TextChunk(text="b"))
    assert seen == [] and not tracker.output_started
    tracker(ToolStarted(name="t", arguments={}))
    assert [type(e).__name__ for e in seen] == ["TextChunk", "TextChunk", "ToolStarted"]
    assert tracker.output_started
    tracker(TextChunk(text="c"))  # after release, text streams straight through
    assert len(seen) == 4


def test_output_tracker_discard_drops_held_text() -> None:
    seen: list[Any] = []
    tracker = _OutputTracker(seen.append, holdback_chars=100)
    tracker(TextChunk(text="never shown"))
    tracker.discard()
    tracker.flush()
    assert seen == [] and not tracker.output_started


def test_recorder_caches_only_successful_results() -> None:
    calls: list[str] = []

    def dispatcher(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        if name == "boom":
            raise RuntimeError("tool failed")
        return {"ok": name}

    recorder = ToolResultRecorder(dispatcher)
    recording = recorder.recording()
    assert recording("fine", {"a": 1}) == {"ok": "fine"}
    try:
        recording("boom", {})
    except RuntimeError:
        pass
    assert len(recorder) == 1

    replaying = recorder.replaying()
    replayed = replaying("fine", {"a": 1})
    assert isinstance(replayed, ReplayedToolResult) and replayed == {"ok": "fine"}
    # The failed tool is not cached, so the fallback executes it for real.
    try:
        replaying("boom", {})
    except RuntimeError:
        pass
    assert calls == ["fine", "boom", "boom"]
    # Argument order does not matter for the cache key.
    assert isinstance(replaying("fine", {"a": 1}), ReplayedToolResult)
