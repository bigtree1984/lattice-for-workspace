"""時間の内訳を測る（M4 の計測用）。別スレッドの作業ごとに、ドライブ・Firestore・Gemini にかかった時間を足し上げ、最後にログに1行出す。"""
import contextvars
import functools
import time

_CUR = contextvars.ContextVar("lattice_timing", default=None)


class Tracker:
    def __init__(self, label):
        self.label, self.t0, self.sum, self.n, self.depth = label, time.time(), {}, {}, 0

    def report(self):
        total = time.time() - self.t0
        parts = " ".join(f"{k}={self.sum[k]:.1f}s/{self.n[k]}回" for k in sorted(self.sum))
        rest = total - sum(self.sum.values())
        print(f"[timing] {self.label} 合計={total:.1f}s {parts} その他={rest:.1f}s", flush=True)


def start(label):
    t = Tracker(label)
    _CUR.set(t)
    return t


def timed(kind):
    """関数にかかった時間を kind に足す。入れ子は外側だけ数える（二重に数えない）。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            t = _CUR.get()
            if t is None or t.depth:
                return fn(*a, **k)
            t.depth += 1
            s = time.time()
            try:
                return fn(*a, **k)
            finally:
                t.depth -= 1
                t.sum[kind] = t.sum.get(kind, 0) + time.time() - s
                t.n[kind] = t.n.get(kind, 0) + 1
        return wrap
    return deco


def instrument(cls, kinds):
    """cls のメソッドを包む。kinds は {メソッド名: 種類}。"""
    for name, kind in kinds.items():
        if hasattr(cls, name):
            setattr(cls, name, timed(kind)(getattr(cls, name)))
