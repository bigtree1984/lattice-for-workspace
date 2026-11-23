"""Subject を作る・Facet の構成を直す（REQUIREMENTS D-5・D-3・E-3）。決定論的な関数（G-1）。

facets.md の荷札の groups が Facet の構成。形は「グループ → 小グループ → Facet」の2段:
  groups: [{id, label, groups: [{id, label, facets: [{id, label, deprecated?}]}]}]
"""
import datetime as dt
import re

from .discussion import JST, Refused, join, now, split
from .storage import StorageError

ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*_\d{4}$")  # <文字列>_<年>（E-3）


def _f(i, label):
    return {"id": i, "label": label}


def _g(i, label, facets):
    return {"id": i, "label": label, "facets": facets}


TEMPLATES = {
    "marketing": {
        "label": "マーケティング",
        "description": "マーケティング戦略キャンバスの基本の Facet（競合は含まない）",
        "groups": [
            {"id": "market", "label": "市場環境", "groups": [
                _g("pest", "PEST", [_f("pest-political", "Political"), _f("pest-economic", "Economic"),
                                    _f("pest-society", "Society"), _f("pest-technology", "Technology")]),
                _g("five-forces", "5F", [_f("five-forces", "5F")]),
                _g("swot", "SWOT", [_f("swot", "SWOT")])]},
            {"id": "strategy", "label": "戦略", "groups": [
                _g("stp", "STP", [_f("stp", "STP")]),
                _g("mvv", "MVV", [_f("mvv", "MVV")])]},
            {"id": "customer", "label": "顧客", "groups": [
                _g("persona", "ペルソナ", [_f("persona", "ペルソナ")]),
                _g("journey", "カスタマージャーニー", [_f("journey", "カスタマージャーニー")])]},
            {"id": "execution", "label": "実行", "groups": [
                _g("4p", "4P（自社）", [_f("product", "Product"), _f("price", "Price"),
                                      _f("place", "Place"), _f("promotion", "Promotion")])]},
        ],
    },
    "research": {
        "label": "研究テーマ",
        "description": "研究の問いから、知見・計画・結果と次の一手まで",
        "groups": [
            {"id": "question", "label": "問い", "groups": [
                _g("aim", "問いと意義", [_f("research-question", "研究の問い"), _f("background", "背景と意義")]),
                _g("goal", "ゴール", [_f("success-criteria", "成功の基準")])]},
            {"id": "knowledge", "label": "知見", "groups": [
                _g("external", "社外の知見", [_f("prior-research", "先行研究"), _f("patents-tech", "特許・競合技術")]),
                _g("internal", "社内の知見", [_f("internal-findings", "社内の過去の知見")])]},
            {"id": "plan", "label": "計画", "groups": [
                _g("hypotheses", "仮説", [_f("hypothesis", "仮説")]),
                _g("approach", "進め方", [_f("method", "実験・調査の方法"), _f("resources", "必要な資源")])]},
            {"id": "outcome", "label": "結果と判断", "groups": [
                _g("findings", "結果", [_f("results", "結果"), _f("discussion", "考察")]),
                _g("decision", "判断", [_f("next-step", "次の一手")])]},
        ],
    },
    "product-dev": {
        "label": "製品開発の検討",
        "description": "要求・設計・事業性・リスクと、関門ごとの判断",
        "groups": [
            {"id": "requirements", "label": "要求", "groups": [
                _g("voice", "顧客", [_f("customer-needs", "顧客の要求")]),
                _g("planning", "企画", [_f("concept", "製品のコンセプト"), _f("targets", "目標（性能・価格・発売時期）")])]},
            {"id": "design", "label": "設計", "groups": [
                _g("specification", "仕様", [_f("spec", "仕様"), _f("tech-issues", "技術課題")]),
                _g("validation", "検証", [_f("prototype", "試作と評価")])]},
            {"id": "business", "label": "事業性", "groups": [
                _g("costs", "コスト", [_f("cost", "原価"), _f("manufacturing", "製造と調達")]),
                _g("revenue", "収益", [_f("profit-plan", "収支の見込み")])]},
            {"id": "risk", "label": "リスク", "groups": [
                _g("compliance", "品質と規制", [_f("quality-safety", "品質・安全"), _f("regulation", "規制・認証")]),
                _g("ip-group", "知財", [_f("ip", "知財")])]},
            {"id": "schedule", "label": "計画", "groups": [
                _g("timeline", "日程", [_f("milestones", "日程とマイルストーン")])]},
        ],
    },
}
# 以前の版で置いた組み込みの雛形の中身（の要約値）。人が手を入れていなければ、新しい版に置き換える
OLD_BUILTIN = {"research": {"f6c0b72fe2e3faad28f381a77cf802a7a98dc52a"},
               "product-dev": {"fc0cf5cd69ea1bf24d2df70b0bab3a8ca568d32c"}}


TPL_DIR = "_templates"  # 共有ドライブ直下の雛形フォルダ。名前が「_」で始まるので Subject 一覧には出ない（NAME にも合わない）
TPL_INDEX = """# Facet の雛形

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
"""


def _tpl_yaml(t):
    import yaml
    return yaml.safe_dump({"label": t["label"], "description": t["description"], "groups": t["groups"]},
                          allow_unicode=True, sort_keys=False, width=200)


SEEDED = "/.seeded.json"  # 置いたことのある組み込みの雛形。人が消した雛形を置き直さないために覚える


def seed_templates(st):
    """雛形フォルダが無ければ作る。組み込みの雛形のうち、まだ置いたことの無いものを置く（あとから足した雛形も届く）。"""
    import json
    new = TPL_DIR not in _dirs(st)
    if new:
        st.create_subject(TPL_DIR, TPL_INDEX)
    try:
        done = set(json.loads(st.read_text(TPL_DIR, SEEDED)))
    except Exception:  # noqa: BLE001  記録が無い（以前の版で作ったフォルダ）: いまあるファイルは置いたものとみなす
        done = {e.path[1:].rsplit(".", 1)[0] for e in st.walk(TPL_DIR)}
    todo = [k for k in TEMPLATES if k not in done]
    for k in todo:
        if not st.exists(TPL_DIR, f"/{k}.yaml"):
            st.write_text(TPL_DIR, f"/{k}.yaml", _tpl_yaml(TEMPLATES[k]))
    import hashlib
    for k, olds in OLD_BUILTIN.items():  # 組み込みの雛形を直したとき: 人が手を入れていない古い版だけ置き換える
        p = f"/{k}.yaml"
        if st.exists(TPL_DIR, p) and hashlib.sha1(st.read_text(TPL_DIR, p).encode()).hexdigest() in olds:
            st.write_text(TPL_DIR, p, _tpl_yaml(TEMPLATES[k]))
            todo.append(k)
    if todo or new:
        try:
            st.write_text(TPL_DIR, SEEDED, json.dumps(sorted(done | set(TEMPLATES))))
        except Exception:  # noqa: BLE001  閲覧のみの共有ドライブなど。雛形は読める
            pass
    return bool(todo)


def _dirs(st):
    try:
        st.walk(TPL_DIR)
        return [TPL_DIR]
    except StorageError:
        return []


def templates(st=None):
    """雛形の一覧（REQUIREMENTS D-5）。共有ドライブの _templates/*.yaml から読む。フォルダが無ければ組み込みの雛形を置いてから読む。"""
    if st is None:
        return [{"id": k, **v} for k, v in TEMPLATES.items()]
    import logging
    import yaml
    seed_templates(st)
    out = []
    for e in st.walk(TPL_DIR):
        m = re.fullmatch(r"/([a-z0-9]+(?:-[a-z0-9]+)*)\.ya?ml", e.path)
        if not m:
            continue
        try:
            d = yaml.safe_load(st.read_text(TPL_DIR, e.path)) or {}
            out.append({"id": m.group(1), "label": str(d.get("label") or m.group(1)),
                        "description": str(d.get("description") or ""), "groups": clean_groups(d.get("groups"))})
        except Exception as ex:  # noqa: BLE001  形の誤った雛形は読み飛ばす
            logging.warning("雛形 %s を読み飛ばしました: %s", e.path, ex)
    return sorted(out, key=lambda t: t["label"])


def clean_groups(groups):
    """画面から来た構成を確かめ、余計なものを落として返す。"""
    out, seen = [], set()
    if not isinstance(groups, list) or not groups:
        raise Refused("グループが1つもありません")
    gids = set()
    for g in groups:
        gid, gl = str(g.get("id", "")).strip(), str(g.get("label", "")).strip()
        if not ID.match(gid) or not gl:
            raise Refused(f"グループ「{gl or gid}」の ID か名前が正しくありません（ID は英小文字・数字・ハイフン）")
        if gid in gids:
            raise Refused(f"グループの ID {gid} が重なっています")
        gids.add(gid)
        subs, sids = [], set()
        for s in g.get("groups") or []:
            sid, sl = str(s.get("id", "")).strip(), str(s.get("label", "")).strip()
            if not ID.match(sid) or not sl:
                raise Refused(f"「{gl}」の中の小グループ「{sl or sid}」の ID か名前が正しくありません")
            if sid in sids:
                raise Refused(f"「{gl}」の中で小グループの ID {sid} が重なっています")
            sids.add(sid)
            facets = []
            for f in s.get("facets") or []:
                fid, fl = str(f.get("id", "")).strip(), str(f.get("label", "")).strip()
                if not ID.match(fid) or not fl:
                    raise Refused(f"Facet「{fl or fid}」の ID か名前が正しくありません（ID は英小文字・数字・ハイフン）")
                if fid in seen:
                    raise Refused(f"Facet の ID {fid} が重なっています（Subject の中で1つずつ）")
                seen.add(fid)
                x = {"id": fid, "label": fl}
                if f.get("deprecated"):
                    x["deprecated"] = True
                facets.append(x)
            if not facets:
                raise Refused(f"小グループ「{sl}」に Facet がありません")
            subs.append({"id": sid, "label": sl, "facets": facets})
        if not subs:
            raise Refused(f"グループ「{gl}」に小グループがありません")
        out.append({"id": gid, "label": gl, "groups": subs})
    return out


def facet_ids(groups):
    return [f["id"] for g in groups for s in g["groups"] for f in s["facets"]]


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def check_owner(owner):
    owner = str(owner or "").strip()
    if owner and not EMAIL.match(owner):
        raise Refused("担当はメールアドレスで入れてください（例: name@example.com）")
    return owner


def create(st, name, title, description, owner, groups, by=None):
    owner = check_owner(owner)
    by = by or owner
    name, title = str(name or "").strip(), str(title or "").strip()
    if not NAME.match(name):
        raise Refused("名前は「英小文字・数字の文字列_年」の形にしてください（例: edible_coffee_2027）")
    if not title:
        raise Refused("表示名を入れてください")
    groups = clean_groups(groups)
    # 同じ名前は、フォルダを作るときに保存先が断る（全 Subject の一覧を取って確かめると約3秒かかった、M4）
    at = now()
    index = join({"okf_version": "0.2", "owner": owner or ""},
                 f"\n# {title}\n\n{str(description or '').strip()}\n\n## いまの要点\n- （まだありません。Discussion を重ねて書いていきます）\n")
    facets = join({"type": "Facet Definition", "title": "Facet 一覧",
                   "description": "この Subject の Facet と、その2段のグループ構造の定義。編集画面（D-5）で直す。",
                   "generated": {"by": f"human:{by}", "at": at}, "groups": groups}, "\n")
    try:
        st.create_subject(name, index)
    except StorageError:
        raise Refused(f"{name} はもうあります")
    today = dt.datetime.now(JST).date().isoformat()
    log = f"# 更新記録\n\n## {today}\n- Subject を作った。Facet {len(facet_ids(groups))} 個。担当: {owner or 'なし'}（human:{by}）\n"
    # フォルダは index.md を書いたときにできているので、残りの2つは並べて書く（書き込み1回が約3秒、M4 の計測）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda pt: st.write_text(name, *pt), [("/facets.md", facets), ("/log.md", log)]))
    return name


def next_year(ds, name, title, description, owner, by, groups):
    """次の年を始める（E-1）。前の年の Subject のファイルを1つずつ新しい Subject にコピーし、表示名・説明・担当を入れ替え、
    Facet の構成はそのまま引き継ぐ（始めたあとは構成を変えない、D-5 2026-11-22）。元の Subject はその年の FIX としてそのまま残す。
    groups は受け取っても使わない（古い画面との互換のため）。"""
    from concurrent.futures import ThreadPoolExecutor
    from .discussion import Discussions
    st, src = ds.st, ds.s
    if ds.registry()["open"]:
        raise Refused("会話中・確認待ちの Discussion があります。片付けてから次の年を始めてください")
    if any(e.get("waiting") for e in ds.edits()):
        raise Refused("人の修正の判断を待っています。終わってから次の年を始めてください")
    name, title = str(name or "").strip(), str(title or "").strip()
    if not NAME.match(name):
        raise Refused("名前は「英小文字・数字の文字列_年」の形にしてください（例: edible_coffee_2027）")
    if not title:
        raise Refused("表示名を入れてください")
    if name in st.subjects():
        raise Refused(f"{name} はもうあります")
    idx, body = split(st.read_text(src, "/index.md"))
    lines = body.splitlines()
    i = next((k for k, l in enumerate(lines) if l.startswith("# ")), None)
    if i is None:
        lines[:0] = [f"# {title}", ""]
    else:
        lines[i] = f"# {title}"
        if description is not None:  # 見出しの次の段落（説明）を入れ替える
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            k = j
            while k < len(lines) and lines[k].strip() and not lines[k].startswith("#"):
                k += 1
            lines[j:k] = [str(description).strip()] if str(description).strip() else []
    old_owner = idx.get("owner")
    owner = check_owner(owner)
    idx["owner"] = owner or old_owner or ""
    try:
        st.create_subject(name, join(idx, "\n".join(lines) + "\n"))
    except StorageError:
        raise Refused(f"{name} という名前のフォルダがもうあります")
    paths = [e.path for e in st.walk(src) if e.path != "/index.md"]
    with ThreadPoolExecutor(8) as ex:  # 読むのは並べて、書くのは順に（フォルダを二重に作らないよう保存先が順に書く）
        for p, data in zip(paths, ex.map(lambda p: st.read_bytes(src, p), paths)):
            st.write_bytes(name, p, data)
    new = Discussions(st, name, by, state=ds.state)
    new.log(f"{src} から次の年を始めた。ファイル {len(paths) + 1} 個をコピー（human:{by}）"
            + (f"。担当: {owner}" if owner and owner != old_owner else ""))
    ds.log(f"この Subject をこの年の FIX とし、{name} で次の年を始めた（human:{by}）")
    return name


def current(st, subject):
    """編集画面に出す、いまの構成と、中身（L1・L2）のある Facet。"""
    fm, _ = split(st.read_text(subject, "/facets.md"))
    idx, body = split(st.read_text(subject, "/index.md"))
    title = next((l[2:].strip() for l in body.splitlines() if l.startswith("# ")), subject)
    lines = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
    used = {e.path.split("/")[2] for e in st.walk(subject) if e.path.startswith("/facets/") and e.path.count("/") >= 3}
    started = bool(used) or any(re.match(r"^/discussions/[^/]+/", e.path) for e in st.walk(subject))
    return {"name": subject, "title": title, "description": lines[0] if lines else "", "owner": idx.get("owner"),
            "groups": fm.get("groups") or [], "used": sorted(used), "locked": started,
            "readonly": not st.can_edit(subject)}


def update(ds, groups):
    """始める前の編集（D-5）。Discussion が1つでもあるか、Facet に中身が1つでもあれば、構成は変えない（2026-11-22）。
    組み直したいときは、新しい Subject を作って1から埋める。"""
    st, subject = ds.st, ds.s
    if not st.can_edit(subject):
        raise Refused("この Subject は Drive で閲覧のみになっているため、Facet の構成は変えられません")
    if current(st, subject)["locked"] or ds.registry()["last"]:
        raise Refused("この Subject は始まっているので、Facet の構成は変えられません。組み直したいときは、新しい Subject を作ってください")
    groups = clean_groups(groups)
    path = "/facets.md"
    fm, body = split(st.read_text(subject, path))
    fm["groups"] = groups
    st.write_text(subject, path, join(fm, body))
    ds.log(f"Facet の構成を直した（始める前）（human:{ds.name}）")
    return groups


def set_owner(ds, owner):
    """担当を替える（B-3・D-3）。index.md の owner を書き換え、log.md に1行残す。誰が替えられるかは仕組みで止めない（B-4）。"""
    st, subject = ds.st, ds.s
    if not st.can_edit(subject):
        raise Refused("この Subject は Drive で閲覧のみになっているため、担当を変えられません")
    owner = check_owner(owner)
    if not owner:
        raise Refused("新しい担当のメールアドレスを入れてください")
    fm, body = split(st.read_text(subject, "/index.md"))
    old = fm.get("owner") or ""
    if old == owner:
        return owner
    fm["owner"] = owner
    st.write_text(subject, "/index.md", join(fm, body))
    ds.log(f"担当を {old or '（なし）'} から {owner} に替えた（human:{ds.email or ds.name}）")
    return owner
