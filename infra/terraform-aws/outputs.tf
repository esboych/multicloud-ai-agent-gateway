output "aws_account_id" {
  description = "AWS account hosting the IAM role and AgentCore runtimes."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "AWS region used for AgentCore."
  value       = var.aws_region
}

output "aws_role_arn" {
  description = "ARN of the IAM role assumed by the gateway pod."
  value       = aws_iam_role.gateway.arn
}

output "aws_oidc_provider_arn" {
  description = "ARN of the AWS OIDC provider that trusts Microsoft Entra ID."
  value       = aws_iam_openid_connect_provider.aad.arn
}

output "aad_tenant_id" {
  description = "Microsoft Entra tenant ID."
  value       = data.azuread_client_config.current.tenant_id
}

output "aad_application_client_id" {
  description = "Entra ID App Registration client_id. Use as token audience GUID."
  value       = azuread_application.gateway.client_id
}

output "aad_service_principal_object_id" {
  description = "SP object ID. Matches the `sub` claim of AAD v2.0 application tokens."
  value       = azuread_service_principal.gateway.object_id
}

output "aad_token_audience_scope" {
  description = "Scope to request from WorkloadIdentityCredential on the gateway pod."
  value       = "api://${var.aad_application_display_name}/.default"
}

output "k8s_configmap_env" {
  description = <<-EOT
    Environment variables to add to the gateway pod's ConfigMap. Names match
    the `GATEWAY_*` settings consumed by multicloud_ai_agent_gateway.settings.

    The gateway code performs the exchange itself (boto3 cannot do AAD):
      1. WorkloadIdentityCredential fetches an AAD token for GATEWAY_AWS_BRIDGE_SCOPE.
      2. sts:AssumeRoleWithWebIdentity exchanges it for STS credentials.
      3. boto3 uses the resulting credentials to call bedrock-agentcore.
  EOT
  value = {
    GATEWAY_AWS_REGION            = var.aws_region
    GATEWAY_AWS_ROLE_ARN          = aws_iam_role.gateway.arn
    GATEWAY_AWS_BRIDGE_TENANT_ID  = data.azuread_client_config.current.tenant_id
    GATEWAY_AWS_BRIDGE_CLIENT_ID  = azuread_application.gateway.client_id
    GATEWAY_AWS_BRIDGE_SCOPE      = "api://${var.aad_application_display_name}/.default"
    GATEWAY_AWS_BRIDGE_TOKEN_FILE = "/var/run/secrets/azure/tokens/azure-identity-token"
  }
}
