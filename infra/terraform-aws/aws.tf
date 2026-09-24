################################################################################
# AWS side of the bridge.
#
# Registers Microsoft Entra ID as an OIDC identity provider, then creates an
# IAM role that the AKS-hosted gateway pod can assume by presenting an AAD
# token (via sts:AssumeRoleWithWebIdentity). The role's only permission is to
# invoke the listed Bedrock AgentCore runtimes.
################################################################################

# Pull the AAD OIDC discovery cert chain so we can populate `thumbprint_list`.
# AWS validates well-known OIDC providers against trusted CAs in addition to
# this list, but the argument is still required by the IAM provider resource.
data "tls_certificate" "aad" {
  url = local.aad_issuer_url
}

resource "aws_iam_openid_connect_provider" "aad" {
  url             = local.aad_issuer_url
  client_id_list  = [azuread_application.gateway.client_id]
  thumbprint_list = [data.tls_certificate.aad.certificates[length(data.tls_certificate.aad.certificates) - 1].sha1_fingerprint]

  tags = local.tags
}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "AssumeRoleFromEntraId"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.aad.arn]
    }

    # `aud` of an AAD v2.0 application token equals the App Registration's
    # client_id (a GUID). This is the primary trust-anchor.
    condition {
      test     = "StringEquals"
      variable = "${local.aad_oidc_url}:aud"
      values   = [azuread_application.gateway.client_id]
    }

    # Optional tightening: also pin `sub` to the SP object ID. For the
    # client-credentials / federated workload-identity flow against an App
    # Registration, AAD v2.0 tokens emit `sub` equal to the SP object ID.
    # Disable this on first apply if you cannot decode a sample token yet.
    dynamic "condition" {
      for_each = var.restrict_to_service_principal_subject ? [1] : []
      content {
        test     = "StringEquals"
        variable = "${local.aad_oidc_url}:sub"
        values   = [azuread_service_principal.gateway.object_id]
      }
    }
  }
}

data "aws_iam_policy_document" "invoke" {
  statement {
    sid    = "InvokeAgentCoreRuntimes"
    effect = "Allow"
    actions = concat(
      ["bedrock-agentcore:InvokeAgentRuntime"],
      var.allow_user_id_propagation ? ["bedrock-agentcore:InvokeAgentRuntimeForUser"] : []
    )
    resources = var.agentcore_runtime_arns
  }

  # Permissions for the gateway's classic (no-LLM) FinOps agent. Cost
  # Explorer's IAM model is account-wide ("*" for resources); the read
  # actions below are non-destructive and return aggregate spend data only.
  statement {
    sid    = "ReadCostExplorer"
    effect = "Allow"
    actions = [
      "ce:GetCostAndUsage",
      "ce:GetCostAndUsageWithResources",
      "ce:GetDimensionValues",
      "ce:GetTags",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "gateway" {
  name                 = var.aws_role_name
  description          = "Assumed by AKS-hosted multicloud-ai-agent-gateway via Microsoft Entra ID OIDC federation."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = var.max_session_duration_seconds
  tags                 = local.tags
}

resource "aws_iam_role_policy" "gateway_invoke" {
  name   = "invoke-agentcore"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.invoke.json
}
