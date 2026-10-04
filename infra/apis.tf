# APIs used by the API server and its build. Never disabled on destroy: other workloads in
# the project may use them too.
locals {
  services = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "iap.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
}

resource "google_project_service" "this" {
  for_each = local.services

  service                    = each.key
  disable_on_destroy         = false
  disable_dependent_services = false
}

import {
  for_each = local.services
  to       = google_project_service.this[each.key]
  id       = "${var.project_id}/${each.key}"
}
