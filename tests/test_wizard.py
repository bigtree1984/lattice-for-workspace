"""M1.5 のウォークスルーの、機械的に決まる状態の変化（C-9・C-10・C-12）。"""
import json
import pathlib
import shutil

import pytest
from fastapi.testclient import TestClient

from app import wizard as W
from app.lattice.storage import LocalStorage

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = "edible_coffee_2026"


@pytest.fixture
def client(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "dev_data" / S, tmp_path / "drive" / S)
    monkeypatch.setattr(W, "ROOT", tmp_path / "wizard")
    monkeypatch.setattr(W, "EVENTS", tmp_path / "wizard" / "events.jsonl")
    from app import main
    main.app.state.storage = LocalStorage(tmp_path / "drive")
    # ウォークスルーは LATTICE_WIZARD=1 のときだけ組み込まれるので、テストでは手で足す（静的ファイルの "/" より前に）
    if not any(getattr(r, "path", "").startswith("/api/wiz") for r in main.app.routes):
        main.app.router.routes[:0] = W.router.routes
    return TestClient(main.app)


def post(c, typ, **kw):
    return c.post("/api/wiz/event", json={"subject": S, "type": typ, **kw}).json()


def state(c):
    return c.get(f"/api/wiz/state?s={S}").json()


def disc(c, did):
    return next(d for d in state(c)["discussions"] if d["id"] == did)


def test_start_numbers_after_existing(client):
    did = post(client, "start", origin="list")["discussion"]
    assert did == "D-008"  # サンプルは D-007 まで
    assert post(client, "start", facet="price")["discussion"] == "D-009"
    assert disc(client, "D-009")["origin"] == "facet:price"


def test_flow_update_approve_closes(client):
    did = post(client, "start")["discussion"]
    post(client, "message", discussion=did, text="価格の話")
    assert disc(client, did)["waiting"] == "reply"
    W.save(S, {**state(client), "discussions": [dict(disc(client, did), candidates=[
        {"facet": "price", "choice": "update"}, {"facet": "persona", "choice": "update"}])]})
    post(client, "confirm", discussion=did)
    d = disc(client, did)
    assert d["state"] == "review" and d["waiting"] == "drafts"
    st = state(client)
    st["discussions"][0]["drafts"] = [{"facet": "price", "files": [{"path": "/facets/price/price.md", "layer": "L1", "new": "x"}], "status": "pending"},
                                      {"facet": "persona", "files": [], "status": "pending"}]
    W.save(S, st)
    post(client, "edit_draft", discussion=did, facet="price", path="/facets/price/price.md", text="y")
    post(client, "approve", discussion=did, facet="price")
    assert client.post("/api/wiz/event", json={"subject": S, "type": "finalize", "discussion": did}).status_code == 409  # 予定がそろっていない
    post(client, "approve", discussion=did, facet="persona")
    post(client, "withdraw", discussion=did, facet="persona")  # 予定は押し直せる
    d = disc(client, did)
    assert d["state"] == "review" and d["drafts"][0]["status"] == "pending"  # 予定だけでは確定しない（離脱しても review のまま）
    post(client, "finalize", discussion=did)
    d = disc(client, did)
    assert d["state"] == "closed" and [x["status"] for x in d["drafts"]] == ["approved", "withdrawn"]
    assert d["drafts"][0]["files"][0] == {"path": "/facets/price/price.md", "layer": "L1", "new": "y", "edited_by_user": True}
    assert next(c for c in d["candidates"] if c["facet"] == "persona")["choice"] == "material"  # 取り下げは材料として残す


def test_all_withdrawn_reopens_and_back(client):
    did = post(client, "start")["discussion"]
    post(client, "choice", discussion=did, facet="price", choice="update")
    post(client, "confirm", discussion=did)
    st = state(client)
    st["discussions"][0]["drafts"] = [{"facet": "price", "files": [], "status": "pending"}]
    W.save(S, st)
    post(client, "withdraw", discussion=did, facet="price")
    post(client, "finalize", discussion=did)
    assert disc(client, did)["state"] == "open"
    post(client, "confirm", discussion=did)
    post(client, "back", discussion=did)
    assert disc(client, did)["state"] == "open"


def test_material_only_closes_without_review(client):
    did = post(client, "start")["discussion"]
    post(client, "choice", discussion=did, facet="price", choice="material")
    post(client, "confirm", discussion=did)
    assert disc(client, did)["state"] == "closed"


def test_discard_and_edit_and_log(client, tmp_path):
    did = post(client, "start")["discussion"]
    post(client, "discard", discussion=did)
    assert disc(client, did)["state"] == "discarded"
    post(client, "edit_file", path="/facets/price/price.md", text="new body")
    assert state(client)["edits"][0]["waiting"] == "judge"
    lines = (tmp_path / "wizard" / "events.jsonl").read_text().splitlines()
    assert [json.loads(l)["type"] for l in lines] == ["start", "discard", "edit_file"]


def test_rejects_bad_subject(client):
    assert client.get("/api/wiz/state?s=../x").status_code == 400


def test_back_allowed_before_finalize(client):
    """予定を選んでいても、確定する前なら戻れる（何も stable になっていないため）。"""
    did = post(client, "start")["discussion"]
    post(client, "choice", discussion=did, facet="price", choice="update")
    post(client, "confirm", discussion=did)
    st = state(client)
    st["discussions"][0]["drafts"] = [{"facet": "price", "files": [], "status": "pending"}]
    W.save(S, st)
    post(client, "approve", discussion=did, facet="price")
    post(client, "back", discussion=did)
    d = disc(client, did)
    assert d["state"] == "open" and d["drafts"][0]["status"] == "withdrawn"


def test_uncandidate_only_user_added(client):
    did = post(client, "start")["discussion"]
    post(client, "choice", discussion=did, facet="pest-political", choice="update")
    st = state(client)
    st["discussions"][0]["candidates"].append({"facet": "price", "suggest": "update", "choice": "update"})
    W.save(S, st)
    post(client, "uncandidate", discussion=did, facet="pest-political")
    post(client, "uncandidate", discussion=did, facet="price")
    assert [c["facet"] for c in disc(client, did)["candidates"]] == ["price"]
