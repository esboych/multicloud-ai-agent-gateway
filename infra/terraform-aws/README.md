# Azure ↔ AWS bridge (Microsoft Entra ID OIDC → AWS IAM)

This module wires the AKS-hosted `multicloud-ai-agent-gateway` pod into AWS so it can
invoke Bedrock AgentCore runtimes **without any long-lived AWS credentials in
the cluster**. The pod keeps its existing AAD workload identity and exchanges
each token for short-lived AWS STS credentials on demand.

## What it creates

Microsoft Entra ID side (`azuread` provider):

- `azuread_application.gateway` — purpose-built App Registration. Issues v2.0
  access tokens whose `aud` equals its client_id and whose `sub` equals the
  service principal object ID.
- `azuread_service_principal.gateway` — SP for the App Registration.
- `azuread_application_federated_identity_credential.gateway_aks` — trusts
  the AKS cluster's OIDC issuer for tokens whose `sub` matches
  `system:serviceaccount:<ns>:<sa>`.

AWS side (`aws` provider):

- `aws_iam_openid_connect_provider.aad` — trusts Microsoft Entra ID v2.0
  issuer URL.
- `aws_iam_role.gateway` — assumable via `sts:AssumeRoleWithWebIdentity` from
  the federated AAD token.
- `aws_iam_role_policy.gateway_invoke` — least-privilege inline policy with
  `bedrock-agentcore:InvokeAgentRuntime` (and optionally
  `InvokeAgentRuntimeForUser`) on the configured runtime ARNs.

## Token flow at runtime

```
AKS pod
  │  1. boto3-style call to AgentCore is requested
  ▼
WorkloadIdentityCredential.get_token("api://<aad_application_display_name>/.default")
  │  2. POST AKS-issued SA token to AAD as client_assertion
  ▼
Microsoft Entra ID
  │  3. validates federated identity credential, returns AAD v2.0 access token
  │     (aud = <client_id>, sub = <SP object id>)
  ▼
sts.assume_role_with_web_identity(RoleArn=AWS_ROLE_ARN, WebIdentityToken=...)
  │  4. AWS validates OIDC provider trust + aud/sub conditions
  ▼
Temporary AWS credentials (15 min – 12 h)
  │  5. boto3.client("bedrock-agentcore").invoke_agent_runtime(...)
  ▼
Amazon Bedrock AgentCore Runtime
```

## Prerequisites

1. **AKS cluster has OIDC issuer enabled.** AKS-managed clusters created in
   the last few years have it on by default. Verify:

   ```bash
   az aks show -g <rg> -n <cluster> --query oidcIssuerProfile.enabled
   ```

   If `false`, enable it:

   ```bash
   az aks update -g <rg> -n <cluster> --enable-oidc-issuer --enable-workload-identity
   ```

2. **Caller is logged into both clouds.** This module needs both:

   ```bash
   az login
   aws sso login --profile <profile>           # or any other AWS auth
   export AWS_PROFILE=<profile>
   ```

3. **Caller has rights to:**
   - Microsoft Entra: `Application.ReadWrite.All` (Graph API) — to create the
     App Registration and federated credential.
   - AWS account: `iam:CreateOpenIDConnectProvider`, `iam:CreateRole`,
     `iam:PutRolePolicy`, `iam:TagRole`.

## Deploy

```bash
cd infra/terraform-aws
cp terraform.tfvars.example terraform.tfvars
# fill in aks_oidc_issuer_url and (optionally) agentcore_runtime_arns
terraform init
terraform apply
```

Capture outputs for the gateway pod's K8s ConfigMap:

```bash
terraform output -json k8s_configmap_env
```

## Wire the gateway pod

The pod needs three things added to its `Deployment`:

1. **ConfigMap entries** (from `k8s_configmap_env` output):

   ```yaml
   GATEWAY_AWS_REGION:            us-west-2
   GATEWAY_AWS_ROLE_ARN:          arn:aws:iam::123456789012:role/multicloud-ai-agent-gateway-agentcore-invoker
   GATEWAY_AWS_BRIDGE_TENANT_ID:  <tenant-guid>
   GATEWAY_AWS_BRIDGE_CLIENT_ID:  <app-reg-client-id>
   GATEWAY_AWS_BRIDGE_SCOPE:      api://<aad_application_display_name>/.default   # exactly as the output prints it
   GATEWAY_AWS_BRIDGE_TOKEN_FILE: /var/run/secrets/azure/tokens/azure-identity-token
   ```

2. **Workload identity labels and annotations on the pod's ServiceAccount.**
   The federated identity credential created by this module already names the
   SA. The cluster-side wiring (the K8s SA that gets a projected token AKS
   recognizes for workload identity) is configured outside Terraform via:

   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: multicloud-ai-agent-gateway
     namespace: agent-gateway
     labels:
       azure.workload.identity/use: "true"
   ```

   The pod itself must also carry `azure.workload.identity/use: "true"` on its
   `metadata.labels` so the AKS Workload Identity webhook injects the
   projected token volume.

3. **Python code** — this is already implemented in the gateway as
   `multicloud_ai_agent_gateway.auth.aws_bridge.AwsBridgeCredentials`. It performs
   the AAD->STS exchange with cache-and-refresh-by-expiry semantics. No
   per-team integration code is needed; populating the env vars is enough.

   `boto3`'s built-in OIDC provider chain (`AWS_WEB_IDENTITY_TOKEN_FILE`)
   cannot perform the AAD exchange itself, which is why the gateway does it
   explicitly.

## Validating the trust before going live

Before flipping `restrict_to_service_principal_subject = true`, decode a real
token from the cluster to confirm the `sub` value:

```bash
# from inside the pod (or via kubectl exec):
python -c "
import os, json, base64
from azure.identity import WorkloadIdentityCredential
cred = WorkloadIdentityCredential(
    tenant_id=os.environ['GATEWAY_AWS_BRIDGE_TENANT_ID'],
    client_id=os.environ['GATEWAY_AWS_BRIDGE_CLIENT_ID'],
    token_file_path=os.environ['GATEWAY_AWS_BRIDGE_TOKEN_FILE'],
)
tok = cred.get_token(os.environ['GATEWAY_AWS_BRIDGE_SCOPE']).token
payload = tok.split('.')[1]
payload += '=' * (-len(payload) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(payload)), indent=2))
"
```

Expected claims:

- `iss`: `https://login.microsoftonline.com/<tenant-id>/v2.0`
- `aud`: matches `aad_application_client_id` output
- `sub`: matches `aad_service_principal_object_id` output

If any of those differ, fix the App Registration / federated credential
before tightening the trust policy.

## Smoke test from the pod

```bash
# Inside the gateway pod, after the env vars are wired:
python - <<'PY'
import asyncio, os, boto3
from azure.identity import WorkloadIdentityCredential
from multicloud_ai_agent_gateway.auth.aws_bridge import AwsBridgeCredentials

cred = WorkloadIdentityCredential(
    tenant_id=os.environ["GATEWAY_AWS_BRIDGE_TENANT_ID"],
    client_id=os.environ["GATEWAY_AWS_BRIDGE_CLIENT_ID"],
    token_file_path=os.environ["GATEWAY_AWS_BRIDGE_TOKEN_FILE"],
)
bridge = AwsBridgeCredentials(
    aad_credential=cred,
    scope=os.environ["GATEWAY_AWS_BRIDGE_SCOPE"],
    role_arn=os.environ["GATEWAY_AWS_ROLE_ARN"],
    region=os.environ["GATEWAY_AWS_REGION"],
)
creds = asyncio.run(bridge.get_credentials())
sts = boto3.client(
    "sts",
    aws_access_key_id=creds["AccessKeyId"],
    aws_secret_access_key=creds["SecretAccessKey"],
    aws_session_token=creds["SessionToken"],
    region_name=os.environ["GATEWAY_AWS_REGION"],
)
print(sts.get_caller_identity())
PY
```

A successful response prints the assumed role's ARN — the bridge is live.

## Tearing down

```bash
terraform destroy
```

Removes the OIDC provider, IAM role, App Registration and federated
credential. Does **not** touch any AgentCore runtimes — those are managed in
AWS independently.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `InvalidIdentityToken: Token signature invalid` | Wrong issuer URL on AWS OIDC provider, or AKS issuer mismatch | Re-check `aks_oidc_issuer_url`; confirm AAD token `iss` claim |
| `AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity` | `aud` or `sub` condition mismatch | Decode a token (see above), confirm both claims match outputs |
| `AccessDeniedException: bedrock-agentcore:InvokeAgentRuntime` | Runtime ARN not in `agentcore_runtime_arns` | Add the ARN, `terraform apply` |
| `tls_certificate` data source times out | Terraform host blocked from `login.microsoftonline.com` | Run from a host with public-internet egress; thumbprint can be hardcoded as a workaround if necessary |
| `ExpiredToken` after exactly 1 hour | Code does not refresh STS credentials | Re-call `assume_role_with_web_identity` before `creds["Expiration"]`; do not cache forever |
