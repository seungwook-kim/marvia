"""메모장(user_memory) 거래처의 평문 전화번호를 PII 열쇠로 잠근다.

새로 저장하는 메모는 코드(services/memory.save → seal_items)가 처음부터 잠근다. 이 스크립트는 평문으로 남은 기존 줄을
한 번에 옮긴다. 옮기지 않아도 그 사용자의 메모가 다음에 저장될 때 잠기지만, 그때까지 평문이 DB에 남는다.
items(지금 메모)와 previous(되돌리기 재료) 둘 다 잠근다. 열쇠가 없으면 아무것도 바꾸지 않는다.
다시 돌려도 평문이 남은 줄만 건드린다. 줄의 version은 올리지 않는다(내용은 같고 모양만 바뀐다).

10-08 QA F6: 예전에는 읽은 뒤 버전 확인 없이 덮어써서, 그사이 서비스가 저장한 메모(새 항목)를 옛 내용으로 지울 수 있었다.
지금은 줄마다 "읽은 version 그대로일 때만" UPDATE하고(낙관적 잠금), 그사이 바뀌어 0줄이면 그 줄을 다시 읽어 MAX_TRIES번까지
다시 한다. 서비스가 저장하면 그 저장이 이미 잠근 값을 쓰므로 다시 읽은 줄은 대개 평문이 없어 그냥 넘어간다.

    python -m scripts.encrypt_memo_phones          # 바꾸지 않고 몇 줄이 대상인지만 센다
    python -m scripts.encrypt_memo_phones --apply  # 실제로 잠근다
"""
from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

from backend.app import crypto
from backend.app.db import connect
from backend.app.services import memory

MAX_TRIES = 5  # 한 줄을 다시 읽고 다시 쓰는 횟수. 사람이 손으로 돌리는 일회성 스크립트라 넉넉히 둔다


def _plain_left(items, prev) -> bool:
    """items나 previous에 아직 잠기지 않은 전화번호가 있나."""
    return memory.has_plain_pii(items or []) or memory.has_plain_pii(prev or [])


def seal_row(conn, user_id: str) -> str:
    """한 사용자의 메모장 줄을 잠근다. 돌려주는 값: sealed / clean(평문 없음·줄 없음) / conflict(계속 바뀌어 못 함).
    잠그지 못한 값(열쇠 문제)이 남으면 RuntimeError: 평문을 지운 채로 두지 않으려고 그 줄은 쓰지 않고 멈춘다."""
    for _ in range(MAX_TRIES):
        row = conn.execute("SELECT items, previous, version FROM user_memory WHERE user_id = %s", [user_id]).fetchone()
        if not row or not _plain_left(row[0], row[1]):
            return "clean"
        items, prev, version = row[0] or [], row[1], row[2]
        sealed, sealed_prev = memory.seal_items(items), memory.seal_items(prev or [])
        if _plain_left(sealed, sealed_prev):
            raise RuntimeError("잠그지 못한 값이 있습니다. 열쇠를 확인하세요")
        cur = conn.execute(
            "UPDATE user_memory SET items = %s::jsonb, previous = %s::jsonb WHERE user_id = %s AND version = %s",
            [json.dumps(sealed, ensure_ascii=False), json.dumps(sealed_prev, ensure_ascii=False) if prev else None,
             user_id, version])
        if cur.rowcount == 1:
            return "sealed"
    return "conflict"


def main(argv: list[str]) -> int:
    load_dotenv(".env")
    if not os.environ.get(crypto.PII_KEY_ENV, "").strip():
        print("PII_ENCRYPTION_KEY가 없어 잠글 수 없습니다. 아무것도 바꾸지 않았습니다")
        return 1
    apply = "--apply" in argv
    with connect() as conn:
        rows = conn.execute("SELECT user_id, items, previous FROM user_memory").fetchall()
        targets = [uid for uid, items, prev in rows if _plain_left(items, prev)]
        print(f"평문 전화번호가 남은 메모장 {len(targets)}줄 / 전체 {len(rows)}줄")
        if not apply or not targets:
            return 0
        results = {"sealed": 0, "clean": 0, "conflict": 0}
        for uid in targets:
            results[seal_row(conn, uid)] += 1
        left = sum(1 for _, items, prev in conn.execute("SELECT user_id, items, previous FROM user_memory").fetchall()
                   if _plain_left(items, prev))
        print(f"잠갔습니다 {results['sealed']}줄, 그사이 이미 잠김 {results['clean']}줄, "
              f"계속 바뀌어 못 함 {results['conflict']}줄. 남은 평문 줄 {left}")
        return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
