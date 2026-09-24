data "azuread_client_config" "current" {}

data "aws_caller_identity" "current" {}

locals {
  # Microsoft Entra ID v2.0 access tokens are issued with:
  #   iss = https://login.microsoftonline.com/<tenant-id>/v2.0
  # AWS stores OIDC providers without the scheme, and trust-policy condition
  # variables use the same scheme-less form.
  aad_issuer_host = "login.microsoftonline.com"
  aad_issuer_url  = "https://${local.aad_issuer_host}/${data.azuread_client_config.current.tenant_id}/v2.0"
  aad_oidc_url    = "${local.aad_issuer_host}/${data.azuread_client_config.current.tenant_id}/v2.0"

  # Federated-credential subject. Matches AKS-issued ServiceAccount tokens for
  # the gateway pod. AKS emits "system:serviceaccount:<ns>:<sa>" by default.
  k8s_subject = "system:serviceaccount:${var.k8s_namespace}:${var.k8s_service_account}"

  tags = merge(
    {
      Project   = "multicloud-ai-agent-gateway"
      ManagedBy = "terraform"
      Component = "azure-aws-bridge"
    },
    var.tags
  )
}
