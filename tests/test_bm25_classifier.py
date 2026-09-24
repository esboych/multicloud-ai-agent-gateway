"""Tests for BM25Classifier -- the metadata-driven Phase-1 router.

The point of BM25 is that it can pick *any* agent in the registry (not just
the four hard-coded ones the legacy keyword classifier knows), and that the
discriminator lives in agent metadata, not in code. So these tests assert:

  - the six production agents are all reachable when given a prompt that
    matches their tags / example_prompts;
  - confidence and the candidates short-list are populated;
  - CamelCase agent names are tokenised so 'AwsSageMakerTrainingSmartAgent'
    contributes 'sage', 'maker', 'training', etc. to the doc.
"""

from __future__ import annotations

import pytest

from multicloud_ai_agent_gateway.core.backend import AgentSpec
from multicloud_ai_agent_gateway.core.bm25 import BM25Classifier


def _production_specs() -> list[AgentSpec]:
    """Snapshot of the agents wired in aks/multicloud-ai-agent-gateway/k8s/configmap.yaml.

    Kept inline (rather than read from the YAML) so this test doesn't need a
    yaml dependency and stays fast. Update both sides if you tune metadata.
    """
    return [
        AgentSpec(
            name="AzureFinOpsAgent",
            backend_id="foundry",
            description="Read-only Azure cost and resource investigation agent.",
            tags=("azure", "finops", "cost", "spend", "billing", "resource-group", "subscription"),
            example_prompts=(
                "Show current month Azure costs and top cost drivers.",
                "What changed in Azure spend and which resources should I inspect first?",
                "Find storage account cost risks in my Azure subscription.",
            ),
        ),
        AgentSpec(
            name="AzureMLTrainingAgent",
            backend_id="foundry",
            description="Constrained Azure Machine Learning training job agent.",
            tags=("azure", "azureml", "ml", "training", "job", "workspace", "cpu-cluster", "pytorch"),
            example_prompts=(
                "List recent Azure ML training jobs and summarize their statuses.",
                "Submit a PyTorch training experiment to mlw-example.",
                "Start an Azure ML training job on cpu-cluster.",
            ),
        ),
        AgentSpec(
            name="AwsFinOpsSmartAgent",
            backend_id="agentcore",
            description="AWS-side LLM-planned FinOps cost investigation agent (Bedrock AgentCore).",
            tags=("aws", "finops", "cost", "spend", "billing", "ec2", "s3", "rds", "cost-explorer", "smart"),
            example_prompts=(
                "Show current month AWS costs and the top three cost drivers.",
                "What's my EC2 cost in us-east-1?",
                "Find S3 storage waste in my AWS account.",
            ),
        ),
        AgentSpec(
            name="AwsFinOpsHiddenCostAgent",
            backend_id="agentcore",
            description="AWS-side read-only investigation agent specialized in hidden, idle, orphaned, zombie and underutilized resources.",
            tags=("aws", "finops", "hidden", "idle", "orphaned", "zombie", "underutilized", "wastage", "unused", "stale"),
            example_prompts=(
                "Find hidden AWS costs and orphaned resources.",
                "Show me idle EC2 instances, unattached EBS volumes and unused elastic IPs.",
                "Audit AWS for zombie and underutilized resources I'm still paying for.",
            ),
        ),
        AgentSpec(
            name="AwsSageMakerTrainingSmartAgent",
            backend_id="agentcore",
            description="AWS-side LLM-planned SageMaker training agent (Bedrock AgentCore).",
            tags=("aws", "sagemaker", "training", "job", "ml", "bedrock", "smart", "experiment", "pytorch"),
            example_prompts=(
                "List recent SageMaker training jobs and summarize their statuses.",
                "Plan and start a SageMaker training experiment.",
                "Diagnose a failed SageMaker training job and suggest a fix.",
            ),
        ),
        AgentSpec(
            name="AwsSageMakerTrainingAgent",
            backend_id="agentcore",
            description="AWS-side classic no-LLM SageMaker training agent. Use when you want deterministic listing and status checks without a planning model.",
            tags=("aws", "sagemaker", "training", "job", "ml", "classic", "deterministic", "list", "status"),
            example_prompts=(
                "Just list SageMaker training jobs without any planning.",
                "Show raw status of SageMaker training jobs.",
                "Classic deterministic SageMaker training listing.",
            ),
        ),
    ]


@pytest.fixture
def classifier() -> BM25Classifier:
    return BM25Classifier(_production_specs())


@pytest.mark.parametrize(
    "prompt,expected_agent",
    [
        ("Show current month Azure costs", "AzureFinOpsAgent"),
        ("List recent Azure ML training jobs", "AzureMLTrainingAgent"),
        ("What's my EC2 spend in us-east-1?", "AwsFinOpsSmartAgent"),
        # The Smart variant should win on planning/experiment phrasing.
        ("Plan and start a SageMaker training experiment", "AwsSageMakerTrainingSmartAgent"),
        # The classic variant should win on raw/list phrasing.
        ("Just give me a raw deterministic list of SageMaker training jobs", "AwsSageMakerTrainingAgent"),
        # The Hidden variant should win on idle/orphaned/zombie phrasing.
        ("Find hidden idle and orphaned AWS resources I'm still paying for", "AwsFinOpsHiddenCostAgent"),
    ],
)
def test_bm25_picks_expected_agent(
    classifier: BM25Classifier, prompt: str, expected_agent: str
) -> None:
    decision = classifier.classify(prompt)
    assert decision.selected_agent == expected_agent, (
        f"prompt={prompt!r} routed to {decision.selected_agent}; "
        f"candidates={decision.candidates}"
    )


def test_bm25_method_and_candidates_shape(classifier: BM25Classifier) -> None:
    decision = classifier.classify("Show me hidden idle AWS resources")
    assert decision.method == "bm25"
    assert 0.0 <= decision.confidence <= 1.0
    assert decision.candidates, "candidates list must not be empty"
    # Candidates list is sorted by score descending.
    scores = [c["score"] for c in decision.candidates]
    assert scores == sorted(scores, reverse=True)
    # The chosen agent is the first candidate.
    assert decision.candidates[0]["agent"] == decision.selected_agent


def test_bm25_emits_top_score_and_margin(classifier: BM25Classifier) -> None:
    decision = classifier.classify("Find S3 storage waste in AWS")
    assert "top_score" in decision.scores
    assert "margin" in decision.scores
    assert decision.scores["top_score"] >= decision.scores["runner_up_score"]


def test_bm25_unrelated_prompt_has_low_confidence(classifier: BM25Classifier) -> None:
    """A prompt with no relevant tokens should still pick *something* but
    flag it as low-confidence so HybridClassifier knows to escalate to LLM."""
    decision = classifier.classify("hello there friend")
    # Will still pick the top-1 (BM25 returns argmax), but confidence ~0.
    assert decision.confidence < 0.2


def test_bm25_camel_case_name_splits_into_tokens() -> None:
    """The agent NAME alone is enough to disambiguate when the prompt uses
    the same words. Tags/examples are bonus weight, not the only signal."""
    specs = [
        AgentSpec(name="AlphaTrainingAgent", backend_id="foundry"),
        AgentSpec(name="BetaCostAgent", backend_id="foundry"),
    ]
    classifier = BM25Classifier(specs)
    assert classifier.classify("show me cost").selected_agent == "BetaCostAgent"
    assert classifier.classify("training job please").selected_agent == "AlphaTrainingAgent"


def test_bm25_rejects_empty_specs() -> None:
    with pytest.raises(ValueError):
        BM25Classifier([])


def test_bm25_to_dict_shape(classifier: BM25Classifier) -> None:
    payload = classifier.classify("Show AWS costs").to_dict()
    assert payload["method"] == "bm25"
    assert "confidence" in payload
    assert "candidates" in payload
    assert isinstance(payload["candidates"], list)
