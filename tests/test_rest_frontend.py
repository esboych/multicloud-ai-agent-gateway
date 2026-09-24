from __future__ import annotations

from fastapi.testclient import TestClient

from multicloud_ai_agent_gateway.core.backend import (
    AgentReply,
    AgentReturnedError,
    NoDispatcherConfigured,
    ToolBudgetExceeded,
    ToolEvent,
)
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app


def _client(router) -> TestClient:
    return TestClient(build_rest_app(router=router, gateway_version="test-0.0.0"))


def test_healthz_reports_backends(router_for) -> None:
    client = _client(router_for)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["backends"] == ["foundry"]


def test_readyz_reports_ready(router_for) -> None:
    client = _client(router_for)
    assert client.get("/readyz").json()["status"] == "ready"


def test_list_agents(router_for) -> None:
    response = _client(router_for).get("/v1/agents")
    assert response.status_code == 200
    names = [a["name"] for a in response.json()["agents"]]
    assert names == ["AzureFinOpsAgent", "AzureMLTrainingAgent"]


def test_invoke_happy_path(router_for, stub_backend) -> None:
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "what is current cost?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["output_text"] == "echo:what is current cost?"
    assert body["conversation_id"] == "resp_stub_1"
    assert body["tool_events"] == []
    assert stub_backend.calls == [("AzureFinOpsAgent", "what is current cost?", None)]


def test_invoke_returns_tool_events(router_for, stub_backend) -> None:
    stub_backend.reply = AgentReply(
        output_text="finished",
        conversation_id="resp_xyz",
        tool_events=[
            ToolEvent(name="runX", arguments={"a": 1}, result={"rows": 3}),
        ],
        usage={"total_tokens": 7},
    )
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "go"},
    )
    body = response.json()
    assert body["tool_events"] == [
        {"name": "runX", "arguments": {"a": 1}, "result": {"rows": 3}, "error": None}
    ]
    assert body["usage"] == {"total_tokens": 7}


def test_continue_requires_conversation_id(router_for) -> None:
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:continue",
        json={"prompt": "more"},
    )
    assert response.status_code == 422  # missing conversation_id


def test_continue_passes_conversation_id(router_for, stub_backend) -> None:
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:continue",
        json={"prompt": "more", "conversation_id": "resp_prev"},
    )
    assert response.status_code == 200
    assert stub_backend.calls == [("AzureFinOpsAgent", "more", "resp_prev")]


def test_invoke_unknown_agent_returns_404(router_for) -> None:
    response = _client(router_for).post(
        "/v1/agents/NopeAgent:invoke",
        json={"prompt": "x"},
    )
    assert response.status_code == 404


def test_invoke_no_dispatcher_returns_422(router_for, stub_backend) -> None:
    stub_backend.raises = NoDispatcherConfigured("runX")
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "x"},
    )
    assert response.status_code == 422
    assert "runX" in response.json()["detail"]


def test_invoke_tool_budget_exceeded_returns_504(router_for, stub_backend) -> None:
    stub_backend.raises = ToolBudgetExceeded(5)
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "x"},
    )
    assert response.status_code == 504


def test_invoke_unexpected_exception_returns_502(router_for, stub_backend) -> None:
    stub_backend.raises = RuntimeError("kaboom")
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "x"},
    )
    assert response.status_code == 502
    assert response.json()["detail"].startswith("backend_error:")


def test_invoke_agent_returned_error_returns_502_with_type_and_message(
    router_for, stub_backend
) -> None:
    """A wrapped server-side error from the upstream agent surfaces as a 502
    whose detail carries both the original error_type and the message, so the
    chat-ui can render it as an error rather than as agent text."""
    stub_backend.raises = AgentReturnedError("ThrottlingException", "Too many tokens per day")
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "x"},
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail.startswith("agent_returned_error:")
    assert "ThrottlingException" in detail
    assert "Too many tokens per day" in detail


def test_invoke_validates_prompt_not_empty(router_for) -> None:
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": ""},
    )
    assert response.status_code == 422


def test_invoke_rejects_oversized_prompt(router_for) -> None:
    response = _client(router_for).post(
        "/v1/agents/AzureFinOpsAgent:invoke",
        json={"prompt": "x" * (32 * 1024 + 1)},
    )
    assert response.status_code == 422
