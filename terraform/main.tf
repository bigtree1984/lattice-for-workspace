# Lattice の GCP の土台。
# Terraform で作れないもの（OAuth の同意画面とクライアント、共有ドライブ、サンドボックスの入れ物、
# シークレットの中身、上限予算）は README.md の手作業の手順で用意する。

locals {
  apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "aiplatform.googleapis.com",
    "drive.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
  ]

  env = merge(
    {
      LATTICE_AGENT              = "gemini"
      LATTICE_PROJECT            = var.project_id
      LATTICE_ALLOWED_DOMAIN     = var.allowed_domain
      LATTICE_MODEL              = var.model
      LATTICE_DAILY_TOKENS_USER  = tostring(var.daily_tokens_user)
      LATTICE_DAILY_TOKENS_TOTAL = tostring(var.daily_tokens_total)
      LATTICE_SESSION_KEY        = var.session_key_name
    },
    var.drive_id != "" ? { LATTICE_DRIVE_ID = var.drive_id } : {},
    var.sandbox_engine != "" ? { LATTICE_SANDBOX_ENGINE = var.sandbox_engine } : {},
    var.login_enabled ? { LATTICE_OAUTH_SECRET = var.oauth_secret_name } : {},
  )
}

# ── API ──
resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ── イメージの置き場 ──
resource "google_artifact_registry_repository" "lattice" {
  location      = var.region
  repository_id = "lattice"
  format        = "DOCKER"
  description   = "Lattice のアプリのイメージ"
  depends_on    = [google_project_service.apis]
}

# ── Firestore（作業の途中の状態・利用量。ファイルの中身は置かない） ──
resource "google_firestore_database" "default" {
  name            = "(default)"
  location_id     = var.region
  type            = "FIRESTORE_NATIVE"
  deletion_policy = "ABANDON" # terraform destroy でもデータは消さない
  depends_on      = [google_project_service.apis]
}

# ── アプリの身分（サービスアカウント）。ドライブの権限は与えない ──
resource "google_service_account" "app" {
  account_id   = "lattice-app"
  display_name = "Lattice アプリ"
  description  = "Cloud Run の Lattice が使う。ドライブは利用者本人の権限で読み書きするので、ここには与えない"
}

resource "google_project_iam_member" "app" {
  for_each = toset([
    "roles/aiplatform.user", # Gemini・Web Grounding・サンドボックス
    "roles/datastore.user",  # Firestore
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.app.email}"
}

# ── シークレット（入れ物だけ。中身は手作業で入れ、Terraform の状態ファイルに残さない） ──
resource "google_secret_manager_secret" "s" {
  for_each  = toset([var.oauth_secret_name, var.session_key_name])
  secret_id = each.value
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "app" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

# ── Web アプリ（Cloud Run） ──
resource "google_cloud_run_v2_service" "lattice" {
  name                 = "lattice"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true # 誰でも開けるが、アプリの Google ログイン（会社のドメインのみ）で守る
  deletion_protection  = true

  template {
    service_account                  = google_service_account.app.email
    timeout                          = "900s"
    max_instance_request_concurrency = 80

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle          = !var.cpu_always_on
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  lifecycle {
    # イメージの更新は gcloud run deploy で行う（⑦ 運用手順）
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      scaling, # サービス全体の台数の設定（GCP が自動で入れる。台数は template の scaling で決める)
    ]
  }

  depends_on = [
    google_project_iam_member.app,
    google_secret_manager_secret_iam_member.app,
    google_firestore_database.default,
  ]
}

# ── イメージを作る身分（Cloud Build）。新しいプロジェクトでは既定の身分に権限が無いので、専用に作る ──
resource "google_service_account" "build" {
  account_id   = "lattice-build"
  display_name = "Lattice のイメージ作成"
}

resource "google_project_iam_member" "build" {
  for_each = toset([
    "roles/logging.logWriter",    # ビルドのログ
    "roles/storage.objectViewer", # アップロードしたソースを読む
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.build.email}"
}

resource "google_artifact_registry_repository_iam_member" "build" {
  location   = google_artifact_registry_repository.lattice.location
  repository = google_artifact_registry_repository.lattice.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}
