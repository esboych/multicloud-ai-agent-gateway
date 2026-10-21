# Multicloud AI Agent Gateway

One internal API in front of AI agents that live on different clouds.
Service consumers call `POST /v1/agents/{name}:invoke` (or `POST /v1/chat`
and let the gateway pick the agent), an MCP client sees every agent as a
tool at `POST /mcp`, and an A2A client finds the same agents as skills
behind `/.well-known/agent-card.json`. The gateway takes care of
authenticating callers, authenticating to each agent platform with the
pod's workload identity, running Foundry tool-call loops, retrying, falling
back, remembering which agent owns a conversation, and streaming.

[![ci](https://github.com/esboych/multicloud-ai-agent-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/esboych/multicloud-ai-agent-gateway/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Backends wired today:

| Backend | Where the agent runs | How the gateway authenticates |
|---|---|---|
| `foundry` | Microsoft Foundry agents (Azure), Responses API | Entra workload identity of the pod |
| `agentcore` | AWS Bedrock AgentCore runtimes | Entra token → `sts:AssumeRoleWithWebIdentity`; no AWS keys in the cluster |
| `gcp-direct` | Deterministic Google Cloud tools (BigQuery billing export, Asset Inventory) | GCP Workload Identity Federation from the same Kubernetes ServiceAccount |
| `direct` | In-process Python handlers, zero LLM (AWS Cost Explorer, a demo echo agent) | Reuses the AWS bridge when needed |

The gateway was built for a FinOps / ML-ops agent family (cost investigation,
training-job agents) but nothing in it is specific to that domain: agents are
declared in one JSON list, and every backend implements the same small
`AgentBackend` protocol.

## Architecture

```
                 Kubernetes cluster (AKS in the reference deployment)
  ┌──────────────────────────────────────────────────────────────────────────┐
  │  browser ──► agent-chat-ui ─┐                                            │
  │  MCP clients ───────────────┤                                            │
  │  A2A clients ───────────────┤                                            │
  │  service pods ──────────────┴──► multicloud-ai-agent-gateway             │
  │                                  Frontends (FastAPI)                     │
  │                                    REST  /v1/agents, /v1/chat, /v1/route │
  │                                          /v1/agents/{n}:invoke|continue  │
  │                                          /v1/agents/{n}:stream (SSE)     │
  │                                          /v1/me, /v1/conversations/{id}  │
  │                                    MCP   POST /mcp   (agents as tools)   │
  │                                    A2A   POST /a2a + agent card          │
  │                                              │                           │
  │                                  Auth: bearer JWT (Entra, AKS OIDC, …)   │
  │                                  Policy: tenant → allowed agents         │
  │                                  GatewayService: sticky conversations,   │
  │                                    ownership, runner-up retry            │
  │                                  Router: BM25 over agent metadata,       │
  │                                    optional LLM tie-break                │
  │                    ┌───────────────┬─────────┴───────┬──────────────┐    │
  │                    ▼               ▼                 ▼              ▼    │
  │             FoundryBackend  AgentCoreBackend  DirectBackend  DirectBackend
  │               (Azure)           (AWS)          (gcp-direct)  (in-process)
  │                    │               │                 │              │    │
  │            Entra workload   Entra → AWS STS    GCP Workload     boto3 via │
  │            identity         (OIDC federation)  Identity Fed.    AWS bridge│
  └────────────────────┼───────────────┼─────────────────┼──────────────┘────┘
                       ▼               ▼                 ▼
               Microsoft Foundry  Bedrock AgentCore  BigQuery / Asset Inventory
```

`GatewayService.invoke(principal, name, prompt, conversation_id)` is the
single funnel: every frontend terminates there, every backend sits behind
`Router` below it. See
[`src/multicloud_ai_agent_gateway/README.md`](src/multicloud_ai_agent_gateway/README.md)
for the package layout, the REST / MCP / A2A contracts, error mapping and
every environment variable.

## What it does

- **Cross-cloud identity without secrets.** The pod holds one projected
  Kubernetes ServiceAccount token. From it the gateway obtains an Entra
  access token (Foundry, Azure OpenAI), exchanges an Entra token for
  short-lived AWS STS credentials (AgentCore, Cost Explorer), and federates
  into GCP (BigQuery, Asset Inventory). `infra/terraform-aws/` and
  `infra/terraform-gcp/` create the trust on the AWS and GCP side.
- **Caller authentication and tenant policy.** With `GATEWAY_AUTH_MODE=jwt`
  every request needs a bearer token from a configured issuer (Microsoft
  Entra ID, the AKS OIDC issuer for in-cluster ServiceAccount tokens, any
  OIDC provider). The tenant is read from a claim (`tid`, or a nested path
  such as `kubernetes.io/namespace`), and `GATEWAY_TENANT_POLICY_JSON` maps
  tenants to the agents they may use, fail-closed. The gateway owns the
  policy, never the identities: see [`docs/multitenancy.md`](docs/multitenancy.md).
- **Conversation store.** Every reply's `conversation_id` is remembered
  with the agent that produced it and the caller that started it. Follow-ups
  through `AutoRouter` stick to that agent instead of being re-classified,
  `:continue` with the wrong agent is a 409, and another caller continuing
  someone else's conversation is a 403. `memory` per replica or `sqlite` on
  a volume.
- **Gateway-side Foundry tool loop.** When a Foundry agent requests a
  function, the gateway executes it through a pluggable dispatcher
  (`GATEWAY_FOUNDRY_DISPATCHER_PATH`, contract in
  [`examples/dispatcher_stub.py`](examples/dispatcher_stub.py)) and feeds
  the result back, up to a configurable round budget.
- **Model fallback and retry, without re-running tools.** Foundry
  rate-limit errors can be retried through a mapped fallback agent (for
  example a smaller model). Tool calls the primary attempt already executed
  are replayed from a per-request cache on the fallback (`replayed: true` in
  `tool_events`), so tools no longer have to be idempotent to be safe. For
  streams, `GATEWAY_FOUNDRY_STREAM_FALLBACK_HOLDBACK_CHARS` holds back the
  first characters so an early rate-limit can still switch models; once
  text has reached the client the stream is committed and the error is
  surfaced instead. AgentCore runtimes that return an error payload with
  HTTP 200 (quota exhausted) are detected, and the request is retried
  through the router's runner-up (for example a zero-LLM `direct` agent).
- **Auto-routing.** `POST /v1/chat` classifies the prompt with BM25 over
  each agent's `description`, `tags` and `example_prompts`; ambiguous
  prompts can be tie-broken by an Azure OpenAI call. New agents get routing
  by adding metadata, not code. `POST /v1/route` shows the decision without
  dispatching. The decision is kept inside the caller's tenant policy.
- **MCP facade.** `POST /mcp` is a Model Context Protocol server
  (Streamable HTTP): one tool per agent, plus `AutoRouter` and a
  `route_prompt` dry run. Point Claude Desktop, Cursor, VS Code or the MCP
  Inspector at it.
- **A2A facade.** `/.well-known/agent-card.json` advertises every agent as
  a skill; `POST /a2a` implements `message/send`, `message/stream` (SSE
  task updates), `tasks/get` and the authenticated extended card.
- **Streaming.** `:stream` returns Server-Sent Events (`routing`,
  `routing-retry`, `chunk`, `tool_started`, `tool_finished`, `done`, `error`)
  for every backend, including the non-streaming ones.
- **Reference client.** `src/agent_chat_ui/` is a stdlib-only browser chat
  UI with cookie login, kept as a thin gateway client for manual testing. It
  authenticates to the gateway with its own projected ServiceAccount token
  (`GATEWAY_BEARER_TOKEN_FILE`).

## Quickstart (no cloud account needed)

```bash
git clone https://github.com/esboych/multicloud-ai-agent-gateway.git
cd multicloud-ai-agent-gateway
python -m venv .venv && source .venv/bin/activate
pip install -c constraints.txt -e ".[dev]"   # constraints = versions the container ships
pytest -q                                    # ~340 offline tests, no network

export GATEWAY_AGENTS_JSON='[{"name":"DemoAgent","backend_id":"direct","direct_handler":"demo_echo","description":"Echo agent for local runs.","tags":["demo","echo","hello"],"example_prompts":["hello","say hi"]}]'
python -m multicloud_ai_agent_gateway            # uvicorn on :8080
```

In another terminal:

```bash
curl -s localhost:8080/v1/agents | jq .
curl -s -X POST localhost:8080/v1/agents/DemoAgent:invoke \
     -H 'Content-Type: application/json' -d '{"prompt":"hello from curl"}' | jq .
curl -s -X POST localhost:8080/v1/chat \
     -H 'Content-Type: application/json' -d '{"input":"say hi"}' | jq .routing
curl -N -X POST localhost:8080/v1/agents/DemoAgent:stream \
     -H 'Content-Type: application/json' -d '{"prompt":"stream me"}'

# the same agent through MCP and A2A (stdlib clients, see examples/)
python examples/mcp_client.py "say hi"
python examples/a2a_client.py "say hi" --stream
```

Optional: the reference chat UI against the same gateway.

```bash
GATEWAY_URL=http://localhost:8080 HEALTH_PORT=8090 agent-chat-ui
# open http://localhost:8090
```

To talk to real agents, add `foundry` / `agentcore` / `gcp-direct` entries to
`GATEWAY_AGENTS_JSON` and set the matching `GATEWAY_*` variables; the
package README lists them per backend.

To require authentication:

```bash
export GATEWAY_AUTH_MODE=jwt
export GATEWAY_AUTH_ISSUERS_JSON='[{"issuer":"https://login.microsoftonline.com/<tenant>/v2.0","audience":"api://multicloud-ai-agent-gateway","tenant_claim":"tid"}]'
export GATEWAY_TENANT_POLICY_JSON='{"<tenant-id>":{"agents":["DemoAgent"]},"*":{"agents":[]}}'
curl -s localhost:8080/v1/me -H "Authorization: Bearer $TOKEN" | jq .
```

## Deploying

- [`aks/multicloud-ai-agent-gateway/`](aks/multicloud-ai-agent-gateway/README.md):
  Dockerfile (build context is the repo root) and kustomize manifests with
  projected ServiceAccount tokens for Azure and GCP, a ClusterIP service and
  an opt-in NetworkPolicy. All identifiers in the manifests are placeholders.
- [`infra/terraform-aws/`](infra/terraform-aws/README.md): Entra App
  Registration + federated credential, AWS OIDC provider, IAM role with a
  least-privilege `bedrock-agentcore:InvokeAgentRuntime` policy. Outputs the
  ConfigMap entries the pod needs.
- [`infra/terraform-gcp/`](infra/terraform-gcp/README.md): project APIs,
  Artifact Registry, BigQuery billing-export dataset, runtime service
  account, Workload Identity Federation pool/provider for the AKS
  ServiceAccount, optional Cloud Run deployment of a GCP-only gateway.
- [`aks/agent-chat-ui/`](aks/agent-chat-ui/README.md): the reference UI.
  Internal `ClusterIP` only; reach it with `port-forward` or put a TLS
  ingress in front (its login is plain HTTP otherwise).

By default (`GATEWAY_AUTH_MODE=none`) the gateway performs no request
authentication and is meant to sit on an internal ClusterIP behind a
NetworkPolicy. Switch on `jwt` mode, or put an authenticating ingress in
front, before consumers outside that boundary can reach it.

## Testing

```bash
pytest -q                 # unit + in-process API tests, all offline (~340)
pytest -q -m integration  # real gateway processes over HTTP, incl. JWT against a local JWKS issuer
pytest -q -s -m load      # 10 concurrent users over REST / SSE / MCP / A2A
python scripts/loadtest.py --url http://localhost:8080 --users 25 --requests 40
ruff check src tests examples scripts
```

Transport-and-shape logic per backend is factored into pure functions
(`run_tool_loop`, `parse_invoke_response`, the BM25/hybrid classifiers,
`JwtAuthenticator` with an injected key resolver) so edge cases are
unit-tested without cloud SDKs or network. See
[`tests/README.md`](tests/README.md).

## Docs

- [`docs/multitenancy.md`](docs/multitenancy.md): where tenant identity
  lives (the issuer), what the gateway owns (policy, conversation
  ownership), and the three steps towards per-tenant cloud principals.
- [`docs/lessons-learned.md`](docs/lessons-learned.md): the things that
  cost real time bringing this up (Terraform `azuread` state drift, AKS
  clusters without the Workload Identity webhook, AgentCore IAM ARN shape,
  two Entra tenants, Bedrock quota errors that look like successes).
- [`docs/roadmap.md`](docs/roadmap.md): what is deliberately not built yet.

## License

Apache-2.0, see [`LICENSE`](LICENSE).
