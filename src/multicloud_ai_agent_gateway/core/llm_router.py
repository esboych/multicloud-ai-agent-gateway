"""LLM-backed classifier used as the tie-breaker in HybridClassifier.

Sends a short prompt + the agent catalog to a chat-completions model and
parses a JSON ``{"agent": ..., "reason": ...}`` reply. We expose a narrow
``LLMClient`` protocol instead of hard-coding the openai SDK so:
  - tests can pass a stub,
  - swapping providers (Azure OpenAI / Bedrock / Anthropic) is a one-class
    change rather than touching the router itself.

The Azure OpenAI factory at the bottom is the only piece that touches the
``openai`` SDK and is wired in lazily by app.py only when the configuration
explicitly asks for it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any, Protocol

from multicloud_ai_agent_gateway.core.backend import AgentSpec, GatewayError
from multicloud_ai_agent_gateway.core.routing import RouteDecision

log = logging.getLogger(__name__)


class LLMRouterError(GatewayError):
    """LLM router could not produce a usable decision (timeout, bad JSON, unknown agent)."""


class LLMClient(Protocol):
    """Minimal sync chat-completions surface the router relies on."""

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class LLMRouter:
    """Asks an LLM to pick exactly one agent name from the registered set."""

    def __init__(
        self,
        specs: Iterable[AgentSpec],
        client: LLMClient,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._specs = list(specs)
        if not self._specs:
            raise ValueError("LLMRouter needs at least one AgentSpec.")
        self._client = client
        self._timeout = float(timeout_seconds)
        self._valid_names = {s.name for s in self._specs}
        self._system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        lines = [
            "You are a routing classifier. Pick exactly ONE agent that should",
            "answer the user's request from the catalog below.",
            "",
            "Respond with a single JSON object: {\"agent\": <name>, \"reason\": <short>}.",
            "The agent name MUST be one of the catalog entries verbatim.",
            "",
            "Agent catalog:",
        ]
        for spec in self._specs:
            tag_part = f" Tags: {', '.join(spec.tags)}." if spec.tags else ""
            ex_part = ""
            if spec.example_prompts:
                joined = "; ".join(spec.example_prompts[:3])
                ex_part = f" Examples: {joined}."
            lines.append(f"- {spec.name}: {spec.description}{tag_part}{ex_part}")
        return "\n".join(lines)

    def classify(self, prompt: str) -> RouteDecision:
        try:
            obj = self._client.complete_json(
                system=self._system_prompt,
                user=prompt,
                timeout_seconds=self._timeout,
            )
        except Exception as exc:
            raise LLMRouterError(f"LLM call failed: {exc}") from exc

        name = str(obj.get("agent") or "").strip()
        reason = str(obj.get("reason") or "").strip()
        if name not in self._valid_names:
            raise LLMRouterError(
                f"LLM returned unknown agent {name!r}; valid: {sorted(self._valid_names)}"
            )
        return RouteDecision(
            selected_agent=name,
            reason=reason or f"LLM picked {name}",
            scores={},
            method="llm",
            confidence=1.0,
            candidates=({"agent": name, "score": 1.0},),
        )


class AzureOpenAIClient:
    """LLMClient implementation backed by Azure OpenAI chat completions.

    Auth: prefers an Azure AD token provider when one is supplied (workload
    identity via WorkloadIdentityCredential is the production path), and
    falls back to an api_key when only that is provided.

    Latency / cost: this whole class exists so a single ~$0.0001 routing
    call only happens when BM25 is *not* confident. Timeouts are aggressive
    on purpose -- a hung LLM should not block agent invocation.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_version: str,
        api_key: str | None = None,
        azure_ad_token_provider: Any | None = None,
    ) -> None:
        if not endpoint or not deployment:
            raise ValueError("AzureOpenAIClient requires endpoint and deployment.")
        if api_key is None and azure_ad_token_provider is None:
            raise ValueError(
                "AzureOpenAIClient requires either api_key or azure_ad_token_provider."
            )
        # Lazy import so the openai dep stays optional.
        from openai import AzureOpenAI  # type: ignore[import-not-found]

        kwargs: dict[str, Any] = {
            "azure_endpoint": endpoint,
            "api_version": api_version,
        }
        if azure_ad_token_provider is not None:
            kwargs["azure_ad_token_provider"] = azure_ad_token_provider
        if api_key is not None:
            kwargs["api_key"] = api_key
        self._client = AzureOpenAI(**kwargs)
        self._deployment = deployment

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        # response_format json_object forces the model to emit valid JSON;
        # we still defensively parse in case the SDK / endpoint version
        # ignores the hint.
        completion = self._client.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=timeout_seconds,
        )
        content = (completion.choices[0].message.content or "").strip()
        if not content:
            raise LLMRouterError("LLM returned empty completion content.")
        try:
            obj = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMRouterError(f"LLM did not return JSON: {content!r}") from exc
        if not isinstance(obj, dict):
            raise LLMRouterError(f"LLM JSON is not an object: {obj!r}")
        return obj


__all__: list[str] = [
    "AzureOpenAIClient",
    "LLMClient",
    "LLMRouter",
    "LLMRouterError",
]
