################################################################################
# Microsoft Entra ID side of the bridge.
#
# A dedicated App Registration acts as the federated identity that AAD issues
# tokens for. The gateway pod's Kubernetes ServiceAccount is wired to this
# App Registration via a federated identity credential, so the pod can swap a
# K8s-issued SA token for an AAD v2.0 access token without any client secret.
#
# That AAD token is then handed to AWS STS:AssumeRoleWithWebIdentity.
################################################################################

resource "azuread_application" "gateway" {
  display_name = var.aad_application_display_name

  # Static identifier URI declared inline (instead of via the separate
  # azuread_application_identifier_uri resource) because that resource was
  # observed to silently lose state in Microsoft Graph -- terraform thought
  # the URI was set, Graph reported []. With a static value declared on the
  # parent application, every `terraform apply` reasserts it and the next
  # AAD token request will resolve correctly.
  #
  # This is the resource principal the gateway pod requests a token for:
  #   WorkloadIdentityCredential.get_token("api://...gateway-aws-bridge/.default")
  identifier_uris = ["api://${var.aad_application_display_name}"]

  api {
    # v2 tokens populate `aud` with the application client_id (a GUID).
    # Keep this at 2 so the AWS trust-policy condition matches client_id.
    requested_access_token_version = 2
  }

  feature_tags {
    enterprise = false
    gallery    = false
    hide       = true
  }
}

resource "azuread_service_principal" "gateway" {
  client_id = azuread_application.gateway.client_id
}

resource "azuread_application_federated_identity_credential" "gateway_aks" {
  application_id = azuread_application.gateway.id
  display_name   = "aks-${var.k8s_namespace}-${var.k8s_service_account}"
  description    = "Trust AKS ServiceAccount tokens for the multicloud-ai-agent-gateway pod."

  # Required AAD audience for federated-identity flows.
  audiences = ["api://AzureADTokenExchange"]

  # AKS cluster's OIDC issuer (the cluster issues SA tokens whose `iss` matches).
  issuer = var.aks_oidc_issuer_url

  # Subject the AAD federated credential expects in the K8s SA token's `sub`.
  subject = local.k8s_subject
}
