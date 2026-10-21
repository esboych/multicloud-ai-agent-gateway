"""Conversation store behaviour as seen through the REST facade."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentSpec,
    StreamDone,
    TextChunk,
)
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.core.routing import KeywordClassifier
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


def _client(router) -> TestClient:
    return TestClient(build_rest_app(router=router, gateway_version="t"))


def test_autorouter_continue_is_sticky(router_with_autorouter, stub_backend) -> None:
    client = _client(router_with_autorouter)
    first = client.post("/v1/chat", json={"input": "azure cost report"}).json()
    assert first["routing"]["selected_agent"] == "AzureFinOpsAgent"
    conv = first["conversation_id"]

    # This prompt would classify to the ML agent, but the conversation is FinOps'.
    resp = client.post(
        "/v1/agents/AutoRouter:continue",
        json={"prompt": "start a training job", "conversation_id": conv},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["routing"]["selected_agent"] == "AzureFinOpsAgent"
    assert body["routing"]["method"] == "sticky"
    assert stub_backend.calls[-1] == ("AzureFinOpsAgent", "start a training job", conv)


def test_chat_with_known_conversation_is_sticky_too(router_with_autorouter, stub_backend) -> None:
    client = _client(router_with_autorouter)
    conv = client.post("/v1/chat", json={"input": "azure cost report"}).json()["conversation_id"]
    body = client.post("/v1/chat", json={"input": "training job", "conversation_id": conv}).json()
    assert body["routing"]["selected_agent"] == "AzureFinOpsAgent"
    assert body["routing"]["method"] == "sticky"


def test_autorouter_continue_unknown_conversation_is_400(router_with_autorouter) -> None:
    resp = _client(router_with_autorouter).post(
        "/v1/agents/AutoRouter:continue",
        json={"prompt": "x", "conversation_id": "never-seen"},
    )
    assert resp.status_code == 400
    assert "conversation store" in resp.json()["detail"]


def test_wrong_agent_for_conversation_is_409(router_for) -> None:
    client = _client(router_for)
    conv = client.post("/v1/agents/AzureFinOpsAgent:invoke", json={"prompt": "hi"}).json()[
        "conversation_id"
    ]
    resp = client.post(
        "/v1/agents/AzureMLTrainingAgent:continue",
        json={"prompt": "more", "conversation_id": conv},
    )
    assert resp.status_code == 409
    assert "belongs to agent 'AzureFinOpsAgent'" in resp.json()["detail"]


def test_conversation_lookup(router_for) -> None:
    client = _client(router_for)
    conv = client.post("/v1/agents/AzureFinOpsAgent:invoke", json={"prompt": "hi"}).json()[
        "conversation_id"
    ]
    record = client.get(f"/v1/conversations/{conv}").json()
    assert record["agent"] == "AzureFinOpsAgent"
    assert record["owner_subject"] is None
    assert client.get("/v1/conversations/missing").status_code == 404


class _StreamingStub:
    backend_id = "foundry"

    async def invoke(self, *args: Any, **kwargs: Any) -> AgentReply:
        raise NotImplementedError

    async def stream(
        self, remote_id: str, prompt: str, *, conversation_id: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        yield TextChunk(text="hi")
        yield StreamDone(conversation_id=f"stream-{remote_id}")

    async def aclose(self) -> None:
        return None


def test_stream_done_remembers_conversation() -> None:
    registry = AgentRegistry(
        [
            AgentSpec(name="AzureFinOpsAgent", backend_id="foundry", remote_id="AzureFinOpsAgent"),
            AgentSpec(name="AzureMLTrainingAgent", backend_id="foundry", remote_id="AzureMLTrainingAgent"),
        ]
    )
    router = Router(registry, {"foundry": _StreamingStub()}, classifier=KeywordClassifier())
    client = _client(router)
    resp = client.post("/v1/agents/AutoRouter:stream", json={"prompt": "azure cost"})
    assert resp.status_code == 200
    assert "event: done" in resp.text
    record = client.get("/v1/conversations/stream-AzureFinOpsAgent").json()
    assert record["agent"] == "AzureFinOpsAgent"
    # And a follow-up stream through AutoRouter stays with that agent.
    resp = client.post(
        "/v1/agents/AutoRouter:stream",
        json={"prompt": "training job", "conversation_id": "stream-AzureFinOpsAgent"},
    )
    assert '"method": "sticky"' in resp.text
    assert '"selected_agent": "AzureFinOpsAgent"' in resp.text
