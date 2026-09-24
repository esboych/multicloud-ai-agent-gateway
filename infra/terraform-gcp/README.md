# GCP Agent Gateway foundation

This Terraform module prepares a GCP project for Agent Gateway backends.
It is intentionally a GCP foundation module, not gateway application code.

## Current project

```text
project_id: my-gcp-project
name:       Multicloud AI Agent Gateway GCP
region:     europe-west1
billing:    billingAccounts/XXXXXX-XXXXXX-XXXXXX
```

## What it creates

- Required service APIs for Vertex AI, Cloud Run, Artifact Registry, BigQuery,
  Cloud Asset Inventory, Cloud Logging, Cloud Monitoring, Secret Manager,
  IAM Credentials, STS and Service Usage.
- Artifact Registry Docker repository for gateway / agent images.
- BigQuery dataset intended as the Cloud Billing export target.
- Runtime and deployer service accounts with scoped project IAM.
- Storage bucket for gateway artifacts.
- Optional GCP Workload Identity Federation provider for the AKS-hosted
  `multicloud-ai-agent-gateway` service account.
- Optional monthly budget guardrail.
- Authenticated Cloud Run service for the GCP-only gateway.
- Narrow OIDC token minting grant for local CLI smoke tests against Cloud Run.

## Deploy

```bash
cd infra/terraform-gcp
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

Useful outputs:

```bash
terraform output docker_repository
terraform output gateway_cloud_run_url
terraform output -json k8s_configmap_env
```

Authenticated Cloud Run smoke from a local CLI:

```bash
URL="$(terraform output -raw gateway_cloud_run_url)"
ACCESS_TOKEN="$(gcloud auth print-access-token)"
ID_TOKEN="$(curl -sS -X POST \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/agent-gateway-runtime@my-gcp-project.iam.gserviceaccount.com:generateIdToken" \
  -d "{\"audience\":\"${URL}\",\"includeEmail\":true}" | jq -r .token)"
curl -sS -H "Authorization: Bearer ${ID_TOKEN}" "${URL}/healthz"
```

## Billing export

Terraform creates the target dataset, but Cloud Billing export must be enabled
on the billing account and pointed at that dataset:

```text
billing account: XXXXXX-XXXXXX-XXXXXX
project:         my-gcp-project
dataset:         billing_export
location:        EU
```

The `gcp-direct` backend queries the billing export
table from this dataset. If GKE cost allocation is needed later, enable it on
the relevant GKE clusters before expecting per-namespace / per-label cost data.

## AKS to GCP identity

For production, avoid JSON service account keys. Set:

```hcl
enable_aks_workload_identity_federation = true
aks_oidc_issuer_url = "https://..."
```

Then configure the AKS pod with a projected service account token whose
audience equals:

```bash
terraform output -raw workload_identity_audience
```

The provider trusts this Kubernetes subject by default:

```text
system:serviceaccount:agent-gateway:multicloud-ai-agent-gateway
```

Add more with `additional_aks_k8s_service_accounts` in `terraform.tfvars`.

## How the gateway uses this project

The `gcp-direct` backend (`src/multicloud_ai_agent_gateway/backends/gcp_direct_handlers.py`)
authenticates with the Workload Identity Federation credential file mounted
from `aks/multicloud-ai-agent-gateway/k8s/gcp-wif-credential-config.yaml` and
calls BigQuery (billing export) and Cloud Asset Inventory as the runtime
service account. Agents are declared in `GATEWAY_AGENTS_JSON` with
`"backend_id": "gcp-direct"`; the `GATEWAY_GCP_*` values come from
`terraform output -json k8s_configmap_env`.
