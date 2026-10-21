"""MCP facade over the stub router: JSON-RPC shapes and tool semantics."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.auth import Principal
from multicloud_ai_agent_gateway.core.backend import AgentReturnedError, AuthenticationRequired
from multicloud_ai_agent_gateway.frontends.mcp import LATEST_PROTOCOL_VERSION, mount_mcp
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


def _client(router, authenticator: Any = None) -> TestClient:
    app = build_rest_app(router=router, gateway_version="t", authenticator=authenticator)
    mount_mcp(
        app, service=app.state.gateway_service, gateway_version="t", authenticator=authenticator
    )
    return TestClient(app)


def _rpc(client: TestClient, method: str, params: Any = None, *, id_: Any = 1, headers=None):
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers or {})


def test_initialize_negotiates_version(router_with_autorouter) -> None:
    client = _client(router_with_autorouter)
    resp = _rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "multicloud-ai-agent-gateway"

    resp = _rpc(client, "initialize", {"protocolVersion": "1999-01-01"})
    assert resp.json()["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_notification_is_accepted_without_body(router_for) -> None:
    client = _client(router_for)
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202
    assert resp.content == b""


def test_ping(router_for) -> None:
    assert _rpc(_client(router_for), "ping").json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_batch_and_parse_errors(router_for) -> None:
    client = _client(router_for)
    resp = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600
    resp = client.post("/mcp", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700
    resp = client.post("/mcp", json={"id": 1, "method": "ping"})
    assert resp.status_code == 400


def test_tools_list_exposes_agents_router_and_route_tool(router_with_autorouter) -> None:
    tools = _rpc(_client(router_with_autorouter), "tools/list").json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["AzureFinOpsAgent", "AzureMLTrainingAgent", "AutoRouter", "route_prompt"]
    finops = tools[0]
    assert finops["inputSchema"]["required"] == ["prompt"]
    assert "conversation_id" in finops["inputSchema"]["properties"]
    assert finops["description"].startswith("Read-only Azure cost agent.")
    assert tools[-1]["annotations"]["readOnlyHint"] is True


def test_tools_list_without_classifier_has_no_router_tools(router_for) -> None:
    names = [t["name"] for t in _rpc(_client(router_for), "tools/list").json()["result"]["tools"]]
    assert names == ["AzureFinOpsAgent", "AzureMLTrainingAgent"]


def test_tools_call_invokes_agent(router_for, stub_backend) -> None:
    resp = _rpc(
        _client(router_for),
        "tools/call",
        {"name": "AzureFinOpsAgent", "arguments": {"prompt": "hello"}},
        id_="abc",
    )
    body = resp.json()
    assert body["id"] == "abc"
    result = body["result"]
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": "echo:hello"}]
    assert result["structuredContent"]["conversation_id"] == "resp_stub_1"
    assert result["structuredContent"]["agent"] == "AzureFinOpsAgent"
    assert result["structuredContent"]["routing"] is None
    assert stub_backend.calls == [("AzureFinOpsAgent", "hello", None)]


def test_tools_call_continues_conversation(router_for, stub_backend) -> None:
    client = _client(router_for)
    _rpc(client, "tools/call", {"name": "AzureFinOpsAgent", "arguments": {"prompt": "a"}})
    _rpc(
        client,
        "tools/call",
        {"name": "AzureFinOpsAgent", "arguments": {"prompt": "b", "conversation_id": "resp_stub_1"}},
    )
    assert stub_backend.calls[-1] == ("AzureFinOpsAgent", "b", "resp_stub_1")


def test_tools_call_autorouter_and_route_tool(router_with_autorouter) -> None:
    client = _client(router_with_autorouter)
    result = _rpc(
        client, "tools/call", {"name": "AutoRouter", "arguments": {"prompt": "azure cost"}}
    ).json()["result"]
    assert result["structuredContent"]["routing"]["selected_agent"] == "AzureFinOpsAgent"
    assert result["structuredContent"]["agent"] == "AzureFinOpsAgent"

    result = _rpc(
        client, "tools/call", {"name": "route_prompt", "arguments": {"prompt": "azure cost"}}
    ).json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["selected_agent"] == "AzureFinOpsAgent"
    assert result["content"][0]["text"].startswith("AzureFinOpsAgent (keyword")


def test_tools_call_errors(router_for, stub_backend) -> None:
    client = _client(router_for)
    assert _rpc(client, "tools/call", {"name": "Nope", "arguments": {"prompt": "x"}}).json()["error"]["code"] == -32602
    assert _rpc(client, "tools/call", {"name": "AzureFinOpsAgent", "arguments": {}}).json()["error"]["code"] == -32602
    assert _rpc(client, "tools/call", {"arguments": {"prompt": "x"}}).json()["error"]["code"] == -32602

    stub_backend.raises = AgentReturnedError("ThrottlingException", "quota")
    result = _rpc(client, "tools/call", {"name": "AzureFinOpsAgent", "arguments": {"prompt": "x"}}).json()["result"]
    assert result["isError"] is True
    assert "ThrottlingException" in result["content"][0]["text"]
    assert result["structuredContent"]["error"]["type"] == "AgentReturnedError"


def test_unknown_method(router_for) -> None:
    body = _rpc(_client(router_for), "resources/list").json()
    assert body["error"]["code"] == -32601


def test_get_and_delete_are_405(router_for) -> None:
    client = _client(router_for)
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


class _Auth:
    def authenticate(self, token: str) -> Principal:
        if token == "ok":
            return Principal(subject="s", tenant="t")
        raise AuthenticationRequired("nope")


def test_mcp_requires_bearer_when_auth_enabled(router_for) -> None:
    client = _client(router_for, authenticator=_Auth())
    assert _rpc(client, "ping").status_code == 401
    assert _rpc(client, "ping", headers={"Authorization": "Bearer ok"}).status_code == 200
