resource "google_iam_workload_identity_pool" "aks" {
  count = var.enable_aks_workload_identity_federation ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = var.workload_identity_pool_id
  display_name              = "AKS Agent Gateway"
  description               = "Trust boundary for the AKS-hosted agent gateway to impersonate the GCP runtime service account."
  disabled                  = false

  depends_on = [
    google_project_service.required["iam.googleapis.com"],
  ]
}

resource "google_iam_workload_identity_pool_provider" "aks" {
  count = var.enable_aks_workload_identity_federation ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.aks[0].workload_identity_pool_id
  workload_identity_pool_provider_id = var.workload_identity_provider_id
  display_name                       = "AKS OIDC"
  description                        = "OIDC provider for the gateway Kubernetes service account in AKS."

  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }

  attribute_condition = local.aks_service_account_condition

  oidc {
    issuer_uri = trimsuffix(var.aks_oidc_issuer_url, "/")
  }

  depends_on = [
    google_project_service.required["sts.googleapis.com"],
  ]
}

resource "google_service_account_iam_member" "aks_gateway_can_impersonate_runtime" {
  for_each = var.enable_aks_workload_identity_federation ? toset(local.aks_service_account_subjects) : toset([])

  service_account_id = google_service_account.gateway_runtime.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.aks[0].workload_identity_pool_id}/subject/${each.value}"
}
