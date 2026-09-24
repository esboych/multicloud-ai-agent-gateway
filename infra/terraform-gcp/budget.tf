resource "google_billing_budget" "project_guardrail" {
  count = var.enable_budget ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "Agent Gateway ${var.project_id} monthly budget"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.monthly_budget_units)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }

  threshold_rules {
    threshold_percent = 0.8
  }

  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [
    google_project_service.required["billingbudgets.googleapis.com"],
  ]
}
