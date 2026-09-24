variable "project_id" {
  type        = string
  description = "Existing GCP project that hosts the Agent Gateway GCP foundation."
  default     = "my-gcp-project"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project id."
  }
}

variable "billing_account_id" {
  type        = string
  description = "Billing account id, without the billingAccounts/ prefix. Used only for optional budget creation."
  default     = "XXXXXX-XXXXXX-XXXXXX"
}

variable "region" {
  type        = string
  description = "Primary GCP region for regional services such as Artifact Registry and Cloud Run."
  default     = "europe-west1"
}

variable "bigquery_location" {
  type        = string
  description = "BigQuery location for billing export data. EU keeps data close to europe-west1."
  default     = "EU"
}

variable "storage_location" {
  type        = string
  description = "Cloud Storage location for gateway artifacts."
  default     = "EU"
}

variable "artifact_repository_id" {
  type        = string
  description = "Artifact Registry Docker repository id for gateway and agent images."
  default     = "agent-gateway"
}

variable "billing_export_dataset_id" {
  type        = string
  description = "BigQuery dataset id that receives Cloud Billing export tables."
  default     = "billing_export"
}

variable "artifacts_bucket_name" {
  type        = string
  description = "Optional globally unique bucket name. Empty means '<project_id>-agent-gateway-artifacts'."
  default     = ""
}

variable "force_destroy_artifacts_bucket" {
  type        = bool
  description = "Allow Terraform destroy to delete objects in the artifacts bucket. Keep false for normal use."
  default     = false
}

variable "gateway_runtime_service_account_id" {
  type        = string
  description = "Service account id used by gateway runtime credentials."
  default     = "agent-gateway-runtime"
}

variable "gateway_deployer_service_account_id" {
  type        = string
  description = "Service account id for CI/CD or local deployment automation."
  default     = "agent-gateway-deployer"
}

variable "enable_aks_workload_identity_federation" {
  type        = bool
  description = "Create a GCP Workload Identity Federation provider that trusts the AKS gateway service account."
  default     = false
}

variable "aks_oidc_issuer_url" {
  type        = string
  description = "AKS OIDC issuer URL. Required only when enable_aks_workload_identity_federation=true."
  default     = ""

  validation {
    condition = (
      var.aks_oidc_issuer_url == "" ||
      can(regex("^https://", var.aks_oidc_issuer_url))
    )
    error_message = "aks_oidc_issuer_url must be empty or start with https://."
  }
}

variable "aks_k8s_namespace" {
  type        = string
  description = "Kubernetes namespace of the gateway pod in AKS."
  default     = "agent-gateway"
}

variable "aks_k8s_service_account" {
  type        = string
  description = "Kubernetes ServiceAccount name of the gateway pod in AKS."
  default     = "multicloud-ai-agent-gateway"
}

variable "additional_aks_k8s_service_accounts" {
  type        = list(string)
  description = "Additional Kubernetes ServiceAccounts in the same namespace that may impersonate the GCP runtime service account."
  default     = []
}

variable "workload_identity_pool_id" {
  type        = string
  description = "GCP Workload Identity Pool id for AKS federation."
  default     = "aks-agent-gateway"
}

variable "workload_identity_provider_id" {
  type        = string
  description = "GCP Workload Identity Pool Provider id for AKS federation."
  default     = "aks-oidc"
}

variable "enable_budget" {
  type        = bool
  description = "Create a simple monthly budget guardrail for this project."
  default     = true
}

variable "deploy_gateway_cloud_run" {
  type        = bool
  description = "Deploy the GCP-only Agent Gateway Cloud Run service."
  default     = true
}

variable "gateway_cloud_run_service_name" {
  type        = string
  description = "Cloud Run service name for the GCP-only gateway."
  default     = "agent-gateway-gcp"
}

variable "gateway_container_image" {
  type        = string
  description = "Container image for the GCP-only gateway Cloud Run service."
  default     = "europe-west1-docker.pkg.dev/my-gcp-project/agent-gateway/multicloud-ai-agent-gateway-gcp:latest"
}

variable "gateway_invokers" {
  type        = list(string)
  description = "IAM members allowed to invoke the private Cloud Run gateway."
  default     = ["user:user@example.com"]
}

variable "gateway_oidc_token_creators" {
  type        = list(string)
  description = "IAM members allowed to mint target-audience OIDC tokens for authenticated CLI smoke tests against the Cloud Run gateway."
  default     = ["user:user@example.com"]
}

variable "monthly_budget_units" {
  type        = number
  description = "Monthly budget amount in whole currency units."
  default     = 50
}

variable "budget_currency" {
  type        = string
  description = "Currency for the project budget."
  default     = "EUR"
}

variable "extra_services" {
  type        = list(string)
  description = "Additional service APIs to enable."
  default     = []
}

variable "labels" {
  type        = map(string)
  description = "Labels applied to supported GCP resources."
  default = {
    managed_by = "terraform"
    project    = "multicloud-ai-agent-gateway"
  }
}
