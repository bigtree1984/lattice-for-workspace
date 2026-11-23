"""会話の中で渡す資料と原典カード（REQUIREMENTS C-14）。ドライブと Gemini は呼ばない。"""
import io
import pathlib
import shutil

import openpyxl
import pytest

from app.agent import runner
from app.lattice import sources
from app.lattice.discussion import Discussions, Refused, split
from app.lattice.storage import LocalStorage

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = "edible_coffee_2026"
SUMMARY = "## 話したこと\n- 原価\n\n## 決定\n- なし\n\n## 持ち越し\n- なし\n"


@pytest.fixture
def ds(store):
    d = Discussions(store, S, "taro@example.com", "taro", model="test")
    return d


def opened(ds):
    did = ds.create("原価の資料を見て")
    ds.update_work(did, lambda w: w.update(waiting=None))
    return did


def xlsx():
    wb = openpyxl.Workbook()
    wb.active.append(["品目", "1個あたり"])
    wb.active.append(["豆", 12])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_upload_makes_file_and_card(ds):
    did = opened(ds)
    card = sources.add(ds, did, "Cost Estimate.xlsx", xlsx())
    d = ds.work(did)["dir"]
    assert card == d + "source-cost-estimate.md"
    assert ds.st.exists(S, d + "source-cost-estimate.xlsx")
    fm, body = split(ds.st.read_text(S, card))
    assert fm["layer"] == "source" and fm["resource"].startswith("アップロード（taro、")
    assert fm["snapshot"] == "source-cost-estimate.xlsx" and "human:taro" == fm["retrieved"]["by"]
    assert ds.work(did)["messages"][-1]["role"] == "file"
    assert "資料を渡した" in ds.st.read_text(S, "/log.md")
    # 同じ名前は番号を付けて分ける
    assert sources.add(ds, did, "cost estimate.xlsx", xlsx()) == d + "source-cost-estimate-2.md"


def test_drive_card_keeps_url_and_japanese_name(ds):
    did = opened(ds)
    card = sources.add(ds, did, "原価見積もり.pdf", b"%PDF-1.4", url="https://docs.google.com/x", by_drive=True)
    fm, body = split(ds.st.read_text(S, card))
    assert card.endswith("/source-原価見積もり.md") and fm["title"] == "原価見積もり.pdf"
    assert fm["resource"] == "https://docs.google.com/x" and "ドライブから" in body


def test_refuses_kind_size_and_state(ds):
    did = opened(ds)
    with pytest.raises(Refused, match="形式"):
        sources.add(ds, did, "movie.mp4", b"x")
    with pytest.raises(Refused, match="30MB"):
        sources.check("big.pdf", sources.MAX_BYTES + 1)
    ds.write_summary(did, "t", "d", SUMMARY)
    ds.set_candidates(did, [{"facet": "price", "reason": "r", "suggest": "update"}])
    ds.confirm(did)
    with pytest.raises(Refused, match="会話中"):
        sources.add(ds, did, "a.txt", b"x")


def test_remove_by_person_and_discard_removes_all(ds):
    did = opened(ds)
    card = sources.add(ds, did, "memo.txt", "メモ".encode())
    sources.remove(ds, did, card)
    assert not ds.st.exists(S, card) and ds.work(did)["sources"] == []
    card = sources.add(ds, did, "memo.md", b"# m")
    assert ds.st.exists(S, card.replace(".md", ".txt"))  # md の実物は txt として置く
    ds.discard(did)
    assert not any("-d008/" in e.path for e in ds.st.walk(S))


def test_card_becomes_summary_source_when_read(ds):
    did = opened(ds)
    card = sources.add(ds, did, "memo.txt", "1個36円".encode())
    run = runner.Run(ds, did, "reply", "test")
    out = run.call("read_source", {"card": card})
    assert out["content"] == "1個36円" and ds.work(did)["sources"][0]["seen"]
    ds.write_summary(did, "t", "d", SUMMARY)
    assert card in [s["resource"] for s in ds.summary(did)[0]["sources"]]


def test_read_source_table_and_pdf(ds):
    did = opened(ds)
    c1 = sources.add(ds, did, "cost.xlsx", xlsx())
    c2 = sources.add(ds, did, "deck.pdf", b"%PDF-1.4 x")
    run = runner.Run(ds, did, "reply", "test")
    assert "| 豆 | 12 |" in run.call("read_source", {"card": c1})["content"]
    run.call("read_source", {"card": c2})
    assert run.attach and run.attach[0].inline_data.mime_type == "application/pdf"
    with pytest.raises(Refused):
        run.call("read_source", {"card": "/facets/price/price.md"})


def test_new_sources_told_once(ds):
    did = opened(ds)
    card = sources.add(ds, did, "memo.txt", b"x")
    first = runner._new_sources(ds, did)
    assert card in first and runner._new_sources(ds, did) == ""


def test_light_history_drops_bytes():
    from google.genai import types
    blob = types.FunctionResponsePart(inline_data=types.FunctionResponseBlob(mime_type="application/pdf", data=b"%PDF"))
    c = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(name="read_source", response={"card": "x"}, parts=[blob]))])
    fr = runner._light(c)["parts"][0]["function_response"]
    assert "parts" not in fr and "read_source" in fr["response"]["content"]


def test_drive_url_parsing():
    from app.lattice import drive_files
    assert drive_files.file_id("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=0") == "1AbCdEfGhIjKlMnOpQrStUvWxYz"
    assert drive_files.file_id("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz") == "1AbCdEfGhIjKlMnOpQrStUvWxYz"
    with pytest.raises(Refused):
        drive_files.file_id("https://example.com/d/1AbCdEfGhIjKlMnOpQrStUvWxYz")


def test_web_search_is_kept_only_if_chosen_at_summary(ds, monkeypatch):
    """検索は会話中の記録にだけ残し、要約のときに選んだものだけを「Facet 更新を確認する」で原典カードにする（C-14）。"""
    did = opened(ds)
    answers = iter([("A社 980円", [{"title": "a.jp", "url": "https://a.jp/p"}]), ("B社 不明", [])])
    monkeypatch.setattr(runner, "web_search", lambda q, m=None: next(answers))
    run = runner.Run(ds, did, "reply", "test")
    a = run.call("web_search", {"question": "競合Aの価格"})["search"]
    b = run.call("web_search", {"question": "競合Bの価格"})["search"]
    assert not any("/source-web-" in e.path for e in ds.st.walk(S))  # まだドライブに書かない
    run.call("write_summary", {"title": "t", "description": "d", "body": SUMMARY, "searches": [a]})  # AI の案: A だけ
    assert [x["keep"] for x in ds.work(did)["searches"]] == [True, False]
    sources.keep_searches(ds, did, one=b, keep=True)   # 人が B も足し、
    sources.keep_searches(ds, did, one=a, keep=False)  # A は外す
    ds.set_candidates(did, [{"facet": "price", "reason": "r", "suggest": "material"}])
    ds.update_work(did, lambda w: [c.update(choice="material") for c in w["candidates"]])
    ds.confirm(did)
    cards = [e.path for e in ds.st.walk(S) if "/source-web-" in e.path]
    assert len(cards) == 1 and "B社 不明" in ds.st.read_text(S, cards[0])
    fm, _ = ds.summary(did)
    assert cards[0] in [x["resource"] for x in fm["sources"]]  # 要約の出どころにも入る


def test_resolve_only_asks_google_redirect():
    assert runner._resolve("https://example.com/x") == "https://example.com/x"


def test_check_url_blocks_inside_addresses():
    from app.lattice import web_pages
    for u in ("http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/",
              "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "https://example.com:8443/", "https://u:p@example.com/"):
        with pytest.raises(Refused):
            web_pages.check_url(u)


def test_page_text_drops_scripts():
    from app.lattice import web_pages
    html = "<html><head><title>価格 &amp; 商品</title><script>var x=1</script></head><body><h1>見出し</h1><p>本文 980円</p><ul><li>a</li></ul></body></html>"
    title, text = web_pages.page_text(html.encode())
    assert title == "価格 & 商品" and "# 見出し" in text and "980円" in text and "var x" not in text and "- a" in text


def test_read_url_page_and_youtube(ds, monkeypatch):
    from app.lattice import web_pages
    did = opened(ds)
    page = ("<title>競合E</title><p>12個入り 1,200円。" + "あ" * 60 + "</p>").encode()
    monkeypatch.setattr(web_pages, "fetch", lambda u: (u, "text/html", page, "utf-8"))
    run = runner.Run(ds, did, "reply", "test")
    out = run.call("read_url", {"url": "https://example.com/e"})
    fm, body = split(ds.st.read_text(S, out["card"]))
    assert fm["resource"] == "https://example.com/e" and "1,200円" in body and out["card"] in ds.work(did)["read"]
    monkeypatch.setattr(runner, "watch_youtube", lambda u, f="", m=None: "- 00:10 価格は 500円")
    out = run.call("read_url", {"url": "https://youtu.be/abc"})
    assert "500円" in out["content"] and "YouTube" in split(ds.st.read_text(S, out["card"]))[1]
    with pytest.raises(Refused):
        run.call("read_url", {"url": "javascript://youtube.com/%0aalert(1)"})
    with pytest.raises(Refused):
        run.call("read_url", {"url": "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit"})


def test_run_python_sends_files_once_and_cleans_up(ds, monkeypatch):
    from app.agent import sandbox
    calls = {"create": 0, "files": [], "deleted": []}
    monkeypatch.setattr(sandbox, "create", lambda label: calls.__setitem__("create", calls["create"] + 1) or "SB1")
    monkeypatch.setattr(sandbox, "execute", lambda sb, code, files=(): calls["files"].append([f[0] for f in files]) or ("36\n", []))
    monkeypatch.setattr(sandbox, "delete", lambda sb: calls["deleted"].append(sb))
    did = opened(ds)
    card = sources.add(ds, did, "cost.xlsx", xlsx())
    run = runner.Run(ds, did, "reply", "test")
    out = run.call("run_python", {"code": "print(36)", "cards": [card]})
    run.call("run_python", {"code": "print(1)", "cards": [card]})
    assert out["output"] == "36\n" and calls["create"] == 1 and calls["files"] == [["source-cost.xlsx"], []]
    with pytest.raises(Refused):
        run.call("run_python", {"code": "x", "cards": ["/facets/price/price.md"]})
    ds.discard(did)
    assert calls["deleted"] == ["SB1"]  # 廃棄で部屋も消す（C-1）


def test_fetch_connects_only_to_checked_address(monkeypatch):
    """DNS の再束縛: 確かめた後に名前の行き先が内側に変わっても、確かめた IP にしかつながない。"""
    import ipaddress
    import socket
    from app.lattice import web_pages
    answers = iter([[(0, 0, 0, "", ("93.184.215.14", 443))], [(0, 0, 0, "", ("169.254.169.254", 443))]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: next(answers))
    seen = {}

    class R:
        status_code, headers, encoding = 200, {"content-type": "text/html"}, "utf-8"

        def iter_content(self, n):
            yield b"<p>ok</p>"

    def fake_get(self, url, headers=None, **k):
        seen.update(url=url, host=headers["Host"])
        return R()
    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)
    web_pages.fetch("https://example.com/a")
    assert seen == {"url": "https://93.184.215.14/a", "host": "example.com"}
    assert ipaddress.ip_address("169.254.169.254").is_global is False
