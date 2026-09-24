"""Tests for HybridClassifier (BM25 fast path + LLM tie-breaker).

The hybrid classifier has three regimes:

  - confident BM25 -> method='bm25', LLM not called
  - ambiguous BM25 + LLM available -> method='llm', BM25 candidates attached
  - ambiguous BM25 + LLM error/missing -> method='bm25_fallback'

We exercise each regime with a stub LLM router so the tests stay sync,
fast, and free of an actual OpenAI dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from multicloud_ai_agent_gateway.core.backend import AgentSpec
from multicloud_ai_agent_gateway.core.bm25 import BM25Classifier
from multicloud_ai_agent_gateway.core.hybrid_router import HybridClassifier
from multicloud_ai_agent_gateway.core.llm_router import LLMRouter, LLMRouterError


@dataclass
class StubLLMClient:
    """Returns whatever JSON object the test scripts in advance."""

    response: dict | None = None
    raises: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def complete_json(
        self, *, system: str, user: str, timeout_seconds: float
    ) -> dict:
        self.calls.append({"system": system, "user": user, "timeout": timeout_seconds})
        if self.raises is not None:
            raise self.raises
        return self.response or {}


def _specs() -> list[AgentSpec]:
    return [
        AgentSpec(
            name="AzureFinOpsAgent",
            backend_id="foundry",
            description="Azure cost investigation agent.",
            tags=("azure", "finops", "cost"),
            example_prompts=("Show current month Azure costs.",),
        ),
        AgentSpec(
            name="AwsFinOpsSmartAgent",
            backend_id="agentcore",
            description="AWS cost investigation agent.",
            tags=("aws", "finops", "cost", "ec2", "s3"),
            example_prompts=("Show AWS costs by service.",),
        ),
        AgentSpec(
            name="AwsFinOpsHiddenCostAgent",
            backend_id="agentcore",
            description="AWS hidden idle orphaned resources agent.",
            tags=("aws", "hidden", "idle", "orphaned", "zombie", "underutilized"),
            example_prompts=("Find hidden AWS costs.",),
        ),
    ]


def test_hybrid_uses_bm25_when_confident() -> None:
    """A prompt with strong, unambiguous signal should never reach the LLM."""
    bm25 = BM25Classifier(_specs())
    stub = StubLLMClient(response={"agent": "AzureFinOpsAgent", "reason": "should not be called"})
    llm = LLMRouter(_specs(), stub)
    hybrid = HybridClassifier(
        bm25, llm=llm, absolute_threshold=0.5, confidence_threshold=0.15
    )

    decision = hybrid.classify("Find hidden idle orphaned zombie AWS resources")
    assert decision.selected_agent == "AwsFinOpsHiddenCostAgent"
    assert decision.method == "bm25"
    assert stub.calls == [], "confident BM25 path must not call the LLM"


def test_hybrid_escalates_to_llm_when_ambiguous() -> None:
    """An ambiguous prompt should reach the LLM and use its choice."""
    bm25 = BM25Classifier(_specs())
    stub = StubLLMClient(
        response={"agent": "AzureFinOpsAgent", "reason": "user said azure"}
    )
    llm = LLMRouter(_specs(), stub)
    # Force the LLM path by making the BM25 thresholds unreachable.
    hybrid = HybridClassifier(
        bm25, llm=llm, absolute_threshold=999.0, confidence_threshold=1.0
    )

    decision = hybrid.classify("anything")
    assert decision.method == "llm"
    assert decision.selected_agent == "AzureFinOpsAgent"
    assert "BM25 ambiguous" in decision.reason
    # BM25 candidates must be attached so the UI can show runner-ups.
    assert decision.candidates, "LLM decision should carry BM25 candidates"
    assert len(stub.calls) == 1


def test_hybrid_falls_back_when_llm_errors() -> None:
    bm25 = BM25Classifier(_specs())
    stub = StubLLMClient(raises=RuntimeError("openai down"))
    llm = LLMRouter(_specs(), stub)
    hybrid = HybridClassifier(
        bm25, llm=llm, absolute_threshold=999.0, confidence_threshold=1.0
    )

    decision = hybrid.classify("show me something cost related")
    assert decision.method == "bm25_fallback"
    assert "LLM error" in decision.reason
    # Whatever BM25 thought is still the chosen agent.
    assert decision.selected_agent in {s.name for s in _specs()}


def test_hybrid_without_llm_marks_fallback() -> None:
    bm25 = BM25Classifier(_specs())
    hybrid = HybridClassifier(
        bm25, llm=None, absolute_threshold=999.0, confidence_threshold=1.0
    )
    decision = hybrid.classify("something low-signal")
    assert decision.method == "bm25_fallback"
    assert "LLM not configured" in decision.reason


def test_hybrid_to_dict_carries_method_and_candidates() -> None:
    bm25 = BM25Classifier(_specs())
    hybrid = HybridClassifier(bm25, llm=None)
    decision = hybrid.classify("Find hidden idle orphaned zombie AWS resources")
    payload = decision.to_dict()
    assert payload["method"] in {"bm25", "bm25_fallback"}
    assert isinstance(payload["candidates"], list)
    assert payload["candidates"][0]["agent"] == payload["selected_agent"]


def test_llm_router_rejects_unknown_agent_name() -> None:
    """If the LLM returns a name not in the registry we must error, not
    silently pick something else -- protecting callers from typos / drift."""
    specs = _specs()
    stub = StubLLMClient(response={"agent": "NotARealAgent", "reason": "nope"})
    llm = LLMRouter(specs, stub)
    with pytest.raises(LLMRouterError):
        llm.classify("anything")


def test_llm_router_builds_system_prompt_with_catalog() -> None:
    """The system prompt the LLM sees must include every registered agent
    so the model can return their exact names."""
    specs = _specs()
    stub = StubLLMClient(response={"agent": "AzureFinOpsAgent", "reason": "ok"})
    llm = LLMRouter(specs, stub)
    llm.classify("query")
    assert len(stub.calls) == 1
    system = stub.calls[0]["system"]
    for spec in specs:
        assert spec.name in system
