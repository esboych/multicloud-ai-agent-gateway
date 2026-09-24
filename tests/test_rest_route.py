"""Tests for the AutoRouter surface in the REST frontend.

Covers:
  - GET /v1/agents includes a virtual AutoRouter entry when the classifier is wired.
  - POST /v1/route returns the routing decision without invoking any backend.
  - POST /v1/agents/AutoRouter:invoke routes then dispatches to the resolved agent
    and returns the routing block in the response.
  - POST /v1/agents/AutoRouter:stream emits a 'routing' SSE event before forwarding.
  - POST /v1/agents/AutoRouter:continue is rejected (400, conversation tied to agent).
  - When no classifier is configured, AutoRouter requests return 422 and the virtual
    entry is omitted from the listing.
  - /healthz / /readyz expose the auto_router_enabled flag.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.core.routing import AUTO_ROUTER_NAME
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


def _client(router) -> TestClient:
    return TestClient(build_rest_app(router=router, gateway_version="test-0.0.0"))


# ---------------------------------------------------------------------------
# /v1/agents listing: AutoRouter visibility depends on classifier
# ---------------------------------------------------------------------------


def test_list_agents_includes_autorouter_when_classifier_configured(
    router_with_autorouter,
) -> None:
    response = _client(router_with_autorouter).get("/v1/agents")
    assert response.status_code == 200
    names = [a["name"] for a in response.json()["agents"]]
    assert AUTO_ROUTER_NAME in names
    autorouter = next(a for a in response.json()["agents"] if a["name"] == AUTO_ROUTER_NAME)
    assert autorouter["backend_id"] == "autorouter"
    assert autorouter["description"]


def test_list_agents_excludes_autorouter_when_classifier_missing(router_for) -> None:
    response = _client(router_for).get("/v1/agents")
    names = [a["name"] for a in response.json()["agents"]]
    assert AUTO_ROUTER_NAME not in names


# ---------------------------------------------------------------------------
# Health endpoints expose auto_router_enabled
# ---------------------------------------------------------------------------


def test_healthz_reports_auto_router_enabled(router_with_autorouter) -> None:
    response = _client(router_with_autorouter).get("/healthz")
    body = response.json()
    assert body["auto_router_enabled"] is True
    assert "autorouter" in body["backends"]


def test_healthz_reports_auto_router_disabled(router_for) -> None:
    response = _client(router_for).get("/healthz")
    body = response.json()
    assert body["auto_router_enabled"] is False
    assert "autorouter" not in body["backends"]


# ---------------------------------------------------------------------------
# POST /v1/route -- routing decision only, no backend invocation
# ---------------------------------------------------------------------------


def test_route_returns_decision(router_with_autorouter, stub_backend) -> None:
    response = _client(router_with_autorouter).post(
        "/v1/route",
        json={"prompt": "Show AWS S3 costs in us-east-1"},
    )
    assert response.status_code == 200
    routing = response.json()["routing"]
    assert routing["selected_agent"] == "AwsFinOpsSmartAgent"
    assert routing["mode"] == "auto"
    assert routing["requested_agent"] == AUTO_ROUTER_NAME
    assert routing["scores"]["aws_signal"] > 0
    # Phase-1 fields surface through REST so the UI can render the badge.
    assert routing["method"] == "keyword"
    assert 0.0 <= routing["confidence"] <= 1.0
    assert routing["candidates"], "candidates list must round-trip through REST"
    assert routing["candidates"][0]["agent"] == routing["selected_agent"]
    # Critical: route must not have called the backend.
    assert stub_backend.calls == []


def test_route_without_classifier_returns_422(router_for) -> None:
    response = _client(router_for).post(
        "/v1/route",
        json={"prompt": "Show AWS S3 costs"},
    )
    assert response.status_code == 422
    assert "AutoRouter" in response.json()["detail"]


def test_route_validates_prompt(router_with_autorouter) -> None:
    response = _client(router_with_autorouter).post("/v1/route", json={"prompt": ""})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/agents/AutoRouter:invoke -- routes then invokes the resolved agent
# ---------------------------------------------------------------------------


def test_autorouter_invoke_dispatches_to_resolved_agent(
    router_with_autorouter, stub_backend
) -> None:
    response = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:invoke",
        json={"prompt": "Show current month Azure costs"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["output_text"] == "echo:Show current month Azure costs"
    # Backend must see the resolved name, not 'AutoRouter'.
    assert stub_backend.calls == [("AzureFinOpsAgent", "Show current month Azure costs", None)]


def test_autorouter_invoke_returns_routing_block(router_with_autorouter) -> None:
    response = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:invoke",
        json={"prompt": "List Azure ML training jobs on cpu-cluster"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["routing"] is not None
    assert body["routing"]["selected_agent"] == "AzureMLTrainingAgent"
    assert body["routing"]["mode"] == "auto"
    assert body["routing"]["requested_agent"] == AUTO_ROUTER_NAME
    assert body["routing"]["scores"]["ml_score"] > 0


def test_chat_endpoint_routes_via_autorouter(
    router_with_autorouter, stub_backend
) -> None:
    response = _client(router_with_autorouter).post(
        "/v1/chat",
        json={"input": "Show current month Azure costs"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["output_text"] == "echo:Show current month Azure costs"
    assert body["routing"]["selected_agent"] == "AzureFinOpsAgent"
    assert body["routing"]["requested_agent"] == AUTO_ROUTER_NAME
    assert stub_backend.calls == [("AzureFinOpsAgent", "Show current month Azure costs", None)]


def test_autorouter_invoke_via_alias(router_with_autorouter, stub_backend) -> None:
    """Aliases like 'auto' / 'router' (case-insensitive) work the same way."""
    response = _client(router_with_autorouter).post(
        "/v1/agents/auto:invoke",
        json={"prompt": "Show current month Azure costs"},
    )
    assert response.status_code == 200
    assert stub_backend.calls[0][0] == "AzureFinOpsAgent"


def test_invoke_without_routing_omits_routing_field(router_with_autorouter) -> None:
    """Direct agent invoke (not via AutoRouter) returns routing=null."""
    response = _client(router_with_autorouter).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "anything"},
    )
    assert response.json()["routing"] is None


def test_autorouter_invoke_without_classifier_returns_422(router_for) -> None:
    response = _client(router_for).post(
        "/v1/agents/AutoRouter:invoke",
        json={"prompt": "anything"},
    )
    assert response.status_code == 422


def test_autorouter_invoke_routes_to_unregistered_agent_returns_404(
    router_with_autorouter,
) -> None:
    """Phase 0 keeps the hardcoded 4-agent classifier; Phase 1 will make this data-driven.

    For now, if the classifier picks an agent name not in the registry (e.g.
    AwsSageMakerTrainingSmartAgent), the gateway returns 404 with that agent's
    name so the caller can see which one was missing.
    """
    response = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:invoke",
        json={"prompt": "Train a SageMaker job in AWS"},
    )
    assert response.status_code == 404
    assert "AwsSageMakerTrainingSmartAgent" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /v1/agents/AutoRouter:continue -- rejected
# ---------------------------------------------------------------------------


def test_autorouter_continue_returns_400(router_with_autorouter) -> None:
    response = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:continue",
        json={"prompt": "more", "conversation_id": "resp_prev"},
    )
    assert response.status_code == 400
    assert "AutoRouter" in response.json()["detail"]


def test_autorouter_continue_alias_also_rejected(router_with_autorouter) -> None:
    response = _client(router_with_autorouter).post(
        "/v1/agents/auto:continue",
        json={"prompt": "more", "conversation_id": "resp_prev"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /v1/agents/AutoRouter:stream -- emits 'routing' SSE event first
# ---------------------------------------------------------------------------


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


def test_autorouter_stream_emits_routing_event_first(router_with_autorouter) -> None:
    """The streaming Foundry stub backend in conftest does not implement stream();
    we expect the routing event followed by an error event for the missing stream
    implementation. The point of this test is the routing event ordering & shape."""
    resp = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:stream",
        json={"prompt": "Show current month Azure costs"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert events, "expected at least one SSE event"
    first_kind, first_data = events[0]
    assert first_kind == "routing"
    assert first_data["selected_agent"] == "AzureFinOpsAgent"
    assert first_data["mode"] == "auto"
    assert first_data["requested_agent"] == AUTO_ROUTER_NAME


def test_autorouter_stream_without_classifier_returns_422(router_for) -> None:
    resp = _client(router_for).post(
        "/v1/agents/AutoRouter:stream",
        json={"prompt": "anything"},
    )
    assert resp.status_code == 422


def test_autorouter_stream_routes_to_unregistered_agent_returns_404(
    router_with_autorouter,
) -> None:
    resp = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:stream",
        json={"prompt": "Train a SageMaker job in AWS"},
    )
    assert resp.status_code == 404
    assert "AwsSageMakerTrainingSmartAgent" in resp.json()["detail"]
