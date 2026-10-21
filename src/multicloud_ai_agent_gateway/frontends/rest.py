from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from multicloud_ai_agent_gateway.auth.principal import Principal
from multicloud_ai_agent_gateway.core import (
    AUTO_ROUTER_BACKEND_ID,
    AUTO_ROUTER_DESCRIPTION,
    AUTO_ROUTER_NAME,
    GatewayError,
    RouteDecision,
    Router,
)
from multicloud_ai_agent_gateway.core.conversations import InMemoryConversationStore
from multicloud_ai_agent_gateway.core.service import (
    GatewayService,
    ServiceEvent,
    service_event_kind,
)
from multicloud_ai_agent_gateway.frontends.common import (
    Authenticator,
    bearer_principal_dependency,
    http_error_for,
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
    # Which classifier produced the decision and how confident it was, plus
    # the top-N short list. ``method`` can also be ``sticky`` (conversation
    # store resolved the agent) or carry ``+retry`` / ``+policy`` suffixes.
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
    # True when the result was replayed from the primary attempt during a
    # Foundry model fallback instead of executing the tool again.
    replayed: bool = False


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
    auth_enabled: bool = False


class PrincipalResponse(BaseModel):
    subject: str
    tenant: str | None = None
    issuer: str | None = None
    scopes: list[str] = Field(default_factory=list)
    anonymous: bool = False
    allowed_agents: list[str] = Field(default_factory=list)


class ConversationResponse(BaseModel):
    conversation_id: str
    agent: str
    owner_subject: str | None = None
    owner_tenant: str | None = None
    created_at: float
    last_seen_at: float


def build_rest_app(
    *,
    gateway_version: str,
    router: Router | None = None,
    service: GatewayService | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Pass ``service`` (what ``app.py`` does) or, for tests and embedding, a
    bare ``router``: it is wrapped in a ``GatewayService`` with an in-memory
    conversation store and no tenant policy.
    """
    if service is None:
        if router is None:
            raise ValueError("build_rest_app needs a service or a router")
        service = GatewayService(router, conversations=InMemoryConversationStore())
    svc = service

    app = FastAPI(
        title="Multicloud AI Agent Gateway",
        version=gateway_version,
        description="Internal cross-cloud agent invocation API.",
    )
    api = APIRouter(prefix="/v1")
    principal_dep = bearer_principal_dependency(authenticator)
    auth_enabled = authenticator is not None

    def _agent_listing(principal: Principal) -> list[AgentDescriptor]:
        items = [AgentDescriptor(**spec.to_dict()) for spec in svc.list_agents(principal)]
        if svc.auto_router_available(principal):
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
        backends = {spec.backend_id for spec in svc.router.registry.list()}
        if svc.auto_router_enabled:
            backends.add(AUTO_ROUTER_BACKEND_ID)
        return sorted(backends)

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok",
            backends=_backends_list(),
            auto_router_enabled=svc.auto_router_enabled,
            auth_enabled=auth_enabled,
        )

    @app.get("/readyz", response_model=HealthResponse, tags=["health"])
    async def readyz() -> HealthResponse:
        return HealthResponse(
            status="ready",
            backends=_backends_list(),
            auto_router_enabled=svc.auto_router_enabled,
            auth_enabled=auth_enabled,
        )

    @api.get("/agents", response_model=AgentListResponse, tags=["agents"])
    async def list_agents(principal: Principal = Depends(principal_dep)) -> AgentListResponse:
        return AgentListResponse(agents=_agent_listing(principal))

    @api.get("/me", response_model=PrincipalResponse, tags=["auth"])
    async def whoami(principal: Principal = Depends(principal_dep)) -> PrincipalResponse:
        return PrincipalResponse(
            **principal.to_dict(),
            allowed_agents=[spec.name for spec in svc.list_agents(principal)],
        )

    @api.get(
        "/conversations/{conversation_id}",
        response_model=ConversationResponse,
        tags=["conversations"],
    )
    async def get_conversation(
        conversation_id: str = Path(..., min_length=1),
        principal: Principal = Depends(principal_dep),
    ) -> ConversationResponse:
        record = await svc.conversations.get(conversation_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown conversation: {conversation_id!r}")
        try:
            svc.check_owner(principal, record)
        except GatewayError as exc:
            raise http_error_for(exc) from exc
        return ConversationResponse(**record.to_dict())

    @api.post("/route", response_model=RouteResponse, tags=["routing"])
    async def route_prompt(
        body: RouteRequest, principal: Principal = Depends(principal_dep)
    ) -> RouteResponse:
        try:
            # Hybrid/LLM classifiers may do blocking I/O; keep the event loop
            # responsive by running the classify call on a worker thread.
            decision = await asyncio.to_thread(svc.route, principal, body.prompt)
        except GatewayError as exc:
            raise http_error_for(exc) from exc
        return RouteResponse(routing=_routing_metadata(decision))

    @api.post("/chat", response_model=InvokeResponse, tags=["chat"])
    async def chat(
        body: ChatRequest, principal: Principal = Depends(principal_dep)
    ) -> InvokeResponse:
        return await _invoke(svc, principal, AUTO_ROUTER_NAME, body.input, body.conversation_id)

    @api.post(
        "/agents/{name}:invoke",
        response_model=InvokeResponse,
        tags=["agents"],
    )
    async def invoke_agent(
        body: InvokeRequest,
        name: str = Path(..., min_length=1),
        principal: Principal = Depends(principal_dep),
    ) -> InvokeResponse:
        return await _invoke(svc, principal, name, body.prompt, body.conversation_id)

    @api.post(
        "/agents/{name}:continue",
        response_model=InvokeResponse,
        tags=["agents"],
    )
    async def continue_agent(
        body: ContinueRequest,
        name: str = Path(..., min_length=1),
        principal: Principal = Depends(principal_dep),
    ) -> InvokeResponse:
        # AutoRouter is accepted here when the conversation store knows which
        # agent owns the conversation (sticky routing); otherwise 400.
        return await _invoke(
            svc, principal, name, body.prompt, body.conversation_id, continuing=True
        )

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
        principal: Principal = Depends(principal_dep),
    ) -> StreamingResponse:
        try:
            prepared = await svc.prepare_stream(principal, name, body.prompt, body.conversation_id)
        except GatewayError as exc:
            raise http_error_for(exc) from exc

        return StreamingResponse(
            _sse_stream(prepared.events()),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(api)
    app.state.gateway_service = svc
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


def sse_line(event: ServiceEvent) -> str:
    """One Server-Sent Event block for a service event."""
    kind = service_event_kind(event)
    if kind == "routing":
        payload = _routing_metadata(event.decision).model_dump()  # type: ignore[union-attr]
    else:
        payload = event.to_dict()
    return f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"


async def _sse_stream(events):
    """Format service events as Server-Sent Events on the wire.

    Retry/fallback decisions are made inside ``GatewayService``; this layer
    only serialises what it is handed (``routing``, ``routing-retry``,
    ``chunk``, ``tool_started``, ``tool_finished``, ``done``, ``error``).
    """
    async for event in events:
        yield sse_line(event)


async def _invoke(
    service: GatewayService,
    principal: Principal,
    name: str,
    prompt: str,
    conversation_id: str | None,
    *,
    continuing: bool = False,
) -> InvokeResponse:
    try:
        outcome = await service.invoke(
            principal, name, prompt, conversation_id, continuing=continuing
        )
    except GatewayError as exc:
        raise http_error_for(exc) from exc

    reply = outcome.reply
    return InvokeResponse(
        output_text=reply.output_text,
        conversation_id=reply.conversation_id,
        finish_reason=reply.finish_reason,
        usage=reply.usage,
        tool_events=[ToolEventResponse(**event.to_dict()) for event in reply.tool_events],
        routing=_routing_metadata(outcome.decision) if outcome.decision is not None else None,
    )
