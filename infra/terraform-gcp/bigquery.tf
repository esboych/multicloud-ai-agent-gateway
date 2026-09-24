resource "google_bigquery_dataset" "billing_export" {
  project                    = var.project_id
  dataset_id                 = var.billing_export_dataset_id
  friendly_name              = "Agent Gateway Billing Export"
  description                = "Target dataset for Cloud Billing export tables consumed by GCP FinOps agents."
  location                   = var.bigquery_location
  delete_contents_on_destroy = false
  labels                     = local.labels

  depends_on = [
    google_project_service.required["bigquery.googleapis.com"],
  ]
}

resource "google_bigquery_dataset_iam_member" "runtime_billing_reader" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.billing_export.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = google_service_account.gateway_runtime.member
}

