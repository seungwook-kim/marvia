"""메모장(user_memory) 거래처의 평문 전화번호를 PII 열쇠로 잠근다 (10-08).

새로 저장하는 메모는 코드(services/memory.save → seal_items)가 처음부터 잠근다. 이 스크립트는 그 전에 평문으로 저장된 줄을
한 번에 옮긴다. 옮기지 않아도 그 사용자의 메모가 다음에 저장될 때 잠기지만, 그때까지 평문이 DB에 남는다.
items(지금 메모)와 previous(되돌리기 재료) 둘 다 잠근다. 한 트랜잭션이다. 열쇠가 없으면 아무것도 바꾸지 않는다.
다시 돌려도 평문이 남은 줄만 건드린다. 줄의 version은 올리지 않는다(내용은 같고 모양만 바뀐다).

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


def main(argv: list[str]) -> int:
    load_dotenv(".env")
    if not os.environ.get(crypto.PII_KEY_ENV, "").strip():
        print("PII_ENCRYPTION_KEY가 없어 잠글 수 없습니다. 아무것도 바꾸지 않았습니다")
        return 1
    apply = "--apply" in argv
    with connect() as conn:
        rows = conn.execute("SELECT user_id, items, previous FROM user_memory").fetchall()
        targets = [(uid, items or [], prev or []) for uid, items, prev in rows
                   if memory.has_plain_pii(items or []) or memory.has_plain_pii(prev or [])]
        print(f"평문 전화번호가 남은 메모장 {len(targets)}줄 / 전체 {len(rows)}줄")
        if not apply or not targets:
            return 0
        with conn.transaction():
            for uid, items, prev in targets:
                sealed, sealed_prev = memory.seal_items(items), memory.seal_items(prev)
                if memory.has_plain_pii(sealed) or memory.has_plain_pii(sealed_prev):
                    raise RuntimeError("잠그지 못한 값이 있어 전부 되돌립니다")
                conn.execute("UPDATE user_memory SET items = %s::jsonb, previous = %s::jsonb WHERE user_id = %s",
                             [json.dumps(sealed, ensure_ascii=False), json.dumps(sealed_prev, ensure_ascii=False) if prev else None, uid])
        left = sum(1 for _, items, prev in conn.execute("SELECT user_id, items, previous FROM user_memory").fetchall()
                   if memory.has_plain_pii(items or []) or memory.has_plain_pii(prev or []))
        print(f"잠갔습니다. 남은 평문 줄 {left}")
        return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
