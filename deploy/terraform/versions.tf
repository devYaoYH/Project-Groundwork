terraform {
  required_version = "= 1.13.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 7.10.0"
    }
  }
  # Created by the administrator, outside this state's ownership.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
