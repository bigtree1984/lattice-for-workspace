"""Lattice GWS 版の Web アプリ（M1: ビューアー）。

起動: .venv/bin/uvicorn app.main:app --port 8000（作業フォルダはこのプロジェクトの直下）
  LATTICE_DRIVE_ID    共有ドライブの ID。あればドライブ版（M3）を使う。D-2 の画面で選ぶ形は M4
  LATTICE_LOCAL_ROOT  ローカルのフォルダ版で使うフォルダ（既定: dev_data）。共有ドライブの代わり。
  LATTICE_DEV_USER    ログインなしで動かすときの利用者のメールアドレス（手元の開発・テスト用）
  LATTICE_OAUTH_SECRET など  ログインを有効にする（app/auth.py、REQUIREMENTS C-4）

- /                               Subject を選ぶ画面
- /index.html?s=<Subject>         その Subject のビューアー
- GET /api/me                     利用者（自分の Subject のハイライト用、REQUIREMENTS D-3）
- GET /api/subjects               Subject の一覧
- GET /api/s/<Subject>/bundle     その Subject の索引の JSON（D-1）
- GET /api/s/<Subject>/asset/<パス>  実物のファイル（新しいタブで開く、D-4）

いまのローカル版にあった /api/open（PC のアプリで開く）と /api/focus（focus.md を書く）は持たない。
PC のアプリは使わず（A-4）、「見ているもの」はチャットと同じ画面の中で伝える（C-1、M2 で作る）。
"""
import mimetypes
import os
import time
import pathlib
from urllib.parse import quote

from fastapi import Request, FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool

from . import auth as auth_mod
from .lattice import bundle
from .lattice.storage import DriveStorage, LocalStorage, StorageError

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

app = FastAPI(title="Lattice", docs_url=None, redoc_url=None, openapi_url=None)
app.state.storage = (DriveStorage(os.environ["LATTICE_DRIVE_ID"]) if os.environ.get("LATTICE_DRIVE_ID")
                     else LocalStorage(os.environ.get("LATTICE_LOCAL_ROOT", ROOT / "dev_data")))
# 会話中の作業の状態: ドライブ版は Firestore（東京）、ローカル版は保存先の隠しファイル（app/lattice/state.py）
if os.environ.get("LATTICE_DRIVE_ID"):
    from .lattice.state import FirestoreState
    app.state.work_state = FirestoreState(os.environ["LATTICE_DRIVE_ID"], project=os.environ.get("LATTICE_PROJECT"))
    from .agent import usage as _usage

    class _LazyCounter:  # Firestore の接続は最初に使うときに作る
        def __getattr__(self, k):
            c = _usage.FirestoreCounter(app.state.work_state.db)
            _usage.COUNTER = c
            return getattr(c, k)
    _usage.COUNTER = _LazyCounter()
else:
    app.state.work_state = None
app.state.user = os.environ.get("LATTICE_DEV_USER", "")
app.include_router(auth_mod.router)
import contextvars  # noqa: E402
_ST = contextvars.ContextVar("lattice_storage", default=None)
_USER_ST = {}  # 利用者 → その人の権限で読み書きするドライブ版の保存先（キャッシュも人ごと）


def _storage_for(request):
    creds = getattr(request.state, "creds", None)
    if creds is None or not os.environ.get("LATTICE_DRIVE_ID"):
        return app.state.storage
    st = _USER_ST.get(request.state.user)
    if st is None:
        from googleapiclient.discovery import build
        if len(_USER_ST) > 200:
            _USER_ST.clear()
        st = _USER_ST[request.state.user] = DriveStorage(os.environ["LATTICE_DRIVE_ID"],
                                                         service=build("drive", "v3", credentials=creds, cache_discovery=False))
    else:
        c = st._svc._http.credentials  # 同じ資格情報の部品を各スレッドが使うので、鍵だけ差し替える
        c.token, c.expiry = creds.token, creds.expiry
    return st


@app.middleware("http")
async def login(request, call_next):
    async def inner(req):
        from .agent import usage
        usage.USER.set(req.state.user)  # Gemini の1日の利用上限を人ごとに数える
        tok = _ST.set(_storage_for(req))
        req.state.storage = _ST.get()
        try:
            return await call_next(req)
        finally:
            _ST.reset(tok)
    return await auth_mod.middleware(request, inner)

# 実物のうち、ブラウザで見せてよい種類。それ以外はダウンロードさせる
INLINE = {"application/pdf", "image/png", "image/jpeg", "image/gif", "text/plain", "video/mp4"}


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _st():
    return _ST.get() or app.state.storage


@app.exception_handler(StorageError)
async def storage_error(request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/subjects.html")


@app.get("/api/me")
def me(request: Request):
    return {"email": request.state.user, "login": auth_mod.auth.enabled}


def _cache_key(user):
    return f"subjects:{user}" if auth_mod.auth.enabled else "subjects"  # 見られる Subject は人によって違いうる


def _fresh_subjects(user=""):
    """ドライブから一覧を作り直し、写し（Firestore）に置く。"""
    out = [bundle.subject_summary(_st(), s) for s in _st().subjects()]
    ws = getattr(app.state, "work_state", None)
    if ws is not None:
        try:
            ws.put("_all", _cache_key(user), {"list": out})
        except Exception:  # noqa: BLE001  写しを置けなくても一覧は返す
            pass
    return out


@app.get("/api/subjects")
def subjects(request: Request, fresh: int = 0):
    """Subject の一覧（REQUIREMENTS D-1）。ドライブ版は、まず写し（Firestore）をすぐ返し、
    画面が fresh=1 でもう一度呼んで、ドライブと照らし合わせた結果に描き直す。写しが無ければドライブから作る。"""
    ws = getattr(app.state, "work_state", None)
    if ws is not None and not fresh:
        try:
            got = ws.get("_all", _cache_key(request.state.user))
        except Exception:  # noqa: BLE001
            got = None
        if got:
            return got["list"]
    return _fresh_subjects(request.state.user)


# ---- Subject を作る・Facet の構成を直す（REQUIREMENTS D-5） ----
def _refused(fn):
    from .lattice.discussion import Refused
    try:
        return fn()
    except Refused as e:
        raise HTTPException(409, str(e))


@app.get("/api/templates")
def get_templates():
    from .lattice import subjects as sj
    return sj.templates(_st())


@app.post("/api/subjects")
async def create_subject(request: Request):
    from fastapi.concurrency import run_in_threadpool
    st = _st()  # 別スレッドでも、この人の名義の保存先を使う
    return await run_in_threadpool(_create_subject_timed, request, await request.json(), st)


def _create_subject_timed(request, b, st):
    """Subject を作る。どこに時間がかかるかを段ごとにログに出す（M4: 作るのに25〜85秒かかっていた）。"""
    from . import timing
    _ST.set(st)
    tr = timing.start(f"create {b.get('name')}")
    steps, t = [], time.time()

    def lap(label):
        nonlocal t
        steps.append(f"{label}={time.time() - t:.1f}s")
        t = time.time()
    try:
        return _create_subject(request, b, lap)
    finally:
        print(f"[timing] create {b.get('name')} 段: {' '.join(steps)}", flush=True)
        tr.report()


def _create_subject(request, b, lap):
    from .lattice import subjects as sj
    if b.get("from"):  # 次の年を始める（E-1）
        from .lattice.discussion import Discussions
        ds = Discussions(_st(), b["from"], request.state.user, state=getattr(app.state, "work_state", None))
        name = _refused(lambda: sj.next_year(ds, b.get("name"), b.get("title"), b.get("description"),
                                             b.get("owner"), request.state.user, b.get("groups")))
        lap("複製")
    else:
        name = _refused(lambda: sj.create(_st(), b.get("name"), b.get("title"), b.get("description"),
                                          b.get("owner") or request.state.user, b.get("groups"), by=request.state.user))
        lap("作成")
        if b.get("migrate_from"):  # 組み直して移す（E-4）。作った Subject に、前の Subject の中身を裏で移す
            from .lattice import migrate
            from .lattice.discussion import Discussions
            ds = Discussions(_st(), name, request.state.user, state=getattr(app.state, "work_state", None))
            _refused(lambda: migrate.start(ds, b["migrate_from"], sj.clean_groups(b.get("groups"))))
            lap("移行の開始")
    # 一覧の写しには、ドライブを読み直さず、作った Subject の1行だけを足す（以前は全 Subject を読み直して約9秒）
    try:
        _add_to_subject_list(request.state.user, name, b, sj)
    except Exception:  # noqa: BLE001
        pass
    lap("一覧の写し")
    return {"ok": True, "subject": name}


def _add_to_subject_list(user, name, b, sj):
    ws = getattr(app.state, "work_state", None)
    if ws is None:
        return
    if b.get("from"):  # 複製は Discussion の数などが元と同じなので、裏で読み直す
        import threading
        st = _st()

        def go():
            _ST.set(st)
            _fresh_subjects(user)
        threading.Thread(target=go, daemon=True).start()
        return
    got = ws.get("_all", _cache_key(user)) or {"list": []}
    row = {"id": name, "title": str(b.get("title") or name).strip(), "owner": (b.get("owner") or user or None),
           "readonly": False, "facets": len(sj.facet_ids(sj.clean_groups(b.get("groups")))), "discussions": 0}
    rows = [x for x in got["list"] if x.get("id") != name] + [row]
    ws.put("_all", _cache_key(user), {"list": sorted(rows, key=lambda x: x["id"])})


@app.get("/api/s/{subject}/facets")
def get_facets(subject: str):
    from .lattice import subjects as sj
    return sj.current(_st(), subject)


@app.put("/api/s/{subject}/facets")
async def put_facets(subject: str, request: Request):
    from .lattice import subjects as sj
    from .lattice.discussion import Discussions
    b = await request.json()
    ds = Discussions(_st(), subject, request.state.user, state=getattr(app.state, "work_state", None))
    return {"ok": True, "groups": await run_in_threadpool(_refused, lambda: sj.update(ds, b.get("groups")))}


@app.put("/api/s/{subject}/owner")
async def put_owner(subject: str, request: Request):
    """担当を替える（B-3）。"""
    from .lattice import subjects as sj
    from .lattice.discussion import Discussions
    b = await request.json()
    ds = Discussions(_st(), subject, request.state.user, state=getattr(app.state, "work_state", None))

    def work():
        owner = _refused(lambda: sj.set_owner(ds, b.get("owner")))
        try:
            _fresh_subjects(request.state.user)  # 一覧の「担当」の印をすぐ合わせる
        except Exception:  # noqa: BLE001
            pass
        return owner
    owner = await run_in_threadpool(work)  # ドライブの呼び出しで、ほかの要求を止めないように
    return {"ok": True, "owner": owner}


@app.get("/api/s/{subject}/bundle")
def get_bundle(subject: str):
    return bundle.load(_st(), subject)


@app.get("/api/s/{subject}/asset/{path:path}")
def get_asset(subject: str, path: str):
    # .md は索引から読む。隠しフォルダ（索引・会話ログ・目録）の中身は出さない
    if path.endswith(".md") or any(p.startswith(".") for p in path.split("/")):
        raise HTTPException(404)
    data = _st().read_bytes(subject, path)
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    disp = "inline" if mime in INLINE else "attachment"
    return Response(data, media_type=mime, headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(pathlib.PurePath(path).name)}"})


# M2: 本物の Discussion（Gemini とファイル）。画面はウォークスルーと同じ API を使う
if os.environ.get("LATTICE_AGENT") == "gemini":
    from . import discuss
    app.include_router(discuss.router)
# M1.5 のウォークスルー（AI の役を Claude が演じる）。ローカルでだけ有効にする
elif os.environ.get("LATTICE_WIZARD") == "1":
    from . import wizard
    app.include_router(wizard.router)

app.mount("/", StaticFiles(directory=HERE / "static"), name="static")
