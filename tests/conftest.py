"""保存先の用意。ふだんはローカルのフォルダ版。LATTICE_TEST_DRIVE=<共有ドライブの ID> なら、その共有ドライブでも同じテストを回す（層3、PLAN M3）。"""
import os
import pathlib
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.lattice.storage import DriveStorage, LocalStorage

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_DRIVE = os.environ.get("LATTICE_TEST_DRIVE", "")  # テスト用の共有ドライブの ID
KINDS = ["local"] + (["drive"] if TEST_DRIVE else [])


def _upload(st, subject, src):
    from app.lattice.storage.drive import ALL, FOLDER
    sid = st.svc.files().create(body={"name": subject, "mimeType": FOLDER, "parents": [st.root]}, fields="id", **ALL).execute(num_retries=5)["id"]
    files = [p for p in sorted(src.rglob("*")) if p.is_file() and ".lattice" not in p.parts]
    index = src / "index.md"
    st.svc.files()  # 先に作っておく
    from googleapiclient.http import MediaInMemoryUpload
    st.svc.files().create(body={"name": "index.md", "parents": [sid]}, media_body=MediaInMemoryUpload(index.read_bytes(), "text/markdown"),
                          fields="id", **ALL).execute(num_retries=5)
    # フォルダを先に作り、ファイルは並べて上げる
    for d in sorted({p.parent for p in files if p.parent != src}, key=lambda x: len(x.parts)):
        st._resolve(subject, "/" + d.relative_to(src).as_posix() + "/x", make_parents=True)
    from app.lattice.drive_files import service

    def put(p):
        if p == index:
            return
        local = DriveStorage(st.drive_id, service=_fresh(), root=st.root)
        local._ids = dict(st._ids)
        local.write_bytes(subject, "/" + p.relative_to(src).as_posix(), p.read_bytes())
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(put, files))


def _fresh():
    import google.auth
    from googleapiclient.discovery import build
    from app.lattice import drive_files
    drive_files.service()  # ADC の場所を設定する
    c, _ = google.auth.default(scopes=drive_files.SCOPES, quota_project_id=os.environ.get("LATTICE_PROJECT"))
    return build("drive", "v3", credentials=c, cache_discovery=False)


@pytest.fixture(scope="session")
def drive_template():
    """lattice_test に Subject の原本を1回だけ上げる。テストごとにはこれを写す。"""
    from app.lattice.storage.drive import ALL, FOLDER
    svc = _fresh()
    root = svc.files().create(body={"name": f"m3-test-{time.strftime('%m%d-%H%M%S')}", "mimeType": FOLDER,
                                    "parents": [TEST_DRIVE]}, fields="id", **ALL).execute(num_retries=5)["id"]
    tpl = svc.files().create(body={"name": "_template", "mimeType": FOLDER, "parents": [root]}, fields="id", **ALL).execute(num_retries=5)["id"]
    _upload(DriveStorage(TEST_DRIVE, service=svc, root=tpl), "edible_coffee_2026", ROOT / "dev_data" / "edible_coffee_2026")
    yield svc, root, tpl
    svc.files().update(fileId=root, body={"trashed": True}, fields="id", **ALL).execute(num_retries=5)  # 後片付けはゴミ箱へ


def _copy_tree(svc, src, dst):
    from app.lattice.storage.drive import ALL, FOLDER, LIST
    jobs, level = [], [(src, dst)]
    while level:
        nxt = []
        for s_id, d_id in level:
            for f in svc.files().list(q=f"'{s_id}' in parents and trashed = false", fields="files(id,name,mimeType)",
                                      pageSize=1000, driveId=TEST_DRIVE, **LIST).execute(num_retries=5)["files"]:
                if f["mimeType"] == FOLDER:
                    nid = svc.files().create(body={"name": f["name"], "mimeType": FOLDER, "parents": [d_id]}, fields="id", **ALL).execute(num_retries=5)["id"]
                    nxt.append((f["id"], nid))
                else:
                    jobs.append((f["id"], f["name"], d_id))
        level = nxt

    def cp(j):
        _fresh().files().copy(fileId=j[0], body={"name": j[1], "parents": [j[2]]}, fields="id", **ALL).execute(num_retries=5)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(cp, jobs))


@pytest.fixture(params=KINDS)
def store(request, tmp_path):
    """Subject（edible_coffee_2026 の写し）を入れた保存先を返す。"""
    s = "edible_coffee_2026"
    if request.param == "local":
        shutil.copytree(ROOT / "dev_data" / s, tmp_path / s, ignore=shutil.ignore_patterns(".lattice"))
        yield LocalStorage(tmp_path)
        return
    from app.lattice.storage.drive import ALL, FOLDER
    svc, root, tpl = request.getfixturevalue("drive_template")
    case = svc.files().create(body={"name": request.node.name[:100], "mimeType": FOLDER, "parents": [root]}, fields="id", **ALL).execute(num_retries=5)["id"]
    _copy_tree(svc, tpl, case)
    yield DriveStorage(TEST_DRIVE, service=svc, root=case)
