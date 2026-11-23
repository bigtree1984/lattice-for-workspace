"""Gemini の1日の利用上限（REQUIREMENTS H、2026-11-15）。"""
import types as T

import pytest

from app.agent import runner, usage
from app.lattice.discussion import Refused


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(usage, "COUNTER", usage.MemoryCounter())
    monkeypatch.setenv("LATTICE_DAILY_TOKENS_USER", "1000")
    monkeypatch.setenv("LATTICE_DAILY_TOKENS_TOTAL", "1500")


def resp(n):
    return T.SimpleNamespace(usage_metadata=T.SimpleNamespace(prompt_token_count=n, thoughts_token_count=0, candidates_token_count=0))


def test_counts_per_user_and_total_and_refuses(monkeypatch):
    calls = []
    fake = T.SimpleNamespace(models=T.SimpleNamespace(generate_content=lambda **k: calls.append(1) or resp(600)))
    monkeypatch.setattr(runner, "client", lambda: fake)
    usage.USER.set("a@x")
    runner.generate("m", [], None)
    runner.generate("m", [], None)  # a は 1200（上限 1000 を超えた）
    with pytest.raises(Refused, match="あなたの AI の利用上限"):
        runner.generate("m", [], None)
    assert len(calls) == 2  # 断るときは Gemini を呼ばない
    usage.USER.set("b@x")
    runner.generate("m", [], None)  # 全体 1800（上限 1500 を超えた）
    with pytest.raises(Refused, match="全体"):
        runner.generate("m", [], None)


def test_zero_means_no_limit(monkeypatch):
    monkeypatch.setenv("LATTICE_DAILY_TOKENS_USER", "0")
    monkeypatch.setenv("LATTICE_DAILY_TOKENS_TOTAL", "0")
    usage.add(resp(10 ** 9), user="a@x")
    usage.check("a@x")


def test_background_keeps_user_and_shows_limit_message():
    import threading
    from app import discuss
    seen, done = [], threading.Event()
    usage.USER.set("c@x")
    discuss.background(None, None, lambda ds: (seen.append(usage.USER.get()), done.set()))
    done.wait(5)
    assert seen == ["c@x"]  # 別スレッドでも利用者が分かる（上限の数え先・ドライブの資格情報）
