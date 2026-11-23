"""M2 の画面とサーバーのつなぎ（/api/wiz を本物に差し替えたもの）。Gemini の代わりに決まった中身を書く偽の AI を使う。"""
import pathlib
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app import discuss
from app.agent import runner
from app.lattice.discussion import split
from app.lattice.storage import LocalStorage

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = "edible_coffee_2026"
SUMMARY = "## 話したこと\n- 価格\n\n## 決定\n- 720円から\n\n## 持ち越し\n- `persona`：見せ方\n"


def fake(monkeypatch):
    def reply(ds, did, model=None):
        w = ds.work(did)
        w["messages"].append({"role": "agent", "text": "返事", "at": "x"})
        w["waiting"] = None
        ds.save_work(w)

    def summarize(ds, did, model=None):
        ds.write_summary(did, "価格", "説明", SUMMARY)
        ds.set_candidates(did, [{"facet": "price", "reason": "r", "suggest": "update"}])

    def drafts(ds, did, model=None):
        ds.write_draft(did, "price", "L1", "# Price\n- 720円")
        ds.drafts_done(did)

    def judge(ds, eid, model=None):
        ds.judge(eid, "minor", "言い回し")

    for name, f in {"reply": reply, "continue_from": reply, "summarize": summarize, "drafts": drafts, "judge": judge,
                    "update_index": lambda *a, **k: None, "carryover": lambda *a, **k: None}.items():
        monkeypatch.setattr(runner, name, f)


@pytest.fixture
def client(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "dev_data" / S, tmp_path / S, ignore=shutil.ignore_patterns(".lattice"))
    fake(monkeypatch)
    from app import main
    main.app.state.storage = LocalStorage(tmp_path)
    main.app.state.user = "taro@example.com"
    main.app.router.routes[:0] = [r for r in discuss.router.routes]
    yield TestClient(main.app)
    del main.app.router.routes[:len(discuss.router.routes)]


def post(c, typ, **kw):
    r = c.post("/api/wiz/event", json={"subject": S, "type": typ, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def wait(c, did, cond, n=50):
    for _ in range(n):
        d = next((x for x in c.get(f"/api/wiz/state?s={S}").json()["discussions"] if x["id"] == did), None)
        if d and cond(d):
            return d
        time.sleep(0.05)
    raise AssertionError(f"{did}: 条件を満たさない: {d}")


def test_full_flow_through_api(client, tmp_path):
    pend = post(client, "start", origin="list")["discussion"]
    assert pend.startswith("new:") and client.get(f"/api/wiz/state?s={S}").json()["discussions"][0]["id"] != "D-008"  # まだ作らない
    did = post(client, "message", discussion=pend, text="価格の話")["discussion"]
    assert did == "D-008"
    wait(client, did, lambda d: d["waiting"] is None and d["messages"][-1]["role"] == "agent")
    post(client, "summarize", discussion=did)
    d = wait(client, did, lambda d: d["summary"] and d["candidates"])
    assert d["summary"].startswith("## 話したこと")
    post(client, "confirm", discussion=did)
    d = wait(client, did, lambda d: d["state"] == "review" and d["drafts"] and d["waiting"] is None)
    assert d["drafts"][0]["files"][0]["path"] == "/facets/price/price.md"
    post(client, "approve", discussion=did, facet="price")
    post(client, "finalize", discussion=did)
    d = wait(client, did, lambda d: d["state"] == "closed")
    f = split((tmp_path / S / "facets/price/price.md").read_text())[0]
    assert f["status"] == "stable" and f["verified"][0]["by"] == "human:taro"
    st = client.get(f"/api/wiz/state?s={S}").json()
    assert st["real"] and [x["id"] for x in st["rewindable"]] == [did]


def test_refusals_come_back_as_409(client):
    did = post(client, "message", discussion="new:list", text="x")["discussion"]
    r = client.post("/api/wiz/event", json={"subject": S, "type": "finalize", "discussion": did})
    assert r.status_code == 409 and r.json()["detail"]


def test_edit_and_judge(client, tmp_path):
    p = "/facets/stp/stp.md"
    body = split((tmp_path / S / p.lstrip("/")).read_text())[1]
    post(client, "edit_file", path=p, text=body.replace("自分で買って", "自分で購入して"))
    for _ in range(50):
        e = client.get(f"/api/wiz/state?s={S}").json()["edits"][0]
        if e["judgement"]:
            break
        time.sleep(0.05)
    assert e["judgement"] == "minor"
    assert "体裁だけ: 言い回し" in (tmp_path / S / "log.md").read_text()


def test_readonly_subject_refuses_writes(client, monkeypatch):
    """Drive で閲覧のみの Subject（REQUIREMENTS E-4）: 状態に印が出て、書き込む操作は 409 で断る。"""
    monkeypatch.setattr(LocalStorage, "can_edit", lambda self, s: False)
    assert client.get(f"/api/wiz/state?s={S}").json()["readonly"] is True
    r = client.post("/api/wiz/event", json={"subject": S, "type": "message", "discussion": "new:list", "text": "x"})
    assert r.status_code == 409 and "閲覧のみ" in r.json()["detail"]
