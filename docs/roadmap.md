# Roadmap

Deliberately not implemented yet, roughly in the order they are likely to
matter.

- **Shared conversation store.** `memory` is per replica and `sqlite` is
  per volume. A Redis / Cosmos backend behind the same `ConversationStore`
  protocol would make sticky AutoRouter and ownership consistent across
  replicas.
- **Per-tenant cloud principals.** The pod acts with one identity per
  cloud; tenant isolation is enforced by the gateway policy only. AWS role
  chaining into `tenant-<id>` roles, one GCP service account per tenant and
  one Foundry project per tenant are the next step (`docs/multitenancy.md`,
  step B).
- **End-user identity propagation.** The IAM permission
  (`InvokeAgentRuntimeForUser`) and the `GATEWAY_AWS_BRIDGE_PROPAGATE_USER_ID`
  flag are reserved. The principal is now known at the gateway, so the
  remaining work is sending it as the AgentCore user-id header and as
  Foundry `metadata.user_id`.
- **Lock the AWS bridge to the service-principal subject.** Set
  `restrict_to_service_principal_subject = true` in `infra/terraform-aws/`
  after confirming the `sub` claim of a real token from the pod.
- **MCP server-to-client stream and sessions.** The MCP facade is
  stateless request/response; `GET /mcp` returns 405. Progress
  notifications for long tool calls would need the SSE response mode.
- **A2A push notifications and task continuation.** Tasks complete
  synchronously; `tasks/pushNotificationConfig/*` and continuing a task
  with `taskId` (input-required flows) answer with the reserved A2A error
  codes.
- **Scope-based authorisation.** Scopes are parsed into the principal but
  the policy only looks at the tenant. A per-agent `scopes` requirement is
  a small addition once there is a consumer that needs it.
- **Embedding fallback for the router.** BM25 plus an optional LLM tie-break
  covers the current agent set; an embedding stage would help once agents
  overlap more.
- **Split `agent_chat_ui` into its own package** so its container image does
  not pull the gateway's cloud SDKs.
