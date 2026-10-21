"""A2A facade: agent card, message/send, message/stream, tasks."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.auth import Principal
from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentReturnedError,
    AgentSpec,
    AuthenticationRequired,
    StreamDone,
    TextChunk,
    ToolFinished,
    ToolStarted,
)
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.core.routing import KeywordClassifier
from multicloud_ai_agent_gateway.frontends.a2a import mount_a2a
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


def _client(router, authenticator: Any = None) -> TestClient:
    app = build_rest_app(router=router, gateway_version="t", authenticator=authenticator)
    mount_a2a(
        app,
        service=app.state.gateway_service,
        gateway_version="t",
        public_url="https://gw.example",
        authenticator=authenticator,
    )
    return TestClient(app)


def _rpc(client: TestClient, method: str, params: Any = None, *, id_: Any = 1, headers=None):
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/a2a", json=body, headers=headers or {})


def _send_params(text: str, *, agent: str | None = None, context_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": "m1",
        "kind": "message",
    }
    if context_id:
        message["contextId"] = context_id
    params: dict[str, Any] = {"message": message}
    if agent:
        params["metadata"] = {"agent": agent}
    return params


def _sse_results(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[len("data:"):].strip()))
    return out


def test_agent_card(router_with_autorouter) -> None:
    client = _client(router_with_autorouter)
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"] == "https://gw.example/a2a"
    assert card["protocolVersion"] == "0.3.0"
    assert card["capabilities"]["streaming"] is True
    assert [s["id"] for s in card["skills"]] == ["AutoRouter", "AzureFinOpsAgent", "AzureMLTrainingAgent"]
    assert "securitySchemes" not in card
    assert client.get("/.well-known/agent.json").json()["name"] == card["name"]


def test_message_send_explicit_agent(router_for, stub_backend) -> None:
    resp = _rpc(_client(router_for), "message/send", _send_params("hi", agent="AzureFinOpsAgent"), id_="r1")
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["contextId"] == "resp_stub_1"
    assert task["artifacts"][0]["parts"] == [{"kind": "text", "text": "echo:hi"}]
    assert [m["role"] for m in task["history"]] == ["user", "agent"]
    assert task["history"][0]["taskId"] == task["id"]
    assert task["metadata"]["agent"] == "AzureFinOpsAgent"
    assert stub_backend.calls == [("AzureFinOpsAgent", "hi", None)]


def test_message_send_auto_routes_without_selector(router_with_autorouter) -> None:
    task = _rpc(_client(router_with_autorouter), "message/send", _send_params("azure cost")).json()["result"]
    assert task["metadata"]["routing"]["selected_agent"] == "AzureFinOpsAgent"
    assert task["metadata"]["agent"] == "AzureFinOpsAgent"


def test_message_send_context_id_continues_conversation(router_for, stub_backend) -> None:
    client = _client(router_for)
    _rpc(client, "message/send", _send_params("a", agent="AzureFinOpsAgent"))
    _rpc(client, "message/send", _send_params("b", agent="AzureFinOpsAgent", context_id="resp_stub_1"))
    assert stub_backend.calls[-1] == ("AzureFinOpsAgent", "b", "resp_stub_1")


def test_message_send_agent_error_yields_failed_task(router_for, stub_backend) -> None:
    stub_backend.raises = AgentReturnedError("ThrottlingException", "quota")
    task = _rpc(_client(router_for), "message/send", _send_params("x", agent="AzureFinOpsAgent")).json()["result"]
    assert task["status"]["state"] == "failed"
    assert "ThrottlingException" in task["status"]["message"]["parts"][0]["text"]


def test_tasks_get_and_cancel(router_for) -> None:
    client = _client(router_for)
    task = _rpc(client, "message/send", _send_params("hi", agent="AzureFinOpsAgent")).json()["result"]
    fetched = _rpc(client, "tasks/get", {"id": task["id"]}).json()["result"]
    assert fetched["id"] == task["id"]
    trimmed = _rpc(client, "tasks/get", {"id": task["id"], "historyLength": 1}).json()["result"]
    assert len(trimmed["history"]) == 1
    assert _rpc(client, "tasks/get", {"id": "missing"}).json()["error"]["code"] == -32001
    assert _rpc(client, "tasks/cancel", {"id": task["id"]}).json()["error"]["code"] == -32002


def test_invalid_params_and_unsupported_operations(router_for) -> None:
    client = _client(router_for)
    assert _rpc(client, "message/send", {}).json()["error"]["code"] == -32602
    assert _rpc(client, "message/send", {"message": {"parts": []}}).json()["error"]["code"] == -32602
    file_part = {"message": {"parts": [{"kind": "file", "file": {"uri": "x"}}]}}
    assert _rpc(client, "message/send", file_part).json()["error"]["code"] == -32005
    with_task = _send_params("x", agent="AzureFinOpsAgent")
    with_task["message"]["taskId"] = "t1"
    assert _rpc(client, "message/send", with_task).json()["error"]["code"] == -32004
    assert _rpc(client, "tasks/pushNotificationConfig/set", {}).json()["error"]["code"] == -32003
    assert _rpc(client, "tasks/resubscribe", {"id": "x"}).json()["error"]["code"] == -32004
    assert _rpc(client, "nope/method").json()["error"]["code"] == -32601
    assert _rpc(client, "message/send", _send_params("x", agent="Nope")).json()["error"]["code"] == -32602


class _StreamingStub:
    backend_id = "foundry"

    async def invoke(self, *args: Any, **kwargs: Any) -> AgentReply:
        raise NotImplementedError

    async def stream(self, remote_id: str, prompt: str, *, conversation_id: str | None = None) -> AsyncIterator[AgentEvent]:
        yield ToolStarted(name="lookup", arguments={"q": 1})
        yield ToolFinished(name="lookup", result={"rows": 2})
        yield TextChunk(text="Hel")
        yield TextChunk(text="lo")
        yield StreamDone(conversation_id=f"ctx-{remote_id}", usage={"total_tokens": 3})

    async def aclose(self) -> None:
        return None


def _streaming_client() -> TestClient:
    registry = AgentRegistry(
        [
            AgentSpec(name="AzureFinOpsAgent", backend_id="foundry", remote_id="AzureFinOpsAgent"),
            AgentSpec(name="AzureMLTrainingAgent", backend_id="foundry", remote_id="AzureMLTrainingAgent"),
        ]
    )
    router = Router(registry, {"foundry": _StreamingStub()}, classifier=KeywordClassifier())
    return _client(router)


def test_message_stream_emits_task_updates_and_final() -> None:
    client = _streaming_client()
    resp = _rpc(client, "message/stream", _send_params("azure cost"), id_="s1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    results = [r["result"] for r in _sse_results(resp.text)]
    kinds = [r["kind"] for r in results]
    assert kinds == [
        "task",            # submitted
        "status-update",   # routing
        "status-update",   # tool_started
        "status-update",   # tool_finished
        "artifact-update", # Hel
        "artifact-update", # lo
        "artifact-update", # lastChunk
        "status-update",   # completed, final
    ]
    assert results[0]["status"]["state"] == "submitted"
    assert results[1]["metadata"]["routing"]["selected_agent"] == "AzureFinOpsAgent"
    assert results[2]["metadata"]["tool_event"]["kind"] == "tool_started"
    assert results[4]["append"] is False and results[5]["append"] is True
    assert results[6]["lastChunk"] is True
    assert results[-1]["final"] is True and results[-1]["status"]["state"] == "completed"
    assert results[-1]["contextId"] == "ctx-AzureFinOpsAgent"
    assert results[-1]["status"]["message"]["parts"][0]["text"] == "Hello"
    # Every event carries the JSON-RPC id of the request.
    assert {r["id"] for r in _sse_results(resp.text)} == {"s1"}

    task_id = results[0]["id"]
    stored = _rpc(client, "tasks/get", {"id": task_id}).json()["result"]
    assert stored["status"]["state"] == "completed"
    assert stored["artifacts"][0]["parts"][0]["text"] == "Hello"
    assert stored["metadata"]["usage"] == {"total_tokens": 3}


def test_message_stream_invalid_params_is_json_error() -> None:
    resp = _rpc(_streaming_client(), "message/stream", {"message": {"parts": []}})
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == -32602


class _Auth:
    def authenticate(self, token: str) -> Principal:
        if token in {"alice", "bob"}:
            return Principal(subject=token, tenant="t")
        raise AuthenticationRequired("nope")


def test_a2a_auth_and_task_ownership(router_for) -> None:
    client = _client(router_for, authenticator=_Auth())
    assert _rpc(client, "tasks/get", {"id": "x"}).status_code == 401
    card = client.get("/.well-known/agent-card.json").json()  # discovery stays open
    assert card["security"] == [{"bearer": []}]
    task = _rpc(
        client, "message/send", _send_params("hi", agent="AzureFinOpsAgent"),
        headers={"Authorization": "Bearer alice"},
    ).json()["result"]
    assert _rpc(client, "tasks/get", {"id": task["id"]}, headers={"Authorization": "Bearer alice"}).json()["result"]["id"] == task["id"]
    assert _rpc(client, "tasks/get", {"id": task["id"]}, headers={"Authorization": "Bearer bob"}).json()["error"]["code"] == -32001
    extended = _rpc(client, "agent/getAuthenticatedExtendedCard", headers={"Authorization": "Bearer alice"}).json()["result"]
    assert [s["id"] for s in extended["skills"]] == ["AzureFinOpsAgent", "AzureMLTrainingAgent"]
