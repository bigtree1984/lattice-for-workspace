"""組み直して移す（REQUIREMENTS E-4）。Gemini の代わりに、決まった中身を書く偽の AI を使う。"""
import pytest

from app.lattice import migrate
from app.lattice import subjects as sj
from app.lattice.discussion import Discussions, Refused, split

OLD = "edible_coffee_2026"
NEW = "coffee_reorg_2027"
GROUPS = [{"id": "market", "label": "市場", "groups": [{"id": "core", "label": "中心", "facets": [
    {"id": "strength", "label": "強み"}, {"id": "weakness", "label": "弱み"}, {"id": "price-new", "label": "価格"}]}]}]
BODY = "## 話したこと\n- 前の price と D-001 を読んだ\n\n## 決定\n- 移した\n\n## 持ち越し\n- なし\n"


class FakeRunner:
    """割り当て: strength と weakness は同じ前の Discussion を使う（複製は1回だけのはず）。price-new は何も使わない。"""

    def __init__(self, old_ids):
        self.d = old_ids[0]

    def migrate_plan(self, ds, old):
        return {"strength": {"old_discussions": [self.d]}, "weakness": {"old_discussions": [self.d]}}

    def migrate_facet(self, ds, did, old, facet, plan):
        from app.agent.runner import Run
        run = Run(ds, did, "migrate")
        run.mig_old, run.mig_summaries = old, Discussions(ds.st, old, state=ds.state).summaries_full()
        run.call("write_migration_summary", {"title": facet, "description": "d", "body": BODY,
                                             "old_discussions": plan.get("old_discussions", [])})
        run.call("write_l1_draft", {"facet": facet, "body": f"{facet} の L1"})
        run.call("write_l2_draft", {"facet": facet, "issue": "main", "body": f"{facet} の L2"})
        ds.update_work(did, lambda w: w.update(waiting=None))


@pytest.fixture
def pair(store):
    sj.create(store, NEW, "組み直し", "説明", "taro@example.com", GROUPS)
    ds = Discussions(store, NEW, "taro@example.com")
    old_ids = sorted(Discussions(store, OLD).summaries_full())
    migrate._put(ds, {"from": OLD, "status": "running", "facets": migrate.facet_order(ds), "done": [], "current": None,
                      "started": "t", "copied": []})
    return ds, old_ids


def test_migrate_all_facets(store, pair):
    ds, old_ids = pair
    migrate.run_all(ds, FakeRunner(old_ids))
    m = migrate.get(ds)
    assert m["status"] == "done" and m["done"] == ["strength", "weakness", "price-new"]
    # Facet ごとに一人 Discussion ができて、閉じている
    full = ds.summaries_full()
    assert len(full) == 3 and not ds.registry()["open"]
    # L1・L2 は stable で、移行の印があり、人の確認（verified）は無い
    fm, _ = split(store.read_text(NEW, "/facets/strength/strength.md"))
    assert fm["status"] == "stable" and fm["migrated"]["from"] == OLD and "verified" not in fm
    # 前の Discussion は1回だけ複製され、2つの一人 Discussion の出典になっている
    d = old_ids[0]
    copies = [e.path for e in store.walk(NEW) if e.path.startswith("/imported/") and e.path.endswith("discussion_summary.md")]
    assert len(copies) == 1 and m["copied"] == [d]
    users = [did for did, (_, f, _) in full.items() if copies[0] in [s["resource"] for s in f.get("sources", [])]]
    assert len(users) == 2
    cfm, _ = split(store.read_text(NEW, copies[0]))
    assert cfm["layer"] == "source" and cfm["imported_from"]["discussion_id"] == d  # 新しい Discussion と番号が重ならない
    # 原典カードも複製され、出典のパスは新しい Subject の中を指す
    for s in cfm.get("sources", []):
        if s["resource"].startswith("/"):
            assert s["resource"].startswith(f"/imported/{OLD}/") and store.exists(NEW, s["resource"])
    # 移さなかったものの一覧
    rec = store.read_text(NEW, f"/imported/{OLD}/migration.md")
    assert "移さなかった" in rec and old_ids[1] in rec.split("移さなかった")[1]
    # 前の Subject には手を付けない
    assert not any(e.path.startswith("/imported/") for e in store.walk(OLD))


def test_human_edit_removes_mark_and_writes_blocked_while_running(store, pair):
    ds, old_ids = pair
    assert migrate.running(ds)
    migrate.run_all(ds, FakeRunner(old_ids))
    assert not migrate.running(ds)
    # 人の Discussion で直すと、移行の印は外れる
    did = ds.create("強みを直したい")
    ds.update_work(did, lambda w: w.update(waiting=None))
    ds.write_summary(did, "直す", "d", BODY)
    ds.set_candidates(did, [{"facet": "strength", "reason": "r", "suggest": "update"}])
    ds.confirm(did)
    ds.write_draft(did, "strength", "L1", "直した L1")
    fm, _ = split(store.read_text(NEW, "/facets/strength/strength.md"))
    assert "migrated" not in fm


def test_resume_after_stop(store, pair, monkeypatch):
    ds, old_ids = pair

    class Boom(FakeRunner):
        def migrate_facet(self, ds, did, old, facet, plan):
            if facet == "weakness":
                raise RuntimeError("止まった")
            super().migrate_facet(ds, did, old, facet, plan)
    migrate.run_all(ds, Boom(old_ids))
    m = migrate.get(ds)
    assert m["status"] == "failed" and m["done"] == ["strength"]
    # 途中の一人 Discussion を捨てて、続きから
    monkeypatch.setattr(migrate, "_go", lambda ds: migrate.run_all(ds, FakeRunner(old_ids)))
    migrate.resume(ds)
    m = migrate.get(ds)
    assert m["status"] == "done" and m["done"] == ["strength", "weakness", "price-new"]
    assert not ds.registry()["open"] and len(ds.summaries_full()) == 3


def test_manifest_only_after_last_facet(store, pair):
    """巻き戻しの目録は、移行を終えた時点（最後の Facet）の1つだけ書く。途中の Facet では書かない（Drive の書き込みを減らす）。"""
    ds, old_ids = pair
    migrate.run_all(ds, FakeRunner(old_ids))
    ms = [e.path for e in store.walk(NEW) if "/manifests/" in e.path] or \
         [p.name for p in (store.root / NEW / ".lattice" / "manifests").glob("D-*.json")]
    assert len(ms) == 1
    assert [r["id"] for r in ds.rewindable()] == [sorted(ds.summaries_full())[-1]]
