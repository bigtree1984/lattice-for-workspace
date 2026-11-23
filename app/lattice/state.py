"""会話中の作業の状態の置き場所（REQUIREMENTS C-11、PLAN M3 の速さの決定）。

置くもの: 会話中・確認待ちの Discussion の一覧と最後に使った番号（registry）、各 Discussion の作業の状態（work/D-NNN）。
どちらも closed・廃棄で消える一時的な情報。L1・L2・要約・資料・記録などの本物のデータはドライブに置く。

- LocalState: 保存先の隠しファイルに置く（これまでと同じ。ローカル版とテスト用）
- FirestoreState: Firestore（東京）に置く。ドライブは1回の読み書きが 0.7〜3 秒かかるため（PLAN M3 の測定）
"""
import json

from .storage import StorageError


class LocalState:
    def __init__(self, st):
        self.st = st

    def _path(self, subject, key):
        if key == "registry":
            return "/.lattice/registry.json"
        return f"/.lattice/work/{key.split('/', 1)[1]}.json"

    def get(self, subject, key):
        try:
            return json.loads(self.st.read_text(subject, self._path(subject, key)))
        except StorageError:
            return None

    def put(self, subject, key, obj):
        self.st.write_text(subject, self._path(subject, key), json.dumps(obj, ensure_ascii=False, indent=1))

    def delete(self, subject, key):
        p = self._path(subject, key)
        if self.st.exists(subject, p):
            self.st.delete(subject, p)


class FirestoreState:
    """文書の ID は「共有ドライブ（またはテスト用のフォルダ）の ID・Subject・キー」。中身は JSON の文字列で持つ。"""

    def __init__(self, space, project=None, database="(default)", client=None):
        self.space = space
        self._db = client
        self.project, self.database = project, database

    @property
    def db(self):
        if self._db is None:
            from google.cloud import firestore
            from . import drive_files
            drive_files.service()  # 手元では Lattice 専用の ADC の場所を設定する
            self._db = firestore.Client(project=self.project, database=self.database)
        return self._db

    def _doc(self, subject, key):
        did = f"{self.space}~{subject}~{key}".replace("/", "~")
        return self.db.collection("lattice_state").document(did)

    def get(self, subject, key):
        snap = self._doc(subject, key).get()
        return json.loads(snap.get("json")) if snap.exists else None

    def put(self, subject, key, obj):
        self._doc(subject, key).set({"json": json.dumps(obj, ensure_ascii=False), "space": self.space,
                                     "subject": subject, "key": key})

    def delete(self, subject, key):
        self._doc(subject, key).delete()


from ..timing import instrument as _instrument  # noqa: E402  M4 の計測（app/timing.py）
_instrument(FirestoreState, {"get": "firestore", "put": "firestore", "delete": "firestore"})
