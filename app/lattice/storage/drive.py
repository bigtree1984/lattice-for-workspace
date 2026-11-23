"""保存先の共有ドライブ版（REQUIREMENTS B-1・C-4・E-2、PLAN M3）。

- 共有ドライブ1つを表す。直下のフォルダのうち index.md のあるものが Subject。
- 読み書きは利用者本人の資格情報で行う（C-4）。版の履歴に本人が編集者として残る。
- パスはドライブに無いので、フォルダの名前をたどって ID を引き、覚えておく（他の人が動かして ID が変わったら引き直す）。
- 版はドライブの版の番号（headRevisionId）。消すのはゴミ箱へ。消したファイルの版に戻すときは、ゴミ箱から戻して上書きする。
- Google のファイル（スプレッドシートなど）は Lattice のファイルではないので、一覧に出さず読まない（資料は C-2 で書き出して置く）。
"""
import io
import mimetypes
import threading
import time
from datetime import datetime, timezone

from .base import Entry, Storage, StorageError, check_rel_path, check_subject_name

FOLDER = "application/vnd.google-apps.folder"
ALL = {"supportsAllDrives": True}
RETRIES = 3  # 通信の一時的な切断（SSL・時間切れ）や 429・5xx は、待って3回までやり直す
LIST = {"supportsAllDrives": True, "includeItemsFromAllDrives": True, "corpora": "drive"}
mimetypes.add_type("text/markdown", ".md")


def _q(s):
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def _ts(rfc):
    return datetime.fromisoformat(rfc.replace("Z", "+00:00")).timestamp()


class DriveStorage(Storage):
    def __init__(self, drive_id, service=None, root=None):
        """root: Subject を並べるフォルダ。ふだんは共有ドライブの直下（None）。テストでは一時フォルダを使う。"""
        self.drive_id = drive_id
        self.root = root or drive_id
        self._svc = service
        self._owner = threading.get_ident()
        self._local = threading.local()  # 通信の部品はスレッドをまたいで使えないので、スレッドごとに作る
        self._ids = {}  # (subject, パス) → ファイル・フォルダの ID
        self._gone, self._mine = {}, {}  # 自分が最近消したファイル ID → 時刻、(Subject, パス) → (時刻, 情報)
        self._snaps, self._content = {}, {}  # Subject → (取った時刻, パス → 情報)、ファイル ID → (更新時刻, 中身)
        self._lock = threading.RLock()
        self._caps = {}
        self._plocks = {}  # (Subject, パス) → そのファイルを書いている間の鍵  # Subject → (取った時刻, 書き込めるか)

    # ---- ドライブの呼び出し
    @property
    def svc(self):
        if self._svc is None:
            from ..drive_files import service
            self._svc = service()
        if threading.get_ident() == self._owner:
            return self._svc
        if getattr(self._local, "svc", None) is None:
            import google_auth_httplib2
            import httplib2
            from googleapiclient.discovery import build
            cred = self._svc._http.credentials
            self._local.svc = build("drive", "v3", http=google_auth_httplib2.AuthorizedHttp(cred, http=httplib2.Http()),
                                    cache_discovery=False)
        return self._local.svc

    def _list(self, q, fields="id,name,mimeType,parents,modifiedTime,size,trashed"):
        out, token = [], None
        while True:
            r = self.svc.files().list(driveId=self.drive_id, q=q, fields=f"nextPageToken,files({fields})",
                                      pageSize=1000, pageToken=token, **LIST).execute(num_retries=RETRIES)
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    def _child(self, parent, name, folder=None, trashed=False):
        q = f"'{_q(parent)}' in parents and name = '{_q(name)}' and trashed = {str(trashed).lower()}"
        if folder is True:
            q += f" and mimeType = '{FOLDER}'"
        elif folder is False:
            q += f" and mimeType != '{FOLDER}'"
        got = self._list(q, "id,name,mimeType,modifiedTime")
        got.sort(key=lambda f: f["modifiedTime"], reverse=True)  # 同じ名前が2つあれば新しいほう
        return got[0]["id"] if got else None

    # ---- パス → ID
    def _subject_id(self, subject):
        key = (check_subject_name(subject), "")
        if key in self._ids:
            return self._ids[key]
        fid = self._child(self.root, subject, folder=True)
        if not fid or not self._child(fid, "index.md", folder=False):
            raise StorageError(f"no subject: {subject!r}")
        self._ids[key] = fid
        return fid

    def _resolve(self, subject, path, make_parents=False):
        """ファイルの ID（無ければ None）と、親フォルダの ID・名前を返す。"""
        path = check_rel_path(path)
        parts = path.split("/")[1:]
        parent = self._subject_id(subject)
        for i, name in enumerate(parts[:-1]):
            key = (subject, "/" + "/".join(parts[:i + 1]) + "/")
            fid = self._ids.get(key) or self._child(parent, name, folder=True)
            if not fid:
                if not make_parents:
                    return None, None, parts[-1]
                fid = self.svc.files().create(body={"name": name, "mimeType": FOLDER, "parents": [parent]},
                                              fields="id", **ALL).execute(num_retries=RETRIES)["id"]
            self._ids[key] = parent = fid
        key = (subject, path)
        fid = self._ids.get(key) or self._child(parent, parts[-1], folder=False)
        if fid:
            self._ids[key] = fid
        return fid, parent, parts[-1]

    def _call(self, subject, path, fn):
        """覚えていた ID が古い（他の人が消した・動かした）ときは、引き直して1回だけやり直す。"""
        from googleapiclient.errors import HttpError
        try:
            return fn()
        except HttpError as e:
            if e.resp.status != 404:
                raise
            self._ids = {k: v for k, v in self._ids.items() if k[0] != subject}
            return fn()

    # ---- Storage の関数
    def subjects(self):
        folders = self._list(f"'{_q(self.root)}' in parents and mimeType = '{FOLDER}' and trashed = false",
                             "id,name,capabilities(canAddChildren)")
        out = []
        for f in folders:
            if f["name"].startswith((".", "_")) or "/" in f["name"]:
                continue
            if self._child(f["id"], "index.md", folder=False):
                self._ids[(f["name"], "")] = f["id"]
                self._caps[f["name"]] = (time.time(), bool((f.get("capabilities") or {}).get("canAddChildren")))
                out.append(f["name"])
        return sorted(out)

    def _tree(self, subject, fields="id,name,mimeType,modifiedTime,size,headRevisionId"):
        """Subject の下を、階層ごとにまとめて問い合わせてたどる（共有ドライブ全体は読まない）。隠しファイルも含めて返す。"""
        root = self._subject_id(subject)
        level, out = {root: ""}, {}
        while level:
            ids = list(level)
            nxt = {}
            for i in range(0, len(ids), 40):  # 1回の問い合わせに入れる親の数を抑える
                q = "(" + " or ".join(f"'{_q(x)}' in parents" for x in ids[i:i + 40]) + ") and trashed = false"
                for f in self._list(q, fields + ",parents"):
                    base = level[(set(f.get("parents") or []) & set(ids)).pop()]
                    p = f"{base}/{f['name']}"
                    if f["mimeType"] == FOLDER:
                        nxt[f["id"]] = p
                        self._ids[(subject, p + "/")] = f["id"]
                    elif not f["mimeType"].startswith("application/vnd.google-apps."):
                        self._ids[(subject, p)] = f["id"]
                        out[p] = f
            level = nxt
        return out

    # ---- 一覧の写しと、読んだ中身の覚え（ドライブは1回の読み書きが遅いため、PLAN M3）
    # 一覧は SNAP_TTL 秒だけ使い回す。他の人の変更は、長くてもその秒数で見える。自分の書き込みはすぐ写しに入れる。
    SNAP_TTL = 5.0
    RECENT = 60.0

    def _snap(self, subject, fresh=False):
        with self._lock:
            got = self._snaps.get(subject)
            if got and not fresh and time.time() - got[0] < self.SNAP_TTL:
                return got[1]
        tree = self._tree(subject)
        with self._lock:
            # ドライブの一覧は、直前の変更がまだ入っていないことがある。自分の変更をしばらく上から重ねる
            t = time.time()
            self._gone = {k: v for k, v in self._gone.items() if t - v < self.RECENT}
            self._mine = {k: v for k, v in self._mine.items() if t - v[0] < self.RECENT}
            tree = {p: f for p, f in tree.items() if f["id"] not in self._gone}
            for (sub, p), (_, f) in self._mine.items():
                if sub == subject and f["id"] not in self._gone and f["modifiedTime"] >= tree.get(p, {}).get("modifiedTime", ""):
                    tree[p] = f
            self._snaps[subject] = (t, tree)
        return tree

    def _note(self, subject, path, f):
        """自分が書いた・消したことを写しに入れる。f が None なら消した。"""
        with self._lock:
            got = self._snaps.get(subject)
            if f is None:
                self._mine.pop((subject, path), None)
            else:
                self._mine[(subject, path)] = (time.time(), f)
            if got:
                if f is None:
                    got[1].pop(path, None)
                else:
                    got[1][path] = f

    @staticmethod
    def _hidden(p):
        return any(x.startswith(".") for x in p.split("/")[1:])

    def create_subject(self, subject, index_text):
        name = check_subject_name(subject)
        with self._lock:
            if self._child(self.root, name, folder=True):
                raise StorageError(f"exists: {subject!r}")
            fid = self.svc.files().create(body={"name": name, "mimeType": FOLDER, "parents": [self.root]},
                                          fields="id", **ALL).execute(num_retries=RETRIES)["id"]
            self._ids[(name, "")] = fid  # index.md を書く前でも、この Subject のフォルダとして扱う
        self.write_text(name, "/index.md", index_text)

    CAPS_TTL = 60.0  # 権限は滅多に変わらないので、1分だけ使い回す（画面は1.5秒ごとに状態を読むため）

    def can_edit(self, subject):
        """フォルダに中身を足せるか（閲覧者・コメント可は False）。Drive の権限をそのまま見る（REQUIREMENTS E-4）。"""
        got = self._caps.get(subject)
        if got and time.time() - got[0] < self.CAPS_TTL:
            return got[1]
        fid = self._subject_id(subject)
        f = self.svc.files().get(fileId=fid, fields="capabilities(canAddChildren)", **ALL).execute(num_retries=RETRIES)
        ok = bool((f.get("capabilities") or {}).get("canAddChildren"))
        self._caps[subject] = (time.time(), ok)
        return ok

    def walk(self, subject):
        return [Entry(p, _ts(f["modifiedTime"]), int(f.get("size", 0)))
                for p, f in sorted(self._snap(subject).items()) if not self._hidden(p)]

    def heads(self, subject):
        return {p: f.get("headRevisionId") for p, f in sorted(self._snap(subject, fresh=True).items()) if not self._hidden(p)}

    def exists(self, subject, path):
        return check_rel_path(path) in self._snap(subject)

    def read_bytes(self, subject, path):
        path = check_rel_path(path)
        f = self._snap(subject).get(path)
        if not f:
            raise StorageError(f"not found: {path!r}")
        got = self._content.get(f["id"])
        if got and got[0] == f["modifiedTime"]:
            return got[1]
        from googleapiclient.errors import HttpError
        try:
            data = self.svc.files().get_media(fileId=f["id"], **ALL).execute(num_retries=RETRIES)
        except HttpError as e:
            if e.resp.status != 404:
                raise
            f = self._snap(subject, fresh=True).get(path)  # 他の人が消した・動かした
            if not f:
                raise StorageError(f"not found: {path!r}") from e
            data = self.svc.files().get_media(fileId=f["id"], **ALL).execute(num_retries=RETRIES)
        self._content[f["id"]] = (f["modifiedTime"], data)
        return data

    def write_bytes(self, subject, path, data):
        from googleapiclient.http import MediaIoBaseUpload
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

        def go():
            # 全体の鍵は、フォルダを作る・ID を引くあいだだけ持つ。送る（数秒かかる）あいだはパスごとの鍵だけにして、
            # 別のファイルの書き込みを並べられるようにする（M4 の計測: 書き込み1回 約4秒が、全部一列に並んでいた）
            key = (subject, check_rel_path(path))
            with self._lock:
                fid, parent, name = self._resolve(subject, path, make_parents=True)
                plock = self._plocks.setdefault(key, threading.Lock())
            with plock:
                fid = self._ids.get(key) or fid  # 同じパスを別のスレッドが先に作っていたら、上書きにする
                media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
                meta = "id,name,mimeType,modifiedTime,size,headRevisionId"
                if fid:
                    f = self.svc.files().update(fileId=fid, media_body=media, fields=meta, **ALL).execute(num_retries=RETRIES)
                else:
                    f = self.svc.files().create(body={"name": name, "parents": [parent]}, media_body=media,
                                                fields=meta, **ALL).execute(num_retries=RETRIES)
                    self._ids[key] = f["id"]
            # 一覧の写しは取り直さない（以前は書くたびに Subject 全体をたどり直していた）。自分の書き込みは _note で重ねる
            self._note(subject, key[1], f)
            self._content[f["id"]] = (f["modifiedTime"], data)
        self._call(subject, path, go)

    def delete(self, subject, path):
        def go():
            fid = self._resolve(subject, path)[0]
            if fid:
                self.svc.files().update(fileId=fid, body={"trashed": True}, fields="id", **ALL).execute(num_retries=RETRIES)
                self._ids.pop((subject, check_rel_path(path)), None)
                self._gone[fid] = time.time()
                self._note(subject, check_rel_path(path), None)
        self._call(subject, path, go)

    def head(self, subject, path):
        f = self._snap(subject).get(check_rel_path(path))
        return f.get("headRevisionId") if f else None

    def _trashed_with(self, subject, path, rev):
        """ゴミ箱の中から、その版を持つ同じパスのファイルを探す。"""
        fid, parent, name = self._resolve(subject, path)
        if fid or not parent:
            return None
        q = f"'{_q(parent)}' in parents and name = '{_q(name)}' and trashed = true"
        for f in self._list(q, "id"):
            revs = self.svc.revisions().list(fileId=f["id"], fields="revisions(id)").execute(num_retries=RETRIES).get("revisions", [])
            if any(r["id"] == rev for r in revs):
                return f["id"]
        return None

    def read_rev(self, subject, path, rev):
        from googleapiclient.errors import HttpError
        fid = self._resolve(subject, path)[0] or self._trashed_with(subject, path, rev)
        if not fid:
            raise StorageError(f"no revision: {rev}")
        try:
            return self.svc.revisions().get_media(fileId=fid, revisionId=rev).execute(num_retries=RETRIES)
        except HttpError as e:
            if e.resp.status == 404:
                fid = self._trashed_with(subject, path, rev)
                if fid:
                    return self.svc.revisions().get_media(fileId=fid, revisionId=rev).execute(num_retries=RETRIES)
            raise StorageError(f"no revision: {rev}") from e

    def restore(self, subject, path, rev):
        """消したファイルに戻すときは、新しく作らずゴミ箱から戻す（ID と版の履歴を保つ、E-2）。"""
        if rev is not None and not self._resolve(subject, path)[0]:
            fid = self._trashed_with(subject, path, rev)
            if fid:
                self.svc.files().update(fileId=fid, body={"trashed": False}, fields="id", **ALL).execute(num_retries=RETRIES)
                self._ids[(subject, check_rel_path(path))] = fid
                self._gone.pop(fid, None)
                self._snap(subject, fresh=True)
        super().restore(subject, path, rev)

    def keep(self, subject, path, rev):
        """版に「永久に保存」の印を付ける（E-2 の目録が指す版。ドライブは古い版を30日・100版で消すため）。"""
        fid = self._resolve(subject, path)[0]
        if fid and rev:
            self.svc.revisions().update(fileId=fid, revisionId=rev, body={"keepForever": True}, fields="id").execute(num_retries=RETRIES)

    def changed_since(self, subject, t):
        # 消したファイルは一覧から消えるだけで時刻が残らないので、ゴミ箱に入った時刻も見る
        return any(e.modified > t for e in self.walk(subject)) or self._trashed_since(subject, t)

    def _trashed_since(self, subject, t):
        folders = [v for (s, p), v in self._ids.items() if s == subject and (p == "" or p.endswith("/"))]
        for i in range(0, len(folders), 40):
            q = "(" + " or ".join(f"'{_q(x)}' in parents" for x in folders[i:i + 40]) + ") and trashed = true"
            if any(f.get("trashedTime") and _ts(f["trashedTime"]) > t for f in self._list(q, "id,trashedTime")):
                return True
        return False


from ...timing import instrument as _instrument  # noqa: E402  M4 の計測（app/timing.py）

_R = {m: "drive_read" for m in ("subjects", "walk", "heads", "exists", "read_bytes", "head", "read_rev", "changed_since")}
_W = {m: "drive_write" for m in ("create_subject", "write_bytes", "delete", "restore", "keep")}
_instrument(DriveStorage, {**_R, **_W})
