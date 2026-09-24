from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from multicloud_ai_agent_gateway.core import (
    AUTO_ROUTER_BACKEND_ID,
    AUTO_ROUTER_DESCRIPTION,
    AUTO_ROUTER_NAME,
    AgentReturnedError,
    AutoRouterNotConfigured,
    NoDispatcherConfigured,
    RouteDecision,
    Router,
    ToolBudgetExceeded,
    UnknownAgent,
    event_kind,
)

log = logging.getLogger(__name__)


class InvokeRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32 * 1024)
    conversation_id: str | None = Field(default=None, description="Opaque ID returned by a previous invoke/continue call.")


class ChatRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=32 * 1024)
    conversation_id: str | None = Field(default=None, description="Opaque ID returned by a previous chat/invoke call.")


class ContinueRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32 * 1024)
    conversation_id: str = Field(..., min_length=1)


class RouteRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32 * 1024)


class RoutingMetadata(BaseModel):
    selected_agent: str
    requested_agent: str = AUTO_ROUTER_NAME
    mode: str = "auto"
    reason: str = ""
    scores: dict[str, float] = Field(default_factory=dict)
    # Phase 1 additions: surface which classifier produced the decision and
    # how confident it was, plus the top-N short list. The UI uses these to
    # render a "via bm25 · 0.42 conf" badge and an offer to rerun against
    # a runner-up agent.
    method: str = "keyword"
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class RouteResponse(BaseModel):
    routing: RoutingMetadata


class ToolEventResponse(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class InvokeResponse(BaseModel):
    output_text: str
    conversation_id: str | None
    finish_reason: str
    usage: dict[str, Any] | None = None
    tool_events: list[ToolEventResponse] = Field(default_factory=list)
    # Populated only when AutoRouter (or alias) was the requested agent.
    routing: RoutingMetadata | None = None


class AgentDescriptor(BaseModel):
    name: str
    backend_id: str
    description: str = ""
    remote_id: str = ""


class AgentListResponse(BaseModel):
    agents: list[AgentDescriptor]


class HealthResponse(BaseModel):
    status: str
    backends: list[str]
    auto_router_enabled: bool = False


def build_rest_app(*, router: Router, gateway_version: str) -> FastAPI:
    app = FastAPI(
        title="Multicloud AI Agent Gateway",
        version=gateway_version,
        description="Internal cross-cloud agent invocation API.",
    )
    api = APIRouter(prefix="/v1")

    def _agent_listing() -> list[AgentDescriptor]:
        items = [AgentDescriptor(**spec.to_dict()) for spec in router.registry.list()]
        if router.auto_router_enabled:
            items.append(
                AgentDescriptor(
                    name=AUTO_ROUTER_NAME,
                    backend_id=AUTO_ROUTER_BACKEND_ID,
                    description=AUTO_ROUTER_DESCRIPTION,
                    remote_id="",
                )
            )
        return items

    def _backends_list() -> list[str]:
        backends = {spec.backend_id for spec in router.registry.list()}
        if router.auto_router_enabled:
            backends.add(AUTO_ROUTER_BACKEND_ID)
        return sorted(backends)

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok",
            backends=_backends_list(),
            auto_router_enabled=router.auto_router_enabled,
        )

    @app.get("/readyz", response_model=HealthResponse, tags=["health"])
    async def readyz() -> HealthResponse:
        return HealthResponse(
            status="ready",
            backends=_backends_list(),
            auto_router_enabled=router.auto_router_enabled,
        )

    @api.get("/agents", response_model=AgentListResponse, tags=["agents"])
    async def list_agents() -> AgentListResponse:
        return AgentListResponse(agents=_agent_listing())

    @api.post("/route", response_model=RouteResponse, tags=["routing"])
    async def route_prompt(body: RouteRequest) -> RouteResponse:
        try:
            # Hybrid/LLM classifiers may do blocking I/O; keep the event loop
            # responsive by running the classify call on a worker thread.
            decision = await asyncio.to_thread(router.route, body.prompt)
        except AutoRouterNotConfigured as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RouteResponse(routing=_routing_metadata(decision))

    @api.post("/chat", response_model=InvokeResponse, tags=["chat"])
    async def chat(body: ChatRequest) -> InvokeResponse:
        return await _invoke(router, AUTO_ROUTER_NAME, body.input, body.conversation_id)

    @api.post(
        "/agents/{name}:invoke",
        response_model=InvokeResponse,
        tags=["agents"],
    )
    async def invoke_agent(
        body: InvokeRequest,
        name: str = Path(..., min_length=1),
    ) -> InvokeResponse:
        return await _invoke(router, name, body.prompt, body.conversation_id)

    @api.post(
        "/agents/{name}:continue",
        response_model=InvokeResponse,
        tags=["agents"],
    )
    async def continue_agent(
        body: ContinueRequest,
        name: str = Path(..., min_length=1),
    ) -> InvokeResponse:
        if Router.is_auto_router(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    "AutoRouter cannot be used with :continue. Pass the concrete agent "
                    "name returned in the previous response's 'routing.selected_agent'."
                ),
            )
        return await _invoke(router, name, body.prompt, body.conversation_id)

    @api.post(
        "/agents/{name}:stream",
        tags=["agents"],
        responses={
            200: {
                "description": "Server-Sent Events stream of AgentEvent items.",
                "content": {"text/event-stream": {}},
            },
            404: {"description": "Unknown agent."},
        },
    )
    async def stream_agent(
        body: InvokeRequest,
        name: str = Path(..., min_length=1),
    ) -> StreamingResponse:
        decision: RouteDecision | None = None
        target_name = name

        if Router.is_auto_router(name):
            try:
                decision = await asyncio.to_thread(router.route, body.prompt)
            except AutoRouterNotConfigured as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            target_name = decision.selected_agent

        try:
            router.registry.get(target_name)
        except UnknownAgent as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return StreamingResponse(
            _sse_stream(router, target_name, body.prompt, body.conversation_id, decision),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(api)
    return app


def _routing_metadata(decision: RouteDecision) -> RoutingMetadata:
    return RoutingMetadata(
        selected_agent=decision.selected_agent,
        requested_agent=decision.requested_agent,
        mode=decision.mode,
        reason=decision.reason,
        scores={k: float(v) for k, v in decision.scores.items()},
        method=decision.method,
        confidence=float(decision.confidence),
        candidates=[dict(c) for c in decision.candidates],
    )


async def _sse_stream(
    router: Router,
    name: str,
    prompt: str,
    conversation_id: str | None,
    decision: RouteDecision | None,
):
    """Format AgentBackend events as Server-Sent Events on the wire.

    When ``decision`` is provided (AutoRouter path) we prepend a ``routing``
    event so the client learns which concrete agent received the prompt
    before any text or tool events arrive.

    Resilience: if the first agent's stream fails *before* any text chunk
    has been emitted (e.g. Bedrock token-quota throttling), we fall back
    to the next BM25 candidate and re-emit a ``routing-retry`` event so
    the UI can update its bubble. Once any text has reached the wire the
    stream is committed -- we can't rewind half a message, so we emit the
    error and stop.
    """
    if decision is not None:
        payload = json.dumps(_routing_metadata(decision).model_dump(), default=str)
        yield f"event: routing\ndata: {payload}\n\n"

    candidates = _candidate_chain(name, decision)
    text_emitted = False
    last_error: tuple[str, str] | None = None

    for hop, target in enumerate(candidates):
        if hop > 0:
            # Tell the UI we switched agents before forwarding any new events.
            yield (
                "event: routing-retry\n"
                "data: "
                + json.dumps(
                    {
                        "selected_agent": target,
                        "previous_agent": candidates[hop - 1],
                        "reason": (
                            f"{candidates[hop - 1]} failed: "
                            f"{last_error[0] if last_error else 'unknown'}; "
                            f"retrying with runner-up {target}"
                        ),
                        "hop": hop,
                    },
                    default=str,
                )
                + "\n\n"
            )

        try:
            async for event in router.stream(target, prompt, conversation_id=conversation_id):
                kind = event_kind(event)
                if kind == "chunk":
                    text_emitted = True
                if kind == "error":
                    # Backend signalled error via StreamError event rather
                    # than raising. Treat the same as an exception below:
                    # retry from the next candidate if safe.
                    err_data = event.to_dict()
                    last_error = (
                        str(err_data.get("error_type") or "AgentError"),
                        str(err_data.get("message") or "stream error"),
                    )
                    if not text_emitted and hop + 1 < len(candidates):
                        log.info(
                            "Stream from %s emitted error before text; trying next candidate.",
                            target,
                        )
                        break
                    # Either we've already streamed chunks (cannot rewind)
                    # or no more candidates -- forward the error as-is.
                    yield f"event: error\ndata: {json.dumps(err_data, default=str)}\n\n"
                    return
                payload = json.dumps(event.to_dict(), default=str)
                yield f"event: {kind}\ndata: {payload}\n\n"
                if kind == "done":
                    return
            else:
                # Stream ended cleanly without 'done' (rare but valid).
                return
            # Hit a 'break' on retryable error: continue the for-hop loop.
            continue
        except Exception as exc:  # noqa: BLE001 - need to catch backend errors
            last_error = (type(exc).__name__, str(exc))
            log.warning(
                "Backend %s raised %s while streaming: %s",
                target, type(exc).__name__, exc,
            )
            if text_emitted or hop + 1 >= len(candidates):
                log.exception("Streaming failed for agent %s", target)
                yield (
                    "event: error\n"
                    "data: "
                    + json.dumps(
                        {"message": str(exc), "error_type": type(exc).__name__},
                        default=str,
                    )
                    + "\n\n"
                )
                return
            # Otherwise fall through the for-hop and try the next candidate.

    # Exhausted candidates without success.
    if last_error is not None:
        yield (
            "event: error\n"
            "data: "
            + json.dumps(
                {
                    "message": f"All candidates failed; last error: {last_error[1]}",
                    "error_type": last_error[0],
                },
                default=str,
            )
            + "\n\n"
        )


def _candidate_chain(initial: str, decision: RouteDecision | None) -> list[str]:
    """Return the ordered list of agents to try: initial first, then runner-ups.

    Runner-ups come from ``decision.candidates`` (BM25 top-N). Capped at 3
    total to avoid cascading retries on broadly-failing infrastructure.
    """
    chain: list[str] = [initial]
    if decision is not None:
        for cand in decision.candidates:
            name = cand.get("agent") if isinstance(cand, dict) else None
            if name and name not in chain:
                chain.append(name)
            if len(chain) >= 3:
                break
    return chain


async def _invoke(
    router: Router,
    name: str,
    prompt: str,
    conversation_id: str | None,
) -> InvokeResponse:
    decision: RouteDecision | None = None
    target_name = name

    if Router.is_auto_router(name):
        try:
            decision = await asyncio.to_thread(router.route, prompt)
        except AutoRouterNotConfigured as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        target_name = decision.selected_agent

    candidates = _candidate_chain(target_name, decision)
    last_error: Exception | None = None
    used_agent = target_name

    for hop, candidate in enumerate(candidates):
        used_agent = candidate
        try:
            reply = await router.invoke(
                candidate, prompt, conversation_id=conversation_id
            )
            break
        except UnknownAgent as exc:
            # Unknown agent is not retryable -- a typo / config error is
            # the only way this happens. Fail fast.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NoDispatcherConfigured as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ToolBudgetExceeded as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except AgentReturnedError as exc:
            last_error = exc
            log.warning(
                "Agent %s returned error %r; %s",
                candidate, exc.error_type,
                f"trying runner-up {candidates[hop + 1]!r}"
                if hop + 1 < len(candidates) else "no candidates left",
            )
            if hop + 1 < len(candidates):
                continue
            raise HTTPException(
                status_code=502,
                detail=f"agent_returned_error: {exc.error_type}: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - last-ditch error envelope
            last_error = exc
            log.exception("Backend invocation failed for agent %s", candidate)
            if hop + 1 < len(candidates):
                continue
            raise HTTPException(
                status_code=502,
                detail=f"backend_error: {type(exc).__name__}",
            ) from exc
    else:
        # Unreachable: the loop above either returns or raises.
        raise HTTPException(
            status_code=502,
            detail=f"all_candidates_failed: {last_error!r}",
        )

    # If we retried, surface that fact in the routing metadata by
    # overwriting the selected_agent. The candidates list is preserved
    # so the UI can show what was tried.
    if decision is not None and used_agent != decision.selected_agent:
        decision = _decision_with_retry(decision, used_agent)

    return InvokeResponse(
        output_text=reply.output_text,
        conversation_id=reply.conversation_id,
        finish_reason=reply.finish_reason,
        usage=reply.usage,
        tool_events=[ToolEventResponse(**event.to_dict()) for event in reply.tool_events],
        routing=_routing_metadata(decision) if decision is not None else None,
    )


def _decision_with_retry(decision: RouteDecision, actual_agent: str) -> RouteDecision:
    """Return a RouteDecision rewritten to reflect a retry-on-failure hop."""
    from dataclasses import replace

    return replace(
        decision,
        selected_agent=actual_agent,
        reason=(
            f"{decision.reason}; retried after runtime failure -> {actual_agent}"
        ),
        method=f"{decision.method}+retry",
    )
