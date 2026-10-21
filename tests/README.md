# tests

`pytest` suite for the gateway. Run from the project root:

```bash
pip install -e ".[dev]"
pytest -q                     # unit + in-process API tests (default)
pytest -q -m integration      # black-box: real gateway processes over HTTP
pytest -q -s -m load          # 10 concurrent users against those processes
```

Three layers, all offline (no Foundry, AAD, AWS, GCP or public JWKS):

| Layer | What runs | Where |
|---|---|---|
| unit / in-process | pure functions and FastAPI `TestClient` against the real app, backends faked | `tests/test_*.py` |
| integration | `python -m multicloud_ai_agent_gateway` started as a subprocess on a free port, sqlite conversation store, real HTTP / SSE; a second process with `GATEWAY_AUTH_MODE=jwt` validates tokens against a local OIDC issuer (discovery document + JWKS served over HTTP), so the production `JwksKeyResolver` path is exercised | `tests/integration/test_live_gateway.py` |
| load | `scripts/loadtest.py` driven by pytest: 10 users × 30 requests, mixed `invoke` / `chat` (sticky continuation) / `stream` / `mcp` / `a2a`; once against the instant demo agent, once against a 100 ms-slow agent to prove requests overlap instead of serialising | `tests/integration/test_load.py` |

`GATEWAY_INTEGRATION_URL=http://host:port pytest -m integration` runs the
anonymous black-box tests against an already running gateway (for example
a port-forwarded cluster service); tests that need the fixture's exact
agent list or auth config skip themselves.

`scripts/loadtest.py --url ... --users 25 --requests 40 --mix invoke=4,stream=2`
is the same generator as a standalone tool, with p50 / p95 / p99 per
operation, throughput and a JSON report (`--json`). It measures the gateway
itself; with real cloud agents the numbers are dominated by the model.

## What is covered

| File | Slice |
|---|---|
| `test_registry.py` | `AgentRegistry` (sorting, dup detection, unknown lookup) and `Router` (backend wiring, conversation-id propagation, aclose). |
| `test_foundry_backend.py` | Pure `run_tool_loop`: completed-on-first-turn, tool dispatch + continuation, dispatcher exceptions surfaced as `ToolEvent.error`, `NoDispatcherConfigured`, `ToolBudgetExceeded`, malformed `arguments` JSON, mixed-type `output[]` items. |
| `test_dispatcher_loader.py` | `load_dispatcher("module:attr")` parsing edges. |
| `test_aws_bridge.py` | `AwsBridgeCredentials`: first-call refresh, cache hit on repeat, refresh inside safety margin, AAD failure wrapped as `AwsBridgeError`, STS failure wrapped, naive-tz expiry handled, constructor validation, `aclose` semantics. Uses fakes for AAD credential and STS client (no boto3 networking). |
| `test_agentcore_backend.py` | `parse_invoke_response`: JSON-extraction by known field, fallback to JSON-dump, plain-text passthrough, broken-JSON passthrough, StreamingBody-like body, empty body. `AgentCoreBackend.invoke`: payload shape, `runtimeSessionId` propagation, conversation_id fallback when server omits it, empty remote_id rejection. |
| `test_rest_frontend.py` | FastAPI `TestClient` against the REST app: happy path, conversation continuation, all error → HTTP-status mappings, body validation. |
| `test_settings.py` | env → `Settings`, agent-list parsing, validation errors. |
| `test_demo_agent.py` | Boots the real app with only the `demo_echo` direct agent and drives list / invoke / chat / stream over HTTP. |
| `test_stream_bridge.py` | Sync-to-async streaming bridge: ordering under back-pressure, producer abort on client disconnect, crash → `error` event. |
| `test_gateway_service.py` | `GatewayService`: sticky AutoRouter, conversation ownership, 409 on agent mismatch, tenant policy on listing / routing / retry chain, runner-up retry, `BackendFailure` wrapping, streaming with `routing` / `routing-retry`. |
| `test_conversation_store.py` | `memory` and `sqlite` stores against one contract: first-writer ownership, TTL, size cap, persistence across instances, factory. |
| `test_jwt_auth.py` | `JwtAuthenticator` with locally generated RSA keys: Entra and Kubernetes ServiceAccount token shapes, nested claim paths, audience / expiry / issuer / signature / algorithm-confusion rejections, resolver failures. |
| `test_tenant_policy.py` | `TenantPolicy`: per-tenant and wildcard rules, fail-closed, anonymous callers, validation. |
| `test_rest_auth.py` | REST with a fake authenticator: 401 challenge, `/v1/me`, open health endpoints, policy-filtered listing and 403s, conversation ownership over HTTP. |
| `test_rest_conversations.py` | Sticky `AutoRouter:continue` and `/v1/chat`, 400 on unknown conversation, 409 on wrong agent, `/v1/conversations/{id}`, streams remembering conversations. |
| `test_foundry_replay.py` | Foundry fallback: tool results replayed from the primary attempt (invoke and stream), new tools executed for real, opt-out, stream hold-back switching / releasing / flushing, `_OutputTracker`, `ToolResultRecorder`. |
| `test_mcp_frontend.py` | MCP facade: `initialize` version negotiation, notifications → 202, batch / parse errors, `tools/list`, `tools/call` (agents, AutoRouter, `route_prompt`, conversations, errors), 405 on GET/DELETE, bearer auth. |
| `test_a2a_frontend.py` | A2A facade: agent card (public and authenticated extended), `message/send`, auto-routing, `contextId` continuation, failed tasks, `tasks/get` / `tasks/cancel`, `message/stream` SSE event sequence, error codes, task ownership. |

## What is not covered (yet)

- Live Foundry / AgentCore / GCP integration. Everything here runs against
  fakes; a live slice would be opt-in (`@pytest.mark.live`).

The pure-function pattern (`run_tool_loop`, `parse_invoke_response`) exists
specifically so the per-backend transport-and-shape logic can be exhaustively
tested without dragging in the Azure or AWS SDKs. When a backend gains a new
edge case, prefer adding a unit case to the corresponding `parse_*` /
`*_loop` test before adding integration cruft.
