output "project_id" {
  description = "GCP project id."
  value       = var.project_id
}

output "project_number" {
  description = "GCP project number."
  value       = data.google_project.current.number
}

output "region" {
  description = "Primary GCP region."
  value       = var.region
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository resource name."
  value       = google_artifact_registry_repository.gateway.name
}

output "docker_repository" {
  description = "Docker repository prefix for image pushes."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.gateway.repository_id}"
}

output "billing_export_dataset" {
  description = "BigQuery dataset id intended for Cloud Billing export."
  value       = google_bigquery_dataset.billing_export.dataset_id
}

output "billing_export_dataset_location" {
  description = "BigQuery dataset location."
  value       = google_bigquery_dataset.billing_export.location
}

output "agent_artifacts_bucket" {
  description = "Cloud Storage bucket for gateway artifacts."
  value       = google_storage_bucket.agent_artifacts.name
}

output "gateway_runtime_service_account_email" {
  description = "Runtime service account email."
  value       = google_service_account.gateway_runtime.email
}

output "gateway_deployer_service_account_email" {
  description = "Deployer service account email."
  value       = google_service_account.gateway_deployer.email
}

output "workload_identity_provider_name" {
  description = "AKS Workload Identity Federation provider resource name, if enabled."
  value       = try(google_iam_workload_identity_pool_provider.aks[0].name, null)
}

output "workload_identity_audience" {
  description = "Audience value to use in a projected Kubernetes serviceAccountToken for GCP federation, if enabled."
  value       = try("//iam.googleapis.com/${google_iam_workload_identity_pool_provider.aks[0].name}", null)
}

output "aks_trusted_subjects" {
  description = "Kubernetes service account subjects trusted by the GCP WIF provider, if enabled."
  value       = var.enable_aks_workload_identity_federation ? local.aks_service_account_subjects : []
}

output "k8s_configmap_env" {
  description = "Environment values consumed by the gateway once the GCP backend is implemented."
  value = {
    GATEWAY_GCP_PROJECT_ID              = var.project_id
    GATEWAY_GCP_REGION                  = var.region
    GATEWAY_GCP_BILLING_DATASET         = google_bigquery_dataset.billing_export.dataset_id
    GATEWAY_GCP_RUNTIME_SERVICE_ACCOUNT = google_service_account.gateway_runtime.email
    GATEWAY_GCP_WORKLOAD_PROVIDER       = try(google_iam_workload_identity_pool_provider.aks[0].name, "")
  }
}

output "gateway_cloud_run_url" {
  description = "Authenticated Cloud Run URL for the GCP-only gateway, if deployed."
  value       = try(google_cloud_run_v2_service.gateway[0].uri, null)
}
