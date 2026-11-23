"""Discussion と L1・L2 を書き換える、決定論的な関数（REQUIREMENTS G-1）。

AI（Gemini）は中身（本文・題名・説明・候補・判断）だけを渡し、次のことはすべてここがコードで行う:
荷札（frontmatter）、Discussion の番号とフォルダ名、状態（open・review・closed）、承認（stable・verified）、
取り下げ（書く直前の版に戻す）、目録（E-2）、巻き戻し、`log.md`、人の修正の記録（C-6）。

置き場所（PLAN「M2 の作り方」）:
- /discussions/<YYYY-MM-DD>-dNNN/discussion_summary.md   要約（「要約を作る」で書く）
- 会話中の作業の状態と、会話中・確認待ちの一覧（registry）は、state（app/lattice/state.py）に置く。closed で消す（C-11）
- /.lattice/manifests/D-NNN.json                       目録（closed のたびに、全ファイル → 版）
- /.lattice/edits.json                                 人の修正の記録（C-6）
"""
import datetime as dt
import functools
import json
import re
import threading

import yaml

from .storage import Storage, StorageError

JST = dt.timezone(dt.timedelta(hours=9))
REGISTRY = "/.lattice/registry.json"
EDITS = "/.lattice/edits.json"
REWIND_MAX, REWIND_DAYS = 10, 30


# 利用者の操作と、裏で動く Gemini の処理が同じファイルを読み書きするので、書き込みはこのロックの中で行う。
# Gemini を呼んでいる間は持たない（呼び出しは長いので）。同じスレッドから入り直せるよう RLock にする
LOCK = threading.RLock()


def locked(f):
    @functools.wraps(f)
    def g(*a, **k):
        with LOCK:
            return f(*a, **k)
    return g


class Refused(Exception):
    """決まりに合わない操作。画面には理由として出し、AI には関数の結果として返す。"""


def now():
    return dt.datetime.now(JST).isoformat(timespec="seconds")


def split(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
    return {}, text


def join(fm, body):
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000) + "---\n" + body


def _is_card(path):
    name = path.rsplit("/", 1)[-1]
    return name.startswith("source-") and name.endswith(".md")


def _num(did):
    return int(did.split("-")[1])


class Discussions:
    def __init__(self, st: Storage, subject: str, email: str = "", name: str = "", model: str = "gemini", state=None):
        from .state import LocalState
        self.st, self.s = st, subject
        self.state = state or LocalState(st)
        self.email = email
        self.name = name or (email.split("@")[0] if email else "user")
        self.model = model

    # ---------- 読み書きの土台 ----------
    def _json(self, path, default):
        try:
            return json.loads(self.st.read_text(self.s, path))
        except StorageError:
            return default

    def _put_json(self, path, obj):
        self.st.write_text(self.s, path, json.dumps(obj, ensure_ascii=False, indent=1))

    def registry(self):
        reg = self.state.get(self.s, "registry")
        if reg is None:  # 初めて（または消えた）: 番号は要約から数え直す
            reg = {"last": max([0] + [_num(d) for d in self._summaries()]), "open": {}}
        return reg

    def _put_registry(self, reg):
        self.state.put(self.s, "registry", reg)

    def _summaries(self):
        """closed を含む、要約のある Discussion（番号 → フォルダ）。"""
        return {did: v[0] for did, v in self.summaries_full().items()}

    def summaries_full(self):
        """要約のある Discussion（番号 → (フォルダ, 前付け, 本文)）。要約はまとめて同時に読む
        （1つずつ読むと、Discussion が増えるほど一覧の表示が遅くなった。M4）。"""
        from concurrent.futures import ThreadPoolExecutor
        paths = [(e.path, m.group(1)) for e in self.st.walk(self.s)
                 for m in [re.match(r"^(/discussions/[^/]+/)discussion_summary\.md$", e.path)] if m]

        def one(pf):
            try:
                return pf[1], split(self.st.read_text(self.s, pf[0]))
            except StorageError:
                return pf[1], ({}, None)
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(one, paths))
        return {fm["discussion_id"]: (folder, fm, body) for folder, (fm, body) in got if fm.get("discussion_id")}

    def folder(self, did):
        reg = self.registry()
        if did in reg["open"]:
            return reg["open"][did]
        d = self._summaries().get(did)
        if not d:
            raise Refused(f"{did} はありません")
        return d

    def work(self, did):
        w = self.state.get(self.s, f"work/{did}")
        if w is None:
            raise Refused(f"{did} は会話中ではありません")
        return w

    @locked
    def update_work(self, did, fn):
        """work を読み、fn で直して書く（ロックの中で）。"""
        w = self.work(did)
        fn(w)
        self.save_work(w)
        return w

    def progress(self, did, text):
        """画面に出す途中経過。表示のためだけなので、失敗しても本来の処理は止めない。"""
        try:
            import time

            def f(w):
                w["progress"] = text
                w.setdefault("wait_since", time.time())  # 待ちが始まった時刻（画面に経過時間を出す）
            self.update_work(did, f)
        except Exception:  # noqa: BLE001
            pass

    def save_work(self, w):
        if not w.get("waiting"):  # 待ちが終わったら、途中経過と始まりの時刻を消す（次の待ちに古い表示が残らないように）
            w.pop("wait_since", None)
            w.pop("progress", None)
            w.pop("feed", None)
        w["updated"] = now()
        self.state.put(self.s, f"work/{w['id']}", w)

    def log(self, line):
        """log.md の今日の見出しの下に1行足す（新しい順）。"""
        today = dt.datetime.now(JST).date().isoformat()
        try:
            text = self.st.read_text(self.s, "/log.md")
        except StorageError:
            text = "# 更新記録\n"
        head = f"## {today}\n"
        if head in text:
            text = text.replace(head, head + f"- {line}\n", 1)
        else:
            i = text.find("\n## ")
            block = f"\n{head}- {line}\n"
            text = text + block if i == -1 else text[:i] + block + text[i:]
        self.st.write_text(self.s, "/log.md", text)

    # ---------- 始める・話す（C-7） ----------
    @locked
    def create(self, first_text, origin="list", viewing=None):
        """最初の発言で Discussion を作る（C-7）。番号はここで付ける。"""
        if not str(first_text or "").strip():
            raise Refused("最初の発言がありません")
        reg = self.registry()
        n = max([reg["last"]] + [_num(d) for d in reg["open"]]) + 1
        did = f"D-{n:03d}"
        date = dt.datetime.now(JST).date().isoformat()
        folder = f"/discussions/{date}-d{n:03d}/"
        reg["last"] = n
        reg["open"][did] = folder
        self._put_registry(reg)
        w = {"id": did, "dir": folder, "date": date, "origin": origin, "state": "open", "started": now(),
             "title": "", "messages": [], "history": [], "read": [], "candidates": [], "drafts": [], "waiting": None,
             "viewing": viewing}
        self.save_work(w)
        self.add_user_message(did, first_text)
        return did

    @locked
    def add_user_message(self, did, text):
        w = self.work(did)
        if w["state"] != "open":
            raise Refused("会話中の Discussion にだけ話しかけられます")
        if w.get("waiting"):
            raise Refused("エージェントの返事を待っています。返事が出てから送ってください")
        # hist・summary_at: 取り消すときに、AI の記録（history）と要約をこの発言の直前と比べて戻すための印
        w["messages"].append({"role": "user", "text": text, "at": now(), "hist": len(w["history"]), "summary_at": w.get("summary_at")})
        w["waiting"] = "reply"
        self.save_work(w)

    def _undo_target(self, w):
        """取り消せる発言の位置（最後の自分の発言）。取り消せないなら、その理由を Refused で返す。"""
        if w["state"] != "open":
            return Refused("会話中のときだけ取り消せます")
        if w.get("waiting"):
            return Refused("エージェントの返事を待っています。返事が出てから取り消してください")
        i = next((i for i in range(len(w["messages"]) - 1, -1, -1) if w["messages"][i]["role"] == "user"), None)
        if i is None or "hist" not in w["messages"][i]:
            return Refused("取り消せる発言がありません")
        if not any(m["role"] == "user" for m in w["messages"][:i]):
            return Refused("最初の発言は取り消せません（この Discussion ごと要らなければ「廃棄する」）")
        if any(m["role"] != "agent" for m in w["messages"][i + 1:]):
            return Refused("この発言のあとに資料の受け渡しやファイルの修正があるので、取り消せません")
        if w.get("summary_at") != w["messages"][i].get("summary_at"):
            return Refused("この発言のあとに要約が作られたので、取り消せません")
        return i

    def can_undo_message(self, w):
        return not isinstance(self._undo_target(w), Refused)

    @locked
    def undo_message(self, did):
        """最後の自分の発言と、それへの AI の返事を取り消す。AI の記録も発言の直前に戻す。発言の本文を返す（入力欄に戻す）。"""
        w = self.work(did)
        i = self._undo_target(w)
        if isinstance(i, Refused):
            raise i
        m = w["messages"][i]
        w["messages"] = w["messages"][:i]
        w["history"] = w["history"][:m["hist"]]
        self.save_work(w)
        return m["text"]

    # ---------- 要約と候補（C-9） ----------
    @locked
    def write_summary(self, did, title, description, body, extra_sources=()):
        w = self.work(did)
        if w["state"] not in ("open", "review"):
            raise Refused("要約は会話中か確認待ちのときだけ書けます")
        body = body.strip() + "\n"
        need = [("話したこと", "What we discussed"), ("決定", "Decisions"), ("持ち越し", "Carry-over")]
        if not all(re.search(rf"^## ({ja}|{en})", body, re.M) for ja, en in need):
            raise Refused("要約には「## 話したこと」「## 決定」「## 持ち越し」の節が要ります（英語の Subject では What we discussed・Decisions・Carry-over）")
        w["title"] = str(title).strip()[:80]
        path = w["dir"] + "discussion_summary.md"
        prev, _ = split(self.st.read_text(self.s, path)) if self.st.exists(self.s, path) else ({}, "")
        srcs = []
        if w["origin"].startswith("continues:"):
            srcs.append(self.folder(w["origin"].split(":", 1)[1]) + "discussion_summary.md")
        srcs += [p for p in list(w["read"]) + list(extra_sources)
                 if p not in srcs and (not p.startswith(w["dir"]) or _is_card(p)) and self.st.exists(self.s, p)]  # 自分の要約は入れない。渡された資料のカードは入れる
        fm = {"type": "Discussion Summary", "title": f"{did} {w['title']}", "description": str(description).strip(),
              "layer": "discussion", "date": w["date"], "discussion_id": did, "discussion_state": w["state"],
              "generated": {"by": self.model, "at": now()},
              "sources": [{"resource": p, "title": self._title(p)} for p in srcs] or prev.get("sources", [])}
        self.st.write_text(self.s, path, join(fm, f"\n# {did} {w['title']}\n\n" + body))
        w["summary_at"] = now()  # 要約より前の発言は取り消せない（要約と会話が食い違うので）
        if w.get("waiting") in ("summary", "carryover"):
            w["waiting"] = None
        self.save_work(w)

    def summary(self, did):
        path = self.folder(did) + "discussion_summary.md"
        return split(self.st.read_text(self.s, path)) if self.st.exists(self.s, path) else ({}, None)

    def _title(self, path):
        try:
            fm, body = split(self.st.read_text(self.s, path))
        except StorageError:
            return path
        return fm.get("title") or next((l[2:].strip() for l in body.splitlines() if l.startswith("# ")), path)

    def facet_ids(self):
        fm, _ = split(self.st.read_text(self.s, "/facets.md"))
        out = []
        def walk(gs):
            for g in gs or []:
                for f in g.get("facets", []) or []:
                    if not f.get("deprecated"):  # 「使わない」の Facet は候補に出さない（D-5）
                        out.append(f["id"])
                walk(g.get("groups"))
        walk(fm.get("groups"))
        return out

    @locked
    def set_candidates(self, did, cands):
        w = self.work(did)
        if w["state"] != "open":
            raise Refused("候補は会話中にだけ出せます")
        ids = set(self.facet_ids())
        chosen = {c["facet"]: c.get("choice") for c in w["candidates"]}
        keep = [c for c in w["candidates"] if c.get("suggest") is None]  # 利用者が足した候補は残す
        out = []
        for c in cands:
            if c.get("facet") not in ids:
                raise Refused(f"Facet {c.get('facet')!r} はありません")
            sug = c.get("suggest") if c.get("suggest") in ("update", "material", "none") else "none"
            out.append({"facet": c["facet"], "reason": str(c.get("reason", ""))[:200], "suggest": sug,
                        "choice": chosen.get(c["facet"], sug)})  # 利用者が選び直したものは、作り直しても残す
        w["candidates"] = out + [c for c in keep if c["facet"] not in {x["facet"] for x in out}]
        self.save_work(w)

    @locked
    def choose(self, did, facet, choice):
        w = self.work(did)
        if w["state"] != "open":
            raise Refused("候補は会話中にだけ選べます")
        if facet not in self.facet_ids() or choice not in ("update", "material", "none"):
            raise Refused("候補が正しくありません")
        for c in w["candidates"]:
            if c["facet"] == facet:
                c["choice"] = choice
                break
        else:
            w["candidates"].append({"facet": facet, "reason": "利用者が追加", "suggest": None, "choice": choice})
        self.save_work(w)

    @locked
    def uncandidate(self, did, facet):
        w = self.work(did)
        w["candidates"] = [c for c in w["candidates"] if not (c["facet"] == facet and c.get("suggest") is None)]
        self.save_work(w)

    @locked
    def confirm(self, did):
        """「Facet 更新を確認する」（C-9 の5）。「更新する」が無ければ②としてその場で閉じる。"""
        w = self.work(did)
        if w["state"] == "review":
            raise Refused("もう確認待ちになっています。「確認の画面を開く」から進めてください")
        if w["state"] != "open" or not self.summary(did)[1]:
            raise Refused("要約を作ってから確認してください")
        if w.get("waiting"):
            raise Refused("エージェントの作業が終わってから確認してください")
        from .sources import write_web_cards
        cards = write_web_cards(self, w)  # 要約のときに選んだ Web 検索を、ここで原典カードにする（C-14）
        if cards:
            path = w["dir"] + "discussion_summary.md"
            fm, body = split(self.st.read_text(self.s, path))
            fm["sources"] = list(fm.get("sources") or []) + [{"resource": c, "title": self._title(c)} for c in cards]
            self.st.write_text(self.s, path, join(fm, body))
            self.save_work(w)
        if any(c.get("choice") == "update" for c in w["candidates"]):
            w["state"], w["waiting"], w["drafts"] = "review", "drafts", []
            self.save_work(w)
            self._set_summary_state(did, "review")
            return "review"
        self.close(did)
        return "closed"

    # ---------- draft を書く（AI の関数の受け口） ----------
    def l2_path(self, facet, issue):
        slug = re.sub(r"-context$", "", str(issue or "").strip().lower())
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
            raise Refused("L2 の論点名は英小文字とハイフンで書いてください（例: price-range）")
        return f"/facets/{facet}/{slug}-context.md"

    @locked
    def write_draft(self, did, facet, layer, body, title="", description="", issue=None, sources=()):
        w = self.work(did)
        if w["state"] != "review":
            raise Refused("draft は「Facet 更新を確認する」のあとにだけ書けます")
        if not any(c["facet"] == facet and c.get("choice") == "update" for c in w["candidates"]):
            raise Refused(f"Facet {facet!r} は「更新する」が選ばれていません")  # G-2: 選ばれていない Facet は書けない
        path = f"/facets/{facet}/{facet}.md" if layer == "L1" else self.l2_path(facet, issue)
        other = self.pending_owner(path, but=did)
        if other:
            raise Refused(f"{path} には、確認待ちの {other} の draft があります。{other} を片付けてから書いてください")
        x = next((x for x in w["drafts"] if x["facet"] == facet), None)
        if x is None:
            x = {"facet": facet, "files": [], "status": "pending"}
            w["drafts"].append(x)
        f = next((f for f in x["files"] if f["path"] == path), None)
        exists = self.st.exists(self.s, path)
        if f is None:
            f = {"path": path, "layer": layer, "before": self.st.head(self.s, path) if exists else None,
                 "old": split(self.st.read_text(self.s, path))[1] if exists else None}  # 確認の画面の「旧」（書く直前の本文）
            x["files"].append(f)
        old, _ = split(self.st.read_text(self.s, path)) if exists else ({}, "")
        summ = w["dir"] + "discussion_summary.md"
        if layer == "L1":  # L1 の出どころは自分の L2（いまある出どころは消さずに足す）
            srcs = [s["resource"] for s in old.get("sources", [])]
            srcs += [y["path"] for y in x["files"] if y["layer"] == "L2" and y["path"] not in srcs]
        else:
            srcs = [s["resource"] for s in old.get("sources", [])]
            for p in [summ, *sources]:
                if p not in srcs and (p == summ or self.st.exists(self.s, p)):
                    srcs.append(p)
        fm = dict(old)  # 既存の荷札を土台にして、アプリが決める項目だけを上書きする（独自のキーを消さない）
        fm.pop("verified", None)
        fm.pop("migrated", None)  # 移行で作った印（E-4）は、人が Discussion で直したら外す
        fm.update({"type": "Decision" if layer == "L1" else "Decision Context",
                   "title": str(title).strip() or old.get("title") or facet,
                   "description": str(description).strip() or old.get("description", ""),
                   "layer": layer, "facet": old.get("facet") or [facet],
                   "status": "draft", "generated": {"by": self.model, "at": now()},
                   "sources": [{"resource": p, "title": self._title(p)} for p in srcs]})
        self.st.write_text(self.s, path, join(fm, "\n" + body.strip() + "\n"))
        f["new"] = body.strip() + "\n"
        self.save_work(w)
        if layer == "L2":  # L1 の出どころは自分の L2 だけ。L2 が増えたら L1 の sources も足す
            l1 = f"/facets/{facet}/{facet}.md"
            if self.st.exists(self.s, l1):
                lfm, lbody = split(self.st.read_text(self.s, l1))
                if path not in [s["resource"] for s in lfm.get("sources", [])] and any(y["path"] == l1 for y in x["files"]):
                    lfm.setdefault("sources", []).append({"resource": path, "title": fm["title"]})
                    self.st.write_text(self.s, l1, join(lfm, lbody))
        return path

    @locked
    def drafts_done(self, did):
        w = self.work(did)
        w["waiting"] = None
        self.save_work(w)

    @locked
    def edit_draft(self, did, facet, path, text):
        """確認のモーダルで、利用者が新の側を直す（C-12）。"""
        w = self.work(did)
        self._not_finishing(w)
        for x in w["drafts"]:
            for f in x["files"]:
                if x["facet"] == facet and f["path"] == path and x["status"] == "pending":
                    fm, _ = split(self.st.read_text(self.s, path))
                    self.st.write_text(self.s, path, join(fm, "\n" + text.strip() + "\n"))
                    f["new"], f["edited_by_user"] = text.strip() + "\n", True
        self.save_work(w)

    # ---------- 承認は2段階（C-12） ----------
    @locked
    def mark(self, did, facet, kind):
        w = self.work(did)
        self._not_finishing(w)
        for x in w["drafts"]:
            if x["facet"] == facet and x["status"] == "pending":
                x["mark"] = kind
        self.save_work(w)

    @locked
    def finalize(self, did):
        w = self.work(did)
        pend = [x for x in w["drafts"] if x["status"] == "pending"]
        if w["state"] != "review" or not pend or any(not x.get("mark") for x in pend):
            raise Refused("すべての Facet で予定を選んでから確定してください")
        approved, withdrawn = [], []
        for x in pend:
            if x.pop("mark") == "approve":
                x["status"] = "approved"
                approved.append(x["facet"])
                for f in x["files"]:
                    fm, body = split(self.st.read_text(self.s, f["path"]))
                    for src in fm.get("sources", []):  # 出どころの題名を、書き終えた時点のものに揃える（L1 を先に書くと古い題名が残るため）
                        src["title"] = self._title(src["resource"])
                    fm["status"] = "stable"
                    fm["verified"] = [{"by": f"human:{self.name}", "at": now()}]
                    self.st.write_text(self.s, f["path"], join(fm, body))
            else:
                x["status"] = "withdrawn"
                withdrawn.append(x["facet"])
                for f in x["files"]:
                    self.st.restore(self.s, f["path"], f["before"])
        for c in w["candidates"]:  # 取り下げた Facet は「材料として残す」に（C-12）
            if c["facet"] in withdrawn:
                c["choice"] = "material"
        w["waiting"] = "finishing"  # AI が持ち越しの1行（取り下げ）と index.md を直してから閉じる
        w["finish"] = {"approved": approved, "withdrawn": withdrawn}
        if not approved:
            w["state"], w["waiting"] = "open", ("carryover" if withdrawn else None)
            self._set_summary_state(did, "open")
        self.save_work(w)
        return approved, withdrawn

    def recover_stale(self, did, limit=1000):
        """待ちが Cloud Run の上限（900秒）を超えて続いているなら、作業していたスレッドはもういない
        （デプロイや入れ替えでインスタンスが止まった）。待ちを解き、下書き待ちなら会話中に戻して、やり直せるようにする。"""
        import time
        w = self.work(did)
        if not w.get("waiting") or w["waiting"] == "finishing" or time.time() - w.get("wait_since", time.time()) < limit:
            return False
        if w["waiting"] == "drafts":
            self.back(did)
            msg = "（Facet の更新の下書きを作る途中で、サーバーが入れ替わって止まりました。もう一度「Facet 更新を確認する」を押してください）"
        else:
            msg = "（返事を作る途中で、サーバーが入れ替わって止まりました。もう一度送るか、ボタンを押し直してください）"
        self.update_work(did, lambda w: (w["messages"].append({"role": "agent", "text": msg, "at": now()}), w.update(waiting=None)))
        return True

    @locked
    def back(self, did):
        """Discussion に戻る: draft を全部、書く直前の版に戻して open に。"""
        w = self.work(did)
        self._not_finishing(w)
        for x in w["drafts"]:
            if x["status"] == "pending":
                x["status"] = "withdrawn"
                x.pop("mark", None)
                for f in x["files"]:
                    self.st.restore(self.s, f["path"], f["before"])
        w["state"], w["waiting"] = "open", None
        self.save_work(w)
        self._set_summary_state(did, "open")

    def fill_olds(self, w):
        """「旧」の本文を持っていない draft（直す前に作られたもの）に、書く直前の版から補う。"""
        if not any("old" not in f for x in w["drafts"] for f in x["files"]):
            return w

        def fill(w):
            for x in w["drafts"]:
                for f in x["files"]:
                    if "old" not in f:
                        f["old"] = (split(self.st.read_rev(self.s, f["path"], f["before"]).decode("utf-8"))[1]
                                    if f["before"] else None)
        return self.update_work(w["id"], fill)

    def _not_finishing(self, w):
        if w.get("waiting") == "finishing":
            raise Refused("確定した内容を仕上げています。終わるまで待ってください")

    def pending_owner(self, path, but=None):
        """そのファイルに、確認待ちの draft を持っている Discussion（C-12 のぶつかり）。"""
        for did in self.registry()["open"]:
            if did == but:
                continue
            try:
                w = self.work(did)
            except Refused:
                continue
            if any(f["path"] == path and x["status"] == "pending" for x in w["drafts"] for f in x["files"]):
                return did
        return None

    def _set_summary_state(self, did, state):
        fm, body = self.summary(did)
        if body is not None:
            fm["discussion_state"] = state
            self.st.write_text(self.s, self.folder(did) + "discussion_summary.md", join(fm, body))

    # ---------- 閉じる・廃棄・巻き戻し（C-10・E-2） ----------
    @locked
    def close(self, did, index_changed=False, by=None, manifest=True):
        w = self.work(did)
        self.progress(did, "要約を閉じた状態にしています…")
        self._set_summary_state(did, "closed")
        fin = w.get("finish", {})
        ap = "・".join(fin.get("approved", [])) or "なし"
        wd = "・".join(fin.get("withdrawn", []))
        self.log(f"{did} を閉じた: [要約]({w['dir']}discussion_summary.md)。承認: {ap}" + (f"。取り下げ（材料として残す）: {wd}" if wd else "")
                 + f"（{by or 'human:' + self.name}）" + ("。index.md の要点を合わせて直した" if index_changed else ""))
        if manifest:  # 移行の途中の一人 Discussion は書かない（E-4。最後の Facet で書く）
            self.progress(did, "巻き戻し用の目録を作っています…")
            self.write_manifest(did)
        from .sources import drop_hidden
        drop_hidden(self, w)
        self.state.delete(self.s, f"work/{did}")
        reg = self.registry()
        reg["open"].pop(did, None)
        self._put_registry(reg)

    @locked
    def discard(self, did):
        """③ 廃棄。draft を戻し、Discussion のフォルダを消す。番号は使い回さない。"""
        w = self.work(did)
        self._not_finishing(w)
        if w["state"] == "review":
            self.back(did)
            w = self.work(did)
        # 人の修正で stable → draft にしたのを戻す（本文は人の修正のまま）。修正から始めた Discussion と、会話の途中の修正（C-6）
        for eb in ([w["edit_before"]] if w.get("edit_before") else []) + w.get("edit_befores", []):
            p, was = eb["path"], eb["status"]
            if self.st.exists(self.s, p):
                fm, body = split(self.st.read_text(self.s, p))
                fm["status"] = was
                if eb.get("verified"):
                    fm["verified"] = eb["verified"]
                self.st.write_text(self.s, p, join(fm, body))
        from .sources import drop_hidden
        drop_hidden(self, w)
        for e in self.st.walk(self.s):
            if e.path.startswith(w["dir"]):
                self.st.delete(self.s, e.path)
        self.state.delete(self.s, f"work/{did}")
        reg = self.registry()
        reg["open"].pop(did, None)
        self._put_registry(reg)
        self.log(f"{did} を廃棄した（Facet への影響なし）")

    def manifest(self):
        return self.st.heads(self.s)

    def write_manifest(self, did):
        files = self.manifest()
        self._put_json(f"/.lattice/manifests/{did}.json", {"id": did, "at": now(), "files": files})
        # 戻れる範囲の版をドライブが消さないように印を付ける（E-2）。前回の目録から変わった版だけ、並べて付ける
        last = self._json("/.lattice/manifests/_kept.json", {})
        todo = [(p, r) for p, r in files.items() if r and last.get(p) != r]

        def one(pr):
            try:
                self.st.keep(self.s, *pr)
                return None
            except Exception:  # noqa: BLE001  印の上限（1ファイル200版）などで付けられなくても閉じるのは止めない
                return pr[0]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            failed = [x for x in ex.map(one, todo) if x]
        for x in failed:
            self.log(f"{did} の目録の版に印を付けられなかった: {x}")
        self._put_json("/.lattice/manifests/_kept.json", {p: r for p, r in files.items() if p not in failed})

    def rewindable(self, summaries=None):
        """戻れる Discussion（直近10個かつ30日以内、E-2）。新しい順。目録は新しい順に、要る数だけ同時に読む。"""
        from concurrent.futures import ThreadPoolExecutor
        ids = sorted(summaries if summaries is not None else self._summaries(), key=_num, reverse=True)[:REWIND_MAX + 5]
        with ThreadPoolExecutor(8) as ex:
            ms = list(ex.map(lambda d: self._json(f"/.lattice/manifests/{d}.json", None), ids))
        out = [{"id": d, "at": m["at"]} for d, m in zip(ids, ms) if m]
        limit = dt.datetime.now(JST) - dt.timedelta(days=REWIND_DAYS)
        return [x for x in out[:REWIND_MAX] if dt.datetime.fromisoformat(x["at"]) >= limit]

    @locked
    def rewind(self, did):
        """「この Discussion の直後に戻る」（E-2）。戻す直前の状態も目録に残す。"""
        if self.registry()["open"]:
            raise Refused("会話中・確認待ちの Discussion があるときは戻せません")
        if any(e.get("waiting") for e in self.edits()):
            raise Refused("人の修正の判断を待っています。終わってから戻してください")
        if did not in [x["id"] for x in self.rewindable()]:
            raise Refused(f"{did} には戻れません（直近10個かつ30日以内）")
        target = self._json(f"/.lattice/manifests/{did}.json", None)["files"]
        self._put_json("/.lattice/manifests/_before_rewind.json",
                       {"id": did, "at": now(), "files": self.manifest(), "last": self.registry()["last"]})
        cur = self.manifest()
        for p in set(cur) | set(target):
            if p == "/log.md":
                continue
            self.st.restore(self.s, p, target.get(p))
        later = sorted({x for x in self._all_ids_in(cur) if _num(x) > _num(did)}, key=_num)
        self.log(f"{did} の直後に戻した。" + (f"{'・'.join(later)} を取り消し" if later else ""))
        return later

    @locked
    def undo_rewind(self):
        """戻したのを取り消す（E-2）。次の Discussion を始めたら（番号が進んだら）できない。"""
        b = self._json("/.lattice/manifests/_before_rewind.json", None)
        if not b or b.get("used") or self.registry()["last"] != b["last"] or self.registry()["open"]:
            raise Refused("戻したのを取り消せるのは、次の Discussion を始めるまでです")
        cur = self.manifest()
        for p in set(cur) | set(b["files"]):
            if p != "/log.md":
                self.st.restore(self.s, p, b["files"].get(p))
        b["used"] = True
        self._put_json("/.lattice/manifests/_before_rewind.json", b)
        self.log(f"{b['id']} の直後に戻したのを取り消した")

    def undoable(self):
        """「戻したのを取り消す」ができるなら、戻した先の Discussion の番号（画面のボタン用）。"""
        b = self._json("/.lattice/manifests/_before_rewind.json", None)
        if not b or b.get("used") or self.registry()["last"] != b["last"] or self.registry()["open"]:
            return None
        return b["id"]

    def _all_ids_in(self, files):
        return {f"D-{int(m):03d}" for p in files for m in re.findall(r"^/discussions/[^/]+-d(\d+)/", p)}

    # ---------- 人の修正（C-6） ----------
    @locked
    def save_edit(self, path, text, into=None):
        """人の修正（C-6）。into は、修正を含める会話中の Discussion（会話の途中で直したとき）。"""
        if into:
            w = self.work(into)
            if w["state"] != "open":
                raise Refused(f"{into} は会話中ではないので、修正を含められません")
            if w.get("waiting"):
                raise Refused(f"{into} はエージェントの返事を待っています。返事が出てから直してください")
        if not re.match(r"^/facets/[^/]+/[^/]+\.md$", path) or not self.st.exists(self.s, path):
            raise Refused("直せるのは L1・L2 の本文だけです")
        fm, old = split(self.st.read_text(self.s, path))
        if fm.get("layer") not in ("L1", "L2"):
            raise Refused("直せるのは L1・L2 の本文だけです")
        other = self.pending_owner(path)
        if other:
            raise Refused(f"{path} には、確認待ちの {other} の draft があります。確認のモーダルで直してください")
        before = self.st.head(self.s, path)
        self.st.write_text(self.s, path, join(fm, "\n" + text.strip() + "\n"))
        edits = self._json(EDITS, [])
        n = max([int(x["id"][2:]) for x in edits], default=0) + 1
        e = {"id": f"E-{n:03d}", "path": path, "at": now(), "by": self.email, "before": before,
             "old": old, "text": text.strip() + "\n", "waiting": "judge", "judgement": None, "into": into}
        edits.insert(0, e)
        self._put_json(EDITS, edits)
        return e["id"]

    def edits(self):
        return self._json(EDITS, [])

    @locked
    def judge(self, eid, kind, reason):
        """AI の判断を受ける。minor は stable のまま log に1行。meaning は draft に戻し、Discussion を始める。"""
        edits = self.edits()
        e = next((x for x in edits if x["id"] == eid), None)
        if e is None:
            raise Refused(f"{eid} はありません")
        e["judgement"], e["reason"], e["waiting"] = kind, reason, None
        into = e.get("into")
        if into:  # 会話の途中で直した: その Discussion に含める（C-6）
            try:
                self.work(into)
            except Refused:
                into = None  # 判断を待つ間に閉じた・廃棄した Discussion なら、会話の外の修正として扱う
        if kind == "minor":
            self.log(f"[{self._title(e['path'])}]({e['path']}) を直した（体裁だけ: {reason}、{self.email}）")
            if into:
                self.update_work(into, lambda w: w["messages"].append(
                    {"role": "edit", "text": f"{e['path']} を直した（体裁だけ: {reason}）", "path": e["path"], "at": now()}))
        elif into:
            fm, body = split(self.st.read_text(self.s, e["path"]))
            before = json.loads(json.dumps({"path": e["path"], "status": fm.get("status"), "verified": fm.get("verified")}, default=str))
            if fm.get("status") == "stable":
                fm["status"] = "draft"
                fm.pop("verified", None)
                self.st.write_text(self.s, e["path"], join(fm, body))
            facet = e["path"].split("/")[2]
            e["discussion"] = into

            def f(w):
                w.setdefault("edit_befores", []).append(before)
                if not any(c["facet"] == facet for c in w["candidates"]):  # 直した Facet は「更新する」の候補に入れる（draft のまま残さないため）
                    w["candidates"].append({"facet": facet, "reason": "あなたが本文を直した", "suggest": None, "choice": "update"})
                w["messages"].append({"role": "edit", "text": f"{e['path']} を直した（意味が変わる修正: {reason}）", "path": e["path"], "at": now()})
                w["waiting"] = "reply"
            self.update_work(into, f)
        else:
            fm, body = split(self.st.read_text(self.s, e["path"]))
            before = json.loads(json.dumps({"path": e["path"], "status": fm.get("status"), "verified": fm.get("verified")},
                                           default=str))  # 荷札の日時は文字列にして持つ
            if fm.get("status") == "stable":
                fm["status"] = "draft"
                fm.pop("verified", None)
                self.st.write_text(self.s, e["path"], join(fm, body))
            did = self.create(f"（{e['path']} の本文を直しました）", origin=f"edit:{e['path']}")
            e["discussion"] = did
            w = self.work(did)
            w["edit"], w["edit_before"] = eid, before
            w["candidates"].append({"facet": e["path"].split("/")[2], "reason": "あなたが本文を直した", "suggest": None, "choice": "update"})
            self.save_work(w)
        self._put_json(EDITS, edits)
        return e.get("discussion")
