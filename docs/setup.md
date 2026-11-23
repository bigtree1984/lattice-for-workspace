# 構築手順書

何もない Google Cloud のプロジェクトから、Lattice を使える状態にするまでの手順です。
この手順は、新しいプロジェクトで最初から最後まで通して確かめてあります（所要 1〜2 時間、待ち時間を含む）。

## 0. 用意するもの

| もの | 内容 |
|---|---|
| 人と権限 | Google Cloud のプロジェクトを作れて、IAM を変えられる人（オーナー相当）。共有ドライブを作れる Google Workspace の利用者。請求先アカウントの管理者 |
| 手元の道具 | `gcloud`（Google Cloud CLI）、Terraform 1.6 以上、Python 3.11（手順 6 だけで使う） |
| このリポジトリ | 自社の非公開リポジトリにコピーしたもの |
| 決めておく値 | [decisions.md](decisions.md) の 1〜4（プロジェクト・地域・ドメイン・共有ドライブ） |

以下、`<project>` は決めたプロジェクト ID、`<region>` は地域（既定 `asia-northeast1`）に読み替えてください。

## 1. プロジェクトを作り、請求先を紐づける

```bash
gcloud projects create <project>
gcloud billing projects link <project> --billing-account=<請求先アカウントの ID>
```

## 2. OAuth の同意画面とクライアント（コンソール）

コンソールで `<project>` を選び、「Google Auth Platform」を開きます。

1. 同意画面を作る。アプリ名（ログインのときに表示される）、サポートのメール、**対象は「内部」**
2. 「クライアント」→「ウェブアプリケーション」を作る。リダイレクト URI は手順 8 で足すので、今は空でよい
3. JSON をダウンロードする。**鍵なので、リポジトリには入れない**

## 3. Terraform で土台を作る

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # project_id と allowed_domain を書く
gcloud auth application-default login
gcloud auth application-default set-quota-project <project>
terraform init
terraform plan -out=tfplan      # 「23 to add」前後になる
terraform apply tfplan
```

- 作られるもの: API、イメージ置き場、Firestore、サービスアカウント2つと権限、シークレットの入れ物2つ、Cloud Run（最初は Google の見本の画面）
- 会社では、状態ファイル（tfstate）を GCS のバケットに置く設定を `versions.tf` で有効にしてください

## 4. シークレットに中身を入れる

```bash
gcloud secrets versions add lattice-oauth-client --project=<project> --data-file=<手順 2 の JSON>
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" \
  | gcloud secrets versions add lattice-session-key --project=<project> --data-file=-
```

入れ終わったら、手元の JSON は消すか、安全な場所に移してください。中身は Terraform に渡さないので、状態ファイルには残りません。

## 5. 共有ドライブを作る（Google ドライブ）

1. 「共有ドライブ」→「新規」で作る
2. 使う人を追加する。編集する人は「コンテンツ管理者」以上、読むだけの人は「閲覧者」
3. 共有ドライブを開いたときの URL の末尾（`/folders/` の後ろ）が ID。`terraform.tfvars` の `drive_id` に書く

## 6. サンドボックスの入れ物を作る

AI の計算機能（表計算など）に使います。地域は us-central1 です（東京は未対応）。

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
LATTICE_PROJECT=<project> .venv/bin/python scripts/make_sandbox_holder.py
```

表示された `projects/<番号>/locations/us-central1/reasoningEngines/<ID>` を `terraform.tfvars` の `sandbox_engine` に書きます。

## 7. 設定を入れ直し、アプリを載せる

```bash
cd terraform
terraform plan -out=tfplan && terraform apply tfplan     # drive_id と sandbox_engine が入る
T=$(terraform output -raw image_repo)/app:v1
gcloud builds submit --project=<project> --region=<region> --config=../cloudbuild.yaml --substitutions=_IMAGE=$T \
  --service-account=$(terraform output -raw build_service_account) --default-buckets-behavior=regional-user-owned-bucket ..
gcloud run deploy lattice --project=<project> --region=<region> --image=$T
```

イメージを作るのは 1〜2 分です。

## 8. OAuth の戻り先を足す（コンソール）

Cloud Run の URL は2つの形があります。両方を、手順 2 のクライアントの「承認済みのリダイレクト URI」に足します。

- `terraform output -raw url` で出る URL + `/auth/callback`
- `https://lattice-<プロジェクト番号>.<region>.run.app/auth/callback`

## 9. 費用の歯止めとデータの扱い（コンソール）

- 「お支払い」→「予算と上限」で、Agent Platform と Cloud Run の上限予算（spend cap）とアラートを作る（[cost.md](cost.md)）
- Gemini の不正利用の監視のためのプロンプトの記録について、Google にオプトアウトを申請する（[SECURITY.md](../SECURITY.md) の 6 章）

## 10. 動作の確認

| 確かめること | 期待すること |
|---|---|
| URL を開く | Google のログインに移る |
| 会社のアカウントでログイン | 同意画面にアプリ名が出て、承認すると Subject の一覧が出る |
| 会社の外のアカウントでログイン | 断られる |
| Subject を作る（「新しく一から」→「雛形から」） | 共有ドライブにフォルダができる |
| Discussion で資料（xlsx など）を添付し、「集計して」と頼む | 表で答えが返る（サンドボックスが動いている） |
| 閲覧者のアカウントで開く | 「閲覧のみ」と出て、書き込む操作ができない |

## うまくいかないとき（実際に起きたもの）

| 症状 | 原因 | 対処 |
|---|---|---|
| `gcloud builds submit` で `storage.objects.get` の権限がない | 新しいプロジェクトでは、Cloud Build の既定の身分に権限が無い | 手順 7 のとおり `--service-account` で `lattice-build` を使う |
| `gcloud builds submit` でログ用のバケットを作れない | 同上 | `--config=../cloudbuild.yaml`（ログは Cloud Logging だけに出す） |
| `make_sandbox_holder.py` で `Reauthentication is needed` | 手元のログインの期限切れ | `gcloud auth application-default login` をやり直す |
| ログインで `redirect_uri_mismatch` | 手順 8 の URI が足りない | エラーに出ている URI をそのまま足す |
| ログイン後に「〜のアカウントでログインしてください」 | `allowed_domain` が違う | `terraform.tfvars` を直して `terraform apply` |
| 一覧が空・Subject を作れない | `drive_id` が無い、またはその人が共有ドライブのメンバーでない | 手順 5・7 |
