variable "aws_region" {
  type        = string
  description = "AWS region where Bedrock AgentCore runtimes live."
  default     = "us-west-2"
}

variable "aks_oidc_issuer_url" {
  type        = string
  default     = ""
  description = <<-EOT
    AKS cluster OIDC issuer URL, so the App Registration trusts tokens issued
    for the gateway pod's Kubernetes ServiceAccount. Leave empty when there is
    no cluster yet (local development with create_local_dev_secret = true);
    the federated credential is then not created.

    Look it up with:
      az aks show -g <rg> -n <cluster> --query oidcIssuerProfile.issuerURL -o tsv

    Returns something like:
      https://eastus2.oic.prod-aks.azure.com/<tenant-guid>/<cluster-guid>/
  EOT

  validation {
    condition     = can(regex("^https://", var.aks_oidc_issuer_url))
    error_message = "aks_oidc_issuer_url must start with https://."
  }
}

variable "k8s_namespace" {
  type        = string
  description = "Kubernetes namespace of the gateway pod."
  default     = "agent-gateway"
}

variable "k8s_service_account" {
  type        = string
  description = "Kubernetes ServiceAccount name of the gateway pod."
  default     = "multicloud-ai-agent-gateway"
}

variable "aad_application_display_name" {
  type        = string
  description = "Microsoft Entra ID App Registration display name for the AWS bridge identity."
  default     = "multicloud-ai-agent-gateway-aws-bridge"
}

variable "aws_role_name" {
  type        = string
  description = "IAM role name the gateway assumes via AssumeRoleWithWebIdentity."
  default     = "multicloud-ai-agent-gateway-agentcore-invoker"
}

variable "agentcore_runtime_arns" {
  type        = list(string)
  description = <<-EOT
    AgentCore runtime ARNs that the gateway role may invoke. Format:
      arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>

    Use ["*"] to allow all runtimes in the account during initial bring-up.
    Tighten to specific ARNs before going to production.
  EOT
  default     = ["*"]

  validation {
    condition     = length(var.agentcore_runtime_arns) > 0
    error_message = "agentcore_runtime_arns must contain at least one ARN."
  }
}

variable "allow_user_id_propagation" {
  type        = bool
  description = <<-EOT
    Grant bedrock-agentcore:InvokeAgentRuntimeForUser. Required only when the
    gateway sends the X-Amzn-Bedrock-AgentCore-Runtime-User-Id header to
    propagate end-user identity into AgentCore audit trails.
  EOT
  default     = false
}

variable "max_session_duration_seconds" {
  type        = number
  description = "Max session duration for STS credentials returned by AssumeRoleWithWebIdentity."
  default     = 3600

  validation {
    condition     = var.max_session_duration_seconds >= 900 && var.max_session_duration_seconds <= 43200
    error_message = "max_session_duration_seconds must be between 900 (15 min) and 43200 (12 h)."
  }
}

variable "restrict_to_service_principal_subject" {
  type        = bool
  description = <<-EOT
    When true, the IAM trust policy additionally requires the AAD token's `sub`
    claim to equal the App Registration's service principal object ID. This is
    the strict, recommended setting once you have decoded a sample token and
    confirmed the `sub` value (see README troubleshooting).

    Leave false on first apply if you are unsure of the exact `sub` value AAD
    will emit; tighten afterwards.
  EOT
  default     = true
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to AWS resources."
  default     = {}
}

variable "create_local_dev_secret" {
  type        = bool
  description = <<-EOT
    Create a client secret on the App Registration so a gateway running
    outside AKS (a laptop, CI) can obtain the same Entra token with
    ClientSecretCredential (GATEWAY_AWS_BRIDGE_CLIENT_SECRET) instead of a
    projected ServiceAccount token. Development and integration tests only:
    a long-lived secret is exactly what the workload-identity design avoids.
  EOT
  default     = false
}

variable "local_dev_secret_lifetime" {
  type        = string
  description = "Lifetime of the local dev secret as a Terraform duration, e.g. 720h."
  default     = "720h"
}
