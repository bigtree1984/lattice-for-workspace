# Lattice for Google Workspace

共有ドライブの上で、テーマ（Subject）ごとの知識を AI（Gemini）との対話（Discussion）で育てていく Web アプリです。
話した内容は要約と下書きになり、人が承認すると共有ドライブの Markdown として確定します。

- データ（文書・資料）はすべて会社の**共有ドライブ**に置かれ、アプリは**利用者本人の権限**で読み書きします
- AI は Vertex AI の Gemini。表計算などは、ネットワークに出られないサンドボックスで行います
- Google Cloud の Cloud Run で動きます。ログインは会社の Google Workspace のアカウントだけ

## 使い始める

このリポジトリは **「Use this template」で自社の非公開リポジトリにコピー**して使うことをおすすめします
（Fork すると公開のままになり、自社の設定を入れにくいため）。

1. 立てる: [docs/setup.md](docs/setup.md)（構築手順書）
2. 動かし続ける: [docs/operations.md](docs/operations.md)（運用手順書）
3. 仕組み: [docs/architecture.md](docs/architecture.md)（構成・権限・データの置き場所・環境変数）
4. 会社で決めること: [docs/decisions.md](docs/decisions.md)
5. セキュリティ: [SECURITY.md](SECURITY.md)
6. 費用: [docs/cost.md](docs/cost.md)

## 手元で動かす（開発）

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
LATTICE_DEV_USER=you@example.com .venv/bin/uvicorn app.main:app --port 8000
```

共有ドライブの代わりに `dev_data/` のフォルダを使います（見本の Subject `edible_coffee_2026` 入り。架空のデータです）。
`LATTICE_DEV_USER` はログインを飛ばす開発用の設定です。**本番では入れないでください。**

テスト:

```bash
.venv/bin/python -m pytest -q tests
```

## 見本のデータ

`examples/ai_tutor/` は、AI の計算機能を試すための架空のデータです（生成AIの家庭教師サービスの価格を考える: アンケート・原価・競合）。

## ライセンス

[MIT](LICENSE)
