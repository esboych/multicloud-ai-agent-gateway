from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from multicloud_ai_agent_gateway.core.backend import AgentBackend, AgentReply, AgentSpec, ToolEvent
from multicloud_ai_agent_gateway.core.registry import AgentRegistry, Router
from multicloud_ai_agent_gateway.core.routing import KeywordClassifier


@dataclass
class StubBackend:
    backend_id: str = "foundry"
    reply: AgentReply | None = None
    raises: Exception | None = None
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def invoke(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        self.calls.append((remote_id, prompt, conversation_id))
        if self.raises is not None:
            raise self.raises
        if self.reply is not None:
            return self.reply
        return AgentReply(
            output_text=f"echo:{prompt}",
            conversation_id="resp_stub_1",
            tool_events=[],
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )

    async def aclose(self) -> None:
        return None


# Sanity check: the stub satisfies the Protocol at type-check time.
_: AgentBackend = StubBackend()


@pytest.fixture
def stub_backend() -> StubBackend:
    return StubBackend()


@pytest.fixture
def router_for(stub_backend: StubBackend) -> Router:
    specs = [
        AgentSpec(
            name="AzureFinOpsAgent",
            backend_id="foundry",
            description="Read-only Azure cost agent.",
            remote_id="AzureFinOpsAgent",
        ),
        AgentSpec(
            name="AzureMLTrainingAgent",
            backend_id="foundry",
            remote_id="AzureMLTrainingAgent",
        ),
    ]
    registry = AgentRegistry(specs)
    return Router(registry, {"foundry": stub_backend})


@pytest.fixture
def router_with_autorouter(stub_backend: StubBackend) -> Router:
    """Same registry as router_for but with the keyword classifier wired in."""
    specs = [
        AgentSpec(
            name="AzureFinOpsAgent",
            backend_id="foundry",
            description="Read-only Azure cost agent.",
            remote_id="AzureFinOpsAgent",
        ),
        AgentSpec(
            name="AzureMLTrainingAgent",
            backend_id="foundry",
            remote_id="AzureMLTrainingAgent",
        ),
    ]
    registry = AgentRegistry(specs)
    return Router(registry, {"foundry": stub_backend}, classifier=KeywordClassifier())


@pytest.fixture
def sample_tool_event() -> ToolEvent:
    return ToolEvent(name="t", arguments={"k": "v"}, result={"ok": True})


@pytest.fixture
def sample_response_with_tool_call() -> dict[str, Any]:
    return {
        "id": "resp_001",
        "status": "requires_action",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "doStuff",
                "arguments": '{"x": 1, "y": "z"}',
            }
        ],
        "output_text": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def sample_response_completed() -> dict[str, Any]:
    return {
        "id": "resp_002",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "all done"}],
            }
        ],
        "output_text": "all done",
        "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    }
