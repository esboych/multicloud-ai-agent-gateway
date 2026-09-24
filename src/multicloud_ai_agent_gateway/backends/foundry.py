from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from typing import Any, AsyncIterator, Callable, Iterable

from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    NoDispatcherConfigured,
    StreamDone,
    StreamError,
    TextChunk,
    ToolBudgetExceeded,
    ToolEvent,
    ToolFinished,
    ToolStarted,
)
from multicloud_ai_agent_gateway.core.dispatcher import ToolDispatcher

log = logging.getLogger(__name__)


# A ResponseSource is what the tool-loop calls each round. It hides whether the
# request actually goes to the OpenAI SDK, a stub for tests, or a recorded
# fixture. Returns the response object (dict-like or pydantic model).
ResponseSource = Callable[[Any, str | None], Any]


# Iterator of streaming events from the OpenAI Responses API (or a stub).
StreamSource = Callable[[list[dict[str, Any]], str | None], Iterable[Any]]


# Callback used by the pure tool-loop streamer to push events to its consumer.
EventSink = Callable[[AgentEvent], None]


def run_tool_loop(
    initial_response: Any,
    *,
    respond_fn: ResponseSource,
    dispatcher: ToolDispatcher | None,
    max_rounds: int,
) -> AgentReply:
    """Pure tool-loop. Extracted from FoundryBackend so it can be unit-tested.

    `respond_fn(outputs, previous_response_id)` is called each round to advance
    the conversation; for the first call we use `initial_response` directly.
    """
    response = initial_response
    tool_events: list[ToolEvent] = []

    for _ in range(max_rounds):
        outputs: list[dict[str, Any]] = []
        for item in _output(response):
            if _get(item, "type") != "function_call":
                continue
            name = str(_get(item, "name") or "")
            arguments = _json_args(_get(item, "arguments"))
            event = ToolEvent(name=name, arguments=arguments)
            if dispatcher is None:
                raise NoDispatcherConfigured(name)
            try:
                result = dispatcher(name, arguments)
                event.result = _ensure_jsonable(result)
            except Exception as exc:  # noqa: BLE001 - report any tool failure to Foundry
                event.error = f"{type(exc).__name__}: {exc}"
                event.result = {"error": event.error}
            tool_events.append(event)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": str(_get(item, "call_id") or ""),
                    "output": json.dumps(event.result, default=str),
                }
            )
        if not outputs:
            break
        response = respond_fn(outputs, _get(response, "id"))
    else:
        # Budget spent, but the answer to the last round has not been
        # inspected yet. It is only an error if the agent *still* wants
        # tools; a completed response is a normal result.
        if _has_function_calls(response):
            raise ToolBudgetExceeded(max_rounds)

    return AgentReply(
        output_text=_get(response, "output_text") or "",
        conversation_id=_get(response, "id"),
        tool_events=tool_events,
        usage=_dict(_get(response, "usage")),
        finish_reason=_get(response, "status") or "completed",
    )


def run_streaming_tool_loop(
    *,
    initial_stream: Iterable[Any],
    respond_stream_fn: StreamSource,
    dispatcher: ToolDispatcher | None,
    max_rounds: int,
    emit: EventSink,
) -> None:
    """Pure streaming tool-loop. Mirrors run_tool_loop, but pushes AgentEvents
    through `emit` as they happen instead of returning a single AgentReply.

    Synchronous on purpose: in production this runs in a worker thread and
    pushes events to an asyncio.Queue via call_soon_threadsafe.

    Each iteration:
      1. drain `stream` -- emit TextChunk for every output_text.delta,
         capture the completed Response.
      2. if the completed response has function_call items, dispatch each one
         (emit ToolStarted/ToolFinished), build outputs[], and start a fresh
         stream via respond_stream_fn.
      3. otherwise, emit StreamDone and exit.
    """
    stream: Iterable[Any] = initial_stream
    final_response: Any = None

    for _ in range(max_rounds):
        completed_response = _drain_stream_emitting_chunks(stream, emit)
        if completed_response is None:
            emit(
                StreamError(
                    message="Stream ended before response.completed event.",
                    error_type="StreamProtocolError",
                )
            )
            return

        outputs: list[dict[str, Any]] = []
        for item in _output(completed_response):
            if _get(item, "type") != "function_call":
                continue
            name = str(_get(item, "name") or "")
            arguments = _json_args(_get(item, "arguments"))
            emit(ToolStarted(name=name, arguments=arguments))

            if dispatcher is None:
                error = NoDispatcherConfigured(name)
                emit(
                    ToolFinished(
                        name=name, error=f"NoDispatcherConfigured: {error}"
                    )
                )
                emit(
                    StreamError(
                        message=str(error), error_type="NoDispatcherConfigured"
                    )
                )
                return

            try:
                result = dispatcher(name, arguments)
                result_dict = _ensure_jsonable(result)
                emit(ToolFinished(name=name, result=result_dict))
            except Exception as exc:  # noqa: BLE001 - feed any tool failure back to Foundry
                error_text = f"{type(exc).__name__}: {exc}"
                result_dict = {"error": error_text}
                emit(ToolFinished(name=name, error=error_text))

            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": str(_get(item, "call_id") or ""),
                    "output": json.dumps(result_dict, default=str),
                }
            )

        if not outputs:
            final_response = completed_response
            break

        stream = respond_stream_fn(outputs, _get(completed_response, "id"))
    else:
        # Budget spent, but the continuation stream started in the last
        # round has not been read yet. Drain it: a completed answer is the
        # result; another tool request is the budget error.
        completed_response = _drain_stream_emitting_chunks(stream, emit)
        if completed_response is None:
            emit(
                StreamError(
                    message="Stream ended before response.completed event.",
                    error_type="StreamProtocolError",
                )
            )
            return
        if _has_function_calls(completed_response):
            emit(
                StreamError(
                    message=f"Foundry agent still requested tools after {max_rounds} rounds.",
                    error_type="ToolBudgetExceeded",
                )
            )
            return
        final_response = completed_response

    emit(
        StreamDone(
            conversation_id=_get(final_response, "id"),
            usage=_dict(_get(final_response, "usage")),
            finish_reason=_get(final_response, "status") or "completed",
        )
    )


def _drain_stream_emitting_chunks(
    stream: Iterable[Any], emit: EventSink
) -> Any | None:
    """Iterate one Foundry streaming response, emit text deltas, return the
    final completed Response object (or None if the stream never completed)."""
    completed: Any = None
    for event in stream:
        event_type = str(_get(event, "type") or "")
        if event_type == "response.output_text.delta":
            delta = _get(event, "delta") or ""
            if delta:
                emit(TextChunk(text=str(delta)))
        elif event_type == "response.completed":
            completed = _get(event, "response")
        # Other event types (response.created, response.in_progress,
        # function_call_arguments.delta, content_part.added/done, etc.) are
        # intentionally ignored in this MVP.
    return completed


class FoundryBackend:
    """Wraps Microsoft Foundry's Responses API + agent_reference extension.

    Sync SDK calls are dispatched to a worker thread so the FastAPI event loop
    stays free.
    """

    backend_id = "foundry"

    def __init__(
        self,
        project_endpoint: str,
        *,
        dispatcher: ToolDispatcher | None,
        max_tool_rounds: int = 5,
        request_timeout_seconds: int = 900,
        model_fallback_enabled: bool = True,
        fallback_agent_names: dict[str, str] | None = None,
    ) -> None:
        if not project_endpoint:
            raise ValueError("FoundryBackend requires a non-empty project_endpoint.")
        self._endpoint = project_endpoint
        self._dispatcher = dispatcher
        self._max_rounds = max_tool_rounds
        self._timeout = request_timeout_seconds
        self._model_fallback_enabled = model_fallback_enabled
        self._fallback_agent_names = dict(fallback_agent_names or {})
        self._lock = asyncio.Lock()
        self._credential: Any = None
        self._project: Any = None

    async def _ensure_started(self) -> None:
        if self._project is not None:
            return
        async with self._lock:
            if self._project is not None:
                return
            credential, project = await asyncio.to_thread(self._build_clients)
            self._credential = credential
            self._project = project

    def _build_clients(self) -> tuple[Any, Any]:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        project = AIProjectClient(
            endpoint=self._endpoint,
            credential=credential,
            allow_preview=True,
        )
        return credential, project

    async def invoke(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        await self._ensure_started()
        return await asyncio.to_thread(
            self._invoke_sync,
            agent_name=remote_id,
            prompt=prompt,
            conversation_id=conversation_id,
        )

    def _invoke_sync(
        self,
        *,
        agent_name: str,
        prompt: str,
        conversation_id: str | None,
    ) -> AgentReply:
        try:
            return self._invoke_sync_once(
                agent_name=agent_name,
                prompt=prompt,
                conversation_id=conversation_id,
            )
        except Exception as exc:
            fallback_agent = self._fallback_agent_for(agent_name, exc)
            if fallback_agent is None:
                raise
            log.warning(
                "Foundry model rate-limited for agent %s; retrying with fallback agent %s.",
                agent_name,
                fallback_agent,
            )
            reply = self._invoke_sync_once(
                agent_name=fallback_agent,
                prompt=prompt,
                conversation_id=conversation_id,
            )
            reply.tool_events.insert(
                0,
                _model_fallback_tool_event(agent_name, fallback_agent, exc),
            )
            return reply

    def _invoke_sync_once(
        self,
        *,
        agent_name: str,
        prompt: str,
        conversation_id: str | None,
    ) -> AgentReply:
        openai = self._project.get_openai_client()
        agent_reference = {
            "agent_reference": {"name": agent_name, "type": "agent_reference"}
        }
        kwargs: dict[str, Any] = {
            "input": prompt,
            "extra_body": agent_reference,
            "timeout": self._timeout,
        }
        if conversation_id:
            kwargs["previous_response_id"] = conversation_id

        initial = openai.responses.create(**kwargs)

        def respond_fn(outputs: list[dict[str, Any]], previous_id: str | None) -> Any:
            return openai.responses.create(
                input=outputs,
                previous_response_id=previous_id,
                extra_body=agent_reference,
                timeout=self._timeout,
            )

        return run_tool_loop(
            initial,
            respond_fn=respond_fn,
            dispatcher=self._dispatcher,
            max_rounds=self._max_rounds,
        )

    async def stream(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        await self._ensure_started()

        def producer(emit: EventSink) -> None:
            self._stream_sync(
                agent_name=remote_id,
                prompt=prompt,
                conversation_id=conversation_id,
                emit=emit,
            )

        async for event in _bridge_to_async(producer):
            yield event

    def _stream_sync(
        self,
        *,
        agent_name: str,
        prompt: str,
        conversation_id: str | None,
        emit: EventSink,
    ) -> None:
        tracker = _OutputTracker(emit)
        try:
            self._stream_sync_once(
                agent_name=agent_name,
                prompt=prompt,
                conversation_id=conversation_id,
                emit=tracker,
            )
            return
        except Exception as exc:
            fallback_agent = self._fallback_agent_for(agent_name, exc)
            if fallback_agent is None:
                raise
            if tracker.output_started:
                # Text or tool events already reached the client. Restarting
                # the prompt on another model would splice two answers into
                # one stream, so surface the error instead.
                log.warning(
                    "Foundry streaming model rate-limited for agent %s after output "
                    "had started; not retrying with %s.",
                    agent_name,
                    fallback_agent,
                )
                raise
            log.warning(
                "Foundry streaming model rate-limited for agent %s; retrying with fallback agent %s.",
                agent_name,
                fallback_agent,
            )
            event = _model_fallback_tool_event(agent_name, fallback_agent, exc)
            emit(ToolStarted(name=event.name, arguments=event.arguments))
            emit(ToolFinished(name=event.name, result=event.result, error=event.error))
            self._stream_sync_once(
                agent_name=fallback_agent,
                prompt=prompt,
                conversation_id=conversation_id,
                emit=emit,
            )

    def _stream_sync_once(
        self,
        *,
        agent_name: str,
        prompt: str,
        conversation_id: str | None,
        emit: EventSink,
    ) -> None:
        openai = self._project.get_openai_client()
        agent_reference = {
            "agent_reference": {"name": agent_name, "type": "agent_reference"}
        }
        first_kwargs: dict[str, Any] = {
            "input": prompt,
            "extra_body": agent_reference,
            "timeout": self._timeout,
            "stream": True,
        }
        if conversation_id:
            first_kwargs["previous_response_id"] = conversation_id

        initial_stream = openai.responses.create(**first_kwargs)

        def respond_stream_fn(
            outputs: list[dict[str, Any]], previous_id: str | None
        ) -> Iterable[Any]:
            return openai.responses.create(
                input=outputs,
                previous_response_id=previous_id,
                extra_body=agent_reference,
                timeout=self._timeout,
                stream=True,
            )

        run_streaming_tool_loop(
            initial_stream=initial_stream,
            respond_stream_fn=respond_stream_fn,
            dispatcher=self._dispatcher,
            max_rounds=self._max_rounds,
            emit=emit,
        )

    def _fallback_agent_for(self, agent_name: str, exc: Exception) -> str | None:
        if not self._model_fallback_enabled or not is_model_rate_limit_error(exc):
            return None
        return self._fallback_agent_names.get(agent_name)

    async def aclose(self) -> None:
        if self._project is None:
            return
        await asyncio.to_thread(self._close_sync)
        self._project = None
        self._credential = None

    def _close_sync(self) -> None:
        for obj in (self._project, self._credential):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort shutdown
                    log.exception("Error closing Foundry SDK client.")


class _OutputTracker:
    """EventSink wrapper that remembers whether client-visible output began."""

    def __init__(self, emit: EventSink) -> None:
        self._emit = emit
        self.output_started = False

    def __call__(self, event: AgentEvent) -> None:
        if isinstance(event, (TextChunk, ToolStarted)):
            self.output_started = True
        self._emit(event)


# ---------------------------------------------------------------------------
# Async bridge: turn a sync producer that emits AgentEvents through a callback
# into an async iterator. Used by every backend's stream() to keep the asyncio
# event loop free while SDK calls run on a worker thread.
# ---------------------------------------------------------------------------


_SENTINEL = object()

# Bounded so a slow SSE client applies back-pressure to the SDK thread instead
# of the queue growing without limit for the whole upstream request.
_QUEUE_MAXSIZE = 256
# How often a blocked producer re-checks whether the consumer is still there.
_PUT_POLL_SECONDS = 0.5


class _ConsumerGone(Exception):
    """Raised inside the producer thread once the SSE consumer has gone away.

    Unwinds the producer (and with it the upstream SDK stream) instead of
    letting it run the cloud request to completion for nobody.
    """


def _bridge_to_async(
    sync_producer: Callable[[EventSink], None],
) -> AsyncIterator[AgentEvent]:
    """Run a sync event producer on a thread, expose its events as an
    async iterator. Must be called from inside a running asyncio loop.

    If the async iterator is closed early (client disconnected), the next
    ``emit`` inside the producer raises ``_ConsumerGone`` so the thread
    stops promptly rather than draining the upstream stream to the end.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()
    consumer_gone = threading.Event()

    def _put(item: Any) -> None:
        if consumer_gone.is_set() or loop.is_closed():
            raise _ConsumerGone()
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        while True:
            try:
                future.result(timeout=_PUT_POLL_SECONDS)
                return
            except concurrent.futures.TimeoutError:
                if consumer_gone.is_set() or loop.is_closed():
                    future.cancel()
                    raise _ConsumerGone() from None

    def emit(event: AgentEvent) -> None:
        _put(event)

    def runner() -> None:
        try:
            sync_producer(emit)
        except _ConsumerGone:
            log.info("Streaming consumer disconnected; upstream producer aborted.")
            return
        except Exception as exc:  # noqa: BLE001 - surface any producer crash
            log.exception("Streaming producer raised an unhandled exception.")
            try:
                _put(StreamError(message=str(exc), error_type=type(exc).__name__))
            except _ConsumerGone:
                return
        try:
            _put(_SENTINEL)
        except _ConsumerGone:
            pass

    loop.run_in_executor(None, runner)
    return _drain_queue(queue, consumer_gone)


async def _drain_queue(
    queue: asyncio.Queue[Any], consumer_gone: threading.Event
) -> AsyncIterator[AgentEvent]:
    try:
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                return
            yield event
    finally:
        # Reached on normal completion, on aclose() from a disconnected
        # client, and on cancellation. Tells the producer thread to stop.
        consumer_gone.set()


# ---------------------------------------------------------------------------
# Helpers shared with run_tool_loop. Kept module-private; tests cover them
# transitively via the loop tests.
# ---------------------------------------------------------------------------


def _output(response: Any) -> list[Any]:
    return list(_get(response, "output") or [])


def _has_function_calls(response: Any) -> bool:
    return any(_get(item, "type") == "function_call" for item in _output(response))


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _dict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    return {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_") and not callable(getattr(obj, k))}


def _json_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ensure_jsonable(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"result": value}


def is_model_rate_limit_error(exc: Exception) -> bool:
    """Best-effort detector for Foundry / Azure OpenAI model throttling.

    The SDK may expose throttling as RateLimitError, APIError, or a provider
    message embedded in a generic exception depending on which streaming phase
    raised. Keep this narrow to model/backend wording; tool API throttles are
    handled by the tool implementations themselves.
    """
    status_code = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").lower()
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        status_code == 429
        or code in {"429", "rate_limit_exceeded"}
        or "rate_limit" in text
        or "rate limit" in text
        or "too many requests" in text
    )


def _model_fallback_tool_event(
    primary_agent: str,
    fallback_agent: str,
    exc: Exception,
) -> ToolEvent:
    return ToolEvent(
        name="model_fallback",
        arguments={
            "primary_agent": primary_agent,
            "fallback_agent": fallback_agent,
            "reason": "primary_model_rate_limited",
        },
        result={
            "used": True,
            "primary_agent": primary_agent,
            "fallback_agent": fallback_agent,
            "error_type": type(exc).__name__,
        },
        error=None,
    )
