# Lattice を立てる（Terraform と手作業）

Terraform は GCP の土台（API、イメージ置き場、Firestore、サービスアカウントと権限、シークレットの入れ物、Cloud Run）を作ります。
次のものは Terraform では作れないので、手作業です（★）。

| 順 | やること | 誰が | どこで |
|---|---|---|---|
| 1 | GCP プロジェクトを作り、請求先を紐づける | GCP 管理者 | コンソール |
| 2 ★ | OAuth 同意画面を「内部」で作り、ウェブ用 OAuth クライアントを作る。JSON をダウンロード | GCP 管理者 | コンソール「Google Auth Platform」 |
| 3 | `terraform apply`（下記） | GCP 管理者 | 手元 |
| 4 ★ | シークレットに中身を入れる（下記） | GCP 管理者 | 手元 |
| 5 ★ | 共有ドライブを作り、使う人を「コンテンツ管理者」か「閲覧者」で追加。ID を `drive_id` に | Workspace 管理者 | ドライブ |
| 6 ★ | サンドボックスの入れ物を作り、名前を `sandbox_engine` に（`LATTICE_PROJECT=<project> python scripts/make_sandbox_holder.py`、us-central1。ADC は `gcloud auth application-default login` で用意） | GCP 管理者 | 手元 |
| 7 | イメージを作って載せる（下記） | GCP 管理者 | 手元 |
| 8 ★ | OAuth クライアントのリダイレクト URI に `<url>/auth/callback` を足す | GCP 管理者 | コンソール |
| 9 ★ | 上限予算（spend cap）とアラートを作る。Gemini の不正利用監視のオプトアウトを申請する | 請求の管理者 | コンソール |

## 3. Terraform

```bash
cp terraform.tfvars.example terraform.tfvars   # 会社の値に書き換える
terraform init
terraform apply
```

## 4. シークレットの中身

```bash
gcloud secrets versions add lattice-oauth-client --project=<project> --data-file=<2 でダウンロードした JSON>
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" \
  | gcloud secrets versions add lattice-session-key --project=<project> --data-file=-
```

中身は Terraform に渡さないので、状態ファイル（tfstate）には残りません。

## 5・6 のあと

`terraform.tfvars` に `drive_id` と `sandbox_engine` を入れて、もう一度 `terraform apply`。

## 7. イメージを載せる

```bash
T=$(terraform output -raw image_repo)/app:v1
gcloud builds submit --project=<project> --region=asia-northeast1 --config=../cloudbuild.yaml --substitutions=_IMAGE=$T \
  --service-account=$(terraform output -raw build_service_account) --default-buckets-behavior=regional-user-owned-bucket ..
gcloud run deploy lattice --project=<project> --region=asia-northeast1 --image=$T
```

以降の更新も同じ手順です。Terraform はイメージの変更を無視します。
