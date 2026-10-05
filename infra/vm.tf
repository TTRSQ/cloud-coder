# The worker VM is created, started and stopped by cloud-coder itself, so Terraform only
# reads it and grants the API's service account access to this one instance. The VM must
# exist before apply, and a recreated VM needs another apply (its IAM goes with it).
data "google_compute_instance" "worker" {
  name = var.instance
  zone = var.zone
}

# cloud-coder gives the VMs it creates this network tag.
locals {
  vm_network_tag = "cloud-coder"
}

resource "google_compute_firewall" "iap_ssh" {
  name        = "cloud-coder-allow-iap-ssh"
  description = "cloud-coder: SSH from IAP TCP forwarding only"
  network     = "default"
  direction   = "INGRESS"
  priority    = 1000

  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.vm_network_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  depends_on = [google_project_service.this]
}

# What `cloud-coder api` does to the VM: describe, start/resume, stop, and add its SSH key
# to the instance metadata (gcloud compute ssh does that when the key is missing).
resource "google_project_iam_custom_role" "vm_operator" {
  role_id     = "cloudCoderVmOperator"
  title       = "cloud-coder VM operator"
  description = "Start, stop and SSH into the cloud-coder worker VM (granted on the instance only)."
  permissions = [
    "compute.instances.get",
    "compute.instances.resume",
    "compute.instances.setMetadata",
    "compute.instances.start",
    "compute.instances.stop",
  ]

  depends_on = [google_project_service.this]
}

# gcloud compute ssh reads the project (for project-wide SSH keys and OS Login settings).
resource "google_project_iam_custom_role" "project_reader" {
  role_id     = "cloudCoderProjectReader"
  title       = "cloud-coder project reader"
  description = "compute.projects.get, which gcloud compute ssh needs."
  permissions = ["compute.projects.get"]

  depends_on = [google_project_service.this]
}

resource "google_compute_instance_iam_member" "api_vm_operator" {
  zone          = var.zone
  instance_name = data.google_compute_instance.worker.name
  role          = google_project_iam_custom_role.vm_operator.id
  member        = google_service_account.api.member
}

resource "google_project_iam_member" "api_project_reader" {
  project = var.project_id
  role    = google_project_iam_custom_role.project_reader.id
  member  = google_service_account.api.member
}

resource "google_iap_tunnel_instance_iam_member" "api_ssh" {
  zone     = var.zone
  instance = data.google_compute_instance.worker.name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = google_service_account.api.member

  depends_on = [google_project_service.this]
}
