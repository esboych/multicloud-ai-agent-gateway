"""Tests for the chat-ui's local route validation.

After Phase 0 the keyword classifier moved into the gateway. The chat-ui's
route_chat_agent is now a thin function that:

  - Returns a RouteDecision with mode="auto" and the AUTO_ROUTER_AGENT
    sentinel for any auto/router alias (the gateway resolves the concrete
    agent on invoke).
  - Returns a RouteDecision with mode="manual" for any agent in the local
    SUPPORTED_CHAT_AGENTS allow-list.
  - Raises ValueError for unknown manual selections so the chat handler can
    return 400 without a wasted gateway round-trip.

The keyword routing tests live in tests/test_routing.py against the
gateway's KeywordClassifier.
"""

from __future__ import annotations

import pytest

from agent_chat_ui.app import (
    AUTO_ROUTER_AGENT,
    CHAT_HTML,
    SUPPORTED_CHAT_AGENTS,
    client_identity,
    load_settings,
    route_chat_agent,
    stream_target_agent,
)


def test_route_chat_agent_autorouter_keeps_sentinel() -> None:
    """Auto mode never resolves locally; gateway picks the concrete agent."""
    decision = route_chat_agent(AUTO_ROUTER_AGENT)
    assert decision.mode == "auto"
    assert decision.requested_agent == AUTO_ROUTER_AGENT
    assert decision.selected_agent == AUTO_ROUTER_AGENT
    assert decision.scores == {}


@pytest.mark.parametrize("alias", ["auto", "autorouter", "router", "Auto", "AUTO"])
def test_route_chat_agent_alias_is_autorouter(alias: str) -> None:
    decision = route_chat_agent(alias)
    assert decision.mode == "auto"
    assert decision.selected_agent == AUTO_ROUTER_AGENT


def test_route_chat_agent_manual_passthrough_for_known_agent() -> None:
    decision = route_chat_agent("AwsFinOpsHiddenCostAgent")
    assert decision.mode == "manual"
    assert decision.requested_agent == "AwsFinOpsHiddenCostAgent"
    assert decision.selected_agent == "AwsFinOpsHiddenCostAgent"
    assert decision.reason == "manual selection"


def test_route_chat_agent_manual_passthrough_for_gcp_agent() -> None:
    decision = route_chat_agent("GcpAgent")
    assert decision.mode == "manual"
    assert decision.requested_agent == "GcpAgent"
    assert decision.selected_agent == "GcpAgent"
    assert decision.reason == "manual selection"


def test_route_chat_agent_manual_works_for_each_chat_agent() -> None:
    for agent in SUPPORTED_CHAT_AGENTS:
        if agent == AUTO_ROUTER_AGENT:
            continue
        decision = route_chat_agent(agent)
        assert decision.selected_agent == agent
        assert decision.mode == "manual"


def test_route_chat_agent_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError, match="Unsupported chat agent"):
        route_chat_agent("NopeAgent")


def test_route_chat_agent_treats_empty_request_as_autorouter() -> None:
    decision = route_chat_agent("")
    assert decision.mode == "auto"
    assert decision.selected_agent == AUTO_ROUTER_AGENT


def test_route_chat_agent_treats_whitespace_as_autorouter() -> None:
    decision = route_chat_agent("   ")
    assert decision.mode == "auto"
    assert decision.selected_agent == AUTO_ROUTER_AGENT


def test_route_chat_agent_treats_none_as_autorouter() -> None:
    decision = route_chat_agent(None)
    assert decision.mode == "auto"
    assert decision.selected_agent == AUTO_ROUTER_AGENT


def test_chat_html_surfaces_router_method_badge() -> None:
    assert "data-router-badge" in CHAT_HTML
    assert "routerMethodLabel" in CHAT_HTML
    assert "router: ${routerMethodLabel(router.method)}" in CHAT_HTML


def test_load_settings_accepts_legacy_live_gateway_env_names() -> None:
    settings = load_settings(
        {
            "GATEWAY_BASE_URL": "http://multicloud-ai-agent-gateway",
            "GATEWAY_TIMEOUT_SECONDS": "300",
        }
    )
    assert settings.gateway_url == "http://multicloud-ai-agent-gateway"
    assert settings.request_timeout_seconds == 300


def test_stream_target_is_autorouter_in_auto_mode() -> None:
    # The gateway can only retry a runner-up when it did the routing itself.
    assert stream_target_agent(route_chat_agent("AutoRouter")) == "AutoRouter"
    assert stream_target_agent(route_chat_agent(None)) == "AutoRouter"


def test_stream_target_is_selected_agent_in_manual_mode() -> None:
    assert stream_target_agent(route_chat_agent("AzureFinOpsAgent")) == "AzureFinOpsAgent"


def test_client_identity_ignores_forwarded_for_unless_proxy_is_trusted() -> None:
    peer = ("192.0.2.9", 1234)
    spoofed = "203.0.113.7, 10.0.0.1"
    assert client_identity(spoofed, peer, trust_proxy_headers=False) == "192.0.2.9"
    assert client_identity(spoofed, peer, trust_proxy_headers=True) == "203.0.113.7"
    assert client_identity("", None, trust_proxy_headers=True) == "unknown"


def test_load_settings_trust_proxy_headers_defaults_to_false() -> None:
    assert load_settings({"GATEWAY_URL": "http://gw"}).trust_proxy_headers is False
    assert load_settings({"GATEWAY_URL": "http://gw", "TRUST_PROXY_HEADERS": "true"}).trust_proxy_headers is True
