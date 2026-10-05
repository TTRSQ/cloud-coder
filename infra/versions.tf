terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # The bucket is passed at init: terraform init -backend-config="bucket=<project>-tfstate"
  backend "gcs" {
    prefix = "cloud-coder"
  }
}
