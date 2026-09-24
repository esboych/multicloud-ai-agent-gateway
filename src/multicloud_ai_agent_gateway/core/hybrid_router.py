"""Hybrid classifier: BM25 fast path + LLM tie-breaker.

Decision flow (per ``classify`` call):

  1. BM25 over agent metadata produces a ranked list + confidence in [0,1]
     (margin between top-1 and top-2 normalized by top-1 score).
  2. If BM25 is *confident enough*  -- both absolute top score >= S_abs and
     confidence >= C_min -- we accept its pick. ``method="bm25"``.
  3. Otherwise, if an LLM router is wired in, we call it. Its choice is
     returned with ``method="llm"``; we attach the BM25 candidates so the
     UI can show what the deterministic path thought.
  4. If the LLM router is not configured *or* fails, we fall back to the
     BM25 top-1 with ``method="bm25_fallback"`` so callers can tell from
     the response that the LLM was skipped / errored.

The point is that most queries land in step (2) and never pay the LLM cost
or latency. Tweak the thresholds via ``GATEWAY_ROUTER_BM25_*`` env vars.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from multicloud_ai_agent_gateway.core.bm25 import BM25Classifier
from multicloud_ai_agent_gateway.core.llm_router import LLMRouter, LLMRouterError
from multicloud_ai_agent_gateway.core.routing import RouteDecision

log = logging.getLogger(__name__)


class HybridClassifier:
    """Compose a BM25 classifier with an optional LLM tie-breaker."""

    def __init__(
        self,
        bm25: BM25Classifier,
        *,
        llm: LLMRouter | None = None,
        absolute_threshold: float = 0.5,
        confidence_threshold: float = 0.15,
    ) -> None:
        self._bm25 = bm25
        self._llm = llm
        self._absolute_threshold = float(absolute_threshold)
        self._confidence_threshold = float(confidence_threshold)

    @property
    def bm25(self) -> BM25Classifier:
        return self._bm25

    @property
    def llm(self) -> LLMRouter | None:
        return self._llm

    def classify(self, prompt: str) -> RouteDecision:
        bm25_decision = self._bm25.classify(prompt)
        top_score = float(bm25_decision.scores.get("top_score", 0.0))
        margin = float(bm25_decision.scores.get("margin", 0.0))
        confident = (
            top_score >= self._absolute_threshold
            and bm25_decision.confidence >= self._confidence_threshold
        )

        if confident:
            return bm25_decision

        if self._llm is None:
            # No LLM wired in: degrade transparently but mark the method
            # so the UI can tell this isn't a "BM25 said it was sure" win.
            return replace(
                bm25_decision,
                method="bm25_fallback",
                reason=(
                    f"{bm25_decision.reason}; "
                    f"LLM not configured, BM25 below threshold "
                    f"(score={top_score:.2f} < {self._absolute_threshold}, "
                    f"conf={bm25_decision.confidence:.2f} < {self._confidence_threshold})"
                ),
            )

        try:
            llm_decision = self._llm.classify(prompt)
        except LLMRouterError as exc:
            log.warning("LLM router failed; falling back to BM25 top match: %s", exc)
            return replace(
                bm25_decision,
                method="bm25_fallback",
                reason=f"BM25 ambiguous, LLM error: {exc}",
            )

        # Surface BM25 candidates alongside the LLM pick so the UI can
        # show "LLM chose X, BM25 thought Y/Z" without a separate request.
        merged_scores: dict[str, Any] = {
            "bm25_top_score": top_score,
            "bm25_margin": margin,
            "bm25_confidence": bm25_decision.confidence,
        }
        bm25_top = bm25_decision.candidates[0]["agent"] if bm25_decision.candidates else ""
        decorated_reason = (
            f"BM25 ambiguous (top={bm25_top!r} score={top_score:.2f} "
            f"conf={bm25_decision.confidence:.2f}); LLM tie-break: {llm_decision.reason}"
        )
        return replace(
            llm_decision,
            scores=merged_scores,
            candidates=bm25_decision.candidates,
            reason=decorated_reason,
        )


__all__: list[str] = ["HybridClassifier"]
