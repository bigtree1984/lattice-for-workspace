terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.30"
    }
  }
  # 状態ファイル（tfstate）の置き場所。会社では GCS のバケットに置くのがふつう（⑧ の決めごと）。
  # backend "gcs" {
  #   bucket = "<会社の tfstate 用バケット>"
  #   prefix = "lattice"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
