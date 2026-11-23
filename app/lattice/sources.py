"""会話の中で渡された資料と、その原典カード（REQUIREMENTS C-2・C-14）。決定論的な関数（G-1）。

渡した時点で、Discussion のフォルダに次の2つを置く:
- source-<名前>.<拡張子>   資料の実物（ドライブの Google ファイルは xlsx・pdf に書き出したもの）
- source-<名前>.md         原典カード。題名・どこから来たか・渡した人・日時。AI は書かない
pptx・docx を AI に読ませるための pdf は、隠しファイル .source-<名前>.pdf として同じフォルダに置き、閉じる・廃棄で消す。
"""
import pathlib
from concurrent.futures import ThreadPoolExecutor
import re

from .discussion import LOCK, Discussions, Refused, join, now

MAX_BYTES = 30 * 1024 * 1024  # Cloud Run の1回の送信の上限（32MB）より少し小さく（C-14）
KINDS = {  # 拡張子 → AI の読み方
    "xlsx": "table", "csv": "table",
    "pdf": "pdf",
    "pptx": "office", "docx": "office",
    "jpg": "image", "jpeg": "image", "png": "image", "heic": "image",
    "txt": "text", "md": "text",
}
MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv",
        "pdf": "application/pdf",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "heic": "image/heic",
        "txt": "text/plain", "md": "text/markdown"}


def ext_of(filename):
    return pathlib.PurePath(str(filename)).suffix.lower().lstrip(".")


def _slug(stem):
    # 日本語の名前はそのまま残す（ドライブで人が見て分かるように）。記号と空白はハイフンにする
    s = re.sub(r"[^\w]+|_+", "-", str(stem).lower()).strip("-")[:40].strip("-")
    return s or "file"


def check(filename, size):
    """上げる前に断る（形式と大きさ）。"""
    ext = ext_of(filename)
    if ext not in KINDS:
        raise Refused(f"この形式は読めません（{ext or '拡張子なし'}）。表は xlsx・csv、資料は pdf・pptx・docx、写真は jpg・png・heic、文章は txt・md で渡してください")
    if size > MAX_BYTES:
        raise Refused("1ファイル 30MB までです。大きい資料はドライブに置いて「ドライブから」で渡してください")
    return ext


def add(ds: Discussions, did, filename, data: bytes, url=None, by_drive=False, how=None):
    """資料を置き、原典カードを作る。返り値はカードのパス。"""
    ext = check(filename, len(data))
    with LOCK:
        w = ds.work(did)
        if w["state"] != "open":
            raise Refused("資料は会話中の Discussion にだけ渡せます")
        stem = pathlib.PurePath(str(filename)).stem
        base, n = _slug(stem), 1
        while True:
            slug = base if n == 1 else f"{base}-{n}"
            card = f"{w['dir']}source-{slug}.md"
            if not ds.st.exists(ds.s, card) and not ds.st.exists(ds.s, f"{w['dir']}source-{slug}.{'txt' if ext == 'md' else ext}"):
                break
            n += 1
        file = f"{w['dir']}source-{slug}.{ext}"
        if ext == "md":  # md の資料はカードと名前がぶつかり、文書としても索引に入ってしまうので、txt として置く
            file = f"{w['dir']}source-{slug}.txt"
        at = now()
        how = how or ("ドライブから取り込み" if by_drive else "PC からアップロード")
        fm = {"type": "Source", "title": str(filename), "description": f"{w['id']} で渡された資料（{how}、{ds.name or ds.email}）。",
              "layer": "source", "resource": url if url else f"アップロード（{ds.name or ds.email}、{at}）",
              "snapshot": file.rsplit("/", 1)[1], "retrieved": {"by": f"human:{ds.name or ds.email}", "at": at, "how": how}}
        body = (f"\n# {filename}\n\n- 渡し方: {how}（{ds.name or ds.email}、{at}）\n"
                + (f"- 元のファイル: {url}（いまの姿はこちら。下の実物は渡した時点のもの）\n" if url else "")
                + f"- 実物: [{fm['snapshot']}]({fm['snapshot']})\n")
        # 実物・カード・更新記録の3つは互いに関係しないので、同時に書く（ドライブは1回 3〜4 秒かかる。M5 の検証で 12.7 秒だった）
        line = f"{w['id']} に資料を渡した: [{filename}]({card})（{how}、human:{ds.name or ds.email}）"
        with ThreadPoolExecutor(3) as ex:
            for fut in [ex.submit(ds.st.write_bytes, ds.s, file, data),
                        ex.submit(ds.st.write_text, ds.s, card, join(fm, body)),
                        ex.submit(ds.log, line)]:
                fut.result()
        entry = {"card": card, "file": file, "name": str(filename), "kind": KINDS[ext], "url": url, "at": at, "seen": False}

        def f(w):
            w.setdefault("sources", []).append(entry)
            w["messages"].append({"role": "file", "text": str(filename), "card": card, "at": at})
        ds.update_work(did, f)
        return card


def remove(ds: Discussions, did, card):
    """渡した資料を消す（open の間だけ、人が決める）。"""
    with LOCK:
        w = ds.work(did)
        if w["state"] != "open":
            raise Refused("資料は会話中の間だけ消せます")
        e = next((x for x in w.get("sources", []) if x["card"] == card), None)
        if e is None:
            raise Refused("その資料はこの Discussion にありません")
        for p in (e["card"], e["file"], e.get("pdf")):
            if p and ds.st.exists(ds.s, p):
                ds.st.delete(ds.s, p)

        def f(w):
            w["sources"] = [x for x in w["sources"] if x["card"] != card]
            w["read"] = [p for p in w["read"] if p not in (e["card"], e["file"])]
            w["messages"].append({"role": "file", "text": e["name"], "card": card, "removed": True, "at": now()})
        ds.update_work(did, f)


def drop_hidden(ds: Discussions, w):
    """閉じる・廃棄のとき、AI 用に作った隠しファイル（pdf への変換）と、計算部屋（サンドボックス、C-1）を消す。"""
    if w.get("sandbox"):
        try:
            from ..agent import sandbox
            sandbox.delete(w["sandbox"])
        except Exception:  # noqa: BLE001  消せなくても期限（TTL）で消える。閉じるのは止めない
            ds.log(f"{w['id']} の計算部屋を消せなかった（期限で消える）: {w['sandbox']}")
    for e in w.get("sources", []):
        p = e.get("pdf")
        if p and ds.st.exists(ds.s, p):
            ds.st.delete(ds.s, p)


def add_web(ds: Discussions, did, query, answer, links):
    """Web 検索（Web Grounding for Enterprise）の結果を、会話中の記録にだけ残す（2026-11-10 改訂、REQUIREMENTS C-14）。
    調べている途中の検索をすべて原典にすると、使わなかったものまで残るため。原典カードにするかは、要約のときに決め
    （AI の案を人が直す）、「Facet 更新を確認する」で選ばれたものだけを write_web_cards で書く。返り値は検索の ID。"""
    with LOCK:
        w = ds.work(did)
        if w["state"] != "open":
            raise Refused("Web 検索は会話中の Discussion でだけ使えます")
        sid = f"web-{len(w.get('searches', [])) + 1}"
        at = now()
        entry = {"id": sid, "query": str(query), "answer": str(answer).strip(), "links": list(links)[:20], "at": at,
                 "by": f"agent:{ds.model}", "keep": False, "card": None}

        def f(w):
            w.setdefault("searches", []).append(entry)
            w["messages"].append({"role": "file", "text": f"Web 検索: {query}", "search": sid, "web": True, "at": at})
        ds.update_work(did, f)
        return sid


def keep_searches(ds: Discussions, did, ids=None, one=None, keep=None):
    """原典に加える検索を決める。ids: AI の案（その一覧だけを残す）。one・keep: 人が1件ずつ直す。"""
    with LOCK:
        w = ds.work(did)
        if w["state"] != "open":
            raise Refused("原典に加える検索は、会話中の間だけ選べます")
        known = {x["id"] for x in w.get("searches", [])}
        if one is not None and one not in known:
            raise Refused(f"{one} という検索はありません")

        def f(w):
            for x in w.get("searches", []):
                if x.get("card"):
                    continue  # もう原典カードにしたものは変えない
                if ids is not None:
                    x["keep"] = x["id"] in ids
                elif x["id"] == one:
                    x["keep"] = bool(keep)
        ds.update_work(did, f)


def write_web_cards(ds: Discussions, w):
    """選ばれた検索を原典カードにする（「Facet 更新を確認する」のとき、ロックの中で呼ぶ）。w を直し、書いたカードのパスを返す。"""
    out = []
    for x in w.get("searches", []):
        if not x.get("keep") or x.get("card"):
            continue
        base, n = "web-" + _slug(x["query"])[:30].strip("-"), 1
        while ds.st.exists(ds.s, f"{w['dir']}source-{base if n == 1 else f'{base}-{n}'}.md"):
            n += 1
        card = f"{w['dir']}source-{base if n == 1 else f'{base}-{n}'}.md"
        links, at, query = x["links"], x["at"], x["query"]
        fm = {"type": "Source", "title": f"Web 検索: {query}"[:80],
              "description": f"{w['id']} でエージェントが Web を検索した結果（Web Grounding for Enterprise、{at[:10]} 時点）。",
              "layer": "source", "resource": [y["url"] for y in links][:20] or "Web 検索（出典なし）",
              "retrieved": {"by": x.get("by") or f"agent:{ds.model}", "at": at, "how": "Web Grounding for Enterprise"}}
        body = (f"\n# Web 検索: {query}\n\n- 検索した日時: {at}（エージェントが検索。ページの中身は、その時点のもの）\n\n## 出典\n\n"
                + "".join(f"- [{y['title']}]({y['url']})\n" for y in links)
                + "\n## 検索の答え（AI がまとめたもの。裏付けは出典で確かめる）\n\n" + x["answer"] + "\n")
        ds.st.write_text(ds.s, card, join(fm, body))
        x["card"] = card
        w.setdefault("sources", []).append({"card": card, "file": card, "name": f"Web 検索: {query}", "kind": "web",
                                            "url": None, "at": at, "seen": True, "told": True})
        if card not in w["read"]:
            w["read"].append(card)
        out.append(card)
    if out:
        ds.log(f"{w['id']} の Web 検索 {len(out)} 件を原典カードにした（要約のときに選んだもの）")
    return out


def add_page(ds: Discussions, did, url, title, text, how="Web ページを取得"):
    """チャットに貼られた URL の中身（Web ページの本文、YouTube なら Gemini が見た要点）を、原典カード1枚にする。
    ページは書き換わるので、取得した時点の本文をカードに残す（C-2）。"""
    with LOCK:
        w = ds.work(did)
        if w["state"] != "open":
            raise Refused("URL の取り込みは会話中の Discussion でだけできます")
        base, n = "url-" + _slug(title or url)[:30].strip("-"), 1
        while ds.st.exists(ds.s, f"{w['dir']}source-{base if n == 1 else f'{base}-{n}'}.md"):
            n += 1
        card = f"{w['dir']}source-{base if n == 1 else f'{base}-{n}'}.md"
        at = now()
        name = (title or url)[:80]
        fm = {"type": "Source", "title": name, "description": f"{w['id']} で渡された URL の中身（{how}、{at[:10]} 時点）。",
              "layer": "source", "resource": url, "retrieved": {"by": f"human:{ds.name or ds.email}", "at": at, "how": how}}
        body = (f"\n# {name}\n\n- 元のページ: {url}\n- 取得した日時: {at}（{how}。下はその時点の中身）\n\n## 中身\n\n"
                + str(text).strip() + "\n")
        ds.st.write_text(ds.s, card, join(fm, body))

        def f(w):
            w.setdefault("sources", []).append({"card": card, "file": card, "name": name, "kind": "web", "url": url,
                                                "at": at, "seen": True, "told": True})
            w["read"].append(card)
            w["messages"].append({"role": "file", "text": name, "card": card, "page": True, "at": at})
        ds.update_work(did, f)
        ds.log(f"{w['id']} に URL の中身を取り込んだ: [{name}]({card})（{how}）")
        return card
