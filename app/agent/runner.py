"""エージェントのループ（PLAN 計画2: 自分で書く。ADK は使わない）。

Gemini に「場面ごとに絞った関数の一覧」を渡し、返ってきた関数の依頼をアプリのコードで実行して結果を返す。
書き込みはすべて app/lattice/discussion.py の決定論的な関数を通る（G-1）。関数が断ったら、その理由を Gemini に返す。

場面（PLAN「M2 の作り方」）:
- reply      会話の返事。読むだけ
- summary    要約と Facet の候補
- drafts     「更新する」を選ばれた Facet の L1・L2 の draft
- carryover  取り下げた Facet の要点を持ち越しに1行
- index      確定した L1・L2 に合わせて index.md（L0）を直す（承認は求めない、C-12）
- judge      人の修正が体裁だけか、意味の変更か（C-6）
- edit_open  意味の変更で始めた Discussion の最初のひと言
- continue   引き継いで始めた Discussion の最初のひと言
"""
import os
import re
import pathlib
import time

from google import genai
from google.genai import types

from ..lattice import sources
from ..lattice.discussion import Discussions, Refused, join, now, split
from ..lattice.storage import StorageError

INSTRUCTIONS = (pathlib.Path(__file__).parent / "instructions.ja.md").read_text()
PROJECT = os.environ.get("LATTICE_PROJECT")
MODEL = os.environ.get("LATTICE_MODEL", "gemini-3.8-flash")
MAX_TURNS = 24  # 1回の応答で呼べる関数の回数の上限（SECURITY ASI08）

_client = None


def client():
    global _client
    if _client is None:
        cred = pathlib.Path.home() / ".config/gcloud-lattice/application_default_credentials.json"
        if cred.is_file() and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred)
        _client = genai.Client(vertexai=True, project=PROJECT, location="global")
    return _client


from ..timing import timed as _timed  # noqa: E402


@_timed("gemini")
def generate(model, contents, cfg, tries=5, on_thought=None):
    """混み合い（429）や一時的な不調（500・503）は、少しずつ待ってやり直す。
    on_thought を渡すと、少しずつ受け取り（ストリーミング）、考えの要約が届くたびに呼ぶ（待ち時間の表示用）。"""
    from google.genai import errors
    from . import usage
    try:
        usage.check()  # 1日の利用上限（REQUIREMENTS H）
    except usage.OverLimit as e:
        raise Refused(str(e))
    for i in range(tries):
        try:
            r = _stream(model, contents, cfg, on_thought) if on_thought else client().models.generate_content(model=model, contents=contents, config=cfg)
            usage.add(r)
            return r
        except errors.APIError as e:
            if getattr(e, "code", None) not in (429, 500, 503) or i == tries - 1:
                raise
            print(f"[timing] Gemini {getattr(e, 'code', None)} のため {2 ** i * 3} 秒待ってやり直す", flush=True)
            time.sleep(2 ** i * 3)  # 3, 6, 12, 24 秒


class _Streamed:
    """少しずつ受け取った応答を、ふつうの応答と同じ形（candidates[0].content・usage_metadata）にまとめたもの。"""
    def __init__(self, parts, usage_metadata):
        self.candidates = [types.Candidate(content=types.Content(role="model", parts=parts))] if parts else []
        self.usage_metadata = usage_metadata


def _stream(model, contents, cfg, on_thought):
    cfg = cfg.model_copy(update={"thinking_config": types.ThinkingConfig(include_thoughts=True)})
    parts, um = [], None
    for ch in client().models.generate_content_stream(model=model, contents=contents, config=cfg):
        um = ch.usage_metadata or um
        c = ch.candidates[0].content if ch.candidates and ch.candidates[0].content else None
        for p in (c.parts if c and c.parts else []):
            if p.thought and p.text:
                try:
                    on_thought(p.text)
                except Exception:  # noqa: BLE001  表示のためだけ
                    pass
            last = parts[-1] if parts else None
            # 続けて届いた文字は1つにまとめる（署名の付いた部品と関数の依頼は、そのまま残す）
            if (last is not None and p.text is not None and last.text is not None and not p.function_call
                    and bool(p.thought) == bool(last.thought) and not p.thought_signature and not last.thought_signature):
                parts[-1] = last.model_copy(update={"text": last.text + p.text})
            else:
                parts.append(p)
    return _Streamed(parts, um)


def fn(name, desc, props=None, required=()):
    return types.FunctionDeclaration(name=name, description=desc,
                                     parameters={"type": "object", "properties": props or {}, "required": list(required)})


S = {"type": "string"}
READ = [
    fn("list_files", "Subject の中のファイルのパスと題名の一覧を返す（隠しファイルは含まない）"),
    fn("read_file", "Subject の中のファイルを荷札ごと読む。パスは / 始まり", {"path": S}, ["path"]),
    fn("search_text", "Subject 全体から文字列を探し、見つかったファイルと行を返す（食い違いの確認に使う）", {"text": S}, ["text"]),
]
WRITE_SUMMARY = fn("write_summary", "要約の本文を書く（書き直す）。荷札と見出しの行はアプリが付ける。"
                   "searches には、この会話の Web 検索のうち要約の根拠に使ったものの ID（web-1 など）を入れる。それが原典カードの案になる",
                   {"title": S, "description": S, "body": S, "searches": {"type": "array", "items": S}}, ["title", "description", "body"])
PROPOSE = fn("propose_candidates", "関係しそうな Facet の候補を出す。facets.md にある Facet ID だけ",
             {"candidates": {"type": "array", "items": {"type": "object", "properties": {
                 "facet": S, "reason": S, "suggest": {"type": "string", "enum": ["update", "material", "none"]}},
                 "required": ["facet", "reason", "suggest"]}}}, ["candidates"])
WRITE_L1 = fn("write_l1_draft", "L1 の draft の本文を書く。パスは渡さない（アプリが Facet ID から決める）",
              {"facet": S, "title": S, "description": S, "body": S}, ["facet", "body"])
WRITE_L2 = fn("write_l2_draft", "L2 の draft の本文を書く。issue は論点名（英小文字とハイフン、既存の論点なら同じ名前）",
              {"facet": S, "issue": S, "title": S, "description": S, "body": S,
               "sources": {"type": "array", "items": S, "description": "この Discussion 以外に根拠にしたファイルのパス"}},
              ["facet", "issue", "body"])
WRITE_INDEX = fn("write_index", "index.md（L0 の目次）の本文を書き直す。荷札は変えない", {"body": S}, ["body"])
READ_SOURCE = fn("read_source", "この Discussion で渡された資料を読む。card は原典カードのパス（source-….md）。"
                 "表は中身を文字で、pdf・画像・pptx・docx は資料そのものを見せる", {"card": S}, ["card"])
IMPORT_DRIVE = fn("import_drive", "ユーザーがチャットに貼ったドライブのファイルの URL を、この Discussion の資料として取り込む"
                  "（原典カードはアプリが作る）。取り込んだら read_source で読む", {"url": S}, ["url"])
WEB_SEARCH = fn("web_search", "Web を検索する（Web Grounding for Enterprise）。結果は会話中の記録に残る（原典カードにするかは要約のときに決める）。"
                "question には、何を知りたいかを具体的に1つ書く", {"question": S}, ["question"])
READ_URL = fn("read_url", "ユーザーがチャットに貼った Web ページ・YouTube の URL を読む（ドライブの URL は import_drive）。"
              "中身は原典カードとして Discussion に残る。focus には、そのページ・動画で何を確かめたいかを書く",
              {"url": S, "focus": S}, ["url"])
RUN_PYTHON = fn("run_python", "計算部屋（ネットワークに出られない Python のサンドボックス）でコードを動かす。pandas・numpy・openpyxl・matplotlib が使える。"
                "cards に渡した原典カードの資料が、同じ名前（source-….xlsx など）のファイルとして部屋に置かれる。"
                "変数とファイルは、この Discussion の間は残る。結果は print で出す",
                {"code": S, "cards": {"type": "array", "items": S, "description": "部屋に置く資料の原典カードのパス"}}, ["code"])
JUDGE = fn("judge_edit", "人の修正の判断を返す", {"kind": {"type": "string", "enum": ["minor", "meaning"]}, "reason": S},
           ["kind", "reason"])

PLAN_MIG = fn("plan_migration", "組み直して移す（E-4）の割り当て。新しい Facet ごとに、材料にする前の Facet と前の Discussion を選ぶ",
              {"plan": {"type": "array", "items": {"type": "object", "properties": {
                  "facet": S, "old_facets": {"type": "array", "items": S}, "old_discussions": {"type": "array", "items": S}},
                  "required": ["facet", "old_facets", "old_discussions"]}}}, ["plan"])
WRITE_MIG_SUMMARY = fn("write_migration_summary", "一人 Discussion の要約を書く。old_discussions には、根拠にした前の Subject の Discussion の番号（D-003 など）を入れる。"
                       "アプリがそれを新しい Subject に複製して出典にする",
                       {"title": S, "description": S, "body": S, "old_discussions": {"type": "array", "items": S}},
                       ["title", "description", "body", "old_discussions"])
TOOLS = {"reply": READ + [READ_SOURCE, IMPORT_DRIVE, READ_URL, WEB_SEARCH, RUN_PYTHON, WRITE_SUMMARY, PROPOSE],  # チャットで「要約を作って」と頼まれたら、ボタンと同じことをする（C-9）
         "continue": READ + [READ_SOURCE], "edit_open": READ,
         "summary": READ + [READ_SOURCE, WRITE_SUMMARY, PROPOSE], "carryover": READ + [WRITE_SUMMARY],
         "drafts": READ + [READ_SOURCE, WRITE_L1, WRITE_L2], "index": READ + [WRITE_INDEX], "judge": READ + [JUDGE],
         "migrate_plan": [PLAN_MIG], "migrate": [WRITE_MIG_SUMMARY, WRITE_L1, WRITE_L2]}


class Run:
    """1回の応答。Gemini の関数の依頼を、場面で許した関数だけ実行する。"""

    def __init__(self, ds: Discussions, did: str | None, mode: str, model: str = MODEL):
        self.ds, self.did, self.mode, self.model = ds, did, mode, model
        self.calls, self.result, self.attach = [], {}, []
        self.t_gemini, self.n_gemini, self.t_tools = 0.0, 0, {}  # どこに時間がかかったか
        self.feed, self._feed_t = [], 0.0  # 待ち時間に見せる、AI とアプリのやりとりの記録

    def _say(self, kind, text, force=False):
        """やりとりを1行足す。画面への書き込みは1秒に1回まで（考えの要約は2〜3秒おきに届く）。"""
        if not self.did:
            return
        text = " ".join(str(text).replace("**", " ").split())  # 考えの要約は「**見出し** 本文」の形で届く
        self.feed.append({"k": kind, "t": text[:300], "at": round(time.time(), 1)})
        self.feed = self.feed[-30:]
        if force or time.time() - self._feed_t > 1.0:
            self._feed_t = time.time()
            feed = list(self.feed)
            try:
                self.ds.update_work(self.did, lambda w: w.update(feed=feed) if w.get("waiting") else None)
            except Exception:  # noqa: BLE001  表示のためだけ
                pass

    # ---- 関数の中身 ----
    def _read_paths(self, path):
        if self.did:
            self.ds.update_work(self.did, lambda w: path in w["read"] or w["read"].append(path))

    def call(self, name, a):
        st, s = self.ds.st, self.ds.s
        if name == "list_files":
            from concurrent.futures import ThreadPoolExecutor
            es = st.walk(s)
            md = [e.path for e in es if e.path.endswith(".md")]
            with ThreadPoolExecutor(8) as ex:  # 題名を読むのはファイルごとに1回ずつの通信なので、並べて読む（M4: 79秒 → 数秒）
                titles = dict(zip(md, ex.map(self.ds._title, md)))
            return {"files": [{"path": e.path, "title": titles[e.path]} if e.path in titles else {"path": e.path} for e in es]}
        if name == "read_file":
            p = "/" + str(a.get("path", "")).lstrip("/")
            if any(x.startswith(".") for x in p.split("/")):
                raise Refused("隠しファイルは読めません")
            text = st.read_text(s, p)
            if p.startswith("/facets/") or p.startswith("/discussions/"):
                self._read_paths(p)
            return {"path": p, "content": text}
        if name == "search_text":
            q = str(a.get("text", ""))
            if len(q) < 2:
                raise Refused("2文字以上で探してください")
            hits = []
            for e in st.walk(s):
                if e.path.endswith(".md"):
                    for i, line in enumerate(st.read_text(s, e.path).splitlines(), 1):
                        if q in line:
                            hits.append({"path": e.path, "line": i, "text": line[:200]})
            return {"hits": hits[:60]}
        if name == "read_source":
            return self._read_source(str(a.get("card", "")))
        if name == "import_drive":
            from ..lattice import drive_files
            fname, data, url = drive_files.fetch(a.get("url"))
            return {"ok": True, "card": sources.add(self.ds, self.did, fname, data, url=url, by_drive=True)}
        if name == "read_url":
            return self._read_url(str(a.get("url", "")).strip(), str(a.get("focus", "")).strip())
        if name == "run_python":
            return self._run_python(str(a.get("code", "")), list(a.get("cards", []) or []))
        if name == "web_search":
            q = str(a.get("question", "")).strip()
            if not q:
                raise Refused("question が空です")
            answer, links = web_search(q, self.model)
            sid = sources.add_web(self.ds, self.did, q, answer, links)
            return {"search": sid, "answer": answer, "sources": links}
        if name == "write_summary":
            self.ds.write_summary(self.did, a.get("title", ""), a.get("description", ""), a.get("body", ""))
            if a.get("searches") is not None and self.ds.work(self.did).get("searches"):
                sources.keep_searches(self.ds, self.did, ids=[str(x) for x in a.get("searches") or []])
            self.result["summary"] = True
            return {"ok": True}
        if name == "propose_candidates":
            self.ds.set_candidates(self.did, list(a.get("candidates", [])))
            self.result["candidates"] = True
            return {"ok": True}
        if name == "write_l1_draft":
            return {"ok": True, "path": self.ds.write_draft(self.did, a.get("facet"), "L1", a.get("body", ""),
                                                            a.get("title", ""), a.get("description", ""))}
        if name == "write_l2_draft":
            return {"ok": True, "path": self.ds.write_draft(self.did, a.get("facet"), "L2", a.get("body", ""),
                                                            a.get("title", ""), a.get("description", ""),
                                                            a.get("issue"), list(a.get("sources", []) or []))}
        if name == "write_index":
            fm, _ = split(st.read_text(s, "/index.md"))
            st.write_text(s, "/index.md", join(fm, "\n" + str(a.get("body", "")).strip() + "\n"))
            self.result["index"] = True
            return {"ok": True}
        if name == "plan_migration":
            self.result["plan"] = list(a.get("plan") or [])
            return {"ok": True}
        if name == "write_migration_summary":
            from ..lattice import migrate
            old = self.mig_old
            want = [str(x).strip() for x in a.get("old_discussions") or []]
            got = migrate.import_many(self.ds, old, want, self.mig_summaries)  # 複製は並べて（M4 の計測）
            paths = [p for p in got if p]
            missing = [d for d in want if d not in self.mig_summaries]
            self.ds.write_summary(self.did, a.get("title", ""), a.get("description", ""), a.get("body", ""), extra_sources=paths)
            self.result["summary"] = True
            return {"ok": True, "sources": paths, **({"unknown": missing} if missing else {})}
        if name == "judge_edit":
            self.result["judge"] = (a.get("kind"), a.get("reason", ""))
            return {"ok": True}
        raise Refused(f"関数 {name} はこの場面では使えません")

    def _read_url(self, url, focus):
        from ..lattice import web_pages
        if "docs.google.com" in url or "drive.google.com" in url:
            raise Refused("ドライブのファイルは import_drive で取り込む")
        if not url.startswith(("https://", "http://")):
            raise Refused("http・https の URL を渡してください")
        if web_pages.is_youtube(url):  # 動画はサーバーに取らず、Gemini に URL で見せる。見た要点をカードに残す
            text = watch_youtube(url, focus, self.model)
            from urllib.parse import parse_qs, urlsplit
            u = urlsplit(url)
            vid = (parse_qs(u.query).get("v") or [u.path.rstrip("/").rsplit("/", 1)[-1]])[0]
            card = sources.add_page(self.ds, self.did, url, f"YouTube {vid}", text, how="YouTube を Gemini が視聴")
            return {"card": card, "content": text}
        final, ctype, data, enc = web_pages.fetch(url)
        if ctype == "application/pdf":
            name = (final.rstrip("/").rsplit("/", 1)[-1] or "page").split("?")[0]
            card = sources.add(self.ds, self.did, name if name.lower().endswith(".pdf") else name + ".pdf", data, url=final,
                               how="Web から取得")
            return self._read_source(card)
        if not (ctype.startswith("text/") or ctype in ("application/xhtml+xml", "")):
            raise Refused(f"この種類のページは読めません（{ctype}）")
        title, text = web_pages.page_text(data, enc)
        if len(text) < 50:
            raise Refused("ページの本文を取り出せませんでした（画面をスクリプトで描くページは読めません）")
        card = sources.add_page(self.ds, self.did, final, title, text)
        return {"card": card, "title": title, "content": text}

    def _run_python(self, code, cards):
        from . import sandbox
        if not code.strip():
            raise Refused("code が空です")
        w = self.ds.work(self.did)
        want = []
        for c in cards:
            e = next((x for x in w.get("sources", []) if x["card"] == "/" + str(c).lstrip("/")), None)
            if e is None:
                raise Refused(f"{c} はこの Discussion で渡された資料ではありません")
            if e["file"] != e["card"]:  # Web のカードは実物が無い（本文はカードにある）
                want.append(e["file"])
        sb, sent = w.get("sandbox"), set(w.get("sandbox_files", []))

        def files(paths):
            return [(p.rsplit("/", 1)[1], self.ds.st.read_bytes(self.ds.s, p), sources.MIME.get(sources.ext_of(p), "application/octet-stream"))
                    for p in paths]
        for attempt in range(2):
            if not sb:  # 最初の計算で部屋を作る。期限で消えていたら作り直して資料を置き直す
                sb, sent = sandbox.create(f"{self.ds.s}-{self.did}"), set()
            todo = [p for p in want if p not in sent]
            try:
                text, made = sandbox.execute(sb, code, files(todo))
                break
            except Exception as ex:  # noqa: BLE001
                if attempt or "NOT_FOUND" not in str(ex) and "404" not in str(ex):
                    raise
                sb = None
        sent |= set(todo)
        self.ds.update_work(self.did, lambda w: w.update(sandbox=sb, sandbox_files=sorted(sent)))
        return {"output": text or "（出力なし。結果は print で出す）", "files_made_in_room": made,
                "files_in_room": [p.rsplit("/", 1)[1] for p in sorted(sent)]}

    def _read_source(self, card):
        w = self.ds.work(self.did)
        e = next((x for x in w.get("sources", []) if x["card"] == "/" + card.lstrip("/")), None)
        if e is None:
            raise Refused("その原典カードはこの Discussion にありません（渡された資料は、アプリからの知らせにカードのパスが出ます）")
        st, s = self.ds.st, self.ds.s
        data, ext = st.read_bytes(s, e["file"]), sources.ext_of(e["file"])

        def seen(w):
            for x in w.get("sources", []):
                if x["card"] == e["card"]:
                    x["seen"] = True
                    if pdf:
                        x["pdf"] = pdf
            for p in (e["card"],):
                p in w["read"] or w["read"].append(p)
        pdf = None
        out = {"card": e["card"], "name": e["name"]}
        if e["kind"] == "text":
            out["content"] = data.decode("utf-8", "replace")[:200_000]
        elif e["kind"] == "table":
            out["content"] = table_text(data, ext)
            out["note"] = "表の中身を文字にしたもの（式は計算結果の値）。細かい集計はサンドボックスで行う"
        else:
            if e["kind"] == "office":  # Gemini は pptx・docx を読めないので、ドライブで pdf にする（作った pdf は隠しファイルで取っておく）
                pdf = e.get("pdf") or e["file"].rsplit("/", 1)[0] + "/." + e["file"].rsplit("/", 1)[1].rsplit(".", 1)[0] + ".pdf"
                if st.exists(s, pdf):
                    data = st.read_bytes(s, pdf)
                else:
                    from ..lattice import drive_files
                    data = drive_files.to_pdf(e["name"], data, ext)
                    st.write_bytes(s, pdf, data)
                mime = sources.MIME["pdf"]
            else:
                mime = sources.MIME[ext]
            self.attach.append(types.FunctionResponsePart(inline_data=types.FunctionResponseBlob(mime_type=mime, data=data)))
            out["content"] = "資料そのものを、この結果に添えた"
        self.ds.update_work(self.did, seen)
        return out

    # ---- 途中経過（画面の「…しています」） ----
    def _label(self, name, a):
        def what(p):
            try:
                return self.ds._title(p) or p.rsplit("/", 1)[-1]
            except Exception:  # noqa: BLE001  表示のためだけなので、読めなければファイル名にする
                return str(p).rsplit("/", 1)[-1]
        if name == "read_file":
            return f"「{what('/' + str(a.get('path', '')).lstrip('/'))}」を読んでいます…"
        if name == "read_source":
            return f"資料「{what(str(a.get('card', '')))}」を読んでいます…"
        if name == "web_search":
            return f"Web で調べています: {str(a.get('question', ''))[:40]}…"
        fixed = {"list_files": "ファイルの一覧を見ています…", "search_text": f"「{str(a.get('text', ''))[:30]}」を探しています…",
                 "import_drive": "ドライブから資料を取り込んでいます…", "read_url": "ページを読みに行っています…",
                 "run_python": "計算しています…", "write_summary": "要約を書いています…", "propose_candidates": "関係する Facet を選んでいます…",
                 "write_l1_draft": f"{a.get('facet', '')} の L1 を書いています…", "write_l2_draft": f"{a.get('facet', '')} の L2 を書いています…",
                 "write_index": "目次（index.md）を直しています…"}
        return fixed.get(name)

    def _progress(self, text):
        if self.did and text:
            self.ds.progress(self.did, text)

    # ---- ループ ----
    def run(self, history, prompt):
        """history: これまでの会話（Gemini の Content の辞書）。prompt: この場面でアプリから渡す指示。"""
        tools = [types.Tool(function_declarations=TOOLS[self.mode])]
        allowed = {f.name for f in TOOLS[self.mode]}
        contents = [types.Content.model_validate(h) for h in history]
        contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
        cfg = types.GenerateContentConfig(system_instruction=INSTRUCTIONS, tools=tools, temperature=0.4)
        t0, text = time.time(), ""
        thinking = "考えています…"
        for _ in range(MAX_TURNS):
            self._progress(thinking)
            t1 = time.time()
            self._say("ask", f"Gemini に相談（{self.n_gemini + 1} 回目）", force=True)
            r = generate(self.model, contents, cfg, on_thought=(lambda t: self._say("think", t)) if self.did else None)
            self.t_gemini += time.time() - t1
            self.n_gemini += 1
            u = getattr(r, "usage_metadata", None)
            self._say("back", f"返事が来た（{time.time() - t1:.0f} 秒、読んだ量 {getattr(u, 'prompt_token_count', 0) or 0:,} トークン）", force=True)
            cand = r.candidates[0].content if r.candidates else None
            if cand is None or not cand.parts:  # 何も返さなかった（「何もしない」と判断したときなど）。そこで終える
                text = ""
                break
            contents.append(cand)
            fcs = [p.function_call for p in (cand.parts or []) if p.function_call]
            text = "".join(p.text or "" for p in (cand.parts or []) if p.text and not getattr(p, "thought", False))
            if not fcs:
                break
            parts = []
            for fc in fcs:
                args = dict(fc.args or {})
                t1 = time.time()
                try:
                    if fc.name not in allowed:
                        raise Refused(f"関数 {fc.name} はこの場面では使えません")
                    label = self._label(fc.name, args)
                    self._progress(label)
                    # ファイルを読む時間は短く、そのあと Gemini が考える時間が長いので、読んだものを見せたまま待つ
                    thinking = (label or "").replace("を読んでいます…", "を読みました。続きを考えています…") if "を読んでいます…" in (label or "") else "続きを考えています…"
                    self._say("tool", label or f"{fc.name} を動かしています…", force=True)
                    out = self.call(fc.name, args)
                except (Refused, StorageError) as e:
                    out = {"error": str(e)}
                    self._say("err", f"断った: {str(e)[:120]}", force=True)
                self.t_tools[fc.name] = round(self.t_tools.get(fc.name, 0) + time.time() - t1, 1)
                self.calls.append({"name": fc.name, "args": {k: (v[:120] + "…" if isinstance(v, str) and len(v) > 120 else v)
                                                             for k, v in args.items()}, "error": out.get("error")})
                # read_source で見せる資料（pdf・画像）は、関数の結果の中に添える（結果の外に置くと Gemini が受け付けない）
                parts.append(types.Part(function_response=types.FunctionResponse(name=fc.name, response=out, parts=self.attach or None)))
                self.attach = []
            contents.append(types.Content(role="user", parts=parts))
        else:
            text = text or "（関数の呼び出しが上限に達したので、いったん止めました）"
        self.seconds = round(time.time() - t0, 1)
        u = getattr(r, "usage_metadata", None)
        print(f"[timing] {self.did} tokens: 入力={getattr(u, 'prompt_token_count', None)} 考える={getattr(u, 'thoughts_token_count', None)} "
              f"出力={getattr(u, 'candidates_token_count', None)}（最後の呼び出し）", flush=True)
        print(f"[timing] {self.did} mode={self.mode} total={self.seconds}s gemini={self.t_gemini:.1f}s({self.n_gemini}回) "
              f"tools={sum(self.t_tools.values()):.1f}s {self.t_tools}", flush=True)
        self.usage = getattr(r, "usage_metadata", None)
        return text.strip(), [_light(c) for c in contents]


def web_search(question, model=MODEL):
    """Web Grounding for Enterprise だけを持たせた別の呼び出しで検索する（A-2 の判断2）。返り値は（答え, 出典）。
    出典の URL は Google の転送用リンクなので、転送先（元のページ）に置き換えて残す。"""
    cfg = types.GenerateContentConfig(tools=[types.Tool(enterprise_web_search=types.EnterpriseWebSearch())], temperature=0.2)
    r = generate(model, [types.Content(role="user", parts=[types.Part(text=(
        "次のことを Web で調べ、事実を出典つきで短くまとめる。分からないことは分からないと書く。\n\n" + question))])], cfg)
    g = r.candidates[0].grounding_metadata if r.candidates else None
    links, seen = [], set()
    for c in (getattr(g, "grounding_chunks", None) or []):
        if c.web and c.web.uri:
            url = _resolve(c.web.uri)
            if url not in seen:
                seen.add(url)
                links.append({"title": c.web.title or url, "url": url})
    return (r.text or "").strip(), links


def watch_youtube(url, focus="", model=MODEL):
    """公開の YouTube 動画を Gemini に URL で見せ、要点を書かせる（動画そのものはサーバーに取らない）。"""
    r = generate(model, [types.Content(role="user", parts=[
        types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
        types.Part(text=("この動画の内容を、話されていること・映っている事実を中心に、時刻（mm:ss）つきの箇条書きでまとめる。"
                         "推測は事実と分けて書く。" + (f"特に確かめたいこと: {focus}" if focus else "")))])],
        types.GenerateContentConfig(temperature=0.2))
    text = (r.text or "").strip()
    if not text:
        raise Refused("動画を見られませんでした（非公開・限定公開の動画は見られません）")
    return text


def _resolve(uri):
    """グラウンディングの転送用リンク（vertexaisearch.cloud.google.com）を、元のページの URL にする。ほかの宛先には問い合わせない。"""
    if not uri.startswith("https://vertexaisearch.cloud.google.com/grounding-api-redirect/"):
        return uri
    try:
        import requests
        loc = requests.head(uri, allow_redirects=False, timeout=5).headers.get("location")
        return loc if loc and loc.startswith(("https://", "http://")) else uri
    except Exception:
        return uri


def _light(c):
    """会話の記録（.work.json）には資料の中身を残さない。次の応答で要るなら、AI が read_source で読み直す。"""
    d = c.model_dump(mode="json", exclude_none=True)
    # 考えの要約は画面に見せるためだけ。記録には残さない（署名の付いた部品は Gemini に返す必要があるので残す）
    d["parts"] = [p for p in d.get("parts", []) if not (p.get("thought") and not p.get("thought_signature") and not p.get("function_call"))]
    for p in d.get("parts", []):
        fr = p.get("function_response")
        if fr and fr.get("parts"):
            fr.pop("parts")
            fr.setdefault("response", {})["content"] = "（ここで資料を見せた。いまは手元に無いので、要るなら read_source で読み直す）"
    return d


def table_text(data, ext, max_rows=200):
    """xlsx・csv を、シートごとの表の文字にする（AI が読む用。値だけ）。"""
    import csv
    import io
    if ext == "csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", "replace"))))
        return _rows("csv", rows, max_rows)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    return "\n\n".join(_rows(ws.title, [["" if v is None else v for v in r] for r in ws.iter_rows(values_only=True)], max_rows)
                        for ws in wb.worksheets)


def _rows(title, rows, max_rows):
    head = f"## シート {title}（{len(rows)} 行）"
    body = "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in rows[:max_rows])
    more = f"\n…（残り {len(rows) - max_rows} 行は省いた）" if len(rows) > max_rows else ""
    return head + "\n" + body + more


# ---------- 場面ごとの入口（discuss.py から別スレッドで呼ぶ） ----------

def _preload(ds, did, paths, note="読む手間を省くため、アプリが先に読んでおいた（read_file で読み直さなくてよい）"):
    """Gemini が後から read_file で読みに行くファイルを、最初の指示にまとめて添える。
    Gemini は1回の呼び出しに 10〜20 秒かかり、読むたびに呼び出しが1回増えるので（M4 の計測）、往復を減らす。"""
    from concurrent.futures import ThreadPoolExecutor
    paths = [p for i, p in enumerate(paths) if p and p not in paths[:i]]

    def one(p):
        try:
            return p, ds.st.read_text(ds.s, p)
        except Exception:  # noqa: BLE001  無いファイルは添えない（Gemini が必要なら自分で探す）
            return p, None
    with ThreadPoolExecutor(8) as ex:
        got = [(p, t) for p, t in ex.map(one, paths) if t is not None]
    if not got:
        return ""
    if did:
        ds.update_work(did, lambda w: [w["read"].append(p) for p, _ in got if p not in w["read"]
                                        and (p.startswith("/facets/") or p.startswith("/discussions/"))])
    return f"\n\n[アプリから] {note}:\n" + "".join(f"\n===== {p} =====\n{t.rstrip()}\n" for p, t in got)


def _facet_files(ds, facets):
    """選んだ Facet の L1 と L2（/facets/<id>/ の .md）のパス。"""
    pre = tuple(f"/facets/{f}/" for f in facets)
    return [e.path for e in ds.st.walk(ds.s) if e.path.startswith(pre) and e.path.endswith(".md")]


def _facet_list(ds):
    fm, _ = split(ds.st.read_text(ds.s, "/facets.md"))
    out = []
    def walk(gs, path):
        for g in gs or []:
            lab = path + [g.get("label", "")]
            for f in g.get("facets", []) or []:
                out.append(f"- `{f['id']}`: {' ＞ '.join(x for x in lab if x)} ＞ {f['label']}")
            walk(g.get("groups"), lab)
    walk(fm.get("groups"), [])
    return "\n".join(out)


def _context(ds, w):
    lines = [f"[アプリから] Subject: {ds.s}。この会話は {w['id']}（{w['origin']} から始まった）。"]
    if w.get("viewing"):
        lines.append(f"ユーザーがいま見ているもの: {w['viewing']}")
    return "\n".join(lines)


def reply(ds, did, model=MODEL):
    w = ds.work(did)
    run = Run(ds, did, "reply", model)
    last = next(m["text"] for m in reversed(w["messages"]) if m["role"] == "user")
    prompt = (_context(ds, w) + "\n\n" if not w["history"] else "") + _new_sources(ds, did) + last
    text, hist = run.run(w["history"], prompt)
    _finish(ds, did, run, text, hist)
    return run


def _new_sources(ds, did):
    """前の返事のあとに渡された資料を、アプリからの知らせとして発言の前に添える（一度だけ）。"""
    new = []

    def f(w):
        for e in w.get("sources", []):
            if not e.get("told"):
                e["told"] = True
                new.append(e)
    ds.update_work(did, f)
    if not new:
        return ""
    return ("[アプリから] ユーザーが資料を渡した（原典カードはアプリが作った。read_source で読める）:\n"
            + "\n".join(f"- {e['name']}: {e['card']}" for e in new) + "\n\n")


def summarize(ds, did, model=MODEL):
    w = ds.work(did)
    run = Run(ds, did, "summary", model)
    prompt = ("[アプリから] ユーザーが「要約を作る」を押した。ここまでの会話の要約を write_summary で書き、"
              "続けて propose_candidates で関係しそうな Facet の候補を出す。最後に、チャットへ短く報告する。\n\n"
              "この Subject の Facet:\n" + _facet_list(ds))
    text, hist = run.run(w["history"], prompt)
    _finish(ds, did, run, text, hist)
    return run


def drafts(ds, did, model=MODEL):
    w = ds.work(did)
    chosen = [c["facet"] for c in w["candidates"] if c.get("choice") == "update"]
    summ = w["dir"] + "discussion_summary.md"
    run = Run(ds, did, "drafts", model)
    prompt = (f"[アプリから] ユーザーが「Facet 更新を確認する」を押した。「更新する」が選ばれた Facet: {', '.join(chosen)}。"
              f"要約は {summ}。それぞれの Facet の L1 と L2 を読み、この Discussion の決定を反映した draft を "
              "write_l1_draft・write_l2_draft で書く。L1 に変化がなければ L1 は書かない。L2 は論点ごとに、既存の論点なら同じ論点名で、"
              "既存の本文を残したまま書き足す（どの Discussion の判断かを書く）。最後に、チャットへ短く報告する。")
    ds.progress(did, "要約と、選んだ Facet の L1・L2 を読んでいます…")
    prompt += _preload(ds, did, [summ] + _facet_files(ds, chosen))
    text, hist = run.run(w["history"], prompt)
    _finish(ds, did, run, text, hist, waiting_done="drafts")
    return run


def carryover(ds, did, withdrawn, model=MODEL):
    w = ds.work(did)
    run = Run(ds, did, "carryover", model)
    prompt = (f"[アプリから] ユーザーが次の Facet の draft を取り下げた: {', '.join(withdrawn)}。取り下げた Facet は「材料として残す」になった。"
              "要約を read_file で読み、「## 持ち越し」に、取り下げた案の要点を Facet ID 付きで1行ずつ足して write_summary で書き直す"
              "（ほかの節は変えない）。チャットへの報告は1文でよい。")
    text, hist = run.run(w["history"], prompt)
    _finish(ds, did, run, text, hist)
    return run


def update_index(ds, did, approved, model=MODEL):
    """確定のあと、index.md を確定した L1 に合わせる。Discussion を閉じたあとに裏で動かす（利用者を待たせない）。
    閉じたあとなので会話の履歴は使わず、index.md と確定した L1 を先に読んで渡す。"""
    run = Run(ds, None, "index", model)
    prompt = (f"[アプリから] ユーザーが {did} を確定した。stable にした Facet: {', '.join(approved)}。"
              "/index.md の「いまの要点」と Facet の一覧の説明が、確定した L1 と食い違っていれば write_index で直す。"
              "食い違いが無ければ何もしない。報告は不要（空でよい）。")
    prompt += _preload(ds, None, ["/index.md"] + [f"/facets/{f}/{f}.md" for f in approved])
    run.run([], prompt)
    return run


def judge(ds, eid, model=MODEL):
    e = next(x for x in ds.edits() if x["id"] == eid)
    run = Run(ds, None, "judge", model)
    prompt = (f"[アプリから] ユーザーが {e['path']} の本文を直した。直す前と後を比べ、judge_edit で判断を返す。"
              "意味が変わる修正なら、reason に「何がどう変わったか」と「同じことに触れていて食い違いそうなファイル」を、"
              "search_text で確かめてから書く。\n\n## 直す前\n" + e["old"] + "\n## 直した後\n" + e["text"])
    run.run([], prompt)
    kind, reason = run.result.get("judge", ("meaning", "判断を返せなかったので、念のため意味の変わる修正として扱います"))
    did = ds.judge(eid, "minor" if kind == "minor" else "meaning", reason)
    if did and e.get("into") == did:  # 会話の途中の修正（C-6）: その会話の続きとして、食い違いを示して聞く
        w = ds.work(did)
        r2 = Run(ds, did, "reply", model)
        p2 = (f"[アプリから] 会話の途中で、ユーザーが {e['path']} の本文を直した（意味の変わる修正、判断の理由: {reason}）。"
              "この修正もこの Discussion の一部として扱う。何が変わったかと、Subject の中で食い違うところを（search_text で確かめて）示し、"
              "どう揃えるかを聞く。直したファイルは draft になり、「更新する」の候補に入っている。"
              "\n\n## 直す前\n" + e["old"] + "\n## 直した後\n" + e["text"])
        text, hist = r2.run(w["history"], p2)
        _finish(ds, did, r2, text, hist)
    elif did:  # 意味が変わる修正: 始めた Discussion で、AI が食い違いを示す最初のひと言を書く
        w = ds.work(did)
        r2 = Run(ds, did, "edit_open", model)
        p2 = (_context(ds, w) + f"\n[アプリから] ユーザーが {e['path']} の本文を直し、意味の変わる修正と判断した（判断の理由: {reason}）。"
              "この Discussion で、何が変わったかと、Subject の中で食い違うところを（search_text で確かめて）示し、どう揃えるかを聞く。"
              "\n\n## 直す前\n" + e["old"] + "\n## 直した後\n" + e["text"])
        text, hist = r2.run([], p2)
        _finish(ds, did, r2, text, hist)
    return run


def continue_from(ds, did, model=MODEL):
    w = ds.work(did)
    prev = w["origin"].split(":", 1)[1]
    run = Run(ds, did, "continue", model)
    prompt = (_context(ds, w) + f"\n[アプリから] この Discussion は {prev} の続き。まず {prev} の要約と、そのとき変えた Facet の L1・L2 を読み、"
              "前の回で決めたことと残ったことを短く示してから、ユーザーの最初の発言に答える。\n\nユーザーの最初の発言: " + w["messages"][-1]["text"])
    ds.progress(did, f"{prev} の要約と、関係する Facet を読んでいます…")
    try:
        sp = ds.folder(prev) + "discussion_summary.md"
        fm, _ = split(ds.st.read_text(ds.s, sp))
        rel = [x.get("resource") for x in fm.get("sources") or [] if str(x.get("resource", "")).startswith("/facets/")]
        facets = sorted({r.split("/")[2] for r in rel})
        prompt += _preload(ds, did, [sp] + _facet_files(ds, facets))
    except Exception:  # noqa: BLE001  先に読めなくても、Gemini が自分で読みに行ける
        pass
    text, hist = run.run([], prompt)
    _finish(ds, did, run, text, hist)
    return run


def _finish(ds, did, run, text, hist, waiting_done=None, quiet=False):
    def f(w):  # 利用者の操作と同時でも消し合わないよう、読み直してから書く（ロックの中）
        w["history"] = hist
        if text and not quiet:
            w["messages"].append({"role": "agent", "text": text, "at": now()})
        w.setdefault("trace", []).append({"mode": run.mode, "model": run.model, "seconds": run.seconds, "calls": run.calls,
                                          "tokens": getattr(run.usage, "total_token_count", None)})
        if w.get("waiting") in ("reply", "summary", "carryover") or w.get("waiting") == waiting_done:
            w["waiting"] = None
    ds.update_work(did, f)


# ---------- 組み直して移す（REQUIREMENTS E-4） ----------
def _old_overview(ds, old):
    """前の Subject の Facet の一覧・L1・Discussion の要約（題名と説明）。割り当てを決める材料。"""
    from ..lattice.discussion import Discussions
    od = Discussions(ds.st, old, state=ds.state)
    full = od.summaries_full()
    l1 = [e.path for e in ds.st.walk(old) if re.match(r"^/facets/([^/]+)/\1\.md$", e.path)]
    out = f"## 前の Subject（{old}）の Facet\n" + _facet_list(od)
    out += "\n\n## 前の Subject の Discussion\n" + "\n".join(
        f"- {d}: {fm.get('title', '')} — {fm.get('description', '')}" for d, (_, fm, _) in sorted(full.items()))
    out += "\n\n## 前の Subject の L1\n" + "".join(f"\n===== {p} =====\n{ds.st.read_text(old, p).rstrip()}\n" for p in l1)
    return out, full


def migrate_plan(ds, old, model=MODEL):
    """新しい Facet ごとに、材料にする前の Facet と Discussion を決める（1回だけ）。"""
    run = Run(ds, None, "migrate_plan", model)
    over, _ = _old_overview(ds, old)
    prompt = ("[アプリから] 前の Subject の中身を、構成を変えた新しい Subject に移す（組み直して移す）。"
              "新しい Facet のそれぞれについて、材料にする前の Facet の ID と、根拠になる前の Discussion の番号を plan_migration で返す。"
              "1つの前の Facet・Discussion を複数の新しい Facet に割り当ててよい。関係するものが無ければ空でよい。\n\n"
              "## 新しい Subject の Facet\n" + _facet_list(ds) + "\n\n" + over)
    run.run([], prompt)
    return {x.get("facet"): x for x in run.result.get("plan", []) if x.get("facet")}


def migrate_facet(ds, did, old, facet, plan, model=MODEL):
    """新しい Facet 1つ分の一人 Discussion。前の L1・L2・要約を読み、要約と L1・L2 を書く。"""
    from ..lattice.discussion import Discussions
    od = Discussions(ds.st, old, state=ds.state)
    full = od.summaries_full()
    run = Run(ds, did, "migrate", model)
    run.mig_old, run.mig_summaries = old, full
    ofs = [f for f in plan.get("old_facets", []) if f]
    ods = [d for d in plan.get("old_discussions", []) if d in full]
    old_files = [e.path for e in ds.st.walk(old) if e.path.endswith(".md") and any(e.path.startswith(f"/facets/{f}/") for f in ofs)]
    old_files += [full[d][0] + "discussion_summary.md" for d in ods]
    new_l1 = [e.path for e in ds.st.walk(ds.s) if re.match(r"^/facets/([^/]+)/\1\.md$", e.path)]
    prompt = (f"[アプリから] これは人のいない一人 Discussion（{did}）。前の Subject（{old}）の中身を、新しい Subject の Facet `{facet}` に組み直して移す。\n"
              "手順: 1) 下の前の L1・L2・Discussion の要約を読み、この Facet に関わることを選び出す。"
              "2) write_migration_summary で要約を書く。「## 話したこと」に、前のどの Facet・Discussion をどう読み、どう組み直したかを書く。"
              "「## 決定」に新しい Facet での判断、「## 持ち越し」に移しきれなかったことや食い違いを書く。old_discussions には根拠にした前の Discussion の番号を入れる。"
              f"3) write_l1_draft と write_l2_draft で `{facet}` の L1・L2 を書く（L2 は論点ごと）。前の Subject のパスは書かない（新しい Subject からは見えない）。"
              "前の中身に何も無ければ、そのことを要約に書き、L1 は「前の Subject には材料が無かった」旨を短く書く。"
              "先に移した新しい Facet の L1 と食い違わないようにする。チャットへの報告は1文でよい。\n\n"
              "## 新しい Subject の Facet\n" + _facet_list(ds) + f"\n\n割り当て（アプリが先に決めた案。足りなければ下の一覧から足してよい）: 前の Facet {ofs or 'なし'}、前の Discussion {ods or 'なし'}"
              "\n\n## 前の Subject の Discussion の一覧\n" + "\n".join(f"- {d}: {fm.get('title', '')}" for d, (_, fm, _) in sorted(full.items())))
    ds.progress(did, f"{old} の材料を読んでいます…")
    pre = [(p, ds.st.read_text(old, p)) for p in old_files]
    prompt += "\n\n[アプリから] 前の Subject の材料:\n" + "".join(f"\n===== {old}:{p} =====\n{t.rstrip()}\n" for p, t in pre)
    prompt += _preload(ds, did, new_l1, note="先に移した新しい Facet の L1")
    text, hist = run.run([], prompt)
    if not run.result.get("summary"):  # 要約を書き忘れたら、もう1回だけ頼む
        text, hist = run.run(hist, "[アプリから] 要約がまだです。write_migration_summary で書いてください。")
    _finish(ds, did, run, text, hist, waiting_done="migrate")
    return run
