# multicloud-ai-agent-gateway on AKS

Container image and Kubernetes manifests for the gateway pod.

```
aks/multicloud-ai-agent-gateway/
├── Dockerfile
├── README.md
└── k8s/
    ├── kustomization.yaml
    ├── namespace.yaml               — `agent-gateway`
    ├── serviceaccount.yaml          — workload-identity-annotated SA
    ├── configmap.yaml               — gateway runtime env (all values are placeholders)
    ├── gcp-wif-credential-config.yaml — GCP Workload Identity Federation credential file
    ├── service.yaml                 — ClusterIP :80 → pod :8080 (internal only)
    ├── deployment.yaml              — pod spec, projected SA tokens for Azure and GCP
    └── networkpolicy.yaml           — opt-in, applied separately
```

Every identifier in `k8s/` (tenant ids, client ids, account numbers, project
ids, hostnames) is a placeholder. Replace them with the outputs of the
Terraform modules under `infra/` and your own Azure resources before
applying.

## Pre-flight (one-time, manual)

The pod runs as the Kubernetes ServiceAccount
`agent-gateway/multicloud-ai-agent-gateway`. Two Entra identities trust that
subject:

1. **A user-assigned managed identity (UAMI)** for the Azure side. It needs
   `Azure AI User` on the Foundry project, plus whatever data-plane roles
   your Foundry tools require (for the FinOps-style agents: `Reader`,
   `Cost Management Reader`, `Monitoring Reader` on the subscription). Add a
   federated credential on it for the gateway SA:

   ```bash
   AKS_OIDC=$(az aks show -g <rg> -n <cluster> --query oidcIssuerProfile.issuerURL -o tsv)
   az identity federated-credential create \
     --identity-name <gateway-uami> \
     --resource-group <mi-rg> \
     --name multicloud-ai-agent-gateway-aks \
     --issuer "$AKS_OIDC" \
     --subject system:serviceaccount:agent-gateway:multicloud-ai-agent-gateway \
     --audiences api://AzureADTokenExchange
   ```

   Put the UAMI's client id into `serviceaccount.yaml`
   (`azure.workload.identity/client-id`) and into `configmap.yaml`
   (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`).

2. **The App Registration created by `infra/terraform-aws/`**, used for the
   AWS bridge. Its federated credential already targets the same subject.
   Copy the module's `k8s_configmap_env` output into `configmap.yaml`.

For GCP, `infra/terraform-gcp/` creates the Workload Identity Federation
pool and provider. Copy its `k8s_configmap_env` output into
`configmap.yaml` and the credential JSON into
`gcp-wif-credential-config.yaml`.

## Build the image

The build context is the repository root:

```bash
az acr login --name <registry>
IMAGE=<registry>.azurecr.io/multicloud-ai-agent-gateway:$(date -u +%Y%m%d-%H%M)
docker build -f aks/multicloud-ai-agent-gateway/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"
```

Then set `image:` in `k8s/deployment.yaml` (or use a kustomize `images:`
patch in your environment overlay).

If your Foundry agents call tools, the gateway needs a dispatcher
(`GATEWAY_FOUNDRY_DISPATCHER_PATH`). Build a derived image that installs
your dispatcher package on top of this one; `examples/dispatcher_stub.py`
shows the contract.

## Deploy

```bash
az aks get-credentials -g <rg> -n <cluster> --overwrite-existing
kubectl apply -k aks/multicloud-ai-agent-gateway/k8s
```

If the cluster runs Calico or Cilium, also apply the NetworkPolicy:

```bash
kubectl apply -f aks/multicloud-ai-agent-gateway/k8s/networkpolicy.yaml
```

## Smoke test

```bash
kubectl -n agent-gateway port-forward svc/multicloud-ai-agent-gateway 18080:80

curl http://127.0.0.1:18080/healthz
curl http://127.0.0.1:18080/v1/agents
curl http://127.0.0.1:18080/.well-known/agent-card.json      # A2A discovery
curl -X POST http://127.0.0.1:18080/mcp -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'     # MCP tools
curl -X POST http://127.0.0.1:18080/v1/agents/AzureFinOpsAgent:invoke \
     -H 'Content-Type: application/json' \
     -d '{"prompt":"Show current month-to-date Azure costs and top drivers."}'
```

A healthy Foundry path returns an `InvokeResponse` with `output_text`
populated and, if the agent used a tool, one entry in `tool_events`.

## What's in the configmap

| Group | Used by |
|---|---|
| `GATEWAY_FOUNDRY_*`, `GATEWAY_AGENTS_JSON`, `GATEWAY_ROUTER_*` | gateway runtime |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE` | pod workload identity (Foundry, Azure OpenAI). Set explicitly because not every AKS cluster runs the Workload Identity webhook, see `docs/lessons-learned.md`. |
| `GATEWAY_AWS_*` | Entra → AWS STS bridge, from `infra/terraform-aws/` output |
| `GATEWAY_GCP_*` | GCP direct agents, from `infra/terraform-gcp/` output |
| `GATEWAY_AUTH_*`, `GATEWAY_TENANT_POLICY_JSON` | caller authentication (bearer JWT from Entra or the cluster's own OIDC issuer) and per-tenant agent allow-list; off by default, see `docs/multitenancy.md` |
| `GATEWAY_CONVERSATION_*` | conversation store (`memory` per replica, `sqlite` on a volume) |
| `GATEWAY_MCP_ENABLED`, `GATEWAY_A2A_ENABLED`, `GATEWAY_PUBLIC_URL` | MCP and A2A facades; the public URL is advertised in the A2A agent card |

Anything your own Foundry tool dispatcher reads from the environment goes
in the same ConfigMap.
