resource "google_service_account" "gateway_runtime" {
  project      = var.project_id
  account_id   = var.gateway_runtime_service_account_id
  display_name = "Agent Gateway runtime"
  description  = "Runtime identity for GCP agent gateway calls into BigQuery, Asset Inventory, Vertex AI and logs."

  depends_on = [
    google_project_service.required["iam.googleapis.com"],
  ]
}

resource "google_service_account" "gateway_deployer" {
  project      = var.project_id
  account_id   = var.gateway_deployer_service_account_id
  display_name = "Agent Gateway deployer"
  description  = "Deployment identity for pushing images and updating GCP runtime surfaces."

  depends_on = [
    google_project_service.required["iam.googleapis.com"],
  ]
}

locals {
  gateway_runtime_project_roles = toset([
    "roles/aiplatform.user",
    "roles/bigquery.jobUser",
    "roles/cloudasset.viewer",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/serviceusage.serviceUsageConsumer",
  ])

  gateway_deployer_project_roles = toset([
    "roles/artifactregistry.writer",
    "roles/cloudbuild.builds.editor",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageConsumer",
  ])
}

resource "google_project_iam_member" "gateway_runtime" {
  for_each = local.gateway_runtime_project_roles

  project = var.project_id
  role    = each.value
  member  = google_service_account.gateway_runtime.member
}

resource "google_project_iam_member" "gateway_deployer" {
  for_each = local.gateway_deployer_project_roles

  project = var.project_id
  role    = each.value
  member  = google_service_account.gateway_deployer.member
}

resource "google_service_account_iam_member" "deployer_can_use_runtime" {
  service_account_id = google_service_account.gateway_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.gateway_deployer.member
}

