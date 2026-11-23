"""計算部屋（Agent Platform のコード実行のサンドボックス、REQUIREMENTS C-1・PLAN「関数とサンドボックスの線引き」）。

- Discussion ごとに1つ作り、closed・廃棄で消す（C-1「Discussion の間だけ残す」）。消し忘れても期限（TTL）で消える。
- ネットワークに出られない。渡すのは、この Discussion で渡された資料だけ。結果は文字で受け取り、ファイルは持ち帰らない
  （残す価値のある結果は、AI が要約や L2 に書く）。
- 入れ物（Agent Engine）は1つを使い回す。名前は LATTICE_SANDBOX_ENGINE（scripts/make_sandbox_holder.py で作る）。
"""
import base64
import json
import os

LOCATION = os.environ.get("LATTICE_SANDBOX_LOCATION", "us-central1")
TTL = "259200s"  # 3日。会話が長引いて消えたら、作り直して資料を渡し直す
MAX_OUT = 20_000

_client = None


def client():
    global _client
    if _client is None:
        import agentplatform
        import google.auth
        from . import runner
        runner.client()  # Lattice 専用の ADC の場所を設定する
        project = os.environ.get("LATTICE_PROJECT")
        c, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"], quota_project_id=project)
        _client = agentplatform.Client(project=project, location=LOCATION, credentials=c)
    return _client


def engine():
    name = os.environ.get("LATTICE_SANDBOX_ENGINE")
    if not name:
        from ..lattice.discussion import Refused
        raise Refused("計算部屋（サンドボックス）が用意されていません（LATTICE_SANDBOX_ENGINE）")
    return name


def create(label):
    op = client().sandboxes.create(name=engine(), spec={"code_execution_environment": {}},
                                                 config={"display_name": f"lattice-{label}"[:60], "ttl": TTL})
    return op.response.name


def execute(sandbox, code, files=()):
    """files: [(名前, 中身のバイト, mime)]。返り値は（出力の文字, 作られたファイルの名前）。"""
    data = {"code": code}
    if files:
        data["files"] = [{"name": n, "content": base64.b64encode(b).decode(), "mime_type": m} for n, b, m in files]
    r = client().sandboxes.execute_code(name=sandbox, input_data=data)
    out, made = {}, []
    for o in r.outputs or []:
        attrs = getattr(o.metadata, "attributes", None) or {}
        fname = attrs.get("file_name")
        if fname:  # 作られたファイル（持ち帰らず、名前だけ返す）
            made.append(fname.decode() if isinstance(fname, bytes) else str(fname))
        elif o.mime_type == "application/json":  # {"msg_out", "msg_err", "exit_status_int"}
            out = json.loads((o.data or b"{}").decode("utf-8", "replace"))
    text = out.get("msg_out", "")
    if out.get("msg_err"):
        text += "\n[エラー]\n" + out["msg_err"][-4000:]
    return text[:MAX_OUT], made


def delete(sandbox):
    client().sandboxes.delete(name=sandbox)
