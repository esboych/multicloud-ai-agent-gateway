# agent-chat-ui K8s deployment

Browser chat UI in front of the multicloud-ai-agent-gateway. Provides login + chat
as a reference client for the gateway: it routes all
agent invocations through the gateway, so it can call both Foundry agents
(Azure) and AgentCore agents (AWS) from a single dropdown.

```
aks/agent-chat-ui/
├── Dockerfile
├── README.md
└── k8s/
    ├── kustomization.yaml
    ├── serviceaccount.yaml      — minimal, no AAD/AWS need
    ├── configmap.yaml           — GATEWAY_URL + auth tunables (no secrets)
    ├── service.yaml             — ClusterIP :80 → pod :8080 (internal only)
    └── deployment.yaml          — pod spec, expects Secret `agent-chat-ui-auth`
```

## Pre-flight

The chat-ui talks to the gateway over in-cluster HTTP. Gateway must be
running first (see `../multicloud-ai-agent-gateway/`).

The pod expects a Secret with login credentials and a session secret:

```bash
AUTH_PASSWORD="$(openssl rand -base64 36 | tr -d '\n')"
AUTH_SESSION_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
kubectl -n agent-gateway create secret generic agent-chat-ui-auth \
  --from-literal=AUTH_USERNAME=demo \
  --from-literal=AUTH_PASSWORD="$AUTH_PASSWORD" \
  --from-literal=AUTH_SESSION_SECRET="$AUTH_SESSION_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Pick whatever username you want.

## Build the image

Build context is the repository root (single-source — no sibling project
required):

```bash
cd multicloud-ai-agent-gateway
az acr login --name myregistry
IMAGE=myregistry.azurecr.io/agent-chat-ui:$(date -u +%Y%m%d-%H%M)
docker build -f aks/agent-chat-ui/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"
```

Update `image:` in `k8s/deployment.yaml` (or use a kustomize `images:` patch
in your environment overlay).

## Deploy

```bash
kubectl apply -k aks/agent-chat-ui/k8s
kubectl -n agent-gateway rollout status deploy/agent-chat-ui
kubectl -n agent-gateway port-forward svc/agent-chat-ui 8090:80
# open http://localhost:8090
```

The Service is `ClusterIP` on purpose: the login form and session cookie
travel over plain HTTP inside the cluster. To expose the UI beyond
`port-forward`, terminate TLS at an ingress in front of it and set
`AUTH_COOKIE_SECURE=true` (and `TRUST_PROXY_HEADERS=true` if that ingress
sets `X-Forwarded-For`, so login rate limiting keys on the real client).

## What's in the configmap

| Group | Used by |
|---|---|
| `GATEWAY_URL` | Where to send `/v1/agents/<name>:invoke` requests. |
| `REQUEST_TIMEOUT_SECONDS` | Per-request timeout for the call to the gateway. |
| `GATEWAY_BEARER_TOKEN_FILE` / `GATEWAY_BEARER_TOKEN` | Bearer token sent to the gateway when it runs with `GATEWAY_AUTH_MODE=jwt`. Point the file at a projected ServiceAccount token (`audience: multicloud-ai-agent-gateway`); it is re-read per request. |
| `AUTH_COOKIE_SECURE` | Mark the session cookie `Secure`. Set `true` behind a TLS ingress. |
| `TRUST_PROXY_HEADERS` | Use `X-Forwarded-For` as the login rate-limit key. Only behind a proxy that overwrites the header; default `false`. |
| `HEALTH_PORT` | Bind port. |
| `AUTH_SESSION_TTL_SECONDS` / `AUTH_MAX_LOGIN_ATTEMPTS` / `AUTH_LOGIN_WINDOW_SECONDS` / `AUTH_LOCKOUT_SECONDS` | Brute-force protection knobs for the cookie login. |

Login secret values come from the `agent-chat-ui-auth` Secret (separate from
the configmap).

## Design notes

- No cloud SDK dependency, no workload identity, no projected SA token: the
  UI only speaks HTTP to the gateway.
- No background loop. The pod only serves the UI.
- Agent selection goes through the gateway's AutoRouter (`POST /v1/chat`);
  the footer of each reply shows which classifier decided and how confident
  it was, and a `routing-retry` event is rendered when the gateway fell back
  to another agent.
- Cookie name is `agent_chat_ui_session`.

## Removing it

```bash
kubectl delete -k aks/agent-chat-ui/k8s
kubectl -n agent-gateway delete secret agent-chat-ui-auth
```

The gateway is unaffected.
