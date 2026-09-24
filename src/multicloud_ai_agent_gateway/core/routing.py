"""Keyword-based agent routing.

Phase 0 of router unification: this module is a port of the chat-ui's
``classify_agent`` / ``route_chat_agent`` functions into the gateway. The
algorithm and the keyword tables are intentionally identical to the prior
in-UI implementation so behaviour does not change.

A later phase will replace the hard-coded mapping to four agent names with a
data-driven lookup against ``AgentRegistry`` entries (using each agent's
``description``/``tags``/``example_prompts``), and the chat-ui copy of these
tables will be removed entirely.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

AUTO_ROUTER_NAME = "AutoRouter"
AUTO_ROUTER_BACKEND_ID = "autorouter"
AUTO_ROUTER_ALIASES = frozenset({"auto", "autorouter", "router"})
AUTO_ROUTER_DESCRIPTION = (
    "Picks the most appropriate agent from the prompt using keyword scoring "
    "(cloud x domain). Pass agent name 'AutoRouter' to :invoke or :stream, "
    "or call POST /v1/route to see the decision without invoking."
)


# Cloud detectors -- decide whether the prompt is about Azure or AWS.
# Kept tight to vendor-specific terms; generic words (cost, vm, storage)
# stay in the domain detectors below so they do not bias cloud detection.
DEFAULT_AZURE_HINTS: tuple[str, ...] = (
    "azure",
    "aks",
    "azure ml",
    "azureml",
    "mlw-",
    "cpu-cluster",
    "cosmos",
    "cosmosdb",
    "cosmos db",
    "synapse",
    "app service",
    "foundry",
    "log analytics",
    "application insights",
    "key vault",
    "service bus",
    "event hub",
    "data factory",
    "spaincentral",
    "westeurope",
    "eastus",
    "northeurope",
)

DEFAULT_AWS_HINTS: tuple[str, ...] = (
    "aws",
    "ec2",
    "s3",
    "lambda",
    "rds",
    "dynamodb",
    "dynamo",
    "vpc",
    "iam",
    "cloudwatch",
    "cloudformation",
    "bedrock",
    "agentcore",
    "sagemaker",
    "sage maker",
    "ebs",
    "elb",
    "alb",
    "nlb",
    "route 53",
    "route53",
    "cloudfront",
    "kinesis",
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-1",
)

# Domain detectors -- FinOps vs ML/training. Cloud-agnostic on purpose.
DEFAULT_FINOPS_HINTS: tuple[str, ...] = (
    "advisor",
    "bill",
    "budget",
    "cost",
    "costs",
    "finops",
    "invoice",
    "overprovisioned",
    "public access",
    "redis",
    "region",
    "resource group",
    "sku",
    "spend",
    "spending",
    "storage",
    "subscription",
    "underutilized",
    "virtual machine",
    "vm",
    "wastage",
    "savings",
)

DEFAULT_ML_HINTS: tuple[str, ...] = (
    "batch",
    "classifier",
    "cuda",
    "dataset",
    "epoch",
    "experiment",
    "gpu",
    "inference",
    "job",
    "jobs",
    "machine learning",
    "metric",
    "model",
    "neural",
    "pytorch",
    "tensorflow",
    "train",
    "training",
    "workspace",
)


@dataclass(frozen=True)
class KeywordTables:
    """Bag of keyword tuples used by ``KeywordClassifier``.

    Exposed as a dataclass so a future phase can swap the tables (e.g. derive
    them from agent metadata) without changing ``KeywordClassifier`` itself.
    """

    azure_hints: tuple[str, ...] = DEFAULT_AZURE_HINTS
    aws_hints: tuple[str, ...] = DEFAULT_AWS_HINTS
    finops_hints: tuple[str, ...] = DEFAULT_FINOPS_HINTS
    ml_hints: tuple[str, ...] = DEFAULT_ML_HINTS


@dataclass(frozen=True)
class RouteDecision:
    """Result of classifying a prompt to a target agent.

    ``method`` names which classifier produced this decision: ``"keyword"``
    (legacy hint-table scoring), ``"bm25"`` (metadata-driven retrieval),
    ``"llm"`` (LLM tie-break), ``"bm25_fallback"`` (LLM failed, BM25 used)
    or ``"manual"`` (user forced an agent name). ``confidence`` is in [0,1];
    ``candidates`` is the top-N short list with per-agent scores. All three
    are surfaced through the REST routing payload so the UI can show why a
    particular agent was picked and which were close runners-up.
    """

    selected_agent: str
    reason: str
    scores: dict[str, float]
    requested_agent: str = AUTO_ROUTER_NAME
    mode: str = "auto"
    method: str = "keyword"
    confidence: float = 0.0
    candidates: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_agent": self.selected_agent,
            "reason": self.reason,
            "scores": dict(self.scores),
            "requested_agent": self.requested_agent,
            "mode": self.mode,
            "method": self.method,
            "confidence": self.confidence,
            "candidates": [dict(c) for c in self.candidates],
        }


def is_auto_router_name(name: str | None) -> bool:
    """True when ``name`` is the AutoRouter sentinel or one of its aliases."""
    if name is None:
        return False
    stripped = name.strip()
    if not stripped:
        return False
    return stripped.lower() in AUTO_ROUTER_ALIASES


def normalize_router_name(name: str | None) -> str:
    """Canonicalize 'auto' / 'router' / '' to ``AUTO_ROUTER_NAME``; otherwise pass through."""
    if name is None:
        return AUTO_ROUTER_NAME
    stripped = name.strip()
    if not stripped:
        return AUTO_ROUTER_NAME
    if stripped.lower() in AUTO_ROUTER_ALIASES:
        return AUTO_ROUTER_NAME
    return stripped


class KeywordClassifier:
    """Two-axis classifier (cloud x domain) -> one of four agents.

    Pure: state is just the keyword tables. ``classify`` is referentially
    transparent in the prompt.
    """

    def __init__(self, tables: KeywordTables | None = None) -> None:
        self._tables = tables or KeywordTables()

    @property
    def tables(self) -> KeywordTables:
        return self._tables

    def classify(self, prompt: str) -> RouteDecision:
        text = (prompt or "").lower()
        azure_signal = _score(text, self._tables.azure_hints)
        aws_signal = _score(text, self._tables.aws_hints)
        finops_score = _score(text, self._tables.finops_hints)
        ml_score = _score(text, self._tables.ml_hints)

        cloud = "aws" if aws_signal > azure_signal else "azure"
        domain = "ml" if ml_score > finops_score else "finops"

        if cloud == "aws" and domain == "ml":
            agent = "AwsSageMakerTrainingSmartAgent"
            reason = "matched AWS ML/SageMaker terms"
        elif cloud == "aws":
            agent = "AwsFinOpsSmartAgent"
            reason = (
                "matched AWS cost/resource terms"
                if aws_signal > 0
                else "default AWS FinOps route"
            )
        elif domain == "ml":
            agent = "AzureMLTrainingAgent"
            reason = "matched Azure ML training/job terms"
        else:
            agent = "AzureFinOpsAgent"
            reason = (
                "matched Azure cost/resource terms"
                if (azure_signal > 0 or finops_score > 0)
                else "default route for general Azure operations"
            )

        # Keyword classifier has no notion of a "runner-up" since it picks
        # by a 2x2 grid, not by ranking. We still expose a single-item
        # candidates list and a coarse confidence derived from total signal
        # strength so the REST/UI shape matches BM25/LLM decisions.
        total_signal = float(azure_signal + aws_signal + finops_score + ml_score)
        confidence = min(1.0, total_signal / 4.0) if total_signal > 0 else 0.0
        return RouteDecision(
            selected_agent=agent,
            reason=reason,
            scores={
                "azure_signal": float(azure_signal),
                "aws_signal": float(aws_signal),
                "finops_score": float(finops_score),
                "ml_score": float(ml_score),
            },
            method="keyword",
            confidence=round(confidence, 4),
            candidates=({"agent": agent, "score": round(confidence, 4)},),
        )


def _score(text: str, keywords: Iterable[str]) -> int:
    score = 0
    for keyword in keywords:
        if keyword in text:
            score += 3 if " " in keyword or "-" in keyword else 1
    return score
