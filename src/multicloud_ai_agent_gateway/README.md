# multicloud_ai_agent_gateway

Python package for the gateway runtime.

## Layout

```
multicloud_ai_agent_gateway/
├── __init__.py
├── __main__.py             — uvicorn entrypoint (`python -m multicloud_ai_agent_gateway`)
├── app.py                  — FastAPI app factory, wires settings → router → REST frontend
├── settings.py             — pydantic-settings, all env-driven (GATEWAY_* prefix)
├── core/
│   ├── backend.py          — AgentBackend Protocol, AgentReply, ToolEvent, AgentSpec, errors
│   ├── registry.py         — AgentRegistry + Router (name → backend lookup; optional classifier)
│   ├── routing.py          — KeywordClassifier + AutoRouter sentinel (cloud x domain)
│   └── dispatcher.py       — load_dispatcher("module:attr") helper
├── auth/
│   └── aws_bridge.py       — AwsBridgeCredentials: AAD → STS exchange + cache
├── backends/
│   ├── foundry.py          — FoundryBackend + pure run_tool_loop helper
│   └── agentcore.py        — AgentCoreBackend + pure parse_invoke_response
└── frontends/
    └── rest.py             — FastAPI router: list / route / invoke / continue / stream (SSE) / healthz / readyz
```

## Canonical internal call

`Router.invoke(name, prompt, conversation_id=None) -> AgentReply` is the single
funnel. All frontends terminate here; all backends implement the
`AgentBackend` Protocol.

## AutoRouter

When `GATEWAY_AUTO_ROUTER_ENABLED` is true (default) the gateway also exposes a
virtual `AutoRouter` agent. Callers can either:

- POST `/v1/chat` with `{ "input": "..." }` — the simplest client contract.
  The gateway classifies the prompt, dispatches to the resolved agent, and
  returns the same `InvokeResponse` shape with `routing` metadata.
- POST `/v1/agents/AutoRouter:invoke` (or `auto`/`router` aliases) — the gateway
  classifies the prompt with `KeywordClassifier`, dispatches to the resolved
  agent, and returns the same `InvokeResponse` shape with an additional
  `routing` block (`selected_agent`, `reason`, `scores`, `mode`,
  `requested_agent`).
- POST `/v1/agents/AutoRouter:stream` — same routing logic; the SSE stream is
  prefixed by a single `routing` event (same shape as above) before the
  backend's stream events are forwarded verbatim.
- POST `/v1/route` — pure routing decision, no backend dispatch. Useful for
  showing the user what would be picked before invoking, or for clients that
  want to handle the dispatch themselves.

`/v1/agents/AutoRouter:continue` is rejected (400): a continuation is tied to
the conversation that the original agent owns, so callers must pass that
agent's name explicitly. The selected name is in `routing.selected_agent` of
the previous response.

`GATEWAY_ROUTER_MODE` selects the classifier: `keyword` (a fixed
cloud x domain keyword table, kept for rollback), `bm25` (data-driven over
each agent's `description` / `tags` / `example_prompts`, so new agents get
routing by adding metadata) or `hybrid` (BM25 plus an optional Azure OpenAI
tie-break on low-confidence prompts; the default).

## What's not here yet

- `frontends/mcp.py`, `frontends/a2a.py` — additional protocol facades. Not implemented (no MCP/A2A code, no related dependencies in `pyproject.toml`, no tests).
- Persistent conversation store (today `conversation_id` is a transparent
  pass-through of Foundry `response.id` or AgentCore `runtimeSessionId`).
- End-user identity propagation via `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`
  and Foundry metadata.user_id (the IAM permission and gateway flag are
  reserved but the request shape doesn't carry user_id yet).
- Sticky AutoRouter on `:continue` (keep a `conversation_id → selected_agent`
  map so callers can keep using AutoRouter mid-thread).
- Embedding-based fallback for low-confidence prompts.

See `docs/roadmap.md` at the repository root.

## Required environment

Foundry side:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GATEWAY_FOUNDRY_PROJECT_ENDPOINT` | Yes (if any Foundry agent) | `""` | Foundry project endpoint URL. |
| `GATEWAY_AGENTS_JSON` | Yes | `"[]"` | JSON array of `AgentEntry` objects. |
| `GATEWAY_FOUNDRY_DISPATCHER_PATH` | No | unset | `"module:attr"` path to a sync tool dispatcher. Without it the gateway returns 422 if any Foundry agent requests a function. |
| `GATEWAY_FOUNDRY_MAX_TOOL_ROUNDS` | No | `5` | Tool-loop ceiling. |
| `GATEWAY_FOUNDRY_REQUEST_TIMEOUT_SECONDS` | No | `900` | Per-request HTTP timeout to Foundry. |

AgentCore (AWS) side — required only when at least one entry in `GATEWAY_AGENTS_JSON` uses `"backend_id": "agentcore"`. Values come straight from the `infra/terraform-aws/` output `k8s_configmap_env`:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GATEWAY_AWS_REGION` | Yes (if AgentCore) | `""` | AWS region of the AgentCore runtime. |
| `GATEWAY_AWS_ROLE_ARN` | Yes | `""` | IAM role assumed via `sts:AssumeRoleWithWebIdentity`. |
| `GATEWAY_AWS_BRIDGE_TENANT_ID` | Yes | `""` | Microsoft Entra tenant ID. |
| `GATEWAY_AWS_BRIDGE_CLIENT_ID` | Yes | `""` | Entra App Registration client ID (also the AAD token `aud`). |
| `GATEWAY_AWS_BRIDGE_SCOPE` | Yes | `""` | Token scope to request. Must match the App Registration identifier URI, i.e. `api://<aad_application_display_name>/.default` as printed by `terraform output -json k8s_configmap_env` (not `api://<client-id>/.default`). |
| `GATEWAY_AWS_BRIDGE_TOKEN_FILE` | No | `/var/run/secrets/azure/tokens/azure-identity-token` | Path to projected K8s SA token used by `WorkloadIdentityCredential`. |
| `GATEWAY_AWS_BRIDGE_SESSION_NAME` | No | `multicloud-ai-agent-gateway` | `RoleSessionName` reported in CloudTrail. |
| `GATEWAY_AWS_BRIDGE_REFRESH_MARGIN_SECONDS` | No | `60` | How early to refresh STS creds before `Expiration`. |
| `GATEWAY_AWS_BRIDGE_PROPAGATE_USER_ID` | No | `false` | Reserved for future use; gateway today does not yet propagate end-user IDs. |

Google Cloud direct side — required only when at least one entry in
`GATEWAY_AGENTS_JSON` uses `"backend_id": "gcp-direct"`. Values come from
`infra/terraform-gcp/` outputs.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GATEWAY_GCP_PROJECT_ID` | Yes (if GCP direct) | `""` | GCP project id used by BigQuery and Asset Inventory clients. |
| `GATEWAY_GCP_REGION` | No | `europe-west1` | Default GCP region. |
| `GATEWAY_GCP_BILLING_DATASET` | Yes for cost handler | `""` | BigQuery dataset containing Cloud Billing export tables. |
| `GATEWAY_GCP_BILLING_TABLE` | No | `""` | Explicit billing export table id; empty means auto-discover standard/detailed export table. |
| `GATEWAY_GCP_BIGQUERY_LOCATION` | No | `EU` | BigQuery job location for billing queries. |

Gateway runtime:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GATEWAY_HEALTH_PORT` | No | `8080` | Bind port for uvicorn. |
| `GATEWAY_LOG_LEVEL` | No | `INFO` | stdlib log level. |
| `GATEWAY_AUTO_ROUTER_ENABLED` | No | `true` | Expose the virtual `AutoRouter` agent and `POST /v1/route`. Set to `false` to force callers to always pass concrete agent names. |
| `GATEWAY_ROUTER_MODE` | No | `hybrid` | Router implementation: `keyword`, `bm25`, or `hybrid`. |
| `GATEWAY_ROUTER_LLM_ENDPOINT` | No | `""` | Azure OpenAI endpoint for hybrid tie-breaks, e.g. `https://<resource>.openai.azure.com/`. |
| `GATEWAY_ROUTER_LLM_DEPLOYMENT` | No | `""` | Azure OpenAI chat deployment used only when BM25 is low-confidence. |
| `GATEWAY_ROUTER_LLM_API_VERSION` | No | `2024-08-01-preview` | Azure OpenAI API version. |
| `GATEWAY_ROUTER_LLM_TIMEOUT_SECONDS` | No | `5.0` | Timeout for the optional tie-break call. |

When `GATEWAY_ROUTER_MODE=hybrid`, the LLM tie-breaker is active only when
both endpoint and deployment are set. If no `GATEWAY_ROUTER_LLM_API_KEY` is
provided, the gateway authenticates to Azure OpenAI with the pod's standard
`AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_FEDERATED_TOKEN_FILE` workload
identity env vars, falling back to the AWS bridge App Registration only for
older deployments that do not expose `AZURE_*`.

`GATEWAY_AGENTS_JSON` shape:

```json
[
  {
    "name": "AzureFinOpsAgent",
    "backend_id": "foundry",
    "description": "Read-only Azure cost agent.",
    "foundry_agent_name": "AzureFinOpsAgent"
  },
  {
    "name": "AzureMLTrainingAgent",
    "backend_id": "foundry",
    "foundry_agent_name": "AzureMLTrainingAgent"
  },
  {
    "name": "AwsCostInsightAgent",
    "backend_id": "agentcore",
    "description": "AWS-side cost reasoning agent.",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/abc"
  },
  {
    "name": "GcpAgent",
    "backend_id": "gcp-direct",
    "description": "Single GCP agent that routes cost, billing, inventory, and resource prompts to deterministic Google Cloud tools.",
    "direct_handler": "gcp_agent",
    "tags": ["gcp", "google cloud", "finops", "cost", "spend", "billing", "assets", "inventory", "resources", "bigquery", "vertex", "gke"],
    "example_prompts": [
      "Show current month GCP costs",
      "What are my Google Cloud cost drivers this month?",
      "List GCP resources",
      "Show Google Cloud asset inventory",
      "Give me a GCP overview"
    ]
  }
]
```

## Local dry-run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export GATEWAY_FOUNDRY_PROJECT_ENDPOINT='https://<acct>.services.ai.azure.com/api/projects/<project>'
export GATEWAY_AGENTS_JSON='[{"name":"AzureFinOpsAgent","backend_id":"foundry","foundry_agent_name":"AzureFinOpsAgent"}]'
# Wire in the existing Azure Foundry dispatcher only if it is installed in the
# same Python environment; otherwise leave unset and use prompts that don't
# trigger function calls.
# export GATEWAY_FOUNDRY_DISPATCHER_PATH='examples.dispatcher_stub:dispatch_function_call'

python -m multicloud_ai_agent_gateway
# → uvicorn on :8080
# curl http://localhost:8080/v1/agents
```

## REST API

| Verb | Path | Body | Returns |
|---|---|---|---|
| GET | `/healthz` | — | `{status, backends, auto_router_enabled}` |
| GET | `/readyz` | — | `{status, backends, auto_router_enabled}` |
| GET | `/v1/agents` | — | `{agents: [{name, backend_id, description, remote_id}]}` (includes `AutoRouter` when enabled) |
| POST | `/v1/route` | `{prompt}` | `{routing: {selected_agent, requested_agent, mode, reason, scores}}` |
| POST | `/v1/agents/{name}:invoke` | `{prompt, conversation_id?}` | `InvokeResponse` (name may be `AutoRouter`/`auto`/`router`) |
| POST | `/v1/agents/{name}:continue` | `{prompt, conversation_id}` | `InvokeResponse` (AutoRouter aliases rejected with 400) |
| POST | `/v1/agents/{name}:stream` | `{prompt, conversation_id?}` | `text/event-stream` (SSE: `routing` (auto only), `chunk`, `tool_started`, `tool_finished`, `done`, `error`) |

`InvokeResponse`:

```json
{
  "output_text": "...",
  "conversation_id": "resp_01J...",
  "finish_reason": "completed",
  "usage": {"input_tokens": 312, "output_tokens": 88, "total_tokens": 400},
  "tool_events": [
    {"name": "...", "arguments": {...}, "result": {...}, "error": null}
  ],
  "routing": {
    "selected_agent": "AzureFinOpsAgent",
    "requested_agent": "AutoRouter",
    "mode": "auto",
    "reason": "matched Azure cost/resource terms",
    "scores": {"azure_signal": 1, "aws_signal": 0, "finops_score": 2, "ml_score": 0}
  }
}
```

`routing` is `null` when the request was sent to a concrete agent name and
populated only when AutoRouter (or alias) was used.

## Error mapping

| Backend exception | HTTP status |
|---|---|
| `UnknownAgent` | 404 |
| `NoDispatcherConfigured` | 422 |
| `AutoRouterNotConfigured` | 422 |
| Pydantic validation error on body | 422 |
| AutoRouter on `:continue` | 400 |
| `ToolBudgetExceeded` | 504 |
| Anything else | 502 (`backend_error: <ExceptionClass>`) |
