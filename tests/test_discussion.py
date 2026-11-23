"""M2 の決定論的な関数（REQUIREMENTS G-1・G-2、C-7〜C-12、E-2）のテスト。Gemini は呼ばない（層1）。"""
import json
import pathlib
import shutil

import pytest

from app.lattice.discussion import Discussions, Refused, split
from app.lattice.storage import LocalStorage

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = "edible_coffee_2026"
SUMMARY = "## 話したこと\n- 価格の話\n\n## 決定\n- 20個入りを700円からにする\n\n## 持ち越し\n- `persona`：見せ方\n"


@pytest.fixture
def ds(store):
    return Discussions(store, S, "taro@example.com", "taro", model="test")


def fm(ds, p):
    return split(ds.st.read_text(S, p))[0]


def replied(ds, did):
    """Gemini の返事が出た状態にする（このテストでは AI を呼ばない）。"""
    ds.update_work(did, lambda w: w.update(waiting=None))
    return did


def to_review(ds, facets=("price",)):
    did = replied(ds, ds.create("価格を見直したい"))
    ds.write_summary(did, "価格の見直し", "価格を見直した。", SUMMARY)
    ds.set_candidates(did, [{"facet": f, "reason": "r", "suggest": "update"} for f in facets])
    assert ds.confirm(did) == "review"
    return did


def test_create_numbers_after_existing_and_waits_for_reply(ds):
    did = ds.create("こんにちは")
    assert did == "D-008"
    w = ds.work(did)
    assert w["dir"].endswith("-d008/") and w["waiting"] == "reply" and w["messages"][0]["text"] == "こんにちは"
    with pytest.raises(Refused):
        ds.create("   ")  # 発言が無ければ作らない（C-7）
    assert ds.create("次") == "D-009"


def test_summary_frontmatter_by_app(ds):
    did = ds.create("x")
    ds.work(did)
    with pytest.raises(Refused):
        ds.write_summary(did, "t", "d", "本文だけ")  # 節が足りない
    ds.write_summary(did, "価格の見直し", "説明", SUMMARY)
    f, body = ds.summary(did)
    assert f["discussion_id"] == did and f["discussion_state"] == "open" and f["layer"] == "discussion"
    assert body.startswith(f"\n# {did} 価格の見直し")


def test_candidates_only_existing_facets(ds):
    did = ds.create("x")
    with pytest.raises(Refused):
        ds.set_candidates(did, [{"facet": "no-such", "reason": "", "suggest": "update"}])
    ds.choose(did, "swot", "material")
    ds.set_candidates(did, [{"facet": "price", "reason": "r", "suggest": "update"}])
    assert [c["facet"] for c in ds.work(did)["candidates"]] == ["price", "swot"]  # 利用者が足した候補は残る


def test_material_only_closes_without_review(ds):
    did = replied(ds, ds.create("x"))
    ds.write_summary(did, "t", "d", SUMMARY)
    ds.set_candidates(did, [{"facet": "persona", "reason": "r", "suggest": "material"}])
    assert ds.confirm(did) == "closed"
    assert fm(ds, ds.folder(did) + "discussion_summary.md")["discussion_state"] == "closed"
    assert did not in ds.registry()["open"]
    assert ds.state.get(S, f"work/{did}") is None  # 会話のログは消える（C-11）


def test_write_draft_guards_and_paths(ds):
    did = to_review(ds)
    with pytest.raises(Refused):
        ds.write_draft(did, "persona", "L1", "x")  # 「更新する」が選ばれていない（G-2）
    with pytest.raises(Refused):
        ds.write_draft(did, "price", "L2", "x", issue="../etc")
    p2 = ds.write_draft(did, "price", "L2", "## 価格\n- 700円", issue="price", sources=["/facets/persona/persona.md"])
    assert p2 == "/facets/price/price-context.md"
    p1 = ds.write_draft(did, "price", "L1", "# Price\n- 700円")
    assert p1 == "/facets/price/price.md"
    f1, f2 = fm(ds, p1), fm(ds, p2)
    assert f1["status"] == f2["status"] == "draft" and "verified" not in f1
    assert ds.folder(did) + "discussion_summary.md" in [s["resource"] for s in f2["sources"]]
    assert all(s["resource"].startswith("/facets/price/") for s in f1["sources"])  # L1 の出どころは自分の L2 だけ


def test_finalize_two_step_approve_and_withdraw(ds):
    did = to_review(ds, ("price", "persona"))
    before = ds.st.read_text(S, "/facets/persona/persona-context.md")
    ds.write_draft(did, "price", "L1", "# Price\n- 700円")
    ds.write_draft(did, "persona", "L2", "# ペルソナ\n- 変えた", issue="persona")
    ds.mark(did, "price", "approve")
    with pytest.raises(Refused):
        ds.finalize(did)  # 予定がそろっていない
    ds.mark(did, "persona", "withdraw")
    approved, withdrawn = ds.finalize(did)
    assert approved == ["price"] and withdrawn == ["persona"]
    f = fm(ds, "/facets/price/price.md")
    assert f["status"] == "stable" and f["verified"][0]["by"] == "human:taro"
    assert ds.st.read_text(S, "/facets/persona/persona-context.md") == before  # 取り下げは書く直前の版に戻す
    assert next(c for c in ds.work(did)["candidates"] if c["facet"] == "persona")["choice"] == "material"
    ds.close(did)
    m = json.loads(ds.st.read_text(S, f"/.lattice/manifests/{did}.json"))
    assert "/facets/price/price.md" in m["files"]
    assert f"{did} を閉じた" in ds.st.read_text(S, "/log.md")


def test_draft_keeps_body_before_it_was_written(ds):
    did = to_review(ds)
    before = split(ds.st.read_text(S, "/facets/price/price.md"))[1]
    ds.write_draft(did, "price", "L1", "# 新しい本文")
    ds.write_draft(did, "price", "L2", "x", issue="brand-new")
    ds.write_draft(did, "price", "L1", "# 書き直し")  # 書き直しても「旧」は最初のまま
    files = {f["path"]: f for f in ds.work(did)["drafts"][0]["files"]}
    assert files["/facets/price/price.md"]["old"] == before
    assert files["/facets/price/brand-new-context.md"]["old"] is None  # 新しいファイルに「旧」は無い


def test_back_restores_new_files(ds):
    did = to_review(ds)
    ds.write_draft(did, "price", "L2", "x", issue="brand-new")
    assert ds.st.exists(S, "/facets/price/brand-new-context.md")
    ds.back(did)
    assert not ds.st.exists(S, "/facets/price/brand-new-context.md") and ds.work(did)["state"] == "open"


def test_discard_removes_folder_and_keeps_number(ds):
    did = to_review(ds)
    ds.write_draft(did, "price", "L1", "x")
    ds.discard(did)
    assert not any(e.path.startswith("/discussions/") and "-d008/" in e.path for e in ds.st.walk(S))
    assert fm(ds, "/facets/price/price.md")["status"] == "stable"  # draft は戻った
    assert ds.create("次") == "D-009"  # 番号は使い回さない


def test_rewind(ds):
    a = to_review(ds)
    ds.write_draft(a, "price", "L1", "# A")
    ds.mark(a, "price", "approve")
    ds.finalize(a)
    ds.close(a)
    b = to_review(ds)
    ds.write_draft(b, "price", "L1", "# B")
    ds.mark(b, "price", "approve")
    ds.finalize(b)
    ds.close(b)
    assert [x["id"] for x in ds.rewindable()][:2] == [b, a]
    assert ds.rewind(a) == [b]
    assert split(ds.st.read_text(S, "/facets/price/price.md"))[1].strip() == "# A"
    assert not ds.st.exists(S, ds.folder(a).replace("d008", "d009") + "discussion_summary.md")
    assert "の直後に戻した" in ds.st.read_text(S, "/log.md")


def test_edit_minor_keeps_stable_meaning_starts_discussion(ds):
    p = "/facets/price/price.md"
    body = split(ds.st.read_text(S, p))[1]
    e1 = ds.save_edit(p, body.replace("主力", "メイン"))
    assert ds.judge(e1, "minor", "言い回し") is None
    assert fm(ds, p)["status"] == "stable"  # 体裁だけなら stable のまま（C-6）
    e2 = ds.save_edit(p, body.replace("600", "720"))
    did = ds.judge(e2, "meaning", "価格が変わった")
    assert did and ds.work(did)["origin"] == f"edit:{p}" and fm(ds, p)["status"] == "draft"
    assert ds.work(did)["candidates"][0]["facet"] == "price"  # 直した Facet は「更新する」の候補に入る（draft のまま残さない）
    with pytest.raises(Refused):
        ds.save_edit("/index.md", "x")  # L1・L2 だけ


def test_waiting_blocks_and_conflicts(ds):
    did = ds.create("x")
    with pytest.raises(Refused):
        ds.add_user_message(did, "続けて送る")  # 返事を待っている間は送れない
    a = to_review(ds)
    ds.write_draft(a, "price", "L1", "# A")
    b = to_review(ds)
    with pytest.raises(Refused, match=a):
        ds.write_draft(b, "price", "L1", "# B")  # 確認待ちの draft とぶつかる（C-12）
    with pytest.raises(Refused, match=a):
        ds.save_edit("/facets/price/price.md", "x")  # draft を持つファイルは人も直せない


def test_finishing_blocks_back_and_discard(ds):
    did = to_review(ds)
    ds.write_draft(did, "price", "L1", "# A")
    ds.mark(did, "price", "approve")
    ds.finalize(did)
    for f in (ds.back, ds.discard):
        with pytest.raises(Refused):
            f(did)


def test_keeps_own_frontmatter_keys_and_no_self_source(ds):
    p = "/facets/price/price.md"
    f0, body = split(ds.st.read_text(S, p))
    f0["custom"] = "keep"
    from app.lattice.discussion import join
    ds.st.write_text(S, p, join(f0, body))
    did = to_review(ds)
    ds.update_work(did, lambda w: w["read"].append(w["dir"] + "discussion_summary.md"))
    ds.write_draft(did, "price", "L1", "# A")
    assert fm(ds, p)["custom"] == "keep"
    ds.back(did)
    ds.write_summary(did, "t", "d", SUMMARY)
    assert all(not s["resource"].startswith(ds.folder(did)) for s in ds.summary(did)[0]["sources"])


def test_undo_rewind_until_next_discussion(ds):
    a = to_review(ds)
    ds.write_draft(a, "price", "L1", "# A")
    ds.mark(a, "price", "approve"); ds.finalize(a); ds.close(a)
    b = to_review(ds)
    ds.write_draft(b, "price", "L1", "# B")
    ds.mark(b, "price", "approve"); ds.finalize(b); ds.close(b)
    assert ds.undoable() is None
    ds.rewind(a)
    assert ds.undoable() == a
    ds.undo_rewind()
    assert ds.undoable() is None
    assert split(ds.st.read_text(S, "/facets/price/price.md"))[1].strip() == "# B"
    ds.rewind(a)
    replied(ds, ds.create("次"))
    with pytest.raises(Refused):
        ds.undo_rewind()


def test_edit_during_discussion_joins_it(ds):
    """会話の途中で直したら、その Discussion に含める（C-6）。"""
    did = replied(ds, ds.create("価格の話"))
    p = "/facets/price/price.md"
    body = split(ds.st.read_text(S, p))[1]
    e1 = ds.save_edit(p, body.replace("主力", "メイン"), into=did)
    assert ds.judge(e1, "minor", "言い回し") is None
    assert fm(ds, p)["status"] == "stable" and ds.work(did)["messages"][-1]["role"] == "edit"
    e2 = ds.save_edit(p, body.replace("600", "720"), into=did)
    assert ds.judge(e2, "meaning", "価格が変わった") == did  # 新しい Discussion は始めない
    w = ds.work(did)
    assert ds.registry()["last"] == 8 and fm(ds, p)["status"] == "draft"
    assert w["waiting"] == "reply" and any(c["facet"] == "price" and c["choice"] == "update" for c in w["candidates"])
    replied(ds, did)
    ds.discard(did)
    assert fm(ds, p)["status"] == "stable" and fm(ds, p).get("verified")  # 廃棄したら stable に戻す（本文は人の修正のまま）


def test_edit_into_refused_unless_open_and_idle(ds):
    did = ds.create("x")  # 返事待ち
    with pytest.raises(Refused, match="返事"):
        ds.save_edit("/facets/stp/stp.md", "x", into=did)
    to = to_review(ds)
    with pytest.raises(Refused, match="会話中ではない"):
        ds.save_edit("/facets/stp/stp.md", "x", into=to)


def test_edit_into_closed_meanwhile_falls_back(ds):
    did = replied(ds, ds.create("x"))
    p = "/facets/stp/stp.md"
    e = ds.save_edit(p, split(ds.st.read_text(S, p))[1] + "\n- 追記", into=did)
    ds.discard(did)
    new = ds.judge(e, "meaning", "追記")
    assert new and new != did and ds.work(new)["origin"] == f"edit:{p}"



def test_recover_stale_drafts(ds):
    import time
    did = ds.create("テスト", origin="list")
    ds.update_work(did, lambda w: w.update(state="review", waiting="drafts", wait_since=time.time() - 2000))
    assert ds.recover_stale(did)
    w = ds.work(did)
    assert w["state"] == "open" and w["waiting"] is None and "入れ替わって" in w["messages"][-1]["text"]
    ds.update_work(did, lambda w: w.update(waiting="reply", wait_since=time.time()))
    assert not ds.recover_stale(did)


def _reply(ds, did, text):
    ds.update_work(did, lambda w: (w["history"].extend([{"role": "user"}, {"role": "model"}]),
                                   w["messages"].append({"role": "agent", "text": text, "at": "t"}), w.update(waiting=None)))


def test_undo_last_message(ds):
    import pytest
    did = ds.create("こんにちは", origin="list")
    _reply(ds, did, "はい")
    assert not ds.can_undo_message(ds.work(did))  # 最初の発言は取り消せない
    ds.add_user_message(did, "まちがい")
    with pytest.raises(Refused):  # 返事を待っている間は取り消せない
        ds.undo_message(did)
    _reply(ds, did, "了解")
    assert ds.can_undo_message(ds.work(did))
    assert ds.undo_message(did) == "まちがい"
    w = ds.work(did)
    assert [m["text"] for m in w["messages"]] == ["こんにちは", "はい"] and len(w["history"]) == 2


def test_undo_refused_after_summary_or_file(ds):
    import pytest
    did = ds.create("こんにちは", origin="list")
    _reply(ds, did, "はい")
    ds.add_user_message(did, "次")
    _reply(ds, did, "どうぞ")
    ds.update_work(did, lambda w: w.update(summary_at="2026-11-21T10:00:00+09:00"))
    with pytest.raises(Refused, match="要約"):
        ds.undo_message(did)
    ds.add_user_message(did, "さらに")
    ds.update_work(did, lambda w: (w["messages"].append({"role": "file", "text": "a.csv", "at": "t"}), w.update(waiting=None)))
    with pytest.raises(Refused, match="資料"):
        ds.undo_message(did)
