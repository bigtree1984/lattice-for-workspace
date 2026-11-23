# 運用手順書

Lattice を動かし続けるための手順です。立てる手順は [setup.md](setup.md) を見てください。
以下、`<project>` はプロジェクト ID、`<region>` は地域（既定 `asia-northeast1`）です。コマンドは `terraform/` の中で打つ前提です。

## 1. 日々の運用で、することはほぼ無い

- 使っていない間、Cloud Run は 0 台になり、費用もほぼかかりません。
- 定期的に見るのは、月の費用（4章）と、脆弱性の知らせ（3章）くらいです。
- 利用者の追加・削除は、共有ドライブのメンバーを変えるだけです（2章）。

## 2. 利用者を追加する・外す

Lattice に利用者の名簿はありません。**共有ドライブのメンバーがそのまま利用者**です。

| したいこと | すること |
|---|---|
| 使える人を足す | 共有ドライブに追加する。編集する人は「コンテンツ管理者」以上、読むだけの人は「閲覧者」 |
| 読むだけにする | 共有ドライブでの権限を「閲覧者」に下げる。Lattice の画面に「閲覧のみ」と出る |
| 外す | 共有ドライブから外す。ログインはできても、Subject は見えなくなる |
| 担当（owner）を替える | Subject の画面の「担当を変える」 |

ログインできるのは、`allowed_domain` のドメインのアカウントだけです。

## 3. アプリを更新する（新しい版・脆弱性の対応）

### 3.1 載せる前に

作業の途中のもの（AI が返事を書いている、組み直しの途中など）があるときに載せ替えると、その作業が止まることがあります。
人が少ない時間に行い、気になる場合は利用者に一声かけてください。止まった組み直しは、画面の「続ける」で再開できます。

### 3.2 載せる

```bash
T=$(terraform output -raw image_repo)/app:<版の名前>      # 例: v2、2026-12-01
gcloud builds submit --project=<project> --region=<region> --config=../cloudbuild.yaml --substitutions=_IMAGE=$T \
  --service-account=$(terraform output -raw build_service_account) --default-buckets-behavior=regional-user-owned-bucket ..
gcloud run deploy lattice --project=<project> --region=<region> --image=$T
```

`gcloud run deploy` はイメージだけを替え、CPU 常時オンなどの設定は保ちます。Terraform はイメージの違いを無視するので、`terraform apply` と混ざっても戻りません。

### 3.3 脆弱性の対応

1. Artifact Registry の脆弱性スキャンの結果、または使っているライブラリの知らせを見る
2. 手元で `requirements.txt` の版を上げる（土台のイメージ `python:3.11-slim` は、作り直すたびに最新になる）
3. テストを流す: `.venv/bin/python -m pytest -q tests`
4. 3.2 で載せる

### 3.4 前の版に戻す

```bash
gcloud run revisions list --service=lattice --project=<project> --region=<region>
gcloud run services update-traffic lattice --project=<project> --region=<region> --to-revisions=<戻したいリビジョン>=100
```

戻したあとに次の版を載せると、新しい版に 100% が移ります。

## 4. 費用を見る

- コンソールの「お支払い」→「レポート」で、プロジェクト・サービスごとに見ます（[cost.md](cost.md)）。
- 上限予算のアラートのメールが来たら、レポートで Vertex AI の増え方を見ます。多くは「組み直して移す」や長い会話です。
- アプリの1日の上限を変えるには、`terraform.tfvars` の `daily_tokens_user` / `daily_tokens_total` を変えて `terraform apply`。

## 5. 設定を変える

設定の一覧は [architecture.md](architecture.md) の 7 章です。設定はすべて Terraform から入れます。

```bash
# terraform.tfvars を直してから
terraform plan -out=tfplan     # 変わるところだけが出ることを確かめる
terraform apply tfplan
```

Cloud Run の画面や `gcloud run services update` で直接変えると、次の `terraform apply` で戻ります。

## 6. 鍵を替える

| 鍵 | 替え方 | 影響 |
|---|---|---|
| セッションの鍵 | setup.md 4 章の2つめのコマンドで新しい版を足し、3.2 の `gcloud run deploy` で同じイメージを載せ直す | 全員が一度ログインし直す |
| OAuth クライアントの秘密 | コンソールでクライアントの秘密を新しく作り、JSON をシークレットの新しい版に入れて、載せ直す。古い秘密はコンソールで無効にする | 全員が一度ログインし直す |

## 7. Subject を片付ける

Subject は共有ドライブで、フォルダをゴミ箱に移せば一覧から消えます。
ただし、Firestore に**作業の状態（会話中の記録を含むことがある）**が残ります。画面には出ませんが、次の道具で消してください。

```bash
LATTICE=<project>; DRIVE=<共有ドライブの ID>
.venv/bin/python scripts/cleanup_state.py $LATTICE $DRIVE <Subject>            # 消すものを表示するだけ
.venv/bin/python scripts/cleanup_state.py $LATTICE $DRIVE <Subject> --delete   # 消す
```

ゴミ箱のフォルダは約30日で完全に消えます。それまでは共有ドライブのゴミ箱から戻せます（Firestore の状態を消していても、Subject は使えます）。

## 8. 困ったとき

| 症状 | まず見るところ | よくある原因と対処 |
|---|---|---|
| だれもログインできない | Cloud Run のログ、OAuth の設定 | `allowed_domain` が空・違う。OAuth のシークレットの版が無い |
| ある人だけ使えない | その人の共有ドライブでの権限 | メンバーでない、または閲覧者 |
| AI が返事をしない・止まる | 会話の画面のメッセージ | 1日の上限に達した（翌日に戻る）。Gemini の混み合い（しばらく待つ） |
| 計算ができない | ログに `sandbox` | サンドボックスの入れ物が消えた → setup.md 6 章で作り直し、`sandbox_engine` を替えて `terraform apply` |
| 組み直しが途中で止まった | 画面の帯 | ログインの期限（1時間）や載せ替えで止まった → 画面の「続ける」 |
| 遅い | ログの `[timing]` の行 | ドライブの読み書きは1回 3〜4 秒かかる。大きな複製は数分かかる |

ログは次で見られます（コンソールの Cloud Run →「ログ」でも同じです）。

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="lattice"' \
  --project=<project> --freshness=1h --limit=50 --format='value(timestamp,textPayload)'
```

## 9. やめるとき

1. `deletion_protection = false` に変えて `terraform apply`、そのあと `terraform destroy`（Firestore のデータは残す設定なので、要らなければコンソールで消す）
2. サンドボックスの入れ物を消す（コンソールの Agent Platform）
3. 共有ドライブは Lattice とは別に残る。文書はそのまま Markdown として読める
