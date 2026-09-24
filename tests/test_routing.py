"""Tests for the gateway-side KeywordClassifier (cloud x domain routing).

Phase 0 of router unification: the algorithm and keyword tables were ported
verbatim from agent_chat_ui.app.classify_agent. These tests are likewise
ported so the cross-cloud routing behaviour is now guarded inside the gateway
package -- the chat-ui's own router became a thin pre-validation function and
no longer carries any keyword logic.
"""

from __future__ import annotations

import pytest

from multicloud_ai_agent_gateway.core.routing import (
    AUTO_ROUTER_ALIASES,
    AUTO_ROUTER_NAME,
    KeywordClassifier,
    KeywordTables,
    RouteDecision,
    is_auto_router_name,
    normalize_router_name,
)


@pytest.fixture
def classifier() -> KeywordClassifier:
    return KeywordClassifier()


# ---------------------------------------------------------------------------
# KeywordClassifier.classify -- cloud x domain classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt,expected_agent",
    [
        # Azure FinOps -- explicit cloud + finops domain
        ("Show current month Azure costs", "AzureFinOpsAgent"),
        ("What is my AKS resource group spending?", "AzureFinOpsAgent"),
        ("Find storage account cost risks in Azure", "AzureFinOpsAgent"),
        # Azure ML -- explicit cloud + ml domain
        ("Start an Azure ML training job on cpu-cluster", "AzureMLTrainingAgent"),
        ("List recent Azure ML jobs", "AzureMLTrainingAgent"),
        ("Submit a PyTorch training experiment to mlw-example", "AzureMLTrainingAgent"),
        # AWS FinOps -- explicit cloud + finops domain
        ("Show AWS spend by service this month", "AwsFinOpsSmartAgent"),
        ("What's my EC2 cost in us-east-1?", "AwsFinOpsSmartAgent"),
        ("Find S3 storage waste in my AWS account", "AwsFinOpsSmartAgent"),
        # AWS ML -- explicit cloud + ml domain
        ("List SageMaker training jobs", "AwsSageMakerTrainingSmartAgent"),
        ("Start a Bedrock training experiment", "AwsSageMakerTrainingSmartAgent"),
        # Ambiguous / no cloud signal -- defaults to Azure FinOps
        ("Show me current cloud costs", "AzureFinOpsAgent"),
        ("Hello", "AzureFinOpsAgent"),
        # Cloud-neutral domain signal: ML wins on "training jobs" -> Azure ML
        ("List training jobs", "AzureMLTrainingAgent"),
    ],
)
def test_classifier_routes_correctly(
    classifier: KeywordClassifier, prompt: str, expected_agent: str
) -> None:
    decision = classifier.classify(prompt)
    assert decision.selected_agent == expected_agent, (
        f"prompt={prompt!r} routed to {decision.selected_agent}"
    )


def test_classifier_returns_routedecision_in_auto_mode(classifier: KeywordClassifier) -> None:
    decision = classifier.classify("Show AWS S3 costs")
    assert isinstance(decision, RouteDecision)
    assert decision.requested_agent == AUTO_ROUTER_NAME
    assert decision.mode == "auto"


def test_classifier_includes_all_signal_scores(classifier: KeywordClassifier) -> None:
    decision = classifier.classify("Show AWS S3 costs in us-east-1")
    assert set(decision.scores.keys()) == {
        "azure_signal",
        "aws_signal",
        "finops_score",
        "ml_score",
    }
    assert decision.scores["aws_signal"] > 0
    assert decision.scores["azure_signal"] == 0
    assert decision.scores["finops_score"] > 0


def test_classifier_aws_wins_when_aws_signal_outweighs_azure(
    classifier: KeywordClassifier,
) -> None:
    decision = classifier.classify("Compare AWS EC2 costs with Azure VMs")
    assert decision.selected_agent.startswith("Aws")
    assert decision.scores["aws_signal"] >= decision.scores["azure_signal"]


def test_classifier_default_route_message_when_no_signal(
    classifier: KeywordClassifier,
) -> None:
    decision = classifier.classify("hello there")
    assert decision.reason == "default route for general Azure operations"
    assert decision.scores == {
        "azure_signal": 0,
        "aws_signal": 0,
        "finops_score": 0,
        "ml_score": 0,
    }


def test_classifier_to_dict_round_trip(classifier: KeywordClassifier) -> None:
    decision = classifier.classify("Show AWS S3 costs")
    payload = decision.to_dict()
    assert payload["selected_agent"] == "AwsFinOpsSmartAgent"
    assert payload["mode"] == "auto"
    assert payload["requested_agent"] == AUTO_ROUTER_NAME
    assert payload["scores"]["aws_signal"] > 0


def test_classifier_accepts_custom_keyword_tables() -> None:
    """Override one bucket and verify routing follows."""
    tables = KeywordTables(
        azure_hints=("azure",),
        aws_hints=("aws",),
        finops_hints=("zzzz_no_match",),  # disabled
        ml_hints=("any",),                # always wins
    )
    classifier = KeywordClassifier(tables=tables)
    decision = classifier.classify("any azure thing")
    # Custom ml_hints fires on "any" -> domain=ml, cloud=azure -> AzureMLTrainingAgent.
    assert decision.selected_agent == "AzureMLTrainingAgent"


# ---------------------------------------------------------------------------
# is_auto_router_name / normalize_router_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["AutoRouter", "autorouter", "auto", "router", "Auto", "AUTO"])
def test_is_auto_router_name_true_for_aliases(name: str) -> None:
    assert is_auto_router_name(name)


@pytest.mark.parametrize("name", ["AzureFinOpsAgent", "AwsFinOpsSmartAgent", "", "  ", None])
def test_is_auto_router_name_false_for_others(name: str | None) -> None:
    assert not is_auto_router_name(name)


def test_aliases_set_is_lowercase() -> None:
    """Sanity: aliases must be stored lowercase since matching is case-insensitive."""
    assert all(alias == alias.lower() for alias in AUTO_ROUTER_ALIASES)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AutoRouter", AUTO_ROUTER_NAME),
        ("auto", AUTO_ROUTER_NAME),
        ("AUTO", AUTO_ROUTER_NAME),
        ("router", AUTO_ROUTER_NAME),
        ("", AUTO_ROUTER_NAME),
        ("   ", AUTO_ROUTER_NAME),
        (None, AUTO_ROUTER_NAME),
        ("AzureFinOpsAgent", "AzureFinOpsAgent"),
        ("  AzureFinOpsAgent  ", "AzureFinOpsAgent"),
    ],
)
def test_normalize_router_name(raw: str | None, expected: str) -> None:
    assert normalize_router_name(raw) == expected
