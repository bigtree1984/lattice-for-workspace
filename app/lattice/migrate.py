"""組み直して移す（REQUIREMENTS E-4）。構成を変えた新しい Subject に、前の Subject の中身を AI が移す。

- 新しい Facet 1つごとに、Gemini の一人 Discussion を立てる（Facet の順に。先に作った新しい Facet も読める）。
- 一人 Discussion の出典は、前の Subject の Discussion の要約と、その原典カード・実物。前の L1・L2 は読む材料で、出典にしない。
- 前の Subject のファイルは参照せず、新しい Subject の /imported/<前の Subject>/ に複製する。
  使われたものだけを、使われたときに1回だけ複製する（前の Subject の中のパスをそのまま保つので、重複はパスで分かる）。
- 人の確認は入れない。移行で作った L1・L2 には荷札に migrated を付け、人が Discussion で直したら外れる。
- 途中で止まっても（サーバーの入れ替わりなど）、終わった Facet は残り、続きから再開できる。
"""
import re
import threading
import time

from .discussion import LOCK, Discussions, Refused, join, now, split

KEY = "work/_migration"  # 置き場所は Discussion の作業の状態と同じ（ローカル版は .lattice/work/ の下）
STALE = 20 * 60  # これより長く進みが無ければ、止まったとみなす（1つの Facet は数分で終わる）


def imported_dir(old):
    return f"/imported/{old}"


def facet_order(ds):
    """新しい Subject の Facet（廃止を除く）を、構成の順に。"""
    fm, _ = split(ds.st.read_text(ds.s, "/facets.md"))
    out = []

    def walk(gs):
        for g in gs or []:
            for f in g.get("facets", []) or []:
                if not f.get("deprecated"):
                    out.append(f["id"])
            walk(g.get("groups"))
    walk(fm.get("groups"))
    return out


def get(ds):
    return ds.state.get(ds.s, KEY)


def _put(ds, m):
    m["updated"] = time.time()
    ds.state.put(ds.s, KEY, m)


def running(ds):
    m = get(ds)
    return bool(m) and m.get("status") == "running"


def view(ds):
    """画面に出す進み具合。止まっていれば stalled。"""
    m = get(ds)
    if not m:
        return None
    v = {k: m.get(k) for k in ("from", "status", "current", "error")}
    v["total"], v["done"] = len(m.get("facets", [])), len(m.get("done", []))
    v["stalled"] = m.get("status") == "running" and time.time() - m.get("updated", 0) > STALE
    return v


def start(ds, old, groups=None):
    """新しい Subject（ds）を作った直後に呼ぶ。裏で全部の Facet を移す。
    画面を待たせないよう、ここでは状態を置くだけにし、ドライブを読む・log.md に書くのは裏で行う（M4: 作るのに85秒かかっていた）。"""
    if groups is None:
        order = facet_order(ds)
    else:
        order = []

        def walk(gs):
            for g in gs or []:
                order.extend(f["id"] for f in g.get("facets", []) or [] if not f.get("deprecated"))
                walk(g.get("groups"))
        walk(groups)
    _put(ds, {"from": old, "status": "running", "facets": order, "done": [], "current": None,
              "started": now(), "copied": [], "logged": False})
    _go(ds)


def resume(ds):
    m = get(ds)
    if not m or m.get("status") == "done":
        raise Refused("続ける移行はありません")
    if m.get("status") == "running" and time.time() - m.get("updated", 0) <= STALE:
        raise Refused("移行はまだ動いています")
    # 途中だった一人 Discussion は捨てて、その Facet からやり直す
    for did in list(ds.registry()["open"]):
        try:
            w = ds.work(did)
        except Refused:
            continue
        if str(w.get("origin", "")).startswith("migrate:"):
            ds.update_work(did, lambda w: w.update(waiting=None))
            ds.discard(did)
    m.update(status="running", error=None, current=None)
    _put(ds, m)
    _go(ds)


def _go(ds):
    from ..agent import runner
    from .. import timing

    def loop():
        tr = timing.start(f"migrate {ds.s}")
        try:
            run_all(ds, runner)
        finally:
            tr.report()
    threading.Thread(target=loop, daemon=True).start()


def run_all(ds, runner):
    m = get(ds)
    old = m["from"]
    try:
        if not m.get("logged"):
            ds.st.read_text(old, "/facets.md")  # 前の Subject が読めるか（読めなければ止まったと出す）
            ds.log(f"{old} の中身を、組み直して移し始めた（E-4。Facet ごとに AI の一人 Discussion を立てる）（human:{ds.name}）")
            m["logged"] = True
            _put(ds, m)
        plan = m.get("plan") or runner.migrate_plan(ds, old)
        m["plan"] = plan
        _put(ds, m)
        for facet in m["facets"]:
            if facet in m["done"]:
                continue
            m["current"] = facet
            _put(ds, m)
            left = [f for f in m["facets"] if f not in m["done"]]
            one(ds, runner, old, facet, plan.get(facet) or {}, last=len(left) == 1)
            m = get(ds)
            m["done"].append(facet)
            _put(ds, m)
        m["status"], m["current"] = "done", None
        _put(ds, m)
        record(ds, old)
    except Exception as e:  # noqa: BLE001  止まったことを画面に出し、続きから再開できるようにする
        print(f"[migrate] {ds.s}: {e!r}", flush=True)
        m = get(ds) or m
        m["status"], m["error"] = "failed", str(e)[:300]
        _put(ds, m)


# ---------- 一人 Discussion（Facet 1つ分） ----------
def create_solo(ds, old, facet):
    """人の発言なしで、AI だけの Discussion を作る。最初から「確認待ち」で、その Facet だけを更新する。"""
    with LOCK:
        import datetime as dt
        from .discussion import JST
        reg = ds.registry()
        n = max([reg["last"]] + [int(d.split("-")[1]) for d in reg["open"]]) + 1
        did = f"D-{n:03d}"
        date = dt.datetime.now(JST).date().isoformat()
        folder = f"/discussions/{date}-d{n:03d}/"
        reg["last"] = n
        reg["open"][did] = folder
        ds._put_registry(reg)
        w = {"id": did, "dir": folder, "date": date, "origin": f"migrate:{old}", "state": "review", "started": now(),
             "title": "", "messages": [{"role": "agent", "text": f"{old} の中身を、Facet「{facet}」に組み直して移します。", "at": now()}],
             "history": [], "read": [], "drafts": [], "waiting": "migrate", "wait_since": time.time(),
             "candidates": [{"facet": facet, "reason": "組み直して移す", "suggest": "update", "choice": "update"}]}
        ds.save_work(w)
        return did


def one(ds, runner, old, facet, plan, last=True):
    did = create_solo(ds, old, facet)
    runner.migrate_facet(ds, did, old, facet, plan)
    w = ds.work(did)
    if not ds.summary(did)[1]:
        raise RuntimeError(f"{facet} の要約が書かれませんでした")
    approve(ds, did, manifest=last)


def approve(ds, did, manifest=True):
    """人の確認なしで確定する。migrated の印を付け、verified は付けない（人は見ていないため）。
    manifest=False なら巻き戻し用の目録を書かない（移行の途中の Facet。戻る先は移行を終えた時点だけでよいので、最後の Facet で書く）。"""
    with LOCK:
        w = ds.work(did)
        approved = []
        files = []
        for x in w["drafts"]:
            x["status"] = "approved"
            approved.append(x["facet"])
            files += x["files"]
        frm = w["origin"].split(":", 1)[1]

        def stamp(f):  # L1 と L2 は別のファイルなので、並べて書く
            fm, body = split(ds.st.read_text(ds.s, f["path"]))
            for src in fm.get("sources", []):
                src["title"] = ds._title(src["resource"])
            fm["status"] = "stable"
            fm.pop("verified", None)
            fm["migrated"] = {"from": frm, "by": ds.model, "at": now()}
            ds.st.write_text(ds.s, f["path"], join(fm, body))
        _par(stamp, files)
        w["finish"], w["waiting"] = {"approved": approved, "withdrawn": []}, None
        ds.save_work(w)
        ds.close(did, by="ai:migrate", manifest=manifest)


# ---------- 前の Subject の Discussion を複製する（使われたものだけ、1回だけ） ----------
_OLD_FILES = {}  # 前の Subject → (読んだ時刻, ファイルの一覧)。移行の間は前の Subject は変わらない前提で、一覧を1回だけ取る
_COPY_LOCK = threading.Lock()


def _old_files(ds, old):
    got = _OLD_FILES.get(old)
    if not got or time.time() - got[0] > 600:
        got = _OLD_FILES[old] = (time.time(), [e.path for e in ds.st.walk(old)])
    return got[1]


def _par(fn, items):
    from concurrent.futures import ThreadPoolExecutor
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(min(8, len(items))) as ex:
        return list(ex.map(fn, items))


def import_many(ds, old, dids, summaries):
    """前の Discussion をいくつか複製する。1つ目で入れ物のフォルダができるので、残りは並べて複製する。"""
    dids = [d for i, d in enumerate(dids) if d and d not in dids[:i]]
    if not dids:
        return []
    first = [import_discussion(ds, old, dids[0], summaries)]
    return first + _par(lambda d: import_discussion(ds, old, d, summaries), dids[1:])


def import_discussion(ds, old, old_did, summaries):
    """前の Subject の Discussion を1つ、/imported/<old>/discussions/<フォルダ>/ に複製し、要約の新しいパスを返す。
    すでに複製してあれば、複製せずにそのパスを返す（要約を最後に書くので、要約があれば複製は終わっている）。"""
    got = summaries.get(old_did)
    if not got:
        return None
    folder = got[0]  # /discussions/<日付>-dNNN/
    base = imported_dir(old) + folder
    summ = base + "discussion_summary.md"
    if ds.st.exists(ds.s, summ):
        return summ
    files = [p for p in _old_files(ds, old) if p.startswith(folder) and not p.endswith("discussion_summary.md")
             and not any(x.startswith(".") for x in p.split("/"))]

    def copy(p):
        if p.endswith(".md"):
            fm, body = split(ds.st.read_text(old, p))
            fm["imported_from"] = {"subject": old, "path": p}
            ds.st.write_text(ds.s, base + p[len(folder):], join(fm, body))
        else:
            ds.st.write_bytes(ds.s, base + p[len(folder):], ds.st.read_bytes(old, p))
    if files:  # 1つ目でフォルダを作り、残りは並べて書く（M4 の計測: 書き込み1回が約4秒）
        copy(files[0])
        _par(copy, files[1:])
    fm, body = split(ds.st.read_text(old, folder + "discussion_summary.md"))
    keep = []
    for s in fm.get("sources") or []:  # 出典は1段。同じフォルダの中のもの（原典カード）だけ新しいパスにして残す
        r = str(s.get("resource", ""))
        if r.startswith(folder):
            keep.append({"resource": base + r[len(folder):], "title": s.get("title", "")})
        elif not r.startswith("/"):
            keep.append(s)
    # 新しい Subject の Discussion と番号が重ならないよう、Discussion としてではなく資料として置く
    new = {"type": "Imported Discussion Summary", "title": f"{old} の {fm.get('title', old_did)}",
           "description": fm.get("description", ""), "layer": "source", "date": fm.get("date"),
           "imported_from": {"subject": old, "path": folder + "discussion_summary.md", "discussion_id": old_did},
           "sources": keep}
    ds.st.write_text(ds.s, summ, join(new, body))
    with _COPY_LOCK:
        m = get(ds)
        if m is not None and old_did not in m.setdefault("copied", []):
            m["copied"].append(old_did)
            _put(ds, m)
    return summ


def record(ds, old):
    """移行の記録。移したものと、移さなかったもの（どの Facet からも使われなかった前の Discussion）。"""
    m = get(ds)
    full = Discussions(ds.st, old, state=ds.state).summaries_full()
    used = set(m.get("copied", []))
    lines = [f"# {old} からの移行の記録", "", f"- 始めた: {m.get('started')}", f"- 終えた: {now()}",
             f"- 新しい Facet: {len(m['facets'])} 個（それぞれ AI の一人 Discussion で書いた。人の確認は入れていない）", "",
             "## 移した前の Discussion（出典として複製した）", ""]
    lines += [f"- {full[d][1].get('title', d)}" for d in sorted(used) if d in full] or ["- なし"]
    lines += ["", "## 移さなかった前の Discussion（どの新しい Facet からも使わなかった。読みたいときは前の Subject を開く）", ""]
    lines += [f"- {full[d][1].get('title', d)}" for d in sorted(full) if d not in used] or ["- なし"]
    fm = {"type": "Migration Record", "title": f"{old} からの移行の記録", "description": "組み直して移す（E-4）の記録",
          "layer": "source", "imported_from": {"subject": old}}
    ds.st.write_text(ds.s, imported_dir(old) + "/migration.md", join(fm, "\n" + "\n".join(lines) + "\n"))
    ds.log(f"{old} からの移行を終えた: [記録]({imported_dir(old)}/migration.md)。移した Discussion {len(used)}・移さなかった {len(full) - len(used)}")
