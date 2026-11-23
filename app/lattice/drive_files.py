"""ドライブのファイルを、利用者本人の権限で取り込む（REQUIREMENTS C-2・C-4・C-14）。

- Google スプレッドシートは xlsx、ドキュメント・スライドは pdf に書き出す（C-2）。それ以外のファイルはそのまま。
- pptx・docx を AI に読ませるための pdf への変換も、ドライブの機能で行う（一時的な Google 形式のファイルを作って消す）。
資格情報は、手元では Lattice 専用の ADC（~/.config/gcloud-lattice）。Cloud Run では利用者の OAuth（M4）に差し替える。
"""
import io
import os
import pathlib
import re

from .discussion import Refused
from .sources import MAX_BYTES, MIME

SCOPES = ["https://www.googleapis.com/auth/drive"]
EXPORT = {"application/vnd.google-apps.spreadsheet": ("xlsx", MIME["xlsx"]),
          "application/vnd.google-apps.document": ("pdf", MIME["pdf"]),
          "application/vnd.google-apps.presentation": ("pdf", MIME["pdf"])}
ALL = {"supportsAllDrives": True}

_svc = None


def use_local_adc():
    """手元では Lattice 専用の ADC（~/.config/gcloud-lattice）を使う。Cloud Run ではファイルが無いので何もしない。"""
    cred = pathlib.Path.home() / ".config/gcloud-lattice/application_default_credentials.json"
    if cred.is_file() and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred)


def service():
    """ログインしている利用者がいれば、その人の権限（app/auth.py の CREDS）。いなければ手元の ADC。"""
    global _svc
    from ..auth import CREDS
    if CREDS.get() is not None:
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=CREDS.get(), cache_discovery=False)
    if _svc is None:
        import google.auth
        from googleapiclient.discovery import build
        use_local_adc()
        # 手元の利用者の資格情報は、費用を付けるプロジェクトを指定する。Cloud Run のサービスアカウントは自分のプロジェクトで足りる
        quota = os.environ.get("LATTICE_PROJECT") if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ else None
        c, _ = google.auth.default(scopes=SCOPES, quota_project_id=quota)
        _svc = build("drive", "v3", credentials=c, cache_discovery=False)
    return _svc


def file_id(url):
    """ドライブ・ドキュメント・スプレッドシート・スライドの URL からファイル ID を取り出す。"""
    m = re.match(r"https://(docs|drive)\.google\.com/.*?(?:/d/|[?&]id=)([A-Za-z0-9_-]{20,})", str(url or "").strip())
    if not m:
        raise Refused("ドライブのファイルの URL ではありません（https://docs.google.com/… か https://drive.google.com/… を渡してください）")
    return m.group(2)


def fetch(url):
    """URL のファイルを取り込む。返り値は（ファイル名, 中身, 元の URL）。"""
    from googleapiclient.errors import HttpError
    svc, fid = service(), file_id(url)
    try:
        meta = svc.files().get(fileId=fid, fields="name,mimeType,size,webViewLink", **ALL).execute()
        if meta["mimeType"] in EXPORT:
            ext, mime = EXPORT[meta["mimeType"]]
            data = svc.files().export(fileId=fid, mimeType=mime).execute()
            name = f"{meta['name']}.{ext}"
        elif meta["mimeType"].startswith("application/vnd.google-apps."):
            raise Refused("この種類の Google ファイルは取り込めません（スプレッドシート・ドキュメント・スライドだけ）")
        else:
            if int(meta.get("size") or 0) > MAX_BYTES:
                raise Refused("1ファイル 30MB までです")
            data = svc.files().get_media(fileId=fid, **ALL).execute()
            name = meta["name"]
    except HttpError as e:
        if e.resp.status in (403, 404):
            raise Refused("そのファイルを開けません（見る権限が無いか、ファイルがありません）")
        raise
    return name, data, meta.get("webViewLink") or url


def to_pdf(name, data, ext):
    """pptx・docx を pdf にする（Gemini が読めるように）。一時ファイルはすぐ消す。"""
    from googleapiclient.http import MediaIoBaseUpload
    gtype = {"pptx": "application/vnd.google-apps.presentation", "docx": "application/vnd.google-apps.document"}[ext]
    svc = service()
    f = svc.files().create(body={"name": f"lattice-tmp-{name}", "mimeType": gtype},
                           media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=MIME[ext]), fields="id").execute()
    try:
        return svc.files().export(fileId=f["id"], mimeType=MIME["pdf"]).execute()
    finally:
        svc.files().delete(fileId=f["id"]).execute()
