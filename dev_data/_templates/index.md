# Facet の雛形

新しい Subject を作る画面（D-5）で読み込む雛形を置くフォルダ。1ファイル = 1雛形（`<id>.yaml`）。
ファイルを足す・直すと、次に画面を開いたときから使われる。形の誤ったファイルは読み飛ばす。

```yaml
label: マーケティング            # 画面に出す名前
description: 基本の Facet         # 一言の説明
groups:                           # グループ → 小グループ → Facet の2段
  - id: market                    # id は英小文字・数字・ハイフン
    label: 市場環境
    groups:
      - id: swot
        label: SWOT
        facets:
          - {id: swot, label: SWOT}
```
