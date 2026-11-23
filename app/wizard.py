"""M1.5 のウォークスルー（オズの魔法使い法、PLAN M1.5）。

画面は本物らしく動かし、AI の役は Claude が演じる。Gemini は呼ばない。

- 利用者の操作は wizard/events.jsonl に1行ずつ追記する（Claude が読む）。
- 画面の状態は wizard/state/<Subject>.json に持つ。
  - 機械的に決まる変化（発言を足す、状態を open→review にする、承認・取り下げ など）は、このサーバーが行う。
    本番でも G-1 の決定論的な関数が受け持つ部分。
  - AI の中身（返事、要約、Facet の候補、L1・L2 の新しい案、修正の判断）は、Claude が wizard/oz.py で書き込む。
- Subject のファイル（dev_data）は書き換えない。承認しても状態の上で stable になるだけ。

Discussion の状態（REQUIREMENTS C-10・C-12）: open → review → closed。廃棄は discarded。
"""
import json
import pathlib
import re
import threading
import time

from fastapi import APIRouter, HTTPException, Request

from .lattice import bundle
from .lattice.storage import StorageError

ROOT = pathlib.Path(__file__).resolve().parent.parent / "wizard"
EVENTS = ROOT / "events.jsonl"
LOCK = threading.Lock()

router = APIRouter(prefix="/api/wiz")


def state_path(subject):
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", subject or ""):
        raise HTTPException(400, "subject")
    return ROOT / "state" / f"{subject}.json"


def load(subject):
    p = state_path(subject)
    return json.loads(p.read_text()) if p.is_file() else {"discussions": [], "edits": [], "rev": 0}


def save(subject, st):
    st["rev"] = st.get("rev", 0) + 1
    p = state_path(subject)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(p)


def log_event(ev):
    ROOT.mkdir(exist_ok=True)
    with EVENTS.open("a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def find(st, did):
    for d in st["discussions"]:
        if d["id"] == did:
            return d
    raise HTTPException(404, "discussion")


def next_id(storage, subject, st):
    nums = [int(m) for d in st["discussions"] for m in re.findall(r"D-(\d+)", d["id"])]
    try:
        for f in bundle.load(storage, subject)["files"] if storage else []:
            nums += [int(m) for m in re.findall(r"^discussion_id:\s*D-(\d+)", f["frontmatter"], re.M)]
    except StorageError:
        pass
    return f"D-{max(nums, default=0) + 1:03d}"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@router.get("/state")
def get_state(s: str):
    return load(s)


@router.post("/event")
async def event(request: Request):
    ev = await request.json()
    subject, typ, did = ev.get("subject"), ev.get("type"), ev.get("discussion")
    ev["at"] = now()
    with LOCK:
        st = load(subject)
        d = None
        if typ == "start":
            d = {"id": next_id(request.app.state.storage, subject, st), "title": "", "state": "open", "origin": ev.get("origin", "list"),
                 "started": ev["at"], "messages": [], "summary": None, "candidates": [], "drafts": [], "waiting": None}
            if ev.get("facet"):
                d["origin"] = f"facet:{ev['facet']}"
            if ev.get("continues"):  # 完了した Discussion を引き継いで始める（C-10）
                d["origin"] = f"continues:{ev['continues']}"
                d["waiting"] = "reply"  # AI が前の回を読んで、最初のひと言を出す
            if ev.get("from_edit"):
                d["origin"] = f"edit:{ev['from_edit']}"
            st["discussions"].insert(0, d)
            ev["discussion"] = d["id"]
        elif typ == "edit_file":  # C-6: 会話の外で L1・L2 の本文を直した。AI が Discussion にするかを判断する
            st["edits"].insert(0, {"id": f"E-{len(st['edits']) + 1:03d}", "path": ev["path"], "text": ev["text"],
                                   "at": ev["at"], "judgement": None, "waiting": "judge"})
        else:
            d = find(st, did)
            if typ == "message":
                d["messages"].append({"role": "user", "text": ev["text"], "at": ev["at"]})
                d["waiting"] = "reply"
            elif typ == "view":  # 「これ」のために、いま見ているものを伝える
                d["viewing"] = ev.get("label")
            elif typ == "summarize":
                d["waiting"] = "summary"
            elif typ == "choice":
                for c in d["candidates"]:
                    if c["facet"] == ev["facet"]:
                        c["choice"] = ev["choice"]
                        break
                else:
                    d["candidates"].append({"facet": ev["facet"], "reason": "利用者が追加", "suggest": None, "choice": ev["choice"]})
            elif typ == "uncandidate":  # 利用者が自分で足した候補だけ外せる
                d["candidates"] = [c for c in d["candidates"] if not (c["facet"] == ev["facet"] and c.get("suggest") is None)]
            elif typ == "confirm":  # 「Facet 更新を確認する」
                if any(c.get("choice") == "update" for c in d["candidates"]):
                    d["state"], d["waiting"], d["drafts"] = "review", "drafts", []
                else:
                    d["state"], d["waiting"] = "closed", None  # ②材料として残すだけ
            elif typ == "edit_draft":
                for x in d["drafts"]:
                    for f in x["files"]:
                        if x["facet"] == ev["facet"] and f["path"] == ev["path"]:
                            f["new"], f["edited_by_user"] = ev["text"], True
            elif typ in ("approve", "withdraw"):  # タブごとの「予定」。押し直せる。まだ確定しない（C-12）
                for x in d["drafts"]:
                    if x["facet"] == ev["facet"] and x.get("status") == "pending":
                        x["mark"] = typ
            elif typ == "finalize":  # 最終確認の「確定する」。ここで初めて stable になる
                if not d["drafts"] or any(x.get("status") == "pending" and not x.get("mark") for x in d["drafts"]):
                    raise HTTPException(409, "not all marked")
                for x in d["drafts"]:
                    if x.get("status") == "pending":
                        x["status"] = "approved" if x.pop("mark") == "approve" else "withdrawn"
                withdrawn = [x["facet"] for x in d["drafts"] if x["status"] == "withdrawn"]
                for c in d["candidates"]:  # 取り下げた Facet は「材料として残す」に。持ち越しの1行は AI が足す
                    if c["facet"] in withdrawn:
                        c["choice"] = "material"
                if withdrawn:
                    d["waiting"] = "carryover"
                d["state"] = "closed" if any(x["status"] == "approved" for x in d["drafts"]) else "open"
            elif typ == "back":  # Discussion に戻る: draft を全部取り下げて open に（確定前なので、いつでも戻れる）
                for x in d["drafts"]:
                    if x.get("status") != "approved":
                        x["status"] = "withdrawn"
                d["state"], d["waiting"] = "open", None
            elif typ == "discard":  # 廃棄: draft も全部取り下げる（本番では書く直前の版に戻す、C-12）
                for x in d["drafts"]:
                    x["status"] = "withdrawn"
                d["state"], d["waiting"] = "discarded", None
            else:
                raise HTTPException(400, "type")
        save(subject, st)
        log_event(ev)
    return {"ok": True, "discussion": ev.get("discussion")}
