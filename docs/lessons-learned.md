# Lessons learned

Things that cost real time while bringing the gateway up on AKS with
workload identity into three clouds. Recorded so nobody has to rediscover
them.

## 1. `azuread_application_identifier_uri` silently loses state

The standalone Terraform resource reported the identifier URI as set, while
Microsoft Graph returned `identifierUris: []`, both right after `apply` and
hours later without any further apply. The fix that stuck: drop the separate
resource and declare `identifier_uris` inline on `azuread_application` with
a static value (`api://multicloud-ai-agent-gateway-aws-bridge`) that does not
depend on the generated client id. Trade-off: the scope the gateway requests
is a fixed string instead of `api://<client_id>/.default`. AWS-side trust
keys off `aud=<client_id>`, so nothing else changes.

## 2. Not every AKS cluster runs the Workload Identity webhook

The `azure.workload.identity/use: "true"` label plus the ServiceAccount
annotation only inject `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and
`AZURE_FEDERATED_TOKEN_FILE` when the admission webhook is installed. On a
cluster without it, nothing happens and `WorkloadIdentityCredential` fails
with an unhelpful error. The manifests in `aks/multicloud-ai-agent-gateway/k8s/`
therefore set those three variables explicitly and mount the projected
ServiceAccount token themselves, which works on both kinds of cluster.

## 3. AgentCore IAM resource ARNs need a trailing `*`

`bedrock-agentcore:InvokeAgentRuntime` is authorised against
`arn:aws:bedrock-agentcore:<region>:<account>:runtime/<name>/runtime-endpoint/<endpoint>`,
not the bare runtime ARN. A policy that lists `.../runtime/<name>` exactly
gets `AccessDenied`. Use `.../runtime/<name>*` or add the
`/runtime-endpoint/*` sub-resource explicitly. `infra/terraform-aws/` does
this for you.

## 4. Two Entra tenants can be in play at once

The pod's own workload identity (used for Foundry, Azure OpenAI and Azure
data-plane calls) and the App Registration that fronts the AWS bridge do
not have to live in the same tenant. The settings keep them apart on
purpose: `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` for the pod identity,
`GATEWAY_AWS_BRIDGE_TENANT_ID` / `GATEWAY_AWS_BRIDGE_CLIENT_ID` for the
bridge. Mixing them up produces a valid-looking token that AWS rejects on
the `aud` condition.

## 5. Bedrock daily quotas surface as agent errors, not HTTP errors

An AgentCore runtime whose model hit its daily token quota still returns
HTTP 200 with an error payload. The gateway detects the shape
(`AgentReturnedError`) and, when configured, retries through the classic
zero-LLM `direct` agent so the caller gets an answer instead of a stack
trace. The chat-ui shows this as a `routing-retry` event.

## 6. A class with `__len__` is falsy when empty

Both the conversation store and the Foundry tool-result recorder define
`__len__` for diagnostics. Written as `conversations or NullStore()` and
`recorder.recording() if recorder else dispatcher`, an *empty* store was
replaced by the null store and an empty recorder was skipped, so nothing was
ever remembered or replayed, with no error anywhere. Every optional
collaborator is now tested with `is not None`, and the regression tests
assert on the recorded state rather than on the absence of exceptions.

## 7. Bring the AWS side up without a cluster

The bridge can be proven from a laptop before any pod exists:
`create_local_dev_secret = true` in `infra/terraform-aws/` adds a client
secret to the App Registration, the gateway picks it up through
`GATEWAY_AWS_BRIDGE_CLIENT_SECRET`, and everything after the Entra token
(`sts:AssumeRoleWithWebIdentity`, Cost Explorer, AgentCore) is the
production path. Apply with `restrict_to_service_principal_subject = false`
first, let `pytest -m live_aws` print and compare the token's `sub`, then
flip it to `true` and apply again; the test suite re-runs green against
the pinned trust policy. A Bedrock model whose daily token quota is spent
answers HTTP 200 with a `ThrottlingException` body; the live suite reports
that as a skip, and the same condition is what triggers the AutoRouter
runner-up retry in `/v1/chat`.
