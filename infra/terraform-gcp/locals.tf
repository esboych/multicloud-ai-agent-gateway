locals {
  labels = merge(
    var.labels,
    {
      component = "gcp-agent-foundation"
    },
  )

  required_services = toset(concat(
    [
      "aiplatform.googleapis.com",
      "apikeys.googleapis.com",
      "agentregistry.googleapis.com",
      "artifactregistry.googleapis.com",
      "bigquery.googleapis.com",
      "bigquerystorage.googleapis.com",
      "billingbudgets.googleapis.com",
      "cloudaicompanion.googleapis.com",
      "cloudasset.googleapis.com",
      "cloudbilling.googleapis.com",
      "cloudbuild.googleapis.com",
      "clouddeploy.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "cloudtrace.googleapis.com",
      "connectors.googleapis.com",
      "discoveryengine.googleapis.com",
      "eventarc.googleapis.com",
      "firebase.googleapis.com",
      "firebaseapphosting.googleapis.com",
      "cloudfunctions.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "generativelanguage.googleapis.com",
      "iap.googleapis.com",
      "integrations.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "notebooks.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "serviceusage.googleapis.com",
      "sts.googleapis.com",
      "storage.googleapis.com",
      "telemetry.googleapis.com",
      "workflows.googleapis.com",
    ],
    var.extra_services,
  ))

  billing_account_name  = "billingAccounts/${var.billing_account_id}"
  artifacts_bucket_name = var.artifacts_bucket_name != "" ? var.artifacts_bucket_name : "${var.project_id}-agent-gateway-artifacts"
  aks_service_account_subjects = distinct(concat(
    ["system:serviceaccount:${var.aks_k8s_namespace}:${var.aks_k8s_service_account}"],
    [
      for service_account in var.additional_aks_k8s_service_accounts :
      "system:serviceaccount:${var.aks_k8s_namespace}:${service_account}"
    ],
  ))

  aks_service_account_condition = join(
    " || ",
    [for subject in local.aks_service_account_subjects : "assertion.sub == \"${subject}\""],
  )

  gcp_gateway_agents = [
    {
      name           = "GcpAgent"
      backend_id     = "gcp-direct"
      description    = "Single GCP agent. It routes GCP cost, billing, asset inventory, and resource questions to deterministic Google Cloud tools."
      direct_handler = "gcp_agent"
      tags = [
        "gcp",
        "google",
        "google-cloud",
        "asset",
        "assets",
        "billing",
        "bigquery",
        "buckets",
        "cloud-run",
        "cost",
        "costs",
        "drivers",
        "finops",
        "gke",
        "inventory",
        "month",
        "current",
        "resources",
        "service",
        "services",
        "spend",
        "spending",
        "storage",
        "breakdown",
        "top",
        "deterministic",
        "no-llm",
        "vertex",
      ]
      example_prompts = [
        "Show current month GCP costs",
        "Show current month Google Cloud costs and top cost drivers",
        "What are my GCP costs this month",
        "Google Cloud cost breakdown by service",
        "List GCP resources",
        "Show Google Cloud asset inventory",
        "What resources exist in my GCP project",
        "Summarize GCP assets by type",
        "Give me a GCP overview",
      ]
    },
  ]
}

data "google_project" "current" {
  project_id = var.project_id
}
