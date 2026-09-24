from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Union, runtime_checkable


@dataclass
class ToolEvent:
    """One function-call iteration during an agent's tool-loop."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class AgentReply:
    """Final answer returned by an agent backend after the tool-loop completes."""

    output_text: str
    conversation_id: str | None
    tool_events: list[ToolEvent] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    finish_reason: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_text": self.output_text,
            "conversation_id": self.conversation_id,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "tool_events": [event.to_dict() for event in self.tool_events],
        }


@dataclass(frozen=True)
class AgentSpec:
    """Registry entry for a single agent surfaced by the gateway.

    ``tags`` and ``example_prompts`` are picked up by the BM25 / LLM router
    classifiers (Phase 1) to score relevance against the user prompt. Both
    default to empty for backward compatibility with the keyword classifier.
    """

    name: str
    backend_id: str
    description: str = ""
    remote_id: str = ""
    tags: tuple[str, ...] = ()
    example_prompts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend_id": self.backend_id,
            "description": self.description,
            "remote_id": self.remote_id,
            "tags": list(self.tags),
            "example_prompts": list(self.example_prompts),
        }


@dataclass
class TextChunk:
    """A delta of free-form text from the agent's final message."""

    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass
class ToolStarted:
    """Emitted when the gateway is about to dispatch a tool the agent requested."""

    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments}


@dataclass
class ToolFinished:
    """Emitted after a tool dispatch returns (or fails)."""

    name: str
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "result": self.result, "error": self.error}


@dataclass
class StreamDone:
    """Terminal event: the agent finished. Carries final metadata."""

    conversation_id: str | None
    usage: dict[str, Any] | None = None
    finish_reason: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
        }


@dataclass
class StreamError:
    """Terminal event: the stream aborted with an error."""

    message: str
    error_type: str

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "error_type": self.error_type}


# Discriminated union of all events a backend may emit during a stream.
AgentEvent = Union[TextChunk, ToolStarted, ToolFinished, StreamDone, StreamError]


def event_kind(event: AgentEvent) -> str:
    """SSE-friendly type name for an event ('chunk', 'tool_started', etc)."""
    return _EVENT_KINDS[type(event)]


_EVENT_KINDS: dict[type, str] = {
    TextChunk: "chunk",
    ToolStarted: "tool_started",
    ToolFinished: "tool_finished",
    StreamDone: "done",
    StreamError: "error",
}


@runtime_checkable
class AgentBackend(Protocol):
    """Backends are async; sync SDK calls live behind asyncio.to_thread."""

    backend_id: str

    async def invoke(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply: ...

    def stream(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]: ...

    async def aclose(self) -> None: ...


class GatewayError(Exception):
    """Base class for errors the gateway maps to HTTP responses."""


class UnknownAgent(GatewayError):
    """Requested agent name is not in the registry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown agent: {name!r}")
        self.name = name


class NoDispatcherConfigured(GatewayError):
    """Foundry agent requested a tool but no dispatcher is wired in."""

    def __init__(self, function_name: str) -> None:
        super().__init__(
            f"Foundry agent requested function {function_name!r} but no dispatcher is configured. "
            "Set GATEWAY_FOUNDRY_DISPATCHER_PATH."
        )
        self.function_name = function_name


class ToolBudgetExceeded(GatewayError):
    """Tool-loop did not converge within max_tool_rounds."""

    def __init__(self, rounds: int) -> None:
        super().__init__(f"Foundry agent still requested tools after {rounds} rounds.")
        self.rounds = rounds


class AutoRouterNotConfigured(GatewayError):
    """AutoRouter was requested but no classifier is wired into the Router.

    Returned as 422 by the REST layer; same shape as NoDispatcherConfigured.
    """

    def __init__(self) -> None:
        super().__init__(
            "AutoRouter was requested but no classifier is configured on the Router."
        )


class AgentReturnedError(GatewayError):
    """The remote agent returned an error payload inside an otherwise-200 reply.

    AgentCore (and Foundry, in some edge cases) wraps server-side exceptions as
    JSON instead of failing the HTTP call. We detect those payloads at the
    backend layer and raise this so the REST/SSE frontends can surface them as
    errors instead of returning them as agent text. Mapped to 502 by the REST
    layer; carried as ``error_type``/``message`` in the SSE error envelope.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
