"""End-to-end check that the gateway boots and serves with zero cloud config.

This is the path the README quickstart takes: one ``direct`` agent backed by
the ``demo_echo`` handler, default router settings, no Azure / AWS / GCP env.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.app import build_app
from multicloud_ai_agent_gateway.settings import Settings

DEMO_AGENTS = [
    {
        "name": "DemoAgent",
        "backend_id": "direct",
        "direct_handler": "demo_echo",
        "description": "Echo agent for local runs.",
        "tags": ["demo", "echo", "hello"],
        "example_prompts": ["hello", "say hi", "echo this back"],
    }
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for var in list(__import__("os").environ):
        if var.startswith("GATEWAY_") or var.startswith("AZURE_"):
            monkeypatch.delenv(var)
    settings = Settings(agents_json=json.dumps(DEMO_AGENTS))
    with TestClient(build_app(settings)) as client:
        yield client


def test_lists_demo_agent_and_autorouter(client: TestClient) -> None:
    names = {a["name"] for a in client.get("/v1/agents").json()["agents"]}
    assert {"DemoAgent", "AutoRouter"} <= names


def test_invoke_by_name_without_any_cloud_config(client: TestClient) -> None:
    resp = client.post("/v1/agents/DemoAgent:invoke", json={"prompt": "hello from curl"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["output_text"].endswith("hello from curl")
    assert body["tool_events"][0]["name"] == "demo_echo"
    assert body["tool_events"][0]["result"] == {"word_count": 3}
    assert body["routing"] is None


def test_chat_endpoint_routes_to_demo_agent(client: TestClient) -> None:
    resp = client.post("/v1/chat", json={"input": "hello"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["routing"]["selected_agent"] == "DemoAgent"


def test_stream_emits_chunk_then_done(client: TestClient) -> None:
    with client.stream(
        "POST", "/v1/agents/DemoAgent:stream", json={"prompt": "stream me"}
    ) as resp:
        assert resp.status_code == 200
        events = [line for line in resp.iter_lines() if line.startswith("event:")]
    assert events[0] == "event: chunk"
    assert events[-1] == "event: done"


def test_demo_agent_does_not_require_aws_bridge() -> None:
    # Regression guard: before demo_echo existed, any 'direct' agent forced
    # the AWS bridge to be configured.
    settings = Settings(agents_json=json.dumps(DEMO_AGENTS))
    build_app(settings)  # must not raise


def test_me_is_anonymous_without_auth(client: TestClient) -> None:
    me = client.get("/v1/me").json()
    assert me["anonymous"] is True
    assert me["allowed_agents"] == ["DemoAgent"]


def test_mcp_tools_call_over_real_app(client: TestClient) -> None:
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert [t["name"] for t in listed["result"]["tools"]] == ["DemoAgent", "AutoRouter", "route_prompt"]
    called = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "DemoAgent", "arguments": {"prompt": "hello mcp"}},
        },
    ).json()
    assert called["result"]["isError"] is False
    assert called["result"]["content"][0]["text"].endswith("hello mcp")


def test_a2a_message_send_over_real_app(client: TestClient) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert [s["id"] for s in card["skills"]] == ["AutoRouter", "DemoAgent"]
    task = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello a2a"}],
                    "messageId": "m1",
                }
            },
        },
    ).json()["result"]
    assert task["status"]["state"] == "completed"
    assert task["metadata"]["routing"]["selected_agent"] == "DemoAgent"
    assert task["artifacts"][0]["parts"][0]["text"].endswith("hello a2a")


def test_sqlite_conversation_store_boots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for var in list(__import__("os").environ):
        if var.startswith("GATEWAY_") or var.startswith("AZURE_"):
            monkeypatch.delenv(var)
    settings = Settings(
        agents_json=json.dumps(DEMO_AGENTS),
        conversation_store="sqlite",
        conversation_store_path=str(tmp_path / "conversations.db"),
    )
    with TestClient(build_app(settings)) as client:
        conv = client.post("/v1/chat", json={"input": "hello"}).json()["conversation_id"]
        assert client.get(f"/v1/conversations/{conv}").json()["agent"] == "DemoAgent"
