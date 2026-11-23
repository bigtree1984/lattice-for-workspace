"""ログイン（REQUIREMENTS C-4、M4）。Google との往復は差し替えて、アプリ側の決まりを確かめる。"""
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import auth as A
from app import main


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app.lattice.storage import LocalStorage
    monkeypatch.setenv("LATTICE_OAUTH_SECRET", json.dumps({"web": {"client_id": "cid", "client_secret": "sec"}}))
    monkeypatch.setenv("LATTICE_SESSION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(A, "DOMAIN", "example.com")
    monkeypatch.setattr(A, "auth", A.Auth())
    main.app.state.storage = LocalStorage(tmp_path)
    return TestClient(main.app)


def fake_google(monkeypatch, email="taro@example.com", hd="example.com", scope=A.SCOPES):
    class R:
        status_code = 200

        def json(self):
            granted = "openid https://www.googleapis.com/auth/drive" if "drive" in scope else "openid"
            return {"access_token": "tok", "expires_in": 3599, "id_token": "x", "scope": granted}
    import requests
    from google.oauth2 import id_token
    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda *a, **k: {"email": email, "email_verified": True, "hd": hd, "name": "D"})


def login(client, monkeypatch, **kw):
    fake_google(monkeypatch, **kw)
    r = client.get("/auth/login?next=/index.html%3Fs%3Dx", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["hd"] == ["example.com"] and "drive" in q["scope"][0] and "access_type" not in q  # リフレッシュトークンは求めない
    return client.get(f"/auth/callback?state={q['state'][0]}&code=c", follow_redirects=False)


def test_pages_and_api_need_login(client):
    assert client.get("/api/subjects").status_code == 401
    r = client.get("/subjects.html", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/auth/login?next=%2Fsubjects.html")


def test_login_sets_session_and_returns_to_page(client, monkeypatch):
    r = login(client, monkeypatch)
    assert r.status_code == 303 and r.headers["location"] == "/index.html?s=x"
    assert client.get("/api/me").json()["email"] == "taro@example.com"
    raw = client.cookies.get(A.COOKIE)
    assert "tok" not in raw  # cookie の中の鍵は暗号化されている


def test_other_domain_and_missing_drive_scope_are_refused(client, monkeypatch):
    assert login(client, monkeypatch, email="x@gmail.com", hd=None).status_code == 403
    assert login(client, monkeypatch, scope="openid").status_code == 403
    assert client.get("/api/me").status_code == 401


def test_bad_state_is_refused(client, monkeypatch):
    fake_google(monkeypatch)
    client.get("/auth/login", follow_redirects=False)
    assert client.get("/auth/callback?state=wrong&code=c").status_code == 400


def test_session_close_to_expiry_asks_to_login_again(client, monkeypatch):
    login(client, monkeypatch)
    assert client.get("/api/me").status_code == 200
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3599 - A.REFRESH_BEFORE + 5)
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json()["login"] == "/auth/login"


def test_open_redirect_is_blocked(client, monkeypatch):
    fake_google(monkeypatch)
    r = client.get("/auth/login?next=//evil.example.com", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    r = client.get(f"/auth/callback?state={q['state'][0]}&code=c", follow_redirects=False)
    assert r.headers["location"] == "/"
