from __future__ import annotations

import pytest

from multicloud_ai_agent_gateway.core.backend import (
    AgentSpec,
    AutoRouterNotConfigured,
    UnknownAgent,
)
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.core.routing import (
    AUTO_ROUTER_NAME,
    KeywordClassifier,
    RouteDecision,
)


def test_registry_returns_specs_sorted_by_name() -> None:
    registry = AgentRegistry(
        [
            AgentSpec(name="Zeta", backend_id="foundry"),
            AgentSpec(name="Alpha", backend_id="foundry"),
        ]
    )
    assert [s.name for s in registry.list()] == ["Alpha", "Zeta"]


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        AgentRegistry(
            [
                AgentSpec(name="Dup", backend_id="foundry"),
                AgentSpec(name="Dup", backend_id="foundry"),
            ]
        )


def test_registry_get_unknown_raises() -> None:
    registry = AgentRegistry([AgentSpec(name="Known", backend_id="foundry")])
    with pytest.raises(UnknownAgent) as info:
        registry.get("Unknown")
    assert info.value.name == "Unknown"


def test_router_rejects_agent_pointing_at_missing_backend() -> None:
    registry = AgentRegistry([AgentSpec(name="A", backend_id="agentcore")])
    with pytest.raises(ValueError, match="not registered"):
        Router(registry, backends={})


async def test_router_routes_by_backend_id_and_uses_remote_id(stub_backend, router_for) -> None:
    reply = await router_for.invoke("AzureFinOpsAgent", "hi")
    assert reply.output_text == "echo:hi"
    assert stub_backend.calls == [("AzureFinOpsAgent", "hi", None)]


async def test_router_propagates_conversation_id(stub_backend, router_for) -> None:
    await router_for.invoke("AzureMLTrainingAgent", "again", conversation_id="resp_prev")
    assert stub_backend.calls == [("AzureMLTrainingAgent", "again", "resp_prev")]


async def test_router_aclose_closes_backends(router_for, stub_backend) -> None:
    closed: list[str] = []

    async def fake_aclose() -> None:
        closed.append(stub_backend.backend_id)

    stub_backend.aclose = fake_aclose  # type: ignore[method-assign]
    await router_for.aclose()
    assert closed == ["foundry"]


# ---------------------------------------------------------------------------
# AutoRouter wiring
# ---------------------------------------------------------------------------


def test_registry_rejects_reserved_autorouter_name() -> None:
    with pytest.raises(ValueError, match="reserved"):
        AgentRegistry([AgentSpec(name=AUTO_ROUTER_NAME, backend_id="foundry")])


def test_router_without_classifier_disables_autorouter(router_for) -> None:
    assert router_for.auto_router_enabled is False
    assert router_for.classifier is None
    with pytest.raises(AutoRouterNotConfigured):
        router_for.route("Show Azure costs")


def test_router_with_classifier_enables_autorouter(router_with_autorouter) -> None:
    assert router_with_autorouter.auto_router_enabled is True
    assert isinstance(router_with_autorouter.classifier, KeywordClassifier)


def test_router_route_returns_decision(router_with_autorouter) -> None:
    decision = router_with_autorouter.route("Show AWS S3 costs")
    assert isinstance(decision, RouteDecision)
    assert decision.selected_agent == "AwsFinOpsSmartAgent"


@pytest.mark.parametrize("name", ["AutoRouter", "auto", "router", "AUTOROUTER"])
def test_router_is_auto_router_helper(name: str) -> None:
    assert Router.is_auto_router(name) is True


@pytest.mark.parametrize("name", ["AzureFinOpsAgent", "", None, " "])
def test_router_is_auto_router_false_for_others(name: str | None) -> None:
    assert Router.is_auto_router(name) is False
