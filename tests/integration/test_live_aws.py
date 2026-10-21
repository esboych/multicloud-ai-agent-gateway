"""Live AWS: the real Entra -> STS bridge, Cost Explorer and AgentCore.

Opt-in (``pytest -m live_aws``) and skipped unless the bridge environment is
present, typically exported from ``infra/terraform-aws``'s ``local_dev_env``
output (client-secret development mode, no AKS needed):

    GATEWAY_AWS_REGION, GATEWAY_AWS_ROLE_ARN, GATEWAY_AWS_BRIDGE_TENANT_ID,
    GATEWAY_AWS_BRIDGE_CLIENT_ID, GATEWAY_AWS_BRIDGE_SCOPE,
    GATEWAY_AWS_BRIDGE_CLIENT_SECRET

Optional:

    GATEWAY_LIVE_AGENTCORE_RUNTIME_ARN         a runtime the role may invoke (any model)
    GATEWAY_LIVE_AGENTCORE_LLM_RUNTIME_ARN     an LLM-backed runtime; quota exhaustion is
                                               reported as a skip, and exercises the
                                               AutoRouter runner-up retry
    GATEWAY_LIVE_AGENTCORE_DENIED_RUNTIME_ARN  a runtime outside the IAM policy

Every test here costs real money (Cost Explorer $0.01 per call, Bedrock
tokens for AgentCore) and talks to the internet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import boto3
import httpx
import pytest

from multicloud_ai_agent_gateway.auth.aws_bridge import AwsBridgeCredentials
from tests.integration.conftest import GatewayProcess, start_gateway, stop_gateway

pytestmark = pytest.mark.live_aws

BRIDGE_VARS = (
    "GATEWAY_AWS_REGION",
    "GATEWAY_AWS_ROLE_ARN",
    "GATEWAY_AWS_BRIDGE_TENANT_ID",
    "GATEWAY_AWS_BRIDGE_CLIENT_ID",
    "GATEWAY_AWS_BRIDGE_SCOPE",
    "GATEWAY_AWS_BRIDGE_CLIENT_SECRET",
)
RUNTIME_ARN = os.environ.get("GATEWAY_LIVE_AGENTCORE_RUNTIME_ARN", "").strip()
LLM_RUNTIME_ARN = os.environ.get("GATEWAY_LIVE_AGENTCORE_LLM_RUNTIME_ARN", "").strip()
DENIED_RUNTIME_ARN = os.environ.get("GATEWAY_LIVE_AGENTCORE_DENIED_RUNTIME_ARN", "").strip()

needs_agentcore = pytest.mark.skipif(not RUNTIME_ARN, reason="GATEWAY_LIVE_AGENTCORE_RUNTIME_ARN not set")
needs_llm = pytest.mark.skipif(not LLM_RUNTIME_ARN, reason="GATEWAY_LIVE_AGENTCORE_LLM_RUNTIME_ARN not set")
needs_denied = pytest.mark.skipif(
    not DENIED_RUNTIME_ARN, reason="GATEWAY_LIVE_AGENTCORE_DENIED_RUNTIME_ARN not set"
)

QUOTA_MARKERS = ("tokens per day", "Too many tokens", "ThrottlingException")


def _skip_if_quota(detail: str) -> None:
    """Bedrock daily token quotas surface as ThrottlingException inside a 200 from
    AgentCore; the gateway maps that to 502. It is an environment condition, so the
    test skips instead of failing, and says why."""
    if any(marker in detail for marker in QUOTA_MARKERS):
        pytest.skip(f"Bedrock model quota exhausted for the LLM runtime: {detail[:160]}")


def _bridge_env() -> dict[str, str]:
    env = {name: os.environ.get(name, "").strip() for name in BRIDGE_VARS}
    missing = [name for name, value in env.items() if not value]
    if missing:
        pytest.skip(f"live AWS bridge env missing: {', '.join(missing)}")
    return env


@pytest.fixture(scope="module")
def bridge_env() -> dict[str, str]:
    return _bridge_env()


@pytest.fixture(scope="module")
def live_agents() -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = [
        {
            "name": "AwsFinOpsClassicAgent",
            "backend_id": "direct",
            "direct_handler": "aws_cost_mtd_by_service",
            "description": "Zero-LLM month-to-date AWS cost by service from Cost Explorer.",
            "tags": ["aws", "cost", "costs", "spend", "billing", "month", "classic", "deterministic"],
            "example_prompts": ["Current AWS costs", "AWS spend this month by service"],
        }
    ]
    if RUNTIME_ARN:
        agents.append(
            {
                "name": "AwsAgentCoreAgent",
                "backend_id": "agentcore",
                "agentcore_runtime_arn": RUNTIME_ARN,
                "description": "AWS FinOps hidden-cost investigation on Bedrock AgentCore.",
                "tags": ["aws", "hidden", "waste", "idle", "unused", "investigation"],
                "example_prompts": ["Find hidden AWS costs", "What AWS resources are idle?"],
            }
        )
    if LLM_RUNTIME_ARN:
        agents.append(
            {
                "name": "AwsFinOpsSmartAgent",
                "backend_id": "agentcore",
                "agentcore_runtime_arn": LLM_RUNTIME_ARN,
                "description": "LLM-backed AWS FinOps agent on Bedrock AgentCore.",
                "tags": ["aws", "finops", "investigate", "why", "explain", "recommend", "smart", "drivers"],
                "example_prompts": ["Why did my AWS bill change?", "Explain my AWS cost drivers"],
            }
        )
    if DENIED_RUNTIME_ARN:
        agents.append(
            {
                "name": "DeniedAgent",
                "backend_id": "agentcore",
                "agentcore_runtime_arn": DENIED_RUNTIME_ARN,
                "description": "Runtime outside the IAM policy; must be refused by AWS.",
                "tags": ["denied"],
            }
        )
    return agents


@pytest.fixture(scope="module")
def aws_gateway(
    tmp_path_factory: pytest.TempPathFactory, bridge_env: dict[str, str], live_agents: list[dict[str, Any]]
) -> Iterator[GatewayProcess]:
    tmp_dir: Path = tmp_path_factory.mktemp("live-aws")
    extra = dict(bridge_env)
    extra["GATEWAY_AGENTS_JSON"] = json.dumps(live_agents)
    extra["GATEWAY_ROUTER_MODE"] = "bm25"
    process = start_gateway(tmp_dir, extra, name="live-aws")
    try:
        yield process
    finally:
        stop_gateway(process)


@pytest.fixture
def client(aws_gateway: GatewayProcess) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=aws_gateway.base_url, timeout=300.0) as client:
        yield client


# -- the bridge itself ---------------------------------------------------------


async def test_entra_token_is_exchanged_for_sts_credentials(bridge_env: dict[str, str]) -> None:
    from azure.identity import ClientSecretCredential

    credential = ClientSecretCredential(
        tenant_id=bridge_env["GATEWAY_AWS_BRIDGE_TENANT_ID"],
        client_id=bridge_env["GATEWAY_AWS_BRIDGE_CLIENT_ID"],
        client_secret=bridge_env["GATEWAY_AWS_BRIDGE_CLIENT_SECRET"],
    )
    bridge = AwsBridgeCredentials(
        aad_credential=credential,
        scope=bridge_env["GATEWAY_AWS_BRIDGE_SCOPE"],
        role_arn=bridge_env["GATEWAY_AWS_ROLE_ARN"],
        region=bridge_env["GATEWAY_AWS_REGION"],
        session_name="live-aws-test",
    )
    creds = await bridge.get_credentials()
    assert creds["AccessKeyId"].startswith("ASIA")  # temporary credentials, not a root/user key
    sts = boto3.client(
        "sts",
        region_name=bridge_env["GATEWAY_AWS_REGION"],
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    identity = sts.get_caller_identity()
    role_name = bridge_env["GATEWAY_AWS_ROLE_ARN"].rsplit("/", 1)[-1]
    assert f":assumed-role/{role_name}/live-aws-test" in identity["Arn"]
    # Second call is served from the cache: no new exchange.
    again = await bridge.get_credentials()
    assert again["AccessKeyId"] == creds["AccessKeyId"]


def test_entra_token_claims_match_trust_policy(bridge_env: dict[str, str]) -> None:
    """The token's aud is the client id and sub the SP object id, which is what
    the IAM trust policy conditions on (and what restrict_to_service_principal_subject pins)."""
    import jwt
    from azure.identity import ClientSecretCredential

    credential = ClientSecretCredential(
        tenant_id=bridge_env["GATEWAY_AWS_BRIDGE_TENANT_ID"],
        client_id=bridge_env["GATEWAY_AWS_BRIDGE_CLIENT_ID"],
        client_secret=bridge_env["GATEWAY_AWS_BRIDGE_CLIENT_SECRET"],
    )
    token = credential.get_token(bridge_env["GATEWAY_AWS_BRIDGE_SCOPE"]).token
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == bridge_env["GATEWAY_AWS_BRIDGE_CLIENT_ID"]
    assert claims["iss"] == f"https://login.microsoftonline.com/{bridge_env['GATEWAY_AWS_BRIDGE_TENANT_ID']}/v2.0"
    assert claims["sub"]
    expected_sub = os.environ.get("GATEWAY_LIVE_EXPECTED_SUB", "").strip()
    if expected_sub:
        assert claims["sub"] == expected_sub


# -- Cost Explorer through the bridge ------------------------------------------


def test_cost_explorer_agent_over_the_bridge(client: httpx.Client) -> None:
    resp = client.post("/v1/agents/AwsFinOpsClassicAgent:invoke", json={"prompt": "Current AWS costs"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "USD" in body["output_text"] or "$" in body["output_text"], body["output_text"][:300]
    event = body["tool_events"][0]
    assert event["name"] == "aws_cost_mtd_by_service"
    assert event["result"]["period"], event["result"]
    assert isinstance(event["result"]["cost_groups"], list)


def test_chat_routes_cost_prompt_to_classic_agent(client: httpx.Client) -> None:
    body = client.post("/v1/chat", json={"input": "AWS spend this month by service"}).json()
    assert body["routing"]["selected_agent"] == "AwsFinOpsClassicAgent"
    assert "USD" in body["output_text"] or "$" in body["output_text"]


# -- AgentCore ---------------------------------------------------------------------


@needs_agentcore
def test_agentcore_invoke_and_continue(client: httpx.Client) -> None:
    resp = client.post("/v1/agents/AwsAgentCoreAgent:invoke", json={"prompt": "Find hidden AWS costs"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["output_text"].strip()
    assert body["conversation_id"], "AgentCore runtimeSessionId expected"
    record = client.get(f"/v1/conversations/{body['conversation_id']}").json()
    assert record["agent"] == "AwsAgentCoreAgent"

    cont = client.post(
        "/v1/agents/AwsAgentCoreAgent:continue",
        json={"prompt": "Find hidden AWS costs", "conversation_id": body["conversation_id"]},
    )
    assert cont.status_code == 200, cont.text
    assert cont.json()["conversation_id"] == body["conversation_id"]
    # Sticky AutoRouter on a real AgentCore session id.
    auto = client.post(
        "/v1/agents/AutoRouter:continue",
        json={"prompt": "Current AWS costs", "conversation_id": body["conversation_id"]},
    ).json()
    assert auto["routing"]["selected_agent"] == "AwsAgentCoreAgent"
    assert auto["routing"]["method"] == "sticky"


@needs_agentcore
def test_agentcore_stream(client: httpx.Client) -> None:
    with client.stream(
        "POST", "/v1/agents/AwsAgentCoreAgent:stream", json={"prompt": "Find hidden AWS costs"}
    ) as resp:
        assert resp.status_code == 200
        text = resp.read().decode()
    kinds = [line[6:].strip() for line in text.splitlines() if line.startswith("event:")]
    assert "chunk" in kinds and kinds[-1] == "done", kinds


@needs_agentcore
def test_agentcore_through_mcp_and_a2a(client: httpx.Client) -> None:
    mcp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "AwsAgentCoreAgent", "arguments": {"prompt": "Find hidden AWS costs"}},
        },
    ).json()["result"]
    assert mcp["isError"] is False, mcp
    assert mcp["content"][0]["text"].strip()
    assert mcp["structuredContent"]["conversation_id"]

    a2a = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0", "id": 2, "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"kind": "text", "text": "Find hidden AWS costs"}], "messageId": "m1"},
                "metadata": {"agent": "AwsAgentCoreAgent"},
            },
        },
    ).json()["result"]
    assert a2a["status"]["state"] == "completed", a2a
    assert a2a["artifacts"][0]["parts"][0]["text"].strip()
    assert a2a["contextId"]


# -- LLM-backed runtime: quota-aware --------------------------------------------


@needs_llm
def test_llm_agentcore_invoke_unless_quota_exhausted(client: httpx.Client) -> None:
    resp = client.post(
        "/v1/agents/AwsFinOpsSmartAgent:invoke",
        json={"prompt": "In one sentence, what can you do? Do not call any tools."},
    )
    if resp.status_code == 502:
        _skip_if_quota(resp.json().get("detail", ""))
    assert resp.status_code == 200, resp.text
    assert resp.json()["output_text"].strip()


@needs_llm
def test_autorouter_retries_runner_up_when_llm_agent_fails(client: httpx.Client) -> None:
    """Lessons-learned #5 in the wild: a quota error inside a 200 from AgentCore
    must not reach the caller as an answer. Either the LLM agent answers, or
    the gateway retries the runner-up and says so in routing.method."""
    body = client.post("/v1/chat", json={"input": "Why did my AWS bill change? Explain the cost drivers."}).json()
    routing = body["routing"]
    assert body["output_text"].strip()
    if routing["method"].endswith("+retry"):
        assert routing["selected_agent"] != "AwsFinOpsSmartAgent"
        assert "retried after runtime failure" in routing["reason"]
    else:
        assert routing["selected_agent"] == "AwsFinOpsSmartAgent"


@needs_denied
def test_runtime_outside_policy_is_refused_by_aws(client: httpx.Client) -> None:
    resp = client.post("/v1/agents/DeniedAgent:invoke", json={"prompt": "hello"})
    assert resp.status_code == 502, resp.text
    assert "AccessDenied" in resp.json()["detail"] or "backend_error" in resp.json()["detail"]
