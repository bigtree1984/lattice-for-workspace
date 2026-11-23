# Lattice のインフラ基盤とデータの流れ

IT 部門向けの引き継ぎ資料の ④ です。Lattice が Google Cloud と Google Workspace のどの部品を使い、データがどこに置かれ、AI（Gemini）に何が送られるかを説明します。

- 対象の版: コンテナイメージ `app:m4-38`（2026-11-23 時点）
- 例: Google Cloud プロジェクト `example-lattice`、リージョン `asia-northeast1`（東京）
- 関連する資料: 構築の手順（⑥）、Terraform（②）、セキュリティ（⑤）。設計の判断の経緯は `REQUIREMENTS.md`（文中の A-2・F-2 などはその番号）

---

## 1. 全体の構成

![Lattice の構成](figures/architecture.png)

```
 利用者（Chrome）
   │  ① 会社の Google アカウントでログイン（OAuth・内部アプリ）
   ▼
 Cloud Run「lattice」（Web 画面と API を1つのコンテナで）
   │  実行するサービスアカウント: lattice-app
   │
   ├─ 利用者本人のアクセストークンで ──→ Google ドライブ（共有ドライブ）… データの正
   │
   ├─ サービスアカウントの権限で ──┬→ Gemini（Gemini Enterprise Agent Platform、global）
   │                               ├→ Web Grounding for Enterprise（Web 検索）
   │                               ├→ サンドボックス（計算用。us-central1、ネットワーク遮断）
   │                               ├→ Firestore（作業の途中の状態だけ）
   │                               └→ Secret Manager（OAuth のクライアント・セッションの鍵）
   │
   └─ ログ ──→ Cloud Logging
```

**大事な点は2つです。**

1. **データの正は共有ドライブです。** Subject（テーマ）ごとに1つのフォルダで、中身はすべて Markdown（.md）と、渡された資料の実物です。人がドライブで直接開いて読めます。
2. **ドライブへの読み書きは、利用者本人の権限で行います。** アプリのサービスアカウントにはドライブの権限を与えていません。利用者が見られないフォルダは、Lattice からも見えません。

---

## 2. 使っている部品

| 部品 | 何に使うか | 設定（例） |
|---|---|---|
| **Cloud Run** サービス `lattice` | Web 画面・API・AI の作業（裏のスレッド）を1つのコンテナで動かす | CPU 1・メモリ 1GiB、**CPU 常時オン**、最小 0 台・最大 2 台、同時リクエスト 80、タイムアウト 900 秒、起動時の CPU ブースト |
| **Artifact Registry** `lattice` | コンテナイメージの置き場 | Docker 形式、`asia-northeast1` |
| **Cloud Build** | ソースからイメージを作る | `gcloud builds submit` で実行 |
| **サービスアカウント** `lattice-app` | Cloud Run を実行する ID | 下の「3. 権限」 |
| **Secret Manager** | 秘密の値を置く | `lattice-oauth-client`（OAuth クライアントの JSON）、`lattice-session-key`（Cookie を暗号化する鍵） |
| **Firestore**（ネイティブモード） | 作業の途中の状態だけを置く（データの正ではない） | `(default)`、`asia-northeast1`、コレクション `lattice_state` |
| **Gemini Enterprise Agent Platform**（旧 Vertex AI） | 会話・要約・L1/L2 の下書き・修正の判断・組み直して移す | モデル `gemini-3.8-flash`、**global のエンドポイント**（最新のモデルは地域を指定できないため。A-2） |
| **Web Grounding for Enterprise** | 会話の中の Web 検索 | Google 検索のグラウンディングは使わない（保持の扱いのため。A-2） |
| **Agent Platform のサンドボックス** | 渡された表などの計算（Python） | `us-central1`、ネットワーク遮断、Discussion ごとに作り、閉じたら消す（期限 3 日） |
| **Google ドライブ API** | Subject のファイルの読み書き | 利用者本人の OAuth トークンで呼ぶ |
| **OAuth クライアント・同意画面** | 会社の Google アカウントでのログイン | 同意画面は「内部」。スコープは `openid email profile` と `drive` |
| **Cloud Logging** | アクセスログと、処理時間の記録（`[timing]` の行） | 既定のまま |

---

## 3. 権限

### 3.1 アプリ（サービスアカウント `lattice-app`）

| ロール | どこに | 何のため |
|---|---|---|
| `roles/aiplatform.user` | プロジェクト | Gemini・Web Grounding・サンドボックスを呼ぶ |
| `roles/datastore.user` | プロジェクト | Firestore に作業の状態を読み書きする |
| `roles/secretmanager.secretAccessor` | 2つのシークレットだけ | OAuth クライアントとセッションの鍵を読む |

ドライブの権限は持たせていません（1 章の「大事な点」の2）。

### 3.2 利用者

- 会社のドメイン（例: `example.co.jp`、設定 `LATTICE_ALLOWED_DOMAIN`）のアカウントだけがログインできます。同意画面を「内部」にしたうえで、アプリでもドメインを確かめています。
- 利用者が Lattice で何をできるかは、**共有ドライブでの権限そのもの**です。
  - 編集できる人: Discussion を始める、確定する、などすべての操作
  - 閲覧のみの人: 読むことだけ。画面に「閲覧のみ」と出し、書き込む操作はサーバーでも断る（E-4）
- 他人の Subject を編集すること自体は、仕組みでは止めていません。運用のルールで扱い、`log.md` に誰がいつ何をしたかが残ります（B-4）。

### 3.3 ログインの仕組み

- Google から受け取るのは、**1時間で切れるアクセストークンだけ**です。リフレッシュトークンは受け取らず、サーバーにも保存しません。
- トークンは、暗号化した Cookie（HttpOnly）に入れて利用者のブラウザが持ちます。暗号の鍵は Secret Manager の `lattice-session-key` です。
- 残りが 15 分を切ると、ログインし直してもらいます。Google にログインしたままなら、画面が一瞬 Google を経由して戻るだけです。本人確認（パスキーなど）を求められることもあります。

---

## 4. データの置き場所

### 4.1 共有ドライブ（データの正）

使う共有ドライブは1つで、ID を設定 `LATTICE_DRIVE_ID` で渡します。その直下に、Subject ごとのフォルダが並びます。

```
<共有ドライブ>/
├─ _templates/              Facet の雛形（YAML）。人が足したり直したりできる
└─ edible_coffee_2026/      Subject（名前は「文字列_年」）
   ├─ index.md              Subject の荷札（表示名・担当・いまの要点）
   ├─ facets.md             Facet の構成
   ├─ log.md                更新記録（誰が・いつ・何を）
   ├─ facets/<Facet>/       Facet ごとの L1（決定）と L2（判断の文脈）
   ├─ discussions/<日付>-dNNN/
   │    ├─ discussion_summary.md   Discussion の要約
   │    └─ source-*.md / 実物       渡された資料の原典カードと、渡した時点の実物
   ├─ imported/<前の Subject>/      組み直して移したときに複製した、前の Subject の Discussion
   └─ .lattice/             アプリの隠しフォルダ（画面には出さない）
        ├─ bundle.json      画面表示用の索引（ドライブから作り直せる）
        └─ manifests/       巻き戻し用の目録（ファイルの ID と版の番号だけ。中身は持たない）
```

- 巻き戻し（E-2）は、ドライブの「版の履歴」を使います。目録が指す版には「永久に保存」の印を付けて、ドライブが古い版を自動で消さないようにしています。

### 4.2 Firestore（作業の途中の状態だけ）

コレクション `lattice_state` に、次のものを置きます。どれも「作業の途中」のもので、データの正ではありません。

| 中身 | いつ消えるか |
|---|---|
| 会話中・確認待ちの Discussion の作業の状態（会話の記録、AI とのやりとりの記録、下書きの一覧） | Discussion を確定・廃棄したとき |
| Discussion の一覧と、最後に使った番号（Subject ごと） | 消えない（Subject を消しても残る。7 章） |
| 組み直して移すの進み具合 | 消えない（終わった状態で残る） |
| Subject の一覧の写し（表示を速くするため） | 次に一覧を作り直したとき上書き |
| Gemini の1日の利用量（上限の管理） | 日ごと |

### 4.3 そのほか

- **利用者のブラウザ**: 暗号化したセッションの Cookie（4.1 のトークン）、表示の設定（localStorage）
- **サンドボックス**: 計算のために渡した資料。Discussion を閉じたら消し、消し忘れても 3 日で消える
- **Cloud Logging**: アクセスログ（URL に Subject 名を含む）と処理時間の記録。会話の中身は書かない

---

## 5. AI（Gemini）に送るもの

| 場面 | 送るもの |
|---|---|
| 会話 | 利用者の発言、AI に読ませた Subject のファイル（L1・L2・要約など）、渡された資料の中身 |
| 要約・下書き | その Discussion の会話の記録、関係する Facet の L1・L2 |
| 確定のあと | 確定した L1 と `index.md`（「いまの要点」を合わせるため） |
| 人の修正の判断 | 直す前と後の本文 |
| 組み直して移す | 前の Subject の L1・L2・Discussion の要約、新しい Facet の構成 |
| Web 検索 | AI が作った検索の問い合わせ（Web Grounding for Enterprise 経由） |

**保持の扱い（Google の規約・文書による。A-2）**
- 送った内容をモデルの学習に使わない（Service Specific Terms の Training Restriction）。
- 不正利用の監視のため、Google がプロンプトを最大 30〜90 日記録しうる。**会社の環境では、IT 部門から Google に記録の停止（オプトアウト）を申請してもらう前提です。**
- 暗黙のキャッシュは既定で有効（24 時間以内に消える）。プロジェクト単位で止められる。
- **global のエンドポイント**なので、処理する地域は固定されない。地域を指定できるモデルが出たら切り替える。
- リクエストとレスポンスの記録（BigQuery）は有効にしていない。

**使いすぎの歯止め**: 1日の利用量に上限があります（既定: 1人 200 万トークン、全体 1,000 万トークン。設定で変えられる）。

---

## 6. 主な処理の流れ

![Lattice の処理の流れ](figures/flow.png)

### 6.1 ログイン
1. 利用者が Lattice を開くと、Google のログインへ移る（会社のドメインに限る）。
2. Google から戻ったら、メールアドレスとドメインを確かめ、アクセストークンを暗号化した Cookie に入れる。

### 6.2 Discussion（会話から Facet の更新まで）
1. 利用者が発言すると、Cloud Run が裏のスレッドで Gemini に返事を作らせる。画面は 1.5 秒ごとに状態を読みに来て、返事を表示する。
2. 要約を作ると、AI が要約と「更新する Facet の候補」を出す。
3. 「Facet 更新を確認する」で、AI が L1・L2 の下書きを書く。利用者が新旧を並べて見て、承認か取り下げを選ぶ。
4. 確定すると、ドライブの L1・L2 が確定版になり、`log.md` に記録し、巻き戻し用の目録を書く。続けて裏で、AI が `index.md` の「いまの要点」を合わせる。

### 6.3 組み直して移す（E-4）
1. 新しい構成の Subject を作ると、裏で AI が「新しい Facet ごとに、前の Subject のどれを材料にするか」を決める。
2. 新しい Facet ごとに AI だけの Discussion を立て、前の L1・L2・Discussion を読んで新しい L1・L2 を書く。使った前の Discussion と資料は、新しい Subject の中に複製して出典にする。
3. 人の確認は入れず、できた L1・L2 に「移行で作った」の印を付ける。
- 実測: 5 Facet で約 9 分（CPU 常時オン）。

### 6.4 複製して続ける（年度の切り替え、E-1）
- 前の Subject のファイルをすべて新しいフォルダにコピーする。前の Subject はその年の FIX として残る。
- 実測: Facet 24・Discussion 15 の Subject で約 4 分半。

---

## 7. 設定値（環境変数の一覧）

アプリが読む環境変数は、これで全部です。本番の値は Terraform（`terraform/main.tf` の `locals.env`）が入れます。

### 7.1 本番で使うもの

| 環境変数 | 中身 | 入れ方 | 例 |
|---|---|---|---|
| `LATTICE_AGENT` | AI の種類。`gemini` で Discussion の機能が有効になる | Terraform（固定） | `gemini` |
| `LATTICE_PROJECT` | Gemini・サンドボックスを呼ぶプロジェクト | Terraform（`project_id`） | `example-lattice` |
| `LATTICE_DRIVE_ID` | 使う共有ドライブの ID | Terraform（`drive_id`） | `0AXXXXXXXXXXXXXXXXX` |
| `LATTICE_ALLOWED_DOMAIN` | ログインを許すドメイン | Terraform（`allowed_domain`） | `example.co.jp` |
| `LATTICE_OAUTH_SECRET` | OAuth クライアントの JSON を入れたシークレットの名前。**これが無いとログインが無効になる** | Terraform（`oauth_secret_name`） | `lattice-oauth-client` |
| `LATTICE_SESSION_KEY` | ログインの Cookie を暗号化する鍵のシークレットの名前。無いと起動のたびに鍵が変わり、全員ログインし直しになる | Terraform（`session_key_name`） | `lattice-session-key` |
| `LATTICE_SANDBOX_ENGINE` | サンドボックスの入れ物の名前。無いと AI の計算機能が使えない | Terraform（`sandbox_engine`） | `projects/123456789012/locations/us-central1/reasoningEngines/…` |
| `LATTICE_MODEL` | Gemini のモデル | Terraform（`model`） | `gemini-3.8-flash` |
| `LATTICE_DAILY_TOKENS_USER` | 1人1日の Gemini トークン上限 | Terraform（`daily_tokens_user`） | 2,000,000 |
| `LATTICE_DAILY_TOKENS_TOTAL` | 全体1日の Gemini トークン上限 | Terraform（`daily_tokens_total`） | 10,000,000 |

### 7.2 ふだんは入れないもの

| 環境変数 | 中身 | 既定 |
|---|---|---|
| `LATTICE_SANDBOX_LOCATION` | サンドボックスの地域 | `us-central1`（東京は未対応） |
| `LATTICE_BASE_URL` | ログインの戻り先の URL を固定する。独自ドメインやロードバランサーの後ろに置くときだけ | 開いた URL から自動で決める |

### 7.3 手元の開発だけで使うもの（本番では絶対に入れない）

| 環境変数 | 中身 |
|---|---|
| `LATTICE_DEV_USER` | ログインなしで、この1人として動かす。**`LATTICE_OAUTH_SECRET` が無いときにこれがあると、誰でもこの人として使えてしまう** |
| `LATTICE_LOCAL_ROOT` | 共有ドライブの代わりに使う手元のフォルダ |
| `LATTICE_WIZARD` | 画面の動きだけを試す見本モード |

---

## 8. 制約と、分かっている課題

| 項目 | 内容 | 扱い |
|---|---|---|
| CPU の設定 | 「応答中だけ CPU」にすると、画面を閉じたときに裏の作業がほぼ止まる（実測） | 常時オンにする（F-2）。最小 0 台なので、使っていない間は料金がかからない |
| ドライブの速さ | 書き込み1回に約 3 秒かかる。処理時間の多くはここ | 並べられる所は並べた。複製（4 分半）はまだ1つずつで、改善の余地あり |
| トークンの期限 | アクセストークンは 1 時間。長い作業の途中で切れると、裏の作業が止まる可能性がある | 残り 15 分でログインし直してもらう。止まった移行は「続きから再開する」で続けられる |
| 複製した直後の巻き戻し | 巻き戻しの目録は複製しないので、複製した時点より前には戻れない | 仕様として説明する |
| Firestore の片付け | ドライブで Subject をゴミ箱に移しても、Firestore の状態は残る | 運用手順（⑦）で片付ける。画面には出ず、害は無い |
| 処理する地域 | Gemini は global のエンドポイント | 地域を指定できるモデルが出たら切り替える（A-2） |
| 台の数 | 最大 2 台。作業の状態はプロセスの中のロックで順番を守っている | 利用者が増えたら見直す |
