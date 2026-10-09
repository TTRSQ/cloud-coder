variable "project_id" {
  description = "GCP project that holds the worker VM and the API."
  type        = string
}

variable "region" {
  description = "Region of Cloud Run and Artifact Registry."
  type        = string
  default     = "asia-northeast1"
}

variable "zone" {
  description = "Zone of the worker VM."
  type        = string
  default     = "asia-northeast1-b"
}

variable "instance" {
  description = "Name of the worker VM. cloud-coder creates it; Terraform only reads it."
  type        = string
  default     = "cloud-coder"
}

variable "ssh_user" {
  description = "User on the VM (ssh.user in config.yaml)."
  type        = string
  default     = "coder"
}

variable "config_yaml" {
  description = <<-EOT
    The vm, git and claude sections of config.yaml for the API server. Copy them from the
    config.yaml you use locally: the VM agent is reinstalled whenever its settings differ
    from the ones last installed. The gcp and ssh sections come from the other variables.
  EOT
  type        = string
  default     = "{}"

  validation {
    condition = (
      can(keys(yamldecode(var.config_yaml)))
      && length(setintersection(keys(yamldecode(var.config_yaml)), ["gcp", "ssh"])) == 0
    )
    error_message = "config_yaml must be a YAML mapping without gcp or ssh sections."
  }
}

variable "image_tag" {
  description = "Tag of the API image in the Artifact Registry repository. Unset: no Cloud Run service yet."
  type        = string
  default     = null
}

variable "public_url" {
  description = <<-EOT
    URL clients reach the API at, without a trailing slash: the OAuth issuer, and with /mcp
    appended the MCP resource. Unset: the Cloud Run URL
    https://cloud-coder-api-<project number>.<region>.run.app.
  EOT
  type        = string
  default     = null
}

variable "google_oauth_client_id" {
  description = <<-EOT
    Client ID of the Google OAuth client (web application) that signs in whoever approves
    an OAuth grant, with the redirect URI <public URL>/authorize/google/callback. Its secret
    goes into the Secret cloud-coder-api-google-client-secret. Required with image_tag.
  EOT
  type        = string
  default     = null
}

variable "oauth_allowed_subs" {
  description = <<-EOT
    Google account subject IDs (the ID token's `sub`, not e-mail addresses) that may approve
    OAuth grants for /mcp. Empty: nobody can approve; the refusal page shows an account's ID.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for s in var.oauth_allowed_subs : can(regex("^[0-9]+$", s))])
    error_message = "oauth_allowed_subs must be Google subject IDs (digits)."
  }
}
