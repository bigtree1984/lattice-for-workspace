"""Subject の索引の JSON（REQUIREMENTS D-1）。

中身はローカル版の build_bundle_json.py と同じ形（各 .md のパス・荷札・本文と、実物のパスの一覧）。
Subject の中の隠しフォルダ `/.lattice/bundle.json` に置き、ビューアーはこれを1つ読んで画面を作る。

作り直すのは次のとき:
- 索引が無い、壊れている、形式の版が違う
- 索引を作った時刻より後に、Subject の中で変わったファイルがある（他人の編集や、ドライブで直接置いた・消した場合）
アプリ経由で書き換えたときは、書いた側が rebuild() を呼ぶ。
"""
import json
import re
import time

from .storage import Storage, StorageError

INDEX = "/.lattice/bundle.json"
VERSION = 1


def split_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[4:end], text[end + 5:]
    return "", text


def build(st: Storage, subject: str) -> dict:
    files, assets = [], []
    for e in st.walk(subject):
        if e.path.endswith(".md"):
            fm, body = split_frontmatter(st.read_text(subject, e.path))
            files.append({"path": e.path, "frontmatter": fm, "body": body})
        else:
            assets.append(e.path)
    # 時刻は、読み始める前ではなく作り終えた時点の「Subject の中で一番新しい更新」に合わせる
    newest = max((e.modified for e in st.walk(subject)), default=0.0)
    return {"version": VERSION, "bundle": subject, "generated_at": max(newest, time.time()),
            "files": files, "assets": assets}


def rebuild(st: Storage, subject: str) -> dict:
    data = build(st, subject)
    # 閲覧のみの Subject（E-4）には索引を置けない。置けなくても、作った索引はそのまま返す（毎回作り直すので遅くなる）
    if st.can_edit(subject):
        try:
            st.write_text(subject, INDEX, json.dumps(data, ensure_ascii=False))
        except Exception:  # noqa: BLE001  権限が途中で変わったときなど
            pass
    return data


def load(st: Storage, subject: str) -> dict:
    """索引を読む。古ければ作り直す。"""
    try:
        data = json.loads(st.read_text(subject, INDEX))
        if data.get("version") == VERSION and not st.changed_since(subject, data["generated_at"]):
            return data
    except (StorageError, ValueError, KeyError):
        pass
    return rebuild(st, subject)


def subject_summary(st: Storage, subject: str) -> dict:
    """Subject を選ぶ画面の1行分（名前・Facet 数・Discussion 数・担当者）。索引から作る。"""
    b = load(st, subject)
    by = {f["path"]: f for f in b["files"]}
    idx = by.get("/index.md", {"frontmatter": "", "body": ""})
    title = next((l[2:].strip() for l in idx["body"].splitlines() if l.startswith("# ")), subject)
    owner = (re.search(r"^owner:\s*(\S+)\s*$", idx["frontmatter"], re.M) or [None, None])[1]
    fdef = by.get("/facets.md", {"frontmatter": ""})["frontmatter"]
    return {"id": subject, "title": title, "owner": owner, "readonly": not st.can_edit(subject),
            "facets": len(re.findall(r"\{\s*id:", fdef)),
            "discussions": sum(1 for p in by if re.match(r"^/discussions/[^/]+/discussion_summary\.md$", p))}
