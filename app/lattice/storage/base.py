"""保存先の差し替え口（REQUIREMENTS F-3、PLAN M1）。

保存先は「Lattice の共有ドライブ」1つを表す。その直下に Subject のフォルダが並ぶ（REQUIREMENTS B-1）。
ローカルのフォルダ版（テスト用）とドライブ版（M3）が、同じ形の関数を持つ。

パスは Subject の中の `/` 始まりの相対パス（例: `/facets/price/price.md`）で受け渡す。
"""
from dataclasses import dataclass


@dataclass
class Entry:
    path: str          # Subject の中の相対パス（/ 始まり）
    modified: float    # 最終更新の時刻（UNIX 秒）
    size: int


class StorageError(Exception):
    pass


class Storage:
    def subjects(self) -> list[str]:
        """Subject のフォルダ名の一覧（index.md のあるものだけ）。"""
        raise NotImplementedError

    def can_edit(self, subject: str) -> bool:
        """この保存先の名義で、Subject に書き込めるか（Drive で閲覧のみなら False。REQUIREMENTS E-4）。"""
        return True

    def walk(self, subject: str) -> list[Entry]:
        """Subject の中の全ファイル。隠しフォルダ（. で始まる）の中は含めない。"""
        raise NotImplementedError

    def read_bytes(self, subject: str, path: str) -> bytes:
        raise NotImplementedError

    def write_bytes(self, subject: str, path: str, data: bytes) -> None:
        raise NotImplementedError

    def create_subject(self, subject: str, index_text: str) -> None:
        """Subject のフォルダを作り、index.md を置く（D-5）。同じ名前のフォルダがあれば断る。"""
        raise NotImplementedError

    def delete(self, subject: str, path: str) -> None:
        """ファイルを消す（ドライブ版はゴミ箱へ）。中身は版として残っているので、版から戻せる。"""
        raise NotImplementedError

    def exists(self, subject: str, path: str) -> bool:
        try:
            self.read_bytes(subject, path)
            return True
        except StorageError:
            return False

    # 版（REQUIREMENTS E-2・C-12）。書き込みのたびに版が残り、版の番号から中身を取り出せる
    def head(self, subject: str, path: str) -> str | None:
        """いまの版の番号。ファイルが無ければ None。"""
        raise NotImplementedError

    def read_rev(self, subject: str, path: str, rev: str) -> bytes:
        raise NotImplementedError

    def restore(self, subject: str, path: str, rev: str | None) -> None:
        """版の中身に戻す。rev が None なら、ファイルが無かった状態（消す）に戻す。"""
        if rev is None:
            if self.exists(subject, path):
                self.delete(subject, path)
        elif self.head(subject, path) != rev:
            self.write_bytes(subject, path, self.read_rev(subject, path, rev))

    def heads(self, subject: str) -> dict:
        """Subject の中の全ファイル → いまの版の番号（E-2 の目録）。"""
        return {e.path: self.head(subject, e.path) for e in self.walk(subject)}

    def keep(self, subject: str, path: str, rev: str) -> None:
        """版を消えないように残す（ドライブ版は「永久に保存」の印）。ローカル版は版が消えないので何もしない。"""

    def changed_since(self, subject: str, t: float) -> bool:
        """時刻 t より後に、Subject の中で変わったファイルがあるか（D-1 の作り直しの判定）。"""
        return any(e.modified > t for e in self.walk(subject))

    # 便利関数
    def read_text(self, subject: str, path: str) -> str:
        return self.read_bytes(subject, path).decode("utf-8")

    def write_text(self, subject: str, path: str, text: str) -> None:
        self.write_bytes(subject, path, text.encode("utf-8"))


def check_subject_name(name: str) -> str:
    """Subject のフォルダ名として受け付けられるか。/ や .. を含むもの、隠しフォルダは受けない。"""
    name = str(name or "")
    if not name or name.startswith(".") or "/" in name or "\\" in name or name in ("..", "."):
        raise StorageError(f"bad subject: {name!r}")
    return name


def check_rel_path(path: str) -> str:
    """Subject の中の相対パスとして受け付けられるか。.. を含むものは受けない。"""
    path = "/" + str(path or "").lstrip("/")
    parts = path.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts) or "\\" in path:
        raise StorageError(f"bad path: {path!r}")
    return path
