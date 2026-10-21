"""Black-box tests over real HTTP against a gateway process."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from tests.integration.conftest import GATEWAY_AUDIENCE, GatewayProcess, LocalIssuer

pytestmark = pytest.mark.integration

EXTERNAL = bool(os.environ.get("GATEWAY_INTEGRATION_URL", "").strip())
needs_local = pytest.mark.skipif(EXTERNAL, reason="needs the fixture-managed gateway config")


def _sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        name = next((line[6:].strip() for line in block.splitlines() if line.startswith("event:")), "")
        data = next((line[5:].strip() for line in block.splitlines() if line.startswith("data:")), "")
        if data:
            events.append((name, json.loads(data)))
    return events


def _rpc(client: httpx.Client, path: str, method: str, params: Any = None, *, headers=None) -> httpx.Response:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": method, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers or {})


# -- REST ---------------------------------------------------------------------


def test_health_reports_version_and_auth_off(client: httpx.Client) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "direct" in body["backends"]
    if not EXTERNAL:
        assert body["auth_enabled"] is False


def test_agents_listing(client: httpx.Client) -> None:
    names = {a["name"] for a in client.get("/v1/agents").json()["agents"]}
    assert "DemoAgent" in names and "AutoRouter" in names


def test_invoke_then_conversation_lookup(client: httpx.Client) -> None:
    body = client.post("/v1/agents/DemoAgent:invoke", json={"prompt": "hello over http"}).json()
    assert body["output_text"].endswith("hello over http")
    assert body["tool_events"][0]["replayed"] is False
    record = client.get(f"/v1/conversations/{body['conversation_id']}").json()
    assert record["agent"] == "DemoAgent"


@needs_local
def test_chat_is_sticky_across_requests(client: httpx.Client) -> None:
    first = client.post("/v1/chat", json={"input": "hello"}).json()
    assert first["routing"]["selected_agent"] == "DemoAgent"
    conv = first["conversation_id"]
    # "mirror this" would route to SecondAgent on its own.
    second = client.post("/v1/chat", json={"input": "mirror this", "conversation_id": conv}).json()
    assert second["routing"]["selected_agent"] == "DemoAgent"
    assert second["routing"]["method"] == "sticky"
    cont = client.post(
        "/v1/agents/AutoRouter:continue", json={"prompt": "again", "conversation_id": conv}
    )
    assert cont.status_code == 200
    wrong = client.post(
        "/v1/agents/SecondAgent:continue", json={"prompt": "x", "conversation_id": conv}
    )
    assert wrong.status_code == 409


def test_autorouter_continue_unknown_conversation_is_400(client: httpx.Client) -> None:
    resp = client.post(
        "/v1/agents/AutoRouter:continue", json={"prompt": "x", "conversation_id": "never-seen"}
    )
    assert resp.status_code == 400


def test_stream_over_real_sse(client: httpx.Client) -> None:
    with client.stream("POST", "/v1/agents/AutoRouter:stream", json={"prompt": "hello stream"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse(resp.read().decode())
    kinds = [k for k, _ in events]
    assert kinds[0] == "routing"
    assert kinds[-1] == "done"
    assert "chunk" in kinds
    done = events[-1][1]
    assert done["conversation_id"]
    assert client.get(f"/v1/conversations/{done['conversation_id']}").status_code == 200


def test_unknown_agent_is_404(client: httpx.Client) -> None:
    assert client.post("/v1/agents/Nope:invoke", json={"prompt": "x"}).status_code == 404


# -- MCP ----------------------------------------------------------------------


def test_mcp_handshake_list_and_call(client: httpx.Client) -> None:
    init = _rpc(
        client, "/mcp", "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "it", "version": "0"}},
    ).json()["result"]
    assert init["serverInfo"]["name"] == "multicloud-ai-agent-gateway"
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    tools = _rpc(client, "/mcp", "tools/list").json()["result"]["tools"]
    assert {"DemoAgent", "AutoRouter", "route_prompt"} <= {t["name"] for t in tools}
    result = _rpc(
        client, "/mcp", "tools/call", {"name": "DemoAgent", "arguments": {"prompt": "mcp over http"}}
    ).json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"].endswith("mcp over http")
    conv = result["structuredContent"]["conversation_id"]
    again = _rpc(
        client, "/mcp", "tools/call",
        {"name": "AutoRouter", "arguments": {"prompt": "mirror this", "conversation_id": conv}},
    ).json()["result"]
    assert again["structuredContent"]["routing"]["method"] == "sticky"
    assert client.get("/mcp").status_code == 405


# -- A2A ----------------------------------------------------------------------


def test_a2a_card_send_and_stream(client: httpx.Client, gateway: GatewayProcess) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"].endswith("/a2a")
    assert "DemoAgent" in {s["id"] for s in card["skills"]}

    message = {"role": "user", "parts": [{"kind": "text", "text": "a2a over http"}], "messageId": "m1"}
    task = _rpc(client, "/a2a", "message/send", {"message": message, "metadata": {"agent": "DemoAgent"}}).json()["result"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"].endswith("a2a over http")
    fetched = _rpc(client, "/a2a", "tasks/get", {"id": task["id"]}).json()["result"]
    assert fetched["id"] == task["id"]

    with client.stream(
        "POST", "/a2a",
        json={"jsonrpc": "2.0", "id": "s", "method": "message/stream", "params": {"message": message}},
    ) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = resp.read().decode()
    results = [json.loads(line[5:].strip())["result"] for line in raw.splitlines() if line.startswith("data:")]
    assert results[0]["kind"] == "task" and results[0]["status"]["state"] == "submitted"
    assert results[-1]["kind"] == "status-update" and results[-1]["final"] is True
    assert results[-1]["status"]["state"] == "completed"


# -- Authentication over the real JWKS path -----------------------------------


@needs_local
def test_auth_rejects_missing_and_bad_tokens(auth_gateway: GatewayProcess, local_issuer: LocalIssuer) -> None:
    with httpx.Client(base_url=auth_gateway.base_url, timeout=30.0) as client:
        assert client.get("/healthz").status_code == 200  # stays open
        resp = client.get("/v1/agents")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")
        wrong_aud = local_issuer.token(sub="svc", tid="tenant-a", aud="api://other")
        assert client.get("/v1/agents", headers={"Authorization": f"Bearer {wrong_aud}"}).status_code == 401
        expired = local_issuer.token(sub="svc", tid="tenant-a", exp=1)
        assert client.get("/v1/agents", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
        assert _rpc(client, "/mcp", "ping").status_code == 401
        assert _rpc(client, "/a2a", "tasks/get", {"id": "x"}).status_code == 401
        assert client.get("/.well-known/agent-card.json").json()["security"] == [{"bearer": []}]


@needs_local
def test_auth_accepts_jwks_signed_token_and_applies_policy(
    auth_gateway: GatewayProcess, local_issuer: LocalIssuer
) -> None:
    allowed = {"Authorization": f"Bearer {local_issuer.token(sub='svc-a', tid='tenant-a', scp='agents.invoke')}"}
    denied = {"Authorization": f"Bearer {local_issuer.token(sub='svc-b', tid='tenant-b')}"}
    with httpx.Client(base_url=auth_gateway.base_url, timeout=30.0) as client:
        me = client.get("/v1/me", headers=allowed).json()
        assert me["subject"] == "svc-a" and me["tenant"] == "tenant-a"
        assert me["scopes"] == ["agents.invoke"]
        assert me["allowed_agents"] == ["DemoAgent"]
        assert me["issuer"] == local_issuer.issuer

        names = {a["name"] for a in client.get("/v1/agents", headers=allowed).json()["agents"]}
        assert names == {"DemoAgent", "AutoRouter"}
        assert client.post("/v1/agents/DemoAgent:invoke", json={"prompt": "hi"}, headers=allowed).status_code == 200
        assert client.post("/v1/agents/SecondAgent:invoke", json={"prompt": "hi"}, headers=allowed).status_code == 403
        # AutoRouter would pick SecondAgent for this prompt; policy redirects it.
        routed = client.post("/v1/chat", json={"input": "mirror this"}, headers=allowed).json()
        assert routed["routing"]["selected_agent"] == "DemoAgent"
        assert routed["routing"]["method"].endswith("+policy")

        assert client.get("/v1/agents", headers=denied).json()["agents"] == []
        assert client.post("/v1/chat", json={"input": "hello"}, headers=denied).status_code == 403

        tools = _rpc(client, "/mcp", "tools/list", headers=allowed).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"DemoAgent", "AutoRouter", "route_prompt"}
        extended = _rpc(client, "/a2a", "agent/getAuthenticatedExtendedCard", headers=allowed).json()["result"]
        assert {s["id"] for s in extended["skills"]} == {"AutoRouter", "DemoAgent"}


@needs_local
def test_auth_conversation_ownership(auth_gateway: GatewayProcess, local_issuer: LocalIssuer) -> None:
    alice = {"Authorization": f"Bearer {local_issuer.token(sub='alice', tid='tenant-a')}"}
    carol = {"Authorization": f"Bearer {local_issuer.token(sub='carol', tid='tenant-a')}"}
    with httpx.Client(base_url=auth_gateway.base_url, timeout=30.0) as client:
        conv = client.post("/v1/agents/DemoAgent:invoke", json={"prompt": "mine"}, headers=alice).json()["conversation_id"]
        assert client.post(
            "/v1/agents/DemoAgent:continue", json={"prompt": "more", "conversation_id": conv}, headers=alice
        ).status_code == 200
        assert client.post(
            "/v1/agents/DemoAgent:continue", json={"prompt": "steal", "conversation_id": conv}, headers=carol
        ).status_code == 403
        assert client.get(f"/v1/conversations/{conv}", headers=carol).status_code == 403
        record = client.get(f"/v1/conversations/{conv}", headers=alice).json()
        assert record["owner_subject"] == "alice" and record["owner_tenant"] == "tenant-a"


def test_token_audience_constant_matches_fixture() -> None:
    assert GATEWAY_AUDIENCE.startswith("api://")
