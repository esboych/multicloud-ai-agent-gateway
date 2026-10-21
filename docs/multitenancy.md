# Multi-tenancy model

Where a tenant's identity lives, what the gateway owns, and what it
deliberately does not. This document is the design behind `GATEWAY_AUTH_*`,
`GATEWAY_TENANT_POLICY_JSON` and the conversation store.

## Three places, kept apart

1. **Identity: the token issuer.** The gateway never stores who a tenant
   is. It trusts tokens from the issuers listed in
   `GATEWAY_AUTH_ISSUERS_JSON` and reads subject, tenant and scopes from
   claims. Two issuers are expected in practice:
   * **Microsoft Entra ID.** It is already the trust anchor for all three
     clouds (pod identity, the AWS bridge, GCP federation), so it is the
     natural source. Tenant is the `tid` claim for separate directories, or
     an app role / custom claim inside one directory.
   * **The Kubernetes cluster itself.** In-cluster callers present a
     projected ServiceAccount token with `audience: multicloud-ai-agent-gateway`;
     the gateway validates it against the AKS OIDC issuer and the tenant is
     the namespace (`tenant_claim: "kubernetes.io/namespace"`). No new
     infrastructure: Kubernetes already issues the identity.
2. **The door: a verified bearer token.** With `GATEWAY_AUTH_MODE=jwt`
   every `/v1`, `/mcp` and `/a2a` request must carry `Authorization: Bearer`.
   The tenant comes only from the verified token, never from the body or a
   custom header. `/healthz`, `/readyz` and the public A2A agent card stay
   open. Validation can also sit on an ingress (KrakenD, Envoy, oauth2-proxy)
   as long as the token reaches the gateway unchanged; the gateway re-verifies.
3. **Policy: the one thing the gateway owns.** `GATEWAY_TENANT_POLICY_JSON`
   maps a tenant to the agents it may use. It is fail-closed: a tenant with
   no rule and no `*` rule gets nothing. Missing tenants are an operator
   mistake, not guests. The conversation store adds ownership: a
   conversation started by one `(subject, tenant)` cannot be continued by
   another, and sticky AutoRouter resolves follow-ups to the owning agent.

```
IdP (Entra / AKS OIDC)  -->  JWT with tenant claim
        |
   gateway: verify signature + audience, tenant := claim      (auth/jwt_auth.py)
        |
   policy: tenant -> allowed agents                           (auth/policy.py)
        |
   service: sticky conversations, ownership, retry chain      (core/service.py)
        |
   backends: one workload identity per pod                    (today)
```

## What this version does not do: per-tenant cloud principals

Downstream of the policy, the pod still acts with **one** identity in each
cloud (Entra UAMI, one AWS IAM role, one GCP service account). Isolation
between tenants is enforced by the gateway, not by cloud IAM. That is the
right trade-off for one organisation with several internal consumers; it is
not enough for tenants that own separate cloud accounts. Three steps up:

| Step | Where the tenant's identity reaches | What changes |
|---|---|---|
| **A. Tenant as data** (this version) | Gateway policy, conversation store, request metadata | Nothing in the clouds. Add end-user id propagation to AgentCore / Foundry when the request shape carries it. |
| **B. Principal per tenant** | Cloud IAM | AWS: the gateway role does `sts:AssumeRole` into `tenant-<id>` roles (or session tags + ABAC). GCP: one service account per tenant, the federated identity gets `roles/iam.serviceAccountTokenCreator` on each. Azure: a Foundry project per tenant, the UAMI gets a role only on that project. A gateway bug can no longer reach another tenant's data. |
| **C. Token exchange end to end** | Cloud IAM, as the end user | Entra on-behalf-of, AWS trust on the same issuer with a tenant claim condition, GCP WIF attribute mapping. Honest and heavy; the agents themselves must support it. |

Recommendation for this stack: start at A (done), move to B one cloud at a
time when a tenant with its own account appears, keep C as a target. Note
that the current AWS trust policy pins `aud` and `sub` of the bridge token,
so distinguishing tenants on the AWS side without role chaining would need
an App Registration per tenant; role chaining is simpler and leaves the
existing bridge untouched.

## Operational notes

* `GET /v1/me` shows what the gateway believes about the caller: subject,
  tenant, scopes and the agents the policy allows. Use it first when a
  tenant reports 403s.
* The conversation store is per replica (`memory`) or per volume
  (`sqlite`). With more than one replica, pin sessions at the ingress or
  accept that sticky routing is best-effort until a shared store exists
  (see `docs/roadmap.md`).
* The reference chat UI authenticates to the gateway with
  `GATEWAY_BEARER_TOKEN_FILE` pointing at its own projected ServiceAccount
  token, so the UI is a tenant like any other in-cluster caller.
