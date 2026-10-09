# The HTTP API (`cloud-coder api`) on Cloud Run, reachable from anywhere and protected only
# by its own OAuth grants, which a Google account on the allowlist approves. Secret values
# are added with gcloud, never through Terraform, so they stay out of the state (see
# README.md).

resource "google_service_account" "api" {
  account_id   = "cloud-coder-api"
  display_name = "cloud-coder HTTP API"
  description  = "Runs cloud-coder api on Cloud Run; may operate the cloud-coder VM only."

  depends_on = [google_project_service.this]
}

resource "google_artifact_registry_repository" "api" {
  repository_id = "cloud-coder"
  location      = var.region
  format        = "DOCKER"
  description   = "cloud-coder API images"

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s" # 30 days
    }
  }

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 3
    }
  }

  depends_on = [google_project_service.this]
}

data "google_project" "this" {}

locals {
  # Cloud Run's deterministic URL; the service's own `uri` would make the env var depend on
  # the service itself.
  public_url = coalesce(
    var.public_url,
    "https://cloud-coder-api-${data.google_project.this.number}.${var.region}.run.app",
  )
  secrets = {
    ssh_key            = "cloud-coder-api-ssh-key"
    oauth_signing_keys = "cloud-coder-api-oauth-signing-keys"
  }
  # Environment variables read from the secrets above.
  secret_env = {
    CLOUD_CODER_OAUTH_SIGNING_KEYS = "oauth_signing_keys"
  }
  config_yaml = yamlencode(merge(
    {
      gcp = { project = var.project_id, zone = var.zone, instance = var.instance }
      ssh = { user = var.ssh_user, iap = true }
    },
    yamldecode(var.config_yaml),
  ))
}

resource "google_secret_manager_secret" "api" {
  for_each = local.secrets

  secret_id = each.value
  replication {
    auto {}
  }

  depends_on = [google_project_service.this]
}

resource "google_secret_manager_secret_iam_member" "api" {
  for_each = local.secrets

  secret_id = google_secret_manager_secret.api[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.api.member
}

resource "google_cloud_run_v2_service" "api" {
  count = var.image_tag == null ? 0 : 1

  name     = "cloud-coder-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "600s"
    max_instance_request_concurrency = 10

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "${google_artifact_registry_repository.api.registry_uri}/api:${var.image_tag}"

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "CLOUD_CODER_CONFIG_YAML"
        value = local.config_yaml
      }
      env {
        name  = "CLOUD_CODER_PUBLIC_URL"
        value = local.public_url
      }
      env {
        name  = "CLOUD_CODER_SSH_KEY_FILE"
        value = "/secrets/ssh/google_compute_engine"
      }
      env {
        name  = "CLOUD_CODER_GOOGLE_CLIENT_ID"
        value = var.google_oauth_client_id
      }
      env {
        name  = "CLOUD_CODER_OAUTH_ALLOWED_SUBS"
        value = join(",", var.oauth_allowed_subs)
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.api[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "ssh-key"
        mount_path = "/secrets/ssh"
      }
    }

    volumes {
      name = "ssh-key"
      secret {
        secret = google_secret_manager_secret.api["ssh_key"].secret_id
        items {
          path    = "google_compute_engine"
          version = "latest"
          mode    = 256 # 0400
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.google_oauth_client_id != null
      error_message = "Set google_oauth_client_id (the Google OAuth client ChatGPT signs in with; see README.md)."
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.api,
    google_compute_instance_iam_member.api_vm_operator,
    google_project_iam_member.api_project_reader,
    google_iap_tunnel_instance_iam_member.api_ssh,
  ]
}

# Public: ChatGPT calls it without Google credentials; OAuth protects /mcp.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = length(google_cloud_run_v2_service.api)

  name     = google_cloud_run_v2_service.api[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
