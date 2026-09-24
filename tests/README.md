# tests

`pytest` suite for the gateway. Run from the project root:

```bash
pip install -e ".[dev]"
pytest -q
```

All tests are offline. No real Foundry, AAD or AWS calls.

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

## What is not covered (yet)

- Live Foundry / AgentCore / GCP integration. Everything here runs against
  fakes; a live slice would be opt-in (`@pytest.mark.live`).

The pure-function pattern (`run_tool_loop`, `parse_invoke_response`) exists
specifically so the per-backend transport-and-shape logic can be exhaustively
tested without dragging in the Azure or AWS SDKs. When a backend gains a new
edge case, prefer adding a unit case to the corresponding `parse_*` /
`*_loop` test before adding integration cruft.
