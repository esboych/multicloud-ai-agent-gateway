# Roadmap

Deliberately not implemented yet, roughly in the order they are likely to
matter.

- **Persistent conversation store.** Today `conversation_id` is a
  transparent pass-through of the Foundry `response.id` or the AgentCore
  `runtimeSessionId`. A store would allow sticky AutoRouter on `:continue`
  and cross-backend threads.
- **End-user identity propagation.** The IAM permission
  (`InvokeAgentRuntimeForUser`) and the `GATEWAY_AWS_BRIDGE_PROPAGATE_USER_ID`
  flag are reserved, but the request shape does not carry a user id yet.
- **Lock the AWS bridge to the service-principal subject.** Set
  `restrict_to_service_principal_subject = true` in `infra/terraform-aws/`
  after confirming the `sub` claim of a real token from the pod.
- **MCP and A2A frontends.** The REST surface is the only one today. The
  `Router.invoke()` funnel was designed so new protocol facades can be added
  under `frontends/` without touching backends.
- **Embedding fallback for the router.** BM25 plus an optional LLM tie-break
  covers the current agent set; an embedding stage would help once agents
  overlap more.
- **Split `agent_chat_ui` into its own package** so its container image does
  not pull the gateway's cloud SDKs.
