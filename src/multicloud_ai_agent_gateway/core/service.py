"""GatewayService: the funnel above ``Router``.

``Router.invoke`` knows agents and backends. This layer adds the three
things every protocol facade (REST, MCP, A2A) needs identically:

* **who** is calling (``Principal``) and what they may use (``TenantPolicy``);
* **which conversation** this is (``ConversationStore``): sticky AutoRouter
  on follow-ups, and ownership so one tenant cannot continue another's
  thread by guessing the id;
* **resilience**: a candidate chain built from the router's runner-ups, with
  retry on ``AgentReturnedError`` for both request/response and streams.

Facades translate ``GatewayError`` subclasses into their own error shapes;
this module never imports FastAPI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, Union

from multicloud_ai_agent_gateway.auth.policy import TenantPolicy
from multicloud_ai_agent_gateway.auth.principal import Principal
from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentReturnedError,
    AgentSpec,
    AutoRouterContinueUnknown,
    BackendFailure,
    ConversationConflict,
    Forbidden,
    GatewayError,
    NoDispatcherConfigured,
    StreamDone,
    StreamError,
    TextChunk,
    ToolBudgetExceeded,
    UnknownAgent,
    event_kind,
)
from multicloud_ai_agent_gateway.core.conversations import (
    ConversationRecord,
    ConversationStore,
    NullConversationStore,
)
from multicloud_ai_agent_gateway.core.registry import Router
from multicloud_ai_agent_gateway.core.routing import AUTO_ROUTER_NAME, RouteDecision

log = logging.getLogger(__name__)

# Cap on how many agents one request may be retried through. Three is enough
# to skip a throttled primary and its runner-up without cascading across a
# broadly failing estate.
DEFAULT_MAX_CANDIDATES = 3


@dataclass(frozen=True)
class RoutingEvent:
    """First event of an AutoRouter stream: which concrete agent was picked."""

    decision: RouteDecision

    def to_dict(self) -> dict[str, Any]:
        return self.decision.to_dict()


@dataclass(frozen=True)
class RoutingRetryEvent:
    """Emitted when a stream switches to the next candidate after a failure."""

    selected_agent: str
    previous_agent: str
    reason: str
    hop: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_agent": self.selected_agent,
            "previous_agent": self.previous_agent,
            "reason": self.reason,
            "hop": self.hop,
        }


ServiceEvent = Union[RoutingEvent, RoutingRetryEvent, AgentEvent]


def service_event_kind(event: ServiceEvent) -> str:
    if isinstance(event, RoutingEvent):
        return "routing"
    if isinstance(event, RoutingRetryEvent):
        return "routing-retry"
    return event_kind(event)


@dataclass(frozen=True)
class InvokeOutcome:
    reply: AgentReply
    agent: str
    decision: RouteDecision | None


@dataclass(frozen=True)
class Resolution:
    """What a request resolved to before any backend was touched."""

    target: str
    decision: RouteDecision | None
    record: ConversationRecord | None


@dataclass
class PreparedStream:
    """A stream whose target has been resolved and validated.

    Facades call ``prepare_stream`` first so that routing errors (unknown
    agent, forbidden, conflict) surface as proper HTTP/JSON-RPC errors before
    any response headers are written; ``events()`` then yields the stream.
    """

    resolution: Resolution
    _service: "GatewayService"
    _principal: Principal
    _prompt: str
    _conversation_id: str | None

    @property
    def target(self) -> str:
        return self.resolution.target

    @property
    def decision(self) -> RouteDecision | None:
        return self.resolution.decision

    def events(self) -> AsyncIterator[ServiceEvent]:
        return self._service._stream_events(
            self._principal, self.resolution, self._prompt, self._conversation_id
        )


class GatewayService:
    def __init__(
        self,
        router: Router,
        *,
        policy: TenantPolicy | None = None,
        conversations: ConversationStore | None = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
    ) -> None:
        self._router = router
        self._policy = policy
        self._conversations: ConversationStore = (
            conversations if conversations is not None else NullConversationStore()
        )
        self._max_candidates = max(1, int(max_candidates))

    # -- introspection ----------------------------------------------------

    @property
    def router(self) -> Router:
        return self._router

    @property
    def policy(self) -> TenantPolicy | None:
        return self._policy

    @property
    def conversations(self) -> ConversationStore:
        return self._conversations

    @property
    def auto_router_enabled(self) -> bool:
        return self._router.auto_router_enabled

    def is_allowed(self, principal: Principal, agent: str) -> bool:
        if self._policy is None:
            return True
        return self._policy.is_allowed(principal, agent)

    def list_agents(self, principal: Principal) -> list[AgentSpec]:
        return [
            spec for spec in self._router.registry.list() if self.is_allowed(principal, spec.name)
        ]

    def auto_router_available(self, principal: Principal) -> bool:
        return self.auto_router_enabled and bool(self.list_agents(principal))

    # -- routing ------------------------------------------------------------

    def route(self, principal: Principal, prompt: str) -> RouteDecision:
        """Classify ``prompt`` and keep the decision inside the caller's allow-list.

        Blocking (hybrid classifiers may call an LLM); facades run it on a
        worker thread.
        """
        decision = self._router.route(prompt)
        if self._policy is None:
            return decision
        allowed = [
            c for c in decision.candidates if self.is_allowed(principal, str(c.get("agent", "")))
        ]
        if self.is_allowed(principal, decision.selected_agent):
            return replace(decision, candidates=tuple(allowed))
        for candidate in allowed:
            name = str(candidate.get("agent", ""))
            if name:
                return replace(
                    decision,
                    selected_agent=name,
                    reason=(
                        f"{decision.reason}; {decision.selected_agent!r} is not allowed for "
                        f"this tenant, using next allowed candidate {name!r}"
                    ),
                    method=f"{decision.method}+policy",
                    candidates=tuple(allowed),
                )
        raise Forbidden(
            f"No agent the router would pick for this prompt is allowed for tenant "
            f"{principal.tenant!r}."
        )

    async def resolve(
        self,
        principal: Principal,
        name: str,
        prompt: str,
        conversation_id: str | None,
        *,
        continuing: bool = False,
    ) -> Resolution:
        record = await self._conversations.get(conversation_id) if conversation_id else None
        if record is not None:
            self.check_owner(principal, record)

        if Router.is_auto_router(name):
            if record is not None:
                decision = _sticky_decision(record)
                if not self.is_allowed(principal, record.agent):
                    raise Forbidden(f"Agent {record.agent!r} is not allowed for this tenant.")
                return Resolution(record.agent, decision, record)
            if continuing:
                raise AutoRouterContinueUnknown(conversation_id or "")
            decision = await asyncio.to_thread(self.route, principal, prompt)
            return Resolution(decision.selected_agent, decision, None)

        if record is not None and record.agent != name:
            raise ConversationConflict(record.conversation_id, record.agent, name)
        # Unknown agents surface as UnknownAgent before any policy answer so a
        # typo is reported as 404, not 403.
        self._router.registry.get(name)
        if not self.is_allowed(principal, name):
            raise Forbidden(f"Agent {name!r} is not allowed for this tenant.")
        return Resolution(name, None, record)

    # -- request / response -------------------------------------------------

    async def invoke(
        self,
        principal: Principal,
        name: str,
        prompt: str,
        conversation_id: str | None = None,
        *,
        continuing: bool = False,
    ) -> InvokeOutcome:
        resolution = await self.resolve(
            principal, name, prompt, conversation_id, continuing=continuing
        )
        candidates = self._candidate_chain(principal, resolution)
        last_error: GatewayError | None = None
        used_agent = resolution.target
        reply: AgentReply | None = None

        for hop, candidate in enumerate(candidates):
            used_agent = candidate
            try:
                reply = await self._router.invoke(candidate, prompt, conversation_id=conversation_id)
                break
            except AgentReturnedError as exc:
                last_error = exc
                log.warning(
                    "Agent %s returned error %r; %s",
                    candidate,
                    exc.error_type,
                    f"trying runner-up {candidates[hop + 1]!r}"
                    if hop + 1 < len(candidates)
                    else "no candidates left",
                )
                if hop + 1 < len(candidates):
                    continue
                raise
            except (UnknownAgent, NoDispatcherConfigured, ToolBudgetExceeded):
                # Configuration or contract problems: retrying will not help.
                raise
            except Exception as exc:  # noqa: BLE001 - last-ditch envelope
                last_error = BackendFailure(candidate, exc)
                log.exception("Backend invocation failed for agent %s", candidate)
                if hop + 1 < len(candidates):
                    continue
                raise last_error from exc

        assert reply is not None  # the loop either breaks with a reply or raises

        decision = resolution.decision
        if decision is not None and used_agent != decision.selected_agent:
            decision = _decision_with_retry(decision, used_agent)

        await self._remember(principal, reply.conversation_id, used_agent)
        return InvokeOutcome(reply=reply, agent=used_agent, decision=decision)

    # -- streaming ----------------------------------------------------------

    async def prepare_stream(
        self,
        principal: Principal,
        name: str,
        prompt: str,
        conversation_id: str | None = None,
    ) -> PreparedStream:
        resolution = await self.resolve(principal, name, prompt, conversation_id)
        self._router.registry.get(resolution.target)  # UnknownAgent before headers go out
        return PreparedStream(resolution, self, principal, prompt, conversation_id)

    async def _stream_events(
        self,
        principal: Principal,
        resolution: Resolution,
        prompt: str,
        conversation_id: str | None,
    ) -> AsyncIterator[ServiceEvent]:
        if resolution.decision is not None:
            yield RoutingEvent(resolution.decision)

        candidates = self._candidate_chain(principal, resolution)
        text_emitted = False
        last_error: tuple[str, str] | None = None

        for hop, target in enumerate(candidates):
            if hop > 0:
                yield RoutingRetryEvent(
                    selected_agent=target,
                    previous_agent=candidates[hop - 1],
                    reason=(
                        f"{candidates[hop - 1]} failed: "
                        f"{last_error[0] if last_error else 'unknown'}; "
                        f"retrying with runner-up {target}"
                    ),
                    hop=hop,
                )
            try:
                async for event in self._router.stream(target, prompt, conversation_id=conversation_id):
                    if isinstance(event, TextChunk):
                        text_emitted = True
                    if isinstance(event, StreamError):
                        last_error = (event.error_type, event.message)
                        if not text_emitted and hop + 1 < len(candidates):
                            log.info(
                                "Stream from %s emitted error before text; trying next candidate.",
                                target,
                            )
                            break
                        yield event
                        return
                    if isinstance(event, StreamDone):
                        await self._remember(principal, event.conversation_id, target)
                        yield event
                        return
                    yield event
                else:
                    # Stream ended cleanly without a terminal event (rare but valid).
                    return
                continue  # broke out on a retryable error: next candidate
            except Exception as exc:  # noqa: BLE001 - backend errors must become events
                last_error = (type(exc).__name__, str(exc))
                log.warning(
                    "Backend %s raised %s while streaming: %s", target, type(exc).__name__, exc
                )
                if text_emitted or hop + 1 >= len(candidates):
                    log.exception("Streaming failed for agent %s", target)
                    yield StreamError(message=str(exc), error_type=type(exc).__name__)
                    return

        if last_error is not None:
            yield StreamError(
                message=f"All candidates failed; last error: {last_error[1]}",
                error_type=last_error[0],
            )

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        await self._conversations.aclose()
        await self._router.aclose()

    # -- internals ----------------------------------------------------------

    def _candidate_chain(self, principal: Principal, resolution: Resolution) -> list[str]:
        chain: list[str] = [resolution.target]
        decision = resolution.decision
        # A sticky or explicit target is tied to an existing conversation; a
        # runner-up has never seen that thread, so there is nothing to retry.
        if decision is None or resolution.record is not None:
            return chain
        for candidate in decision.candidates:
            name = candidate.get("agent") if isinstance(candidate, dict) else None
            if not name or name in chain or not self.is_allowed(principal, str(name)):
                continue
            chain.append(str(name))
            if len(chain) >= self._max_candidates:
                break
        return chain

    def check_owner(self, principal: Principal, record: ConversationRecord) -> None:
        if principal.anonymous:
            return
        if record.owner_subject is None and record.owner_tenant is None:
            return  # started before auth was enabled, or by an anonymous caller
        if record.owner_tenant != principal.tenant or record.owner_subject != principal.subject:
            raise Forbidden(
                f"Conversation {record.conversation_id!r} belongs to another caller."
            )

    async def _remember(self, principal: Principal, conversation_id: str | None, agent: str) -> None:
        if not conversation_id:
            return
        try:
            await self._conversations.remember(
                conversation_id,
                agent,
                owner_subject=None if principal.anonymous else principal.subject,
                owner_tenant=None if principal.anonymous else principal.tenant,
            )
        except Exception:  # noqa: BLE001 - the answer is already produced; never fail it
            log.exception("Conversation store write failed for %s", conversation_id)


def _sticky_decision(record: ConversationRecord) -> RouteDecision:
    return RouteDecision(
        selected_agent=record.agent,
        reason=f"sticky: conversation {record.conversation_id!r} is owned by {record.agent!r}",
        scores={},
        requested_agent=AUTO_ROUTER_NAME,
        mode="auto",
        method="sticky",
        confidence=1.0,
        candidates=({"agent": record.agent, "score": 1.0},),
    )


def _decision_with_retry(decision: RouteDecision, actual_agent: str) -> RouteDecision:
    return replace(
        decision,
        selected_agent=actual_agent,
        reason=f"{decision.reason}; retried after runtime failure -> {actual_agent}",
        method=f"{decision.method}+retry",
    )


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "GatewayService",
    "InvokeOutcome",
    "PreparedStream",
    "Resolution",
    "RoutingEvent",
    "RoutingRetryEvent",
    "ServiceEvent",
    "service_event_kind",
]
