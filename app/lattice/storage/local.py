"""保存先のローカルのフォルダ版（テスト用。REQUIREMENTS F-2）。

root のフォルダを「Lattice の共有ドライブ」とみなす。
"""
import hashlib
import os
import pathlib

from .base import Entry, Storage, StorageError, check_rel_path, check_subject_name


class LocalStorage(Storage):
    """版は、内容のハッシュを番号にして Subject の中の `/.lattice/revs/` に残す（ドライブの版の代わり）。"""

    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()

    def _subject_dir(self, subject):
        d = (self.root / check_subject_name(subject)).resolve()
        if d.parent != self.root or not (d / "index.md").is_file():
            raise StorageError(f"no subject: {subject!r}")
        return d

    def _file(self, subject, path):
        d = self._subject_dir(subject)
        p = (d / check_rel_path(path).lstrip("/")).resolve()
        if not p.is_relative_to(d):
            raise StorageError(f"outside subject: {path!r}")
        return p

    def subjects(self):
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir()
                      if d.is_dir() and not d.name.startswith((".", "_")) and (d / "index.md").is_file())

    def can_edit(self, subject):
        import os
        d = self._subject_dir(subject)
        return os.access(d, os.W_OK) and os.access(d / "index.md", os.W_OK)

    def walk(self, subject):
        d = self._subject_dir(subject)
        out = []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = sorted(x for x in dirnames if not x.startswith("."))
            for f in sorted(filenames):
                if f.startswith("."):
                    continue
                p = pathlib.Path(dirpath) / f
                st = p.stat()
                out.append(Entry("/" + p.relative_to(d).as_posix(), st.st_mtime, st.st_size))
        return out

    def read_bytes(self, subject, path):
        p = self._file(subject, path)
        if not p.is_file():
            raise StorageError(f"not found: {path!r}")
        return p.read_bytes()

    def write_bytes(self, subject, path, data):
        p = self._file(subject, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if not check_rel_path(path).startswith("/.lattice/"):
            self._keep(subject, data)

    def create_subject(self, subject, index_text):
        d = self.root / check_subject_name(subject)
        if d.exists():
            raise StorageError(f"exists: {subject!r}")
        d.mkdir(parents=True)
        (d / "index.md").write_text(index_text, encoding="utf-8")

    def delete(self, subject, path):
        p = self._file(subject, path)
        if p.is_file():
            self._keep(subject, p.read_bytes())  # 消す前の中身も版として残す
            p.unlink()

    def _rev_file(self, subject, rev):
        return self._subject_dir(subject) / ".lattice" / "revs" / rev

    def _keep(self, subject, data):
        rev = hashlib.sha256(data).hexdigest()[:32]
        f = self._rev_file(subject, rev)
        if not f.is_file():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        return rev

    def head(self, subject, path):
        p = self._file(subject, path)
        return self._keep(subject, p.read_bytes()) if p.is_file() else None

    def read_rev(self, subject, path, rev):
        f = self._rev_file(subject, rev)
        if not f.is_file():
            raise StorageError(f"no revision: {rev}")
        return f.read_bytes()
