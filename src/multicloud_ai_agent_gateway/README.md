# multicloud_ai_agent_gateway

Python package for the gateway runtime.

## Layout

```
multicloud_ai_agent_gateway/
├── __init__.py
├── __main__.py             — uvicorn entrypoint (`python -m multicloud_ai_agent_gateway`)
├── app.py                  — app factory: settings → router → service → REST/MCP/A2A
├── settings.py             — pydantic-settings, all env-driven (GATEWAY_* prefix)
├── core/
│   ├── backend.py          — AgentBackend Protocol, AgentReply, ToolEvent, AgentSpec, errors
│   ├── registry.py         — AgentRegistry + Router (name → backend lookup; optional classifier)
│   ├── service.py          — GatewayService: principal + policy + conversations + retry chain
│   ├── conversations.py    — ConversationStore (none / memory / sqlite)
│   ├── routing.py          — KeywordClassifier + AutoRouter sentinel (cloud x domain)
│   ├── bm25.py, hybrid_router.py, llm_router.py — data-driven classifiers
│   └── dispatcher.py       — load_dispatcher("module:attr") helper
├── auth/
│   ├── principal.py        — Principal (subject, tenant, scopes) carried with every request
│   ├── jwt_auth.py         — JwtAuthenticator: OIDC issuers, JWKS, claim paths
│   ├── policy.py           — TenantPolicy: tenant → allowed agents, fail-closed
│   └── aws_bridge.py       — AwsBridgeCredentials: AAD → STS exchange + cache
├── backends/
│   ├── foundry.py          — FoundryBackend, tool loop, fallback replay, stream hold-back
│   ├── agentcore.py        — AgentCoreBackend + pure parse_invoke_response
│   ├── direct.py, direct_handlers.py, gcp_direct_handlers.py — zero-LLM agents
└── frontends/
    ├── common.py           — bearer-token dependency, GatewayError → HTTP status table
    ├── rest.py             — FastAPI router: list / route / invoke / continue / stream / me / conversations
    ├── mcp.py              — Model Context Protocol (Streamable HTTP) facade
    └── a2a.py              — A2A JSON-RPC facade + agent card
```

## Canonical internal call

`GatewayService.invoke(principal, name, prompt, conversation_id) ->
InvokeOutcome` is the single funnel. All frontends terminate here; it
resolves the agent (AutoRouter, sticky conversation, tenant policy), builds
the retry chain and calls `Router.invoke`, behind which all backends
implement the `AgentBackend` Protocol. `GatewayService.prepare_stream`
is the streaming twin: it validates first (so errors become proper HTTP /
JSON-RPC errors) and then yields `routing`, `routing-retry` and backend
events.

## Authentication and tenants

`GATEWAY_AUTH_MODE=none` (default): every caller is the anonymous
principal, the gateway trusts the network boundary.

`GATEWAY_AUTH_MODE=jwt`: every `/v1`, `/mcp` and `/a2a` request needs
`Authorization: Bearer <jwt>` from one of the issuers in
`GATEWAY_AUTH_ISSUERS_JSON`:

```json
[
  {"issuer": "https://login.microsoftonline.com/<dir>/v2.0",
   "audience": "api://multicloud-ai-agent-gateway",
   "tenant_claim": "tid", "label": "entra"},
  {"issuer": "https://<region>.oic.prod-aks.azure.com/<tenant>/<cluster>",
   "audience": ["multicloud-ai-agent-gateway"],
   "tenant_claim": "kubernetes.io/namespace", "label": "in-cluster"}
]
```

Keys are fetched from `jwks_uri` or discovered from
`<issuer>/.well-known/openid-configuration`. `tenant_claim` is a claim name
or a `/`-separated path into nested claims. Scopes are read from `scp`,
`scope` and `roles`. `/healthz`, `/readyz` and the public A2A agent card
stay open.

`GATEWAY_TENANT_POLICY_JSON` maps tenants to agents; `*` is the wildcard
rule and a tenant with no rule gets nothing:

```json
{"<tenant-id>": {"agents": ["AzureFinOpsAgent"]}, "platform": {"agents": ["*"]}, "*": {"agents": []}}
```

`GET /v1/me` reports the resolved principal and its allowed agents. Design
notes: `docs/multitenancy.md`.

## Conversations

Every reply's `conversation_id` (Foundry `response.id`, AgentCore
`runtimeSessionId`, ...) is remembered with the agent that produced it and
the principal that started it:

- `AutoRouter:continue` / `/v1/chat` with a known `conversation_id` go to
  the owning agent (`routing.method == "sticky"`); an unknown id is a 400.
- A concrete agent name that does not own the conversation is a 409.
- With auth on, another `(subject, tenant)` continuing the conversation is
  a 403. Conversations started anonymously stay open.
- `GET /v1/conversations/{id}` shows the record.

`GATEWAY_CONVERSATION_STORE`: `memory` (default, per replica), `sqlite`
(`GATEWAY_CONVERSATION_STORE_PATH`), or `none` for the old pass-through.

## AutoRouter

When `GATEWAY_AUTO_ROUTER_ENABLED` is true (default) the gateway also exposes a
virtual `AutoRouter` agent. Callers can either:

- POST `/v1/chat` with `{ "input": "..." }` — the simplest client contract.
  The gateway classifies the prompt, dispatches to the resolved agent, and
  returns the same `InvokeResponse` shape with `routing` metadata.
- POST `/v1/agents/AutoRouter:invoke` (or `auto`/`router` aliases) — same,
  with an additional `routing` block (`selected_agent`, `reason`, `scores`,
  `mode`, `method`, `confidence`, `candidates`, `requested_agent`).
- POST `/v1/agents/AutoRouter:stream` — same routing logic; the SSE stream is
  prefixed by a single `routing` event before the backend's stream events
  are forwarded. If the selected agent fails before any text was sent, the
  gateway switches to the next candidate and emits `routing-retry`.
- POST `/v1/route` — pure routing decision, no backend dispatch.

`GATEWAY_ROUTER_MODE` selects the classifier: `keyword` (a fixed
cloud x domain keyword table, kept for rollback), `bm25` (data-driven over
each agent's `description` / `tags` / `example_prompts`, so new agents get
routing by adding metadata) or `hybrid` (BM25 plus an optional Azure OpenAI
tie-break on low-confidence prompts; the default). With a tenant policy the
decision is restricted to allowed agents (`method` gets a `+policy` suffix
when the top pick was replaced).

## MCP facade (`POST /mcp`)

Model Context Protocol, Streamable HTTP transport, stateless: one JSON-RPC
message per POST, JSON responses, no session id, `GET /mcp` is 405.
Supported: `initialize` (protocol versions 2025-06-18 / 2025-03-26 /
2024-11-05), `notifications/initialized`, `ping`, `tools/list`,
`tools/call`. Tools: one per agent the caller may use (`prompt`,
optional `conversation_id`), `AutoRouter`, and `route_prompt` (dry run).
`tools/call` returns `content[0].text` = the answer and `structuredContent`
= the REST `InvokeResponse` plus `agent`; agent/backend failures come back
as `isError: true`, an unknown or forbidden tool as JSON-RPC `-32602`.
Example client: `examples/mcp_client.py`.

## A2A facade (`POST /a2a`)

Agent card at `/.well-known/agent-card.json` (also `/.well-known/agent.json`),
`url` built from `GATEWAY_PUBLIC_URL`. Skills = agents (+ `AutoRouter`).
Methods: `message/send` (completed `Task` with one text artifact),
`message/stream` (SSE: `task` submitted, `status-update` working with
routing / tool events, `artifact-update` chunks, final `status-update`),
`tasks/get`, `tasks/cancel` (always `-32002`, tasks are synchronous),
`agent/getAuthenticatedExtendedCard` (skills filtered by tenant policy).
Pick the agent with `metadata.agent` on the params or the message (aliases
`skill`, `agent_name`); omit it to auto-route. `contextId` is the gateway
conversation id. Push notifications, `tasks/resubscribe` and continuing a
task by `taskId` answer with the reserved A2A codes (`-32003`, `-32004`).
Example client: `examples/a2a_client.py`.

## What's not here yet

- Shared conversation store across replicas (memory / sqlite only).
- Per-tenant cloud principals (one workload identity per pod today).
- End-user identity propagation to AgentCore / Foundry (flag reserved).
- MCP server-initiated streams and sessions; A2A push notifications.

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
| `GATEWAY_FOUNDRY_MODEL_FALLBACK_ENABLED` | No | `true` | Retry model rate-limits through the fallback mapping. |
| `GATEWAY_FOUNDRY_FALLBACK_AGENTS_JSON` | No | `{}` | `{"Primary": "Fallback"}` agent-name mapping. |
| `GATEWAY_FOUNDRY_FALLBACK_REPLAY_TOOL_RESULTS` | No | `true` | On fallback, serve tools the primary already ran (same name + arguments) from a per-request cache; `tool_events[].replayed` marks them. |
| `GATEWAY_FOUNDRY_STREAM_FALLBACK_HOLDBACK_CHARS` | No | `0` | Hold back the first N streamed characters so an early rate-limit can still switch to the fallback. `0` streams immediately. |

AgentCore (AWS) side — required only when at least one entry in `GATEWAY_AGENTS_JSON` uses `"backend_id": "agentcore"`. Values come straight from the `infra/terraform-aws/` output `k8s_configmap_env`:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GATEWAY_AWS_REGION` | Yes (if AgentCore) | `""` | AWS region of the AgentCore runtime. |
| `GATEWAY_AWS_ROLE_ARN` | Yes | `""` | IAM role assumed via `sts:AssumeRoleWithWebIdentity`. |
| `GATEWAY_AWS_BRIDGE_TENANT_ID` | Yes | `""` | Microsoft Entra tenant ID. |
| `GATEWAY_AWS_BRIDGE_CLIENT_ID` | Yes | `""` | Entra App Registration client ID (also the AAD token `aud`). |
| `GATEWAY_AWS_BRIDGE_SCOPE` | Yes | `""` | Token scope to request. Must match the App Registration identifier URI, i.e. `api://<aad_application_display_name>/.default` as printed by `terraform output -json k8s_configmap_env` (not `api://<client-id>/.default`). |
| `GATEWAY_AWS_BRIDGE_TOKEN_FILE` | No | `/var/run/secrets/azure/tokens/azure-identity-token` | Path to projected K8s SA token used by `WorkloadIdentityCredential`. |
| `GATEWAY_AWS_BRIDGE_CLIENT_SECRET` | No | `""` | Development only: use `ClientSecretCredential` instead of the projected token so the gateway can run outside AKS (see `infra/terraform-aws/README.md`, local development). |
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

Caller authentication, tenant policy, conversations, facades:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GATEWAY_AUTH_MODE` | No | `none` | `none` or `jwt`. |
| `GATEWAY_AUTH_ISSUERS_JSON` | Yes (if `jwt`) | `[]` | Trusted issuers: `issuer`, `audience` (string or list), optional `jwks_uri`, `tenant_claim` (default `tid`), `subject_claim` (`sub`), `scope_claims` (`scp`, `scope`, `roles`), `algorithms` (`RS256`), `label`. |
| `GATEWAY_AUTH_LEEWAY_SECONDS` | No | `30` | Clock skew tolerated on `exp` / `nbf`. |
| `GATEWAY_AUTH_JWKS_TIMEOUT_SECONDS` | No | `5` | HTTP timeout for OIDC discovery and JWKS fetches. |
| `GATEWAY_TENANT_POLICY_JSON` | No | `{}` | Tenant → `{"agents": [...]}` allow-list, `*` wildcard; empty = everything allowed. |
| `GATEWAY_CONVERSATION_STORE` | No | `memory` | `none`, `memory` or `sqlite`. |
| `GATEWAY_CONVERSATION_STORE_PATH` | Yes (if `sqlite`) | `""` | SQLite file path (mount a volume). |
| `GATEWAY_CONVERSATION_TTL_SECONDS` | No | `86400` | Forget conversations idle longer than this. |
| `GATEWAY_CONVERSATION_MAX_ENTRIES` | No | `10000` | Size cap per store. |
| `GATEWAY_MCP_ENABLED` | No | `true` | Serve `POST /mcp`. |
| `GATEWAY_A2A_ENABLED` | No | `true` | Serve `POST /a2a` and the agent card. |
| `GATEWAY_PUBLIC_URL` | No | `http://localhost:8080` | External base URL advertised in the A2A agent card. |

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
| GET | `/healthz` | — | `{status, backends, auto_router_enabled, auth_enabled}` (no auth) |
| GET | `/readyz` | — | same (no auth) |
| GET | `/v1/agents` | — | `{agents: [{name, backend_id, description, remote_id}]}` filtered by tenant policy; includes `AutoRouter` when usable |
| GET | `/v1/me` | — | `{subject, tenant, issuer, scopes, anonymous, allowed_agents}` |
| GET | `/v1/conversations/{id}` | — | `{conversation_id, agent, owner_subject, owner_tenant, created_at, last_seen_at}` |
| POST | `/v1/route` | `{prompt}` | `{routing: {selected_agent, requested_agent, mode, reason, scores, method, confidence, candidates}}` |
| POST | `/v1/chat` | `{input, conversation_id?}` | `InvokeResponse` via AutoRouter (sticky when the conversation is known) |
| POST | `/v1/agents/{name}:invoke` | `{prompt, conversation_id?}` | `InvokeResponse` (name may be `AutoRouter`/`auto`/`router`) |
| POST | `/v1/agents/{name}:continue` | `{prompt, conversation_id}` | `InvokeResponse`; `AutoRouter` allowed when the store knows the conversation |
| POST | `/v1/agents/{name}:stream` | `{prompt, conversation_id?}` | `text/event-stream` (SSE: `routing` (auto only), `routing-retry`, `chunk`, `tool_started`, `tool_finished`, `done`, `error`) |
| POST | `/mcp` | JSON-RPC | MCP, see above |
| POST | `/a2a` | JSON-RPC | A2A, see above |
| GET | `/.well-known/agent-card.json` | — | A2A agent card (no auth) |

`InvokeResponse`:

```json
{
  "output_text": "...",
  "conversation_id": "resp_01J...",
  "finish_reason": "completed",
  "usage": {"input_tokens": 312, "output_tokens": 88, "total_tokens": 400},
  "tool_events": [
    {"name": "...", "arguments": {...}, "result": {...}, "error": null, "replayed": false}
  ],
  "routing": {
    "selected_agent": "AzureFinOpsAgent",
    "requested_agent": "AutoRouter",
    "mode": "auto",
    "reason": "matched Azure cost/resource terms",
    "scores": {"azure_signal": 1, "aws_signal": 0, "finops_score": 2, "ml_score": 0},
    "method": "bm25",
    "confidence": 0.42,
    "candidates": [{"agent": "AzureFinOpsAgent", "score": 1.0}]
  }
}
```

`routing` is `null` when the request was sent to a concrete agent name and
populated only when AutoRouter (or alias) was used. `method` is `sticky`
when the conversation store resolved the agent, and carries `+retry` /
`+policy` suffixes when a runner-up was used or the top pick was replaced.

## Error mapping

| Backend exception | HTTP status |
|---|---|
| `UnknownAgent` | 404 |
| `NoDispatcherConfigured` | 422 |
| `AutoRouterNotConfigured` | 422 |
| Pydantic validation error on body | 422 |
| `AuthenticationRequired` (no / bad bearer token) | 401 + `WWW-Authenticate: Bearer` |
| `Forbidden` (tenant policy, conversation owned by another caller) | 403 |
| `AutoRouterContinueUnknown` (AutoRouter on `:continue` with an unknown conversation) | 400 |
| `ConversationConflict` (conversation owned by another agent) | 409 |
| `ToolBudgetExceeded` | 504 |
| `AgentReturnedError` | 502 (`agent_returned_error: <type>: <message>`) |
| Anything else | 502 (`backend_error: <ExceptionClass>`) |

MCP and A2A map the same exceptions to JSON-RPC errors (`-32602` for
unknown / forbidden / conflicting targets, `isError` tool results or
`failed` tasks for backend failures).
