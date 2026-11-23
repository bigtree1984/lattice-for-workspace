"""Subject を作る・Facet の構成を直す（REQUIREMENTS D-5）。"""
import copy

import pytest

from app.lattice import subjects as sj
from app.lattice.discussion import Discussions, Refused, split

S = "edible_coffee_2026"


def mk(store):
    g = copy.deepcopy(sj.TEMPLATES["marketing"]["groups"])
    g.append({"id": "competition", "label": "競合", "groups": [
        {"id": "competitor-a", "label": "競合A", "facets": [{"id": "competitor-a-price", "label": "Price"}]}]})
    return sj.create(store, "new_brand_2027", "新ブランド", "説明の1行", "taro@example.com", g)


def test_create_from_template_with_own_group(store):
    assert mk(store) == "new_brand_2027"
    assert "new_brand_2027" in store.subjects()
    idx, body = split(store.read_text("new_brand_2027", "/index.md"))
    assert idx["owner"] == "taro@example.com" and "# 新ブランド" in body
    fm, _ = split(store.read_text("new_brand_2027", "/facets.md"))
    assert "competitor-a-price" in sj.facet_ids(fm["groups"]) and len(sj.facet_ids(fm["groups"])) == 15
    assert "Subject を作った" in store.read_text("new_brand_2027", "/log.md")
    with pytest.raises(Refused, match="もうあります"):
        mk(store)


def test_create_refuses_bad_names_and_duplicate_ids(store):
    g = sj.TEMPLATES["marketing"]["groups"]
    for bad in ("NewBrand_2027", "new brand_2027", "new_brand", "../x_2027"):
        with pytest.raises(Refused):
            sj.create(store, bad, "t", "", "a@b.jp", g)
    dup = copy.deepcopy(g)
    dup[0]["groups"][0]["facets"].append({"id": "price", "label": "重複"})
    with pytest.raises(Refused, match="重なって"):
        sj.create(store, "x_2027", "t", "", "a@b.jp", dup)


def test_update_rules(store):
    """始めたあとは構成を変えない（D-5 2026-11-22）。始める前（Discussion も中身も無い）だけ自由に直せる。"""
    ds = Discussions(store, S, "taro@example.com", "taro", model="test")
    cur = sj.current(store, S)
    assert cur["locked"] and "price" in cur["used"]
    g = copy.deepcopy(cur["groups"])
    g[0]["groups"][0]["facets"].append({"id": "pest-legal", "label": "Legal"})
    with pytest.raises(Refused, match="始まっている"):
        sj.update(ds, g)
    sj.create(store, "fresh_2027", "新しい", "", "taro@example.com", g)
    fresh = Discussions(store, "fresh_2027", "taro@example.com", "taro", model="test")
    assert not sj.current(store, "fresh_2027")["locked"]
    g2 = copy.deepcopy(g)
    g2[0]["groups"][0]["facets"] = g2[0]["groups"][0]["facets"][:1]  # 始める前なら消すのも自由
    sj.update(fresh, g2)
    assert "pest-legal" not in sj.facet_ids(sj.current(store, "fresh_2027")["groups"])
    fresh.create("話す")
    with pytest.raises(Refused, match="始まっている"):
        sj.update(fresh, g)


def test_templates_live_in_drive_folder(store):
    got = sj.templates(store)  # 初回は _templates を作り、組み込みの雛形を置く
    by = {t["id"]: t for t in got}
    assert set(by) == {"marketing", "research", "product-dev"} and len(sj.facet_ids(by["marketing"]["groups"])) == 14
    assert len(sj.facet_ids(by["research"]["groups"])) == 12 and len(sj.facet_ids(by["product-dev"]["groups"])) == 13
    assert "_templates" not in store.subjects()  # Subject 一覧には出ない
    store.write_text("_templates", "/retail.yaml",
                     "label: 小売\ndescription: 店頭\ngroups:\n  - id: shop\n    label: 店\n    groups:\n"
                     "      - id: floor\n        label: 売場\n        facets:\n          - {id: shelf, label: 棚}\n")
    store.write_text("_templates", "/broken.yaml", "label: 壊れ\ngroups: []\n")
    store.write_text("_templates", "/marketing.yaml", store.read_text("_templates", "/marketing.yaml").replace("ペルソナ", "顧客像"))
    got = {t["id"]: t for t in sj.templates(store)}
    assert set(got) == {"marketing", "research", "product-dev", "retail"}  # 壊れた雛形は読み飛ばす
    assert "顧客像" in str(got["marketing"]["groups"]) and sj.facet_ids(got["retail"]["groups"]) == ["shelf"]


def test_next_year_copies_and_keeps_numbering(store):
    """次の年を始める（E-1）。ファイルを全部コピーし、表示名・担当を入れ替え、元は FIX として残す。"""
    src = Discussions(store, S, "taro@example.com")
    cur = sj.current(store, S)
    files = {e.path for e in store.walk(S)}
    last = src.registry()["last"]
    g = copy.deepcopy(cur["groups"])
    g[0]["groups"][0]["facets"].append({"id": "new-one", "label": "新しい Facet"})
    assert sj.next_year(src, "edible_coffee_2027", "新しい年", "今年の説明", "next@example.com", "taro@example.com", g) == "edible_coffee_2027"
    N = "edible_coffee_2027"
    assert {e.path for e in store.walk(N)} == files
    idx, body = split(store.read_text(N, "/index.md"))
    assert idx["owner"] == "next@example.com" and "# 新しい年" in body and "今年の説明" in body
    assert "new-one" not in sj.facet_ids(sj.current(store, N)["groups"])  # 構成はそのまま引き継ぐ（組み直しは新しい Subject で）
    assert "次の年を始めた" in store.read_text(N, "/log.md") and "FIX" in store.read_text(S, "/log.md")
    assert Discussions(store, N, "x").registry()["last"] == last
    assert split(store.read_text(S, "/index.md"))[0].get("owner") != "next@example.com"  # 元はそのまま


def test_readonly_subject_cannot_change_structure(store, monkeypatch):
    name = mk(store)
    monkeypatch.setattr(type(store), "can_edit", lambda self, s: False)
    assert sj.current(store, name)["readonly"] is True
    with pytest.raises(Refused, match="閲覧のみ"):
        sj.update(Discussions(store, name, "taro@example.com"), sj.current(store, name)["groups"])


def test_readonly_subject_can_still_be_read(store, monkeypatch):
    """閲覧のみでも、索引を置けないだけで一覧と中身は読める。"""
    from app.lattice import bundle
    monkeypatch.setattr(type(store), "can_edit", lambda self, s: False)
    monkeypatch.setattr(type(store), "write_bytes", lambda *a: (_ for _ in ()).throw(AssertionError("書いた")))
    assert bundle.subject_summary(store, S)["readonly"] is True


def test_set_owner_writes_index_and_log(store):
    name = mk(store)
    ds = Discussions(store, name, "boss@example.com")
    assert sj.set_owner(ds, "new@example.com") == "new@example.com"
    idx, _ = split(store.read_text(name, "/index.md"))
    assert idx["owner"] == "new@example.com"
    assert "担当を taro@example.com から new@example.com に替えた（human:boss@example.com）" in store.read_text(name, "/log.md")
    with pytest.raises(Refused, match="メールアドレス"):
        sj.set_owner(ds, "だれか")


def test_create_owner_and_creator_differ(store):
    name = sj.create(store, "other_brand_2027", "別", "", "member@example.com", sj.TEMPLATES["marketing"]["groups"], by="boss@example.com")
    idx, _ = split(store.read_text(name, "/index.md"))
    assert idx["owner"] == "member@example.com"
    assert "担当: member@example.com（human:boss@example.com）" in store.read_text(name, "/log.md")


def test_new_builtin_templates_reach_old_folder_once(store):
    """以前の版で作った雛形フォルダ（marketing だけ）にも、あとから足した組み込みの雛形が1回だけ届く。消した雛形は置き直さない。"""
    store.create_subject("_templates", sj.TPL_INDEX)
    store.write_text("_templates", "/marketing.yaml", sj._tpl_yaml(sj.TEMPLATES["marketing"]))
    assert {t["id"] for t in sj.templates(store)} == {"marketing", "research", "product-dev"}
    store.delete("_templates", "/research.yaml")
    assert {t["id"] for t in sj.templates(store)} == {"marketing", "product-dev"}


def test_old_builtin_template_is_upgraded_unless_edited(store, monkeypatch):
    """以前の版の組み込みの雛形は、手を入れていなければ新しい版に置き換える。手を入れたものはそのまま。"""
    import hashlib
    sj.templates(store)
    old = "label: 研究テーマ\ngroups: []\n"
    store.write_text("_templates", "/research.yaml", old)
    store.write_text("_templates", "/product-dev.yaml", sj._tpl_yaml(sj.TEMPLATES["product-dev"]).replace("製品開発の検討", "直した"))
    monkeypatch.setitem(sj.OLD_BUILTIN, "research", {hashlib.sha1(old.encode()).hexdigest()})
    got = {t["id"]: t for t in sj.templates(store)}
    assert got["research"]["groups"][0]["groups"][0]["label"] == "問いと意義"
    assert got["product-dev"]["label"] == "直した"
