"""Tests for the sync-producer -> async-iterator bridge behind every stream().

The bridge runs the SDK call on a worker thread and hands events to the
event loop through a bounded queue. Two properties matter: events arrive in
order under back-pressure, and a disconnected consumer stops the producer
instead of letting it run the upstream request to the end.
"""

from __future__ import annotations

import asyncio
import threading

from multicloud_ai_agent_gateway.backends.foundry import _bridge_to_async, _ConsumerGone
from multicloud_ai_agent_gateway.core.backend import StreamDone, StreamError, TextChunk


async def test_bridge_forwards_events_in_order_under_backpressure() -> None:
    def producer(emit) -> None:
        for i in range(2000):  # well above the queue size
            emit(TextChunk(text=str(i)))
        emit(StreamDone(conversation_id="c", usage=None, finish_reason="completed"))

    events = [event async for event in _bridge_to_async(producer)]

    assert len(events) == 2001
    assert [e.text for e in events[:-1]] == [str(i) for i in range(2000)]
    assert isinstance(events[-1], StreamDone)


async def test_bridge_aborts_producer_when_consumer_disconnects() -> None:
    finished = threading.Event()
    state = {"aborted": False, "emitted": 0}

    def producer(emit) -> None:
        try:
            for i in range(100_000):
                emit(TextChunk(text=str(i)))
                state["emitted"] += 1
        except _ConsumerGone:
            state["aborted"] = True
            raise
        finally:
            finished.set()

    stream = _bridge_to_async(producer)
    first = await stream.__anext__()
    assert first.text == "0"

    await stream.aclose()  # what Starlette does when the SSE client goes away

    assert await asyncio.to_thread(finished.wait, 5.0)
    assert state["aborted"] is True
    assert state["emitted"] < 100_000


async def test_bridge_turns_producer_exception_into_stream_error() -> None:
    def producer(emit) -> None:
        emit(TextChunk(text="a"))
        raise ValueError("boom")

    events = [event async for event in _bridge_to_async(producer)]

    assert isinstance(events[0], TextChunk)
    assert isinstance(events[-1], StreamError)
    assert events[-1].error_type == "ValueError"
