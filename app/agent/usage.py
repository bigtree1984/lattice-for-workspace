"""Gemini の1日の利用上限（REQUIREMENTS H、2026-11-15 の判断 A）。

Gemini を呼ぶたびに使った量（入力・考える・出力のトークン数）を足し、1日（日本時間）の上限を超えたら断る。
上限は1人あたりと全体の2つ。数字は設定値で変えられる（0 なら上限なし）。
  LATTICE_DAILY_TOKENS_USER   既定 2,000,000
  LATTICE_DAILY_TOKENS_TOTAL  既定 10,000,000
置き場所: Firestore（ドライブ版）、なければプロセスの中（手元・テスト）。
"""
import contextvars
import datetime as dt
import os
import threading

JST = dt.timezone(dt.timedelta(hours=9))
USER = contextvars.ContextVar("lattice_usage_user", default="")  # いま Gemini を呼んでいる利用者（main のミドルウェアが入れる）


def limits():
    return (int(os.environ.get("LATTICE_DAILY_TOKENS_USER", "2000000")),
            int(os.environ.get("LATTICE_DAILY_TOKENS_TOTAL", "10000000")))


def today():
    return dt.datetime.now(JST).date().isoformat()


class MemoryCounter:
    def __init__(self):
        self.n, self.lock = {}, threading.Lock()

    def get(self, key):
        return self.n.get(key, 0)

    def add(self, key, v):
        with self.lock:
            self.n[key] = self.n.get(key, 0) + v


class FirestoreCounter:
    """文書 lattice_usage/<日付>~<利用者 or _all> の tokens を足していく（同時に足しても数え漏れない）。"""

    def __init__(self, db):
        self.db = db

    def _doc(self, key):
        return self.db.collection("lattice_usage").document(key.replace("/", "~"))

    def get(self, key):
        s = self._doc(key).get()
        return int(s.get("tokens") or 0) if s.exists else 0

    def add(self, key, v):
        from google.cloud import firestore
        self._doc(key).set({"tokens": firestore.Increment(v), "day": key.split("~")[0]}, merge=True)


COUNTER = MemoryCounter()


class OverLimit(Exception):
    pass


def check(user=None):
    """上限を超えていれば OverLimit。呼ぶ前に確かめる。"""
    user = user if user is not None else USER.get()
    per_user, total = limits()
    d = today()
    if total and COUNTER.get(f"{d}~_all") >= total:
        raise OverLimit("今日の Lattice 全体の AI の利用上限に達しました。明日（日本時間の0時）からまた使えます。急ぎの場合は管理者に連絡してください。")
    if per_user and user and COUNTER.get(f"{d}~{user}") >= per_user:
        raise OverLimit("今日のあなたの AI の利用上限に達しました。明日（日本時間の0時）からまた使えます。")


def add(response, user=None):
    """Gemini の応答の使用量を足す。"""
    u = getattr(response, "usage_metadata", None)
    n = sum(int(getattr(u, k, None) or 0) for k in ("prompt_token_count", "thoughts_token_count", "candidates_token_count",
                                                     "tool_use_prompt_token_count"))
    if not n:
        return 0
    user = user if user is not None else USER.get()
    d = today()
    COUNTER.add(f"{d}~_all", n)
    if user:
        COUNTER.add(f"{d}~{user}", n)
    return n
