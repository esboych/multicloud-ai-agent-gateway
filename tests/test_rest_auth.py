"""REST facade with authentication and tenant policy switched on."""

from __future__ import annotations

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.auth import Principal, TenantPolicy
from multicloud_ai_agent_gateway.core.backend import AuthenticationRequired
from multicloud_ai_agent_gateway.core.conversations import InMemoryConversationStore
from multicloud_ai_agent_gateway.core.service import GatewayService
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


class FakeAuthenticator:
    def __init__(self, tokens: dict[str, Principal]) -> None:
        self._tokens = tokens

    def authenticate(self, token: str) -> Principal:
        try:
            return self._tokens[token]
        except KeyError as exc:
            raise AuthenticationRequired("bad token") from exc


TOKENS = {
    "alice-token": Principal(subject="alice", tenant="t1", scopes=("agents.invoke",)),
    "bob-token": Principal(subject="bob", tenant="t2"),
    "nobody-token": Principal(subject="nobody", tenant="t-unlisted"),
}


def _client(router, policy_json: str | None = None) -> TestClient:
    policy = TenantPolicy.from_json(policy_json) if policy_json else None
    service = GatewayService(router, policy=policy, conversations=InMemoryConversationStore())
    app = build_rest_app(
        service=service, gateway_version="t", authenticator=FakeAuthenticator(TOKENS)
    )
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_missing_token_is_401_with_challenge(router_for) -> None:
    resp = _client(router_for).get("/v1/agents")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")


def test_bad_token_is_401(router_for) -> None:
    resp = _client(router_for).get("/v1/agents", headers=_auth("garbage"))
    assert resp.status_code == 401
    assert "bad token" in resp.json()["detail"]


def test_non_bearer_scheme_is_401(router_for) -> None:
    resp = _client(router_for).get("/v1/agents", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_health_endpoints_stay_open(router_for) -> None:
    client = _client(router_for)
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json()["auth_enabled"] is True
    assert client.get("/readyz").status_code == 200


def test_good_token_lists_agents_and_me(router_for) -> None:
    client = _client(router_for)
    assert client.get("/v1/agents", headers=_auth("alice-token")).status_code == 200
    me = client.get("/v1/me", headers=_auth("alice-token")).json()
    assert me["subject"] == "alice"
    assert me["tenant"] == "t1"
    assert me["scopes"] == ["agents.invoke"]
    assert me["allowed_agents"] == ["AzureFinOpsAgent", "AzureMLTrainingAgent"]


def test_invoke_with_token_passes_through(router_for, stub_backend) -> None:
    resp = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke", json={"prompt": "hi"}, headers=_auth("alice-token")
    )
    assert resp.status_code == 200
    assert stub_backend.calls == [("AzureFinOpsAgent", "hi", None)]


def test_policy_filters_listing_and_forbids(router_with_autorouter) -> None:
    client = _client(
        router_with_autorouter,
        '{"t1": {"agents": ["AzureMLTrainingAgent"]}, "t2": {"agents": ["*"]}}',
    )
    names = [a["name"] for a in client.get("/v1/agents", headers=_auth("alice-token")).json()["agents"]]
    assert names == ["AzureMLTrainingAgent", "AutoRouter"]
    resp = client.post(
        "/v1/agents/AzureFinOpsAgent:invoke", json={"prompt": "x"}, headers=_auth("alice-token")
    )
    assert resp.status_code == 403
    assert client.post(
        "/v1/agents/AzureMLTrainingAgent:invoke", json={"prompt": "x"}, headers=_auth("alice-token")
    ).status_code == 200
    bob_names = [a["name"] for a in client.get("/v1/agents", headers=_auth("bob-token")).json()["agents"]]
    assert bob_names == ["AzureFinOpsAgent", "AzureMLTrainingAgent", "AutoRouter"]


def test_unlisted_tenant_is_denied_everything(router_with_autorouter) -> None:
    client = _client(router_with_autorouter, '{"t1": {"agents": ["AzureMLTrainingAgent"]}}')
    listing = client.get("/v1/agents", headers=_auth("nobody-token")).json()["agents"]
    assert listing == []
    assert client.post("/v1/chat", json={"input": "azure cost"}, headers=_auth("nobody-token")).status_code == 403
    assert client.post(
        "/v1/agents/AzureFinOpsAgent:stream", json={"prompt": "x"}, headers=_auth("nobody-token")
    ).status_code == 403


def test_conversation_ownership_over_rest(router_for) -> None:
    client = _client(router_for)
    first = client.post(
        "/v1/agents/AzureFinOpsAgent:invoke", json={"prompt": "hi"}, headers=_auth("alice-token")
    ).json()
    conv = first["conversation_id"]
    resp = client.post(
        "/v1/agents/AzureFinOpsAgent:continue",
        json={"prompt": "more", "conversation_id": conv},
        headers=_auth("bob-token"),
    )
    assert resp.status_code == 403
    assert client.get(f"/v1/conversations/{conv}", headers=_auth("bob-token")).status_code == 403
    record = client.get(f"/v1/conversations/{conv}", headers=_auth("alice-token")).json()
    assert record["owner_subject"] == "alice"
    assert record["owner_tenant"] == "t1"
