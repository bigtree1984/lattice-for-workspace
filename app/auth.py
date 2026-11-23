"""ログイン（REQUIREMENTS C-4、M4）。会社の Google アカウントでログインし、ドライブは本人の権限で読み書きする。

- Google から受け取るのは1時間で切れるアクセストークンだけ（リフレッシュトークンは受け取らない）。
- トークンはサーバーに保存せず、暗号化した cookie に入れる。残り REFRESH_BEFORE 秒を切ったら、ログインし直してもらう。
- 有効にする: LATTICE_OAUTH_SECRET（Secret Manager の OAuth クライアントの JSON。名前か、projects/.../secrets/... の形）。
  無ければ従来どおり LATTICE_DEV_USER の1人で動く（手元の開発・テスト用）。
- LATTICE_SESSION_KEY（Fernet の鍵、または Secret Manager の名前）。無ければ起動ごとに作る（再起動でログインし直し）。
- LATTICE_ALLOWED_DOMAIN（必須。空ならだれもログインできない）。
"""
import base64
import contextvars
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

COOKIE, FLOW = "lattice_session", "lattice_login"
SCOPES = "openid email profile https://www.googleapis.com/auth/drive"
REFRESH_BEFORE = 15 * 60  # Gemini の応答が数分かかっても途中で切れないよう、残り15分でログインし直す
PROJECT = os.environ.get("LATTICE_PROJECT")
DOMAIN = os.environ.get("LATTICE_ALLOWED_DOMAIN", "")  # 空ならだれもログインできない（入れ忘れても開かない側に倒す）
OPEN = ("/auth/", "/favicon.ico", "/favicon.svg", "/tokens.css", "/healthz")  # ログインなしで通す

router = APIRouter()
# いま処理している利用者のドライブの資格情報（drive_files がこれを使う。スレッドプールにも引き継がれる）
CREDS = contextvars.ContextVar("lattice_creds", default=None)


def _secret(value):
    """値そのもの、または Secret Manager の名前（アプリの資格情報で読む）。"""
    if not value or value.lstrip().startswith("{") or value.endswith("="):  # JSON か Fernet の鍵そのもの
        return value
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from .lattice.drive_files import use_local_adc
    use_local_adc()
    name = value if value.startswith("projects/") else f"projects/{PROJECT}/secrets/{value}"
    if "/versions/" not in name:
        name += "/versions/latest"
    c, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"],
                               quota_project_id=PROJECT if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ else None)
    r = AuthorizedSession(c).get(f"https://secretmanager.googleapis.com/v1/{name}:access", timeout=30)
    r.raise_for_status()
    return base64.b64decode(r.json()["payload"]["data"]).decode()


class Auth:
    def __init__(self):
        self.enabled = bool(os.environ.get("LATTICE_OAUTH_SECRET"))
        self.dev_user = os.environ.get("LATTICE_DEV_USER", "")
        if not self.enabled:
            return
        c = json.loads(_secret(os.environ["LATTICE_OAUTH_SECRET"]))
        c = c.get("web") or c.get("installed") or c
        self.client_id, self.client_secret = c["client_id"], c["client_secret"]
        key = _secret(os.environ.get("LATTICE_SESSION_KEY", "")) or Fernet.generate_key().decode()
        self.f = Fernet(key.strip().encode())

    # ---- cookie
    def _read(self, request, name):
        raw = request.cookies.get(name)
        if not raw:
            return None
        try:
            return json.loads(self.f.decrypt(raw.encode(), ttl=2 * 3600))
        except (InvalidToken, ValueError):
            return None

    def _set(self, resp, request, name, data, max_age):
        resp.set_cookie(name, self.f.encrypt(json.dumps(data).encode()).decode(), max_age=max_age, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")

    def session(self, request):
        s = self._read(request, COOKIE)
        if not s or s.get("exp", 0) - time.time() < REFRESH_BEFORE:
            return None
        return s

    # ---- Google との往復
    def _redirect_uri(self, request):
        base = os.environ.get("LATTICE_BASE_URL") or str(request.base_url).rstrip("/")
        return base + "/auth/callback"

    def login(self, request):
        nxt = request.query_params.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        old = self._read(request, COOKIE) or {}
        q = {"client_id": self.client_id, "redirect_uri": self._redirect_uri(request), "response_type": "code",
             "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             "hd": DOMAIN, "include_granted_scopes": "true"}
        if old.get("email"):
            q["login_hint"] = old["email"]  # 2回目からはアカウントの選択を飛ばす
        resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(q))
        self._set(resp, request, FLOW, {"state": state, "verifier": verifier, "next": nxt}, 600)
        return resp

    def callback(self, request):
        import requests
        from google.auth.transport.requests import Request as GReq
        from google.oauth2 import id_token
        flow = self._read(request, FLOW)
        q = request.query_params
        if not flow or q.get("state") != flow["state"]:
            return JSONResponse({"error": "ログインの手順が古くなりました。もう一度開き直してください。"}, 400)
        if q.get("error") or not q.get("code"):
            return JSONResponse({"error": f"ログインできませんでした（{q.get('error', 'no code')}）"}, 403)
        r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
            "code": q["code"], "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self._redirect_uri(request), "grant_type": "authorization_code", "code_verifier": flow["verifier"]})
        if r.status_code != 200:
            return JSONResponse({"error": "ログインできませんでした（トークンの受け取りに失敗）"}, 403)
        tok = r.json()
        info = id_token.verify_oauth2_token(tok["id_token"], GReq(), self.client_id)
        email = info.get("email", "")
        if not info.get("email_verified") or info.get("hd") != DOMAIN or not email.endswith("@" + DOMAIN):
            return JSONResponse({"error": f"{DOMAIN} のアカウントでログインしてください"}, 403)
        if "https://www.googleapis.com/auth/drive" not in tok.get("scope", "").split():
            return JSONResponse({"error": "ドライブの権限が許可されていません。ログインし直して、ドライブへのアクセスを許可してください。"}, 403)
        resp = RedirectResponse(flow["next"], status_code=303)
        self._set(resp, request, COOKIE, {"email": email, "name": info.get("name", ""), "token": tok["access_token"],
                                          "exp": int(time.time()) + int(tok.get("expires_in", 3600))}, 2 * 3600)
        resp.delete_cookie(FLOW, path="/")
        return resp


auth = Auth()


def credentials(sess):
    from google.oauth2.credentials import Credentials
    import datetime as dt
    return Credentials(token=sess["token"], expiry=dt.datetime.utcfromtimestamp(sess["exp"]))


async def middleware(request: Request, call_next):
    """ログインしていない・鍵が切れかけている → ページはログインへ回し、API は 401（画面がログインへ回す）。"""
    if not auth.enabled:
        request.state.user = getattr(request.app.state, "user", None) or auth.dev_user  # テストは app.state.user で差し替える
        request.state.creds = None
        return await call_next(request)
    path = request.url.path
    if path.startswith(OPEN):
        request.state.user, request.state.creds = "", None
        return await call_next(request)
    s = auth.session(request)
    if not s:
        if path.startswith("/api/"):
            return JSONResponse({"error": "login", "login": "/auth/login"}, 401)
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/auth/login?" + urlencode({"next": nxt}))
    request.state.user = s["email"]
    request.state.creds = credentials(s)
    tok = CREDS.set(request.state.creds)
    try:
        return await call_next(request)
    finally:
        CREDS.reset(tok)


@router.get("/auth/login")
def login(request: Request):
    if not auth.enabled:
        return RedirectResponse("/")
    return auth.login(request)


@router.get("/auth/callback")
def callback(request: Request):
    return auth.callback(request)


@router.get("/auth/logout")
def logout():
    resp = RedirectResponse("/auth/bye")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/auth/bye")
def bye():
    from fastapi.responses import HTMLResponse
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Lattice</title>'
                        '<p style="font-family:sans-serif;margin:3em">ログアウトしました。<a href="/auth/login">もう一度ログインする</a></p>')
