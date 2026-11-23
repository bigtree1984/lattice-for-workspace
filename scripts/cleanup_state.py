"""ゴミ箱に移した Subject の、Firestore に残った作業の状態を消す（運用手順書 7章）。

Subject を共有ドライブでゴミ箱に移しても、Firestore の lattice_state には、会話中の記録や移行の状態が残る。
画面には出ないが、会話の本文を含むことがあるので、この道具で消す。

使い方（ADC は gcloud auth application-default login で用意）:
  python scripts/cleanup_state.py <project> <共有ドライブの ID> <Subject>            消すものを表示するだけ
  python scripts/cleanup_state.py <project> <共有ドライブの ID> <Subject> --delete   本当に消す
"""
import sys

from google.cloud import firestore


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    project, space, subject = argv[:3]
    really = "--delete" in argv[3:]
    db = firestore.Client(project=project)
    docs = [d for d in db.collection("lattice_state").where(filter=firestore.FieldFilter("space", "==", space)).stream()
            if d.get("subject") == subject]
    for d in docs:
        print(("消す: " if really else "対象: ") + d.id)
        if really:
            d.reference.delete()
    print(f"{len(docs)} 件{'を消しました' if really else '（--delete を付けると消します）'}")


if __name__ == "__main__":
    main(sys.argv[1:])
