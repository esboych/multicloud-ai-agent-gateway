resource "google_artifact_registry_repository" "gateway" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Docker images for the standalone agent gateway and GCP agents."
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [
    google_project_service.required["artifactregistry.googleapis.com"],
  ]
}

