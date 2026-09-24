from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from multicloud_ai_agent_gateway.core.backend import (
    AgentBackend,
    AgentEvent,
    AgentReply,
    AgentSpec,
    AutoRouterNotConfigured,
    UnknownAgent,
)
from multicloud_ai_agent_gateway.core.routing import (
    AUTO_ROUTER_NAME,
    RouteDecision,
    is_auto_router_name,
)


@runtime_checkable
class PromptClassifier(Protocol):
    """Anything that maps a prompt string to a RouteDecision.

    Lives in registry.py (not routing.py) on purpose: routing.py is the
    legacy keyword classifier; bm25.py / llm_router.py / hybrid_router.py
    plug into the Router via this Protocol without forming an import cycle.
    """

    def classify(self, prompt: str) -> RouteDecision: ...


class AgentRegistry:
    """In-memory map of canonical agent name -> AgentSpec."""

    def __init__(self, specs: Iterable[AgentSpec]) -> None:
        self._by_name: dict[str, AgentSpec] = {}
        for spec in specs:
            if spec.name == AUTO_ROUTER_NAME:
                raise ValueError(
                    f"Agent name {AUTO_ROUTER_NAME!r} is reserved for the gateway's "
                    "AutoRouter and cannot be registered as a real agent."
                )
            if spec.name in self._by_name:
                raise ValueError(f"Duplicate agent name in registry: {spec.name!r}")
            self._by_name[spec.name] = spec

    def get(self, name: str) -> AgentSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise UnknownAgent(name) from exc

    def list(self) -> list[AgentSpec]:
        return sorted(self._by_name.values(), key=lambda spec: spec.name)


class Router:
    """Routes invoke calls to the correct backend based on the agent registry.

    Optionally owns a ``PromptClassifier`` (keyword / BM25 / hybrid / LLM):
    when provided, the REST layer can use ``router.route(prompt)`` to resolve
    the AutoRouter sentinel into a concrete agent name before dispatching.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        backends: dict[str, AgentBackend],
        *,
        classifier: PromptClassifier | None = None,
    ) -> None:
        self._registry = registry
        self._backends = backends
        self._classifier = classifier
        for spec in registry.list():
            if spec.backend_id not in backends:
                raise ValueError(
                    f"Agent {spec.name!r} references backend {spec.backend_id!r} "
                    "which is not registered."
                )

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def classifier(self) -> PromptClassifier | None:
        return self._classifier

    @property
    def auto_router_enabled(self) -> bool:
        return self._classifier is not None

    def route(self, prompt: str) -> RouteDecision:
        """Classify ``prompt`` against the configured classifier.

        Raises ``AutoRouterNotConfigured`` if no classifier is wired in.
        Pure with respect to the Router (does not invoke any backend).
        """
        if self._classifier is None:
            raise AutoRouterNotConfigured()
        return self._classifier.classify(prompt)

    @staticmethod
    def is_auto_router(name: str | None) -> bool:
        return is_auto_router_name(name)

    async def invoke(
        self,
        name: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        spec = self._registry.get(name)
        backend = self._backends[spec.backend_id]
        return await backend.invoke(
            spec.remote_id or spec.name,
            prompt,
            conversation_id=conversation_id,
        )

    def stream(
        self,
        name: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        spec = self._registry.get(name)
        backend = self._backends[spec.backend_id]
        return backend.stream(
            spec.remote_id or spec.name,
            prompt,
            conversation_id=conversation_id,
        )

    async def aclose(self) -> None:
        for backend in self._backends.values():
            await backend.aclose()
