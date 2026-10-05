output "url" {
  description = "Base URL of the API (null until image_tag is set)."
  value       = one(google_cloud_run_v2_service.api[*].uri)
}

output "image" {
  description = "Image name to build and push; append :<tag>."
  value       = "${google_artifact_registry_repository.api.registry_uri}/api"
}

output "service_account" {
  description = "Service account the API runs as."
  value       = google_service_account.api.email
}

output "secrets" {
  description = "Secret Manager secrets whose versions you add with gcloud."
  value       = { for k, s in google_secret_manager_secret.api : k => s.secret_id }
}
