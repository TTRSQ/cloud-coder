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
