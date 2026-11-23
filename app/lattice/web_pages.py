"""チャットに貼られた Web ページの URL を、アプリのサーバーが取りに行く（REQUIREMENTS C-14）。

サーバーから外へ取りに行くので、社内のアドレスに向けられないよう止める（SSRF 対策、SECURITY A01）:
- http・https だけ。ポートは 80・443 だけ
- 名前を引いた先が、プライベート・ループバック・リンクローカル・予約済みのアドレスなら断る
- 転送（リダイレクト）は自分でたどり、行き先ごとに同じ確かめをする（3回まで）
- 大きさは 5MB まで、待ち時間は 10 秒まで。Cookie や認証の情報は付けない
"""
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .discussion import Refused

MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 60_000
UA = "Lattice/0.1 (page snapshot for a discussion)"


def check_url(url):
    u = urlsplit(str(url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Refused("http・https の Web ページの URL を渡してください")
    if u.port not in (None, 80, 443) or u.username or u.password:
        raise Refused("この URL には取りに行けません")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Refused("そのページの場所が見つかりません")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise Refused("社内や手元のアドレスのページには取りに行けません")
        ips.append(ip)
    return u.geturl(), ips[0]


def _get(url, ip):
    """確かめた IP アドレスにだけつなぐ（DNS の再束縛: 確かめた後に名前の行き先を内側へ変えられるのを防ぐ）。
    https の証明書は元の名前で確かめる。"""
    import requests
    from requests.adapters import HTTPAdapter
    u = urlsplit(url)
    host = u.hostname

    class Pinned(HTTPAdapter):
        def init_poolmanager(self, *a, **k):
            k["server_hostname"] = host
            k["assert_hostname"] = host
            super().init_poolmanager(*a, **k)

    addr = f"[{ip}]" if ip.version == 6 else str(ip)
    target = u._replace(netloc=addr + (f":{u.port}" if u.port else "")).geturl()
    s = requests.Session()
    s.mount("https://", Pinned())
    hdr = {"User-Agent": UA, "Host": host + (f":{u.port}" if u.port else "")}
    return s.get(target, headers=hdr, timeout=10, allow_redirects=False, stream=True)


def fetch(url):
    """ページを取る。返り値は（最後の URL, content-type, 中身）。"""
    import requests
    for _ in range(4):
        url, ip = check_url(url)
        r = _get(url, ip)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = urljoin(url, r.headers["location"])
            r.close()
            continue
        if r.status_code != 200:
            raise Refused(f"ページを取れませんでした（{r.status_code}）。ログインが要るページは読めません")
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > MAX_BYTES:
                raise Refused("ページが大きすぎます（5MB まで）")
        return url, r.headers.get("content-type", "").split(";")[0].strip().lower(), data, r.encoding
    raise Refused("転送が多すぎます")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol", "header", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self.in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def page_text(data, encoding=None):
    """HTML から題名と本文の文字だけを取り出す（スクリプトや装飾は捨てる）。"""
    raw = data.decode(encoding or "utf-8", "replace")
    m = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', raw[:4000], re.I)
    if m and (encoding or "utf-8").lower() != m.group(1).lower():
        try:
            raw = data.decode(m.group(1), "replace")
        except LookupError:
            pass
    p = _Text()
    p.feed(raw)
    text = re.sub(r"[ \t　]+", " ", "".join(p.out))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return html.unescape(p.title).strip(), text[:MAX_TEXT]


def is_youtube(url):
    h = (urlsplit(str(url)).hostname or "").lower()
    return h in ("youtu.be", "www.youtube.com", "youtube.com", "m.youtube.com")
