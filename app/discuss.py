"""M2: 本物の Discussion（Gemini とファイル）。画面はウォークスルーのもの（wizard.js）を使い回す。

API の形はウォークスルー（app/wizard.py）と同じ（/api/wiz/state・/api/wiz/event）。中身は、
- 状態はファイルから作る（app/lattice/discussion.py）。
- AI の中身は Gemini が、別スレッドで書く（app/agent/runner.py）。その間は waiting に「何を待っているか」を出す。
`LATTICE_AGENT=gemini` のときに組み込む。
"""
import hashlib
import json
import contextvars
import threading
import traceback

from fastapi.concurrency import run_in_threadpool
from fastapi import APIRouter, HTTPException, Request

from .agent import runner
from .lattice import sources
from .lattice.discussion import LOCK, Discussions, Refused, now

router = APIRouter(prefix="/api/wiz")


def ds_for(request, subject):
    st = getattr(request.state, "storage", None) or request.app.state.storage
    try:
        if not st.head(subject, "/index.md"):  # Subject があるか（ドライブ版は ID を覚えるので2回目からは速い）
            raise ValueError
    except Exception:
        raise HTTPException(400, "subject")
    return Discussions(st, subject, request.state.user, model=runner.MODEL, state=request.app.state.work_state)


def background(ds, did, fn, *args):
    """Gemini を呼ぶ処理は時間がかかるので、別スレッドで走らせる。失敗したら会話にそう書き、待ちを解く。"""
    def go():
        from . import timing
        tr = timing.start(f"{getattr(fn, '__name__', fn)} {did or ''}")
        try:
            fn(ds, did, *args) if did else fn(ds, *args)
        except Exception as e:  # noqa: BLE001  画面に出して止まらないようにする
            traceback.print_exc()
            if did:
                try:
                    def f(w):
                        msg = str(e)[:300] if isinstance(e, Refused) else f"エラーで止まりました: {type(e).__name__}: {str(e)[:200]}"
                        w["messages"].append({"role": "agent", "text": f"（{msg}）", "at": now()})
                        if w.get("waiting") != "finishing":
                            w["waiting"] = None
                    ds.update_work(did, f)
                except Exception:
                    pass
        finally:
            tr.report()
    # 利用者（ドライブの資格情報・利用上限の数え先）を別スレッドにも引き継ぐ
    threading.Thread(target=contextvars.copy_context().run, args=(go,), daemon=True).start()


def finish(ds, did, approved, withdrawn):
    """確定のあと: 取り下げがあれば持ち越しに1行書いて、閉じる。index.md を合わせる作業は、閉じてから裏で行う
    （M4 の計測: index の更新に Gemini で 1 分以上かかり、その間ずっと画面を待たせていた）。"""
    ds.progress(did, "確定した内容を仕上げています…")
    try:
        if withdrawn:
            runner.carryover(ds, did, withdrawn)
    finally:  # Gemini が落ちても、確定した L1・L2 の Discussion は必ず閉じる（目録と log を残す）
        if approved:
            ds.close(did)
    if approved:
        try:
            if runner.update_index(ds, did, approved).result.get("index"):
                ds.log(f"{did} の確定に合わせて index.md の要点を直した（agent）")
                ds.write_manifest(did)  # 巻き戻しの目録に、直した index.md も含める
        except Exception:  # noqa: BLE001  index を直せなくても、確定は済んでいる
            traceback.print_exc()


def view(ds, w=None, did=None, closed=None):
    if w is not None:
        w = ds.fill_olds(w)
        fm, body = ds.summary(w["id"])
        return {"id": w["id"], "title": w["title"], "state": w["state"], "origin": w["origin"], "started": w["started"],
                "messages": w["messages"], "summary": _body(body), "candidates": w["candidates"],
                "drafts": [{"facet": x["facet"], "status": x["status"], "mark": x.get("mark"),
                            "files": [{"path": f["path"], "layer": f["layer"], "new": f.get("new", ""), "old": f.get("old"),
                                       "edited_by_user": f.get("edited_by_user", False)} for f in x["files"]]}
                           for x in w["drafts"]],
                "waiting": w["waiting"], "progress": w.get("progress"), "wait_since": w.get("wait_since"), "feed": w.get("feed") or [], "can_undo": ds.can_undo_message(w),
                "searches": [{k: x.get(k) for k in ("id", "query", "keep", "card", "links")} for x in w.get("searches", [])],
                "sources": [{"card": e["card"], "file": e["file"], "name": e["name"], "url": e.get("url"), "seen": e.get("seen", False)}
                            for e in w.get("sources", [])]}
    fm, body = closed
    return {"id": did, "title": fm.get("title", did).split(" ", 1)[-1], "state": fm.get("discussion_state", "closed"),
            "origin": "", "started": str(fm.get("date", "")), "messages": [], "summary": _body(body),
            "candidates": [], "drafts": [], "waiting": None}


def _body(body):
    if body is None:
        return None
    lines = body.strip().splitlines()
    return "\n".join(lines[1:] if lines and lines[0].startswith("# ") else lines).strip() + "\n"


@router.get("/state")
def get_state(request: Request, s: str):
    ds = ds_for(request, s)
    reg = ds.registry()
    out = []
    for did in reg["open"]:
        try:
            ds.recover_stale(did)
            out.append(view(ds, ds.work(did)))
        except Refused:
            pass
    full = ds.summaries_full()  # 要約は1回だけ、まとめて読む（以前は閉じた Discussion ごとに一覧を作り直していた）
    for did, (_, fm, body) in full.items():
        if did not in reg["open"]:
            out.append(view(ds, did=did, closed=(fm, body)))
    out.sort(key=lambda d: int(d["id"].split("-")[1]), reverse=True)
    edits = [{k: e.get(k) for k in ("id", "path", "at", "waiting", "judgement", "reason", "discussion", "into")} for e in ds.edits()]
    from .lattice import migrate
    st = {"real": True, "readonly": not ds.st.can_edit(s), "migration": migrate.view(ds), "discussions": out, "edits": edits, "rewindable": ds.rewindable(full), "undo_rewind": ds.undoable()}
    st["rev"] = hashlib.sha1(json.dumps(st, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return st


@router.post("/upload")
async def upload(request: Request, s: str, d: str, name: str):
    """PC からの資料（C-14）。multipart にせず、本文をそのまま受ける（ファイル名はクエリで）。"""
    ds = ds_for(request, s)
    try:
        await run_in_threadpool(_writable, ds)
        sources.check(name, int(request.headers.get("content-length") or 0))
        data = await request.body()
        # ドライブへの書き込みは別スレッドで（ここで待つと、その間ほかの人の画面の確認まで止まる）
        return {"ok": True, "card": await run_in_threadpool(sources.add, ds, d, name, data)}
    except Refused as e:
        raise HTTPException(409, str(e))


READONLY = "この Subject は Drive で閲覧のみになっているため、書き込む操作はできません（読むことと、これを元に新しい Subject を作ることはできます）"


def _writable(ds):
    if not ds.st.can_edit(ds.s):
        raise Refused(READONLY)


@router.post("/event")
async def event(request: Request):
    ev = await request.json()
    # ドライブの呼び出しは時間がかかるので、別スレッドで動かす（その間も画面の状態の確認に答えられるように）
    return await run_in_threadpool(_event, request, ev)


def _event(request, ev):
    ds = ds_for(request, ev.get("subject"))
    typ, did = ev.get("type"), ev.get("discussion")
    try:
        if typ != "view":
            _writable(ds)
        from .lattice import migrate
        if typ == "resume_migration":
            migrate.resume(ds)
            return {"ok": True}
        if typ != "view" and migrate.running(ds):
            raise Refused("前の Subject の中身を移している途中です。終わるまで、書き込む操作はできません")
        with LOCK:
            if typ == "start":  # 何も作らない。最初の発言で作る（C-7）
                origin = (f"facet:{ev['facet']}" if ev.get("facet") else f"continues:{ev['continues']}" if ev.get("continues") else "list")
                return {"ok": True, "discussion": "new:" + origin}
            if typ == "message":
                if str(did or "").startswith("new:"):
                    origin = did[4:]
                    did = ds.create(ev["text"], origin=origin, viewing=ev.get("viewing"))
                    background(ds, did, runner.continue_from if origin.startswith("continues:") else runner.reply)
                else:
                    ds.add_user_message(did, ev["text"])
                    background(ds, did, runner.reply)
            elif typ == "summarize":
                w = ds.work(did)
                if w["state"] != "open" or w.get("waiting"):
                    raise Refused("要約は、会話中でエージェントの返事が出たあとに作れます")
                ds.update_work(did, lambda w: w.update(waiting="summary"))
                background(ds, did, runner.summarize)
            elif typ == "undo_message":
                return {"ok": True, "text": ds.undo_message(did)}
            elif typ == "choice":
                ds.choose(did, ev["facet"], ev["choice"])
            elif typ == "uncandidate":
                ds.uncandidate(did, ev["facet"])
            elif typ == "confirm":
                if ds.confirm(did) == "review":
                    background(ds, did, runner.drafts)
            elif typ == "edit_draft":
                ds.edit_draft(did, ev["facet"], ev["path"], ev["text"])
            elif typ in ("approve", "withdraw"):
                ds.mark(did, ev["facet"], typ)
            elif typ == "finalize":
                approved, withdrawn = ds.finalize(did)
                if approved or withdrawn:
                    background(ds, did, finish, approved, withdrawn)
            elif typ == "back":
                ds.back(did)
            elif typ == "discard":
                ds.discard(did)
            elif typ == "edit_file":
                eid = ds.save_edit(ev["path"], ev["text"], into=ev.get("into") or None)
                background(ds, None, runner.judge, eid)
            elif typ == "rewind":
                return {"ok": True, "cancelled": ds.rewind(ev["target"])}
            elif typ == "undo_rewind":
                ds.undo_rewind()
            elif typ == "drive":  # ドライブから（C-14）。取り込みはドライブを呼ぶので、ロックの外で行う
                pass
            elif typ == "keep_search":
                sources.keep_searches(ds, did, one=ev["search"], keep=bool(ev.get("keep")))
            elif typ == "remove_source":
                sources.remove(ds, did, ev["card"])
            elif typ == "view":
                ds.update_work(did, lambda w: w.update(viewing=ev.get("label")))
            else:
                raise HTTPException(400, "type")
        if typ == "drive":
            from .lattice import drive_files
            ds.work(did)
            name, data, url = drive_files.fetch(ev.get("url"))
            return {"ok": True, "card": sources.add(ds, did, name, data, url=url, by_drive=True)}
    except Refused as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "discussion": did}
