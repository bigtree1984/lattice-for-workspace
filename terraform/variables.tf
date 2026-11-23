# ── 会社が決める値（⑧）。terraform.tfvars に書く ──

variable "project_id" {
  description = "Lattice を置く GCP プロジェクト（先に作っておく。請求先の紐づけも済ませる）"
  type        = string
}

variable "region" {
  description = "Cloud Run・Firestore・Artifact Registry を置く地域"
  type        = string
  default     = "asia-northeast1"
}

variable "allowed_domain" {
  description = "ログインを許す Google Workspace のドメイン（例: example.co.jp）"
  type        = string
}

variable "drive_id" {
  description = "Lattice が使う共有ドライブの ID（手作業で作る。空なら起動はするが Subject を扱えない）"
  type        = string
  default     = ""
}

variable "sandbox_engine" {
  description = "サンドボックスの入れ物の名前 projects/<番号>/locations/us-central1/reasoningEngines/<ID>（手作業で作る。空なら AI の計算機能なし）"
  type        = string
  default     = ""
}

variable "image" {
  description = "最初に載せるイメージ。ふだんの更新は gcloud run deploy で行い、Terraform はイメージの変更を無視する"
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "model" {
  description = "Gemini のモデル"
  type        = string
  default     = "gemini-3.8-flash"
}

variable "daily_tokens_user" {
  description = "1人あたり1日の Gemini トークン上限"
  type        = number
  default     = 2000000
}

variable "daily_tokens_total" {
  description = "全体の1日の Gemini トークン上限"
  type        = number
  default     = 10000000
}

variable "cpu_always_on" {
  description = "CPU を常時オンにするか（true: 裏の作業が止まらない。費用は増える。REQUIREMENTS F-2）"
  type        = bool
  default     = true
}

variable "max_instances" {
  description = "Cloud Run の最大インスタンス数（いまのロックは1台の中だけで効くので、小さく保つ）"
  type        = number
  default     = 2
}

variable "oauth_secret_name" {
  description = "OAuth クライアントの JSON を入れるシークレットの名前"
  type        = string
  default     = "lattice-oauth-client"
}

variable "session_key_name" {
  description = "ログインの Cookie を暗号化する鍵（Fernet）を入れるシークレットの名前"
  type        = string
  default     = "lattice-session-key"
}

variable "login_enabled" {
  description = "ログインを有効にするか。OAuth クライアントをシークレットに入れてから true にする"
  type        = bool
  default     = true
}
