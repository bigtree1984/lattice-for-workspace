output "url" {
  description = "Lattice の URL。OAuth クライアントのリダイレクト URI には <url>/auth/callback を登録する"
  value       = google_cloud_run_v2_service.lattice.uri
}

output "image_repo" {
  description = "イメージを置く場所（gcloud builds submit --tag=<これ>/app:<版>）"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.lattice.repository_id}"
}

output "service_account" {
  value = google_service_account.app.email
}

output "build_service_account" {
  description = "gcloud builds submit --service-account=<これ> に渡す"
  value       = google_service_account.build.id
}
