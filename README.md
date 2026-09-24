# Multicloud AI Agent Gateway

One internal HTTP API in front of AI agents that live on different clouds.
Service consumers call `POST /v1/agents/{name}:invoke` (or `POST /v1/chat`
and let the gateway pick the agent). The gateway takes care of
authenticating to each agent platform with the pod's workload identity,
running Foundry tool-call loops, retrying, falling back, and streaming.

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
  ┌──────────────────────────────────────────────────────────────────────┐
  │  browser ──► agent-chat-ui ─┐                                        │
  │                             │                                        │
  │  service pods ──────────────┴──► multicloud-ai-agent-gateway            │
  │                                  REST frontend (FastAPI)             │
  │                                    GET  /v1/agents                   │
  │                                    POST /v1/chat          (auto)     │
  │                                    POST /v1/route         (dry run)  │
  │                                    POST /v1/agents/{n}:invoke        │
  │                                    POST /v1/agents/{n}:continue      │
  │                                    POST /v1/agents/{n}:stream (SSE)  │
  │                                              │                       │
  │                                  Router: BM25 over agent metadata,   │
  │                                  optional LLM tie-break              │
  │                    ┌───────────────┬─────────┴───────┬──────────────┐│
  │                    ▼               ▼                 ▼              ▼│
  │             FoundryBackend  AgentCoreBackend  DirectBackend  DirectBackend
  │               (Azure)           (AWS)          (gcp-direct)  (in-process)
  │                    │               │                 │              │
  │            Entra workload   Entra → AWS STS    GCP Workload     boto3 via
  │            identity         (OIDC federation)  Identity Fed.    AWS bridge
  └────────────────────┼───────────────┼─────────────────┼──────────────┘
                       ▼               ▼                 ▼
               Microsoft Foundry  Bedrock AgentCore  BigQuery / Asset Inventory
```

`Router.invoke(name, prompt, conversation_id)` is the single funnel: every
frontend terminates there, every backend sits behind it. See
[`src/multicloud_ai_agent_gateway/README.md`](src/multicloud_ai_agent_gateway/README.md)
for the package layout, the full REST contract, error mapping and every
environment variable.

## What it does

- **Cross-cloud identity without secrets.** The pod holds one projected
  Kubernetes ServiceAccount token. From it the gateway obtains an Entra
  access token (Foundry, Azure OpenAI), exchanges an Entra token for
  short-lived AWS STS credentials (AgentCore, Cost Explorer), and federates
  into GCP (BigQuery, Asset Inventory). `infra/terraform-aws/` and
  `infra/terraform-gcp/` create the trust on the AWS and GCP side.
- **Gateway-side Foundry tool loop.** When a Foundry agent requests a
  function, the gateway executes it through a pluggable dispatcher
  (`GATEWAY_FOUNDRY_DISPATCHER_PATH`, contract in
  [`examples/dispatcher_stub.py`](examples/dispatcher_stub.py)) and feeds
  the result back, up to a configurable round budget.
- **Model fallback and retry.** Foundry rate-limit errors can be retried
  through a mapped fallback agent (for example a smaller model). AgentCore
  runtimes that return an error payload with HTTP 200 (quota exhausted) are
  detected, and the request can be retried through a zero-LLM `direct`
  agent. Two limits by design: a fallback re-runs the prompt from scratch,
  so any tool the primary run already executed runs again (keep Foundry
  tools idempotent), and a stream that has already delivered output is
  never restarted on another model.
- **Auto-routing.** `POST /v1/chat` classifies the prompt with BM25 over
  each agent's `description`, `tags` and `example_prompts`; ambiguous
  prompts can be tie-broken by an Azure OpenAI call. New agents get routing
  by adding metadata, not code. `POST /v1/route` shows the decision without
  dispatching.
- **Streaming.** `:stream` returns Server-Sent Events (`routing`, `chunk`,
  `tool_started`, `tool_finished`, `done`, `error`) for every backend,
  including the non-streaming ones.
- **Reference client.** `src/agent_chat_ui/` is a stdlib-only browser chat
  UI with cookie login, kept as a thin gateway client for manual testing.

## Quickstart (no cloud account needed)

```bash
git clone https://github.com/esboych/multicloud-ai-agent-gateway.git
cd multicloud-ai-agent-gateway
python -m venv .venv && source .venv/bin/activate
pip install -c constraints.txt -e ".[dev]"   # constraints = versions the container ships
pytest -q                                    # ~230 offline tests, no network

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
```

Optional: the reference chat UI against the same gateway.

```bash
GATEWAY_URL=http://localhost:8080 HEALTH_PORT=8090 agent-chat-ui
# open http://localhost:8090
```

To talk to real agents, add `foundry` / `agentcore` / `gcp-direct` entries to
`GATEWAY_AGENTS_JSON` and set the matching `GATEWAY_*` variables; the
package README lists them per backend.

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

The gateway itself performs no request authentication. It is meant to sit
on an internal ClusterIP behind a NetworkPolicy; put an authenticating
ingress in front of it if consumers live outside the cluster.

## Testing

```bash
pytest -q          # all offline: fakes for Entra, STS, Foundry, AgentCore, BigQuery
ruff check src tests examples
```

Transport-and-shape logic per backend is factored into pure functions
(`run_tool_loop`, `parse_invoke_response`, the BM25/hybrid classifiers) so
edge cases are unit-tested without cloud SDKs. See
[`tests/README.md`](tests/README.md).

## Docs

- [`docs/lessons-learned.md`](docs/lessons-learned.md): the things that
  cost real time bringing this up (Terraform `azuread` state drift, AKS
  clusters without the Workload Identity webhook, AgentCore IAM ARN shape,
  two Entra tenants, Bedrock quota errors that look like successes).
- [`docs/roadmap.md`](docs/roadmap.md): what is deliberately not built yet.

## License

Apache-2.0, see [`LICENSE`](LICENSE).
