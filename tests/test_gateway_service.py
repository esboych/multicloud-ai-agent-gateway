"""GatewayService: sticky conversations, ownership, policy and retry chains."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from multicloud_ai_agent_gateway.auth import Principal, TenantPolicy
from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentReturnedError,
    AgentSpec,
    AutoRouterContinueUnknown,
    BackendFailure,
    ConversationConflict,
    Forbidden,
    StreamDone,
    TextChunk,
    UnknownAgent,
)
from multicloud_ai_agent_gateway.core.conversations import InMemoryConversationStore
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.core.routing import AUTO_ROUTER_NAME, RouteDecision
from multicloud_ai_agent_gateway.core.service import (
    GatewayService,
    RoutingEvent,
    RoutingRetryEvent,
)

ANON = Principal.anonymous_principal()
ALICE = Principal(subject="alice", tenant="t1")
BOB = Principal(subject="bob", tenant="t2")


class _Classifier:
    """Deterministic classifier: FinOps first, ML as runner-up."""

    def classify(self, prompt: str) -> RouteDecision:
        return RouteDecision(
            selected_agent="FinOps",
            reason="stub",
            scores={"top_score": 1.0},
            method="bm25",
            confidence=0.9,
            candidates=({"agent": "FinOps", "score": 1.0}, {"agent": "ML", "score": 0.5}),
        )


class _Backend:
    backend_id = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail: dict[str, Exception] = {}
        self.conversation_ids: dict[str, str] = {}

    async def invoke(self, remote_id: str, prompt: str, *, conversation_id: str | None = None) -> AgentReply:
        self.calls.append((remote_id, prompt, conversation_id))
        if remote_id in self.fail:
            raise self.fail[remote_id]
        return AgentReply(
            output_text=f"{remote_id}:{prompt}",
            conversation_id=self.conversation_ids.get(remote_id, f"conv-{remote_id}"),
        )

    async def stream(self, remote_id: str, prompt: str, *, conversation_id: str | None = None) -> AsyncIterator[AgentEvent]:
        self.calls.append((remote_id, prompt, conversation_id))
        if remote_id in self.fail:
            raise self.fail[remote_id]
        yield TextChunk(text=f"{remote_id}:{prompt}")
        yield StreamDone(conversation_id=self.conversation_ids.get(remote_id, f"conv-{remote_id}"))

    async def aclose(self) -> None:
        return None


def _service(*, policy: TenantPolicy | None = None, classifier: Any = _Classifier()) -> tuple[GatewayService, _Backend]:
    backend = _Backend()
    registry = AgentRegistry(
        [
            AgentSpec(name="FinOps", backend_id="stub", remote_id="FinOps", description="cost"),
            AgentSpec(name="ML", backend_id="stub", remote_id="ML", description="training"),
        ]
    )
    router = Router(registry, {"stub": backend}, classifier=classifier)
    return GatewayService(router, policy=policy, conversations=InMemoryConversationStore()), backend


async def test_autorouter_is_sticky_on_known_conversation() -> None:
    service, backend = _service()
    first = await service.invoke(ANON, AUTO_ROUTER_NAME, "cost please")
    assert first.agent == "FinOps"
    assert first.reply.conversation_id == "conv-FinOps"

    # A follow-up that the classifier would send elsewhere stays with FinOps.
    backend.fail = {}
    second = await service.invoke(ANON, AUTO_ROUTER_NAME, "anything", "conv-FinOps", continuing=True)
    assert second.agent == "FinOps"
    assert second.decision is not None
    assert second.decision.method == "sticky"
    assert backend.calls[-1] == ("FinOps", "anything", "conv-FinOps")


async def test_autorouter_continue_unknown_conversation_rejected() -> None:
    service, _ = _service()
    with pytest.raises(AutoRouterContinueUnknown):
        await service.invoke(ANON, AUTO_ROUTER_NAME, "x", "never-seen", continuing=True)


async def test_explicit_agent_conflicts_with_owning_agent() -> None:
    service, _ = _service()
    await service.invoke(ANON, "FinOps", "hi")
    with pytest.raises(ConversationConflict):
        await service.invoke(ANON, "ML", "hi", "conv-FinOps")
    # Same agent is fine.
    outcome = await service.invoke(ANON, "FinOps", "again", "conv-FinOps")
    assert outcome.agent == "FinOps"


async def test_conversation_ownership_enforced_for_authenticated_callers() -> None:
    service, _ = _service()
    await service.invoke(ALICE, "FinOps", "hi")
    with pytest.raises(Forbidden):
        await service.invoke(BOB, "FinOps", "hi", "conv-FinOps")
    # Owner can continue; anonymous (auth disabled) is never blocked.
    await service.invoke(ALICE, "FinOps", "hi", "conv-FinOps")
    await service.invoke(ANON, "FinOps", "hi", "conv-FinOps")


async def test_conversation_started_anonymously_is_open() -> None:
    service, _ = _service()
    await service.invoke(ANON, "FinOps", "hi")
    await service.invoke(BOB, "FinOps", "hi", "conv-FinOps")  # no owner recorded


async def test_policy_filters_listing_and_blocks_invoke() -> None:
    policy = TenantPolicy.from_json('{"t1": {"agents": ["ML"]}, "t2": {"agents": ["*"]}}')
    service, _ = _service(policy=policy)
    assert [s.name for s in service.list_agents(ALICE)] == ["ML"]
    assert [s.name for s in service.list_agents(BOB)] == ["FinOps", "ML"]
    with pytest.raises(Forbidden):
        await service.invoke(ALICE, "FinOps", "hi")
    assert (await service.invoke(ALICE, "ML", "hi")).agent == "ML"


async def test_policy_redirects_autorouter_to_allowed_candidate() -> None:
    policy = TenantPolicy.from_json('{"t1": {"agents": ["ML"]}}')
    service, _ = _service(policy=policy)
    decision = service.route(ALICE, "cost please")
    assert decision.selected_agent == "ML"
    assert decision.method == "bm25+policy"
    outcome = await service.invoke(ALICE, AUTO_ROUTER_NAME, "cost please")
    assert outcome.agent == "ML"


async def test_policy_with_no_allowed_candidate_is_forbidden() -> None:
    policy = TenantPolicy.from_json('{"t1": {"agents": []}}')
    service, _ = _service(policy=policy)
    with pytest.raises(Forbidden):
        service.route(ALICE, "cost please")
    assert not service.auto_router_available(ALICE)


async def test_unknown_agent_is_404_not_403_even_with_policy() -> None:
    policy = TenantPolicy.from_json('{"t1": {"agents": ["ML"]}}')
    service, _ = _service(policy=policy)
    with pytest.raises(UnknownAgent):
        await service.invoke(ALICE, "Nope", "hi")


async def test_retry_runner_up_on_agent_error() -> None:
    service, backend = _service()
    backend.fail = {"FinOps": AgentReturnedError("ThrottlingException", "quota")}
    outcome = await service.invoke(ANON, AUTO_ROUTER_NAME, "cost")
    assert outcome.agent == "ML"
    assert outcome.decision is not None
    assert outcome.decision.method == "bm25+retry"
    assert [c[0] for c in backend.calls] == ["FinOps", "ML"]


async def test_generic_failure_on_last_candidate_is_backend_failure() -> None:
    service, backend = _service()
    backend.fail = {"FinOps": RuntimeError("boom"), "ML": ValueError("worse")}
    with pytest.raises(BackendFailure) as excinfo:
        await service.invoke(ANON, AUTO_ROUTER_NAME, "cost")
    assert excinfo.value.cause_type == "ValueError"
    assert excinfo.value.agent == "ML"


async def test_no_retry_for_explicit_agent() -> None:
    service, backend = _service()
    backend.fail = {"FinOps": AgentReturnedError("X", "y")}
    with pytest.raises(AgentReturnedError):
        await service.invoke(ANON, "FinOps", "cost")
    assert len(backend.calls) == 1


async def test_policy_excludes_runner_ups_from_retry_chain() -> None:
    policy = TenantPolicy.from_json('{"t1": {"agents": ["FinOps"]}}')
    service, backend = _service(policy=policy)
    backend.fail = {"FinOps": AgentReturnedError("X", "y")}
    with pytest.raises(AgentReturnedError):
        await service.invoke(ALICE, AUTO_ROUTER_NAME, "cost")
    assert [c[0] for c in backend.calls] == ["FinOps"]


async def test_stream_emits_routing_then_events_and_remembers() -> None:
    service, _ = _service()
    prepared = await service.prepare_stream(ALICE, AUTO_ROUTER_NAME, "cost")
    events = [event async for event in prepared.events()]
    assert isinstance(events[0], RoutingEvent)
    assert isinstance(events[1], TextChunk)
    assert isinstance(events[2], StreamDone)
    record = await service.conversations.get("conv-FinOps")
    assert record is not None and record.agent == "FinOps" and record.owner_subject == "alice"


async def test_stream_retries_runner_up_before_text() -> None:
    service, backend = _service()
    backend.fail = {"FinOps": RuntimeError("down")}
    prepared = await service.prepare_stream(ANON, AUTO_ROUTER_NAME, "cost")
    events = [event async for event in prepared.events()]
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["RoutingEvent", "RoutingRetryEvent", "TextChunk", "StreamDone"]
    assert isinstance(events[1], RoutingRetryEvent)
    assert events[1].selected_agent == "ML"


async def test_prepare_stream_raises_before_streaming() -> None:
    service, _ = _service()
    with pytest.raises(UnknownAgent):
        await service.prepare_stream(ANON, "Nope", "x")
    await service.invoke(ANON, "FinOps", "hi")
    with pytest.raises(ConversationConflict):
        await service.prepare_stream(ANON, "ML", "x", "conv-FinOps")
