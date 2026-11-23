"""M1 の単体テスト（PLAN F-3 の層1）: 保存先のローカル版、索引の JSON（D-1）、Web の入口。"""
import importlib.util
import json
import os
import pathlib
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app.lattice import bundle
from app.lattice.storage import LocalStorage, StorageError

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "dev_data" / "edible_coffee_2026"
S = "edible_coffee_2026"


@pytest.fixture
def drive(tmp_path):
    shutil.copytree(SAMPLE, tmp_path / S)
    return LocalStorage(tmp_path)


# --- 保存先 ---

def test_subjects_lists_only_folders_with_index(drive, tmp_path):
    (tmp_path / "no_index").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "index.md").write_text("x")
    assert drive.subjects() == [S]


@pytest.mark.parametrize("subject,path", [
    ("..", "/index.md"), (".hidden", "/index.md"), ("a/b", "/index.md"),
    (S, "/../x.md"), (S, "/facets/../../x.md"), (S, ""), (S, "/a//b.md"),
])
def test_rejects_paths_outside_subject(drive, subject, path):
    with pytest.raises(StorageError):
        drive.read_bytes(subject, path)


def test_walk_skips_hidden(drive):
    drive.write_text(S, "/.lattice/x.json", "{}")
    assert not any(e.path.startswith("/.") for e in drive.walk(S))


# --- 索引（D-1） ---

def test_index_is_written_and_reused(drive):
    first = bundle.load(drive, S)
    stored = json.loads(drive.read_text(S, bundle.INDEX))
    assert stored["generated_at"] == first["generated_at"]
    assert bundle.load(drive, S)["generated_at"] == first["generated_at"]  # 変わっていなければ作り直さない


def test_index_rebuilt_after_outside_change(drive, tmp_path):
    first = bundle.load(drive, S)
    p = tmp_path / S / "facets.md"
    p.write_text(p.read_text() + "\n")
    t = first["generated_at"] + 5
    os.utime(p, (t, t))  # ドライブで誰かが直した、という想定
    assert bundle.load(drive, S)["generated_at"] >= t


def test_index_rebuilt_when_broken(drive):
    drive.write_text(S, bundle.INDEX, "not json")
    assert bundle.load(drive, S)["files"]


def test_summary(drive):
    s = bundle.subject_summary(drive, S)
    assert s["owner"] == "taro@example.com"
    assert s["facets"] > 0 and s["discussions"] == 6


# --- Web の入口 ---

@pytest.fixture
def client(drive):
    from app import main
    main.app.state.storage = drive
    main.app.state.user = "taro@example.com"
    return TestClient(main.app)


def test_api_subjects_and_bundle(client):
    assert client.get("/api/subjects").json()[0]["id"] == S
    assert any(f["path"] == "/facets.md" for f in client.get(f"/api/s/{S}/bundle").json()["files"])
    assert client.get("/api/me").json()["email"] == "taro@example.com"


def test_api_rejects_bad_subject_and_hidden_assets(client):
    assert client.get("/api/s/../bundle").status_code == 404
    assert client.get(f"/api/s/{S}/asset/.lattice/bundle.json").status_code == 404
    assert client.get(f"/api/s/{S}/asset/index.md").status_code == 404
    assert client.get(f"/api/s/{S}/asset/discussions/x/../../index.md").status_code == 404


def test_security_headers(client):
    r = client.get("/subjects.html")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_asset_opens_inline_or_downloads(client, drive):
    drive.write_bytes(S, "/discussions/2026-10-30-d001/source-x.pdf", b"%PDF-1.4 test")
    drive.write_bytes(S, "/discussions/2026-10-30-d001/source-資料.xlsx", b"PK")
    r = client.get(f"/api/s/{S}/asset/discussions/2026-10-30-d001/source-x.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    r = client.get(f"/api/s/{S}/asset/discussions/2026-10-30-d001/source-資料.xlsx")
    assert r.headers["content-disposition"].startswith("attachment")
    assert "/discussions/2026-10-30-d001/source-x.pdf" in client.get(f"/api/s/{S}/bundle").json()["assets"]


def test_revisions_and_restore(drive):
    p = "/facets/price/price.md"
    r0 = drive.head(S, p)
    drive.write_text(S, p, "new")
    assert drive.head(S, p) != r0 and drive.read_rev(S, p, r0).decode().startswith("---")
    drive.restore(S, p, r0)
    assert drive.head(S, p) == r0
    drive.write_text(S, "/facets/price/x.md", "x")
    drive.restore(S, "/facets/price/x.md", None)
    assert not drive.exists(S, "/facets/price/x.md")
    drive.delete(S, p)
    drive.restore(S, p, r0)  # 消したファイルも版から戻せる
    assert drive.head(S, p) == r0
