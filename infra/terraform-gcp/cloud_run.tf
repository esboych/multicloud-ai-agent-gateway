resource "google_cloud_run_v2_service" "gateway" {
  count = var.deploy_gateway_cloud_run ? 1 : 0

  project             = var.project_id
  name                = var.gateway_cloud_run_service_name
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = local.labels

  template {
    service_account = google_service_account.gateway_runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.gateway_container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "GATEWAY_HEALTH_PORT"
        value = "8080"
      }

      env {
        name  = "GATEWAY_LOG_LEVEL"
        value = "INFO"
      }

      env {
        name  = "GATEWAY_AUTO_ROUTER_ENABLED"
        value = "true"
      }

      env {
        name  = "GATEWAY_ROUTER_MODE"
        value = "bm25"
      }

      env {
        name  = "GATEWAY_AGENTS_JSON"
        value = jsonencode(local.gcp_gateway_agents)
      }

      env {
        name  = "GATEWAY_GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GATEWAY_GCP_REGION"
        value = var.region
      }

      env {
        name  = "GATEWAY_GCP_BILLING_DATASET"
        value = google_bigquery_dataset.billing_export.dataset_id
      }

      env {
        name  = "GATEWAY_GCP_BIGQUERY_LOCATION"
        value = var.bigquery_location
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    # Google sets default service-level scaling fields to zero. The provider
    # cannot clear them, so ignore this computed block and manage revision
    # scaling in template.scaling above.
    ignore_changes = [scaling]
  }

  depends_on = [
    google_project_service.required["run.googleapis.com"],
    google_project_service.required["artifactregistry.googleapis.com"],
  ]
}

resource "google_cloud_run_v2_service_iam_member" "gateway_invoker" {
  for_each = var.deploy_gateway_cloud_run ? toset(var.gateway_invokers) : toset([])

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.gateway[0].name
  role     = "roles/run.invoker"
  member   = each.value
}

resource "google_cloud_run_v2_service_iam_member" "gateway_runtime_self_invoker" {
  count = var.deploy_gateway_cloud_run ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.gateway[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.gateway_runtime.email}"
}

resource "google_service_account_iam_member" "gateway_runtime_oidc_token_creator" {
  for_each = var.deploy_gateway_cloud_run ? toset(var.gateway_oidc_token_creators) : toset([])

  service_account_id = google_service_account.gateway_runtime.name
  role               = "roles/iam.serviceAccountOpenIdTokenCreator"
  member             = each.value
}
