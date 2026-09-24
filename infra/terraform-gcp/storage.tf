resource "google_storage_bucket" "agent_artifacts" {
  project                     = var.project_id
  name                        = local.artifacts_bucket_name
  location                    = var.storage_location
  force_destroy               = var.force_destroy_artifacts_bucket
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  versioning {
    enabled = true
  }

  depends_on = [
    google_project_service.required["storage.googleapis.com"],
  ]
}

resource "google_storage_bucket_iam_member" "runtime_artifact_reader" {
  bucket = google_storage_bucket.agent_artifacts.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.gateway_runtime.member
}

resource "google_storage_bucket_iam_member" "deployer_artifact_admin" {
  bucket = google_storage_bucket.agent_artifacts.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.gateway_deployer.member
}

