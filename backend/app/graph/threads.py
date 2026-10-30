"""체크포인트 스레드 이름과 사용자별 삭제. 근거: docs/specs/v2-langgraph.md 4절 "thread_id 설계"와 "보관과 정리", EC-13, AC-14.

책임: (1) 같은 사용자가 같은 B/L로 이은 요청만 같은 스레드가 되도록 thread_id를 만든다. (2) 계정 삭제·기록 삭제가 그 사용자의
스레드를 지우는 SQL을 한 곳에 둔다.

이 파일은 langgraph를 import하지 않는다. services/account.py, routers/history.py가 이 파일을 불러도 그래프를 적재하지 않으려는 것이다
(AGENT_GRAPH가 꺼져 있을 때 langgraph를 읽지 않는다는 뜻과 같다). 메모리 체크포인터(테스트용)의 스레드 지우기는
checkpoint.py가 on_user_delete로 등록한 함수를 불러서 한다.

thread_id 형식: "verdict:{owner}:{cargo}" (4절).
  owner = sha256(quota.owner_key(session_id, user_id))의 앞 16자. 로그인 사용자는 "user:{uuid}", 아니면 "session:{session_id}"를 해시한다.
          해시를 쓰는 이유는 임의 문자열(session_id)이 DB 평문 칸(thread_id, 색인)에 들어가지 않게 하려는 것이다.
  cargo = services/cargo.cargo_key(bl_no)(B/L의 HMAC 지문, 되돌릴 수 없음)의 앞 32자. 열쇠가 없으면 None이라 스레드를 만들지 않는다.
"""
from __future__ import annotations

import hashlib
from typing import Callable

from backend.app.db import atomic
from backend.app.observability.logging import log_failure
from backend.app.services import cargo, quota

THREAD_PREFIX = "verdict"
OWNER_LEN = 16   # owner 해시 앞 글자 수 (4절)
CARGO_LEN = 32   # cargo 지문 앞 글자 수 (4절)

# langgraph-checkpoint-postgres가 쓰는 표 넷. 같은 이름의 표를 마이그레이션 032가 만든다. 지우는 순서는 의미 없다(외래키 없음)
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")

# 메모리 체크포인터(GRAPH_CHECKPOINT=memory)가 자기 스레드를 지우려고 등록하는 함수. 인자는 owner 접두사 (예: "verdict:ab12...:")
_memory_deleters: list[Callable[[str], None]] = []


def on_user_delete(deleter: Callable[[str], None]) -> None:
    """사용자 삭제 때 불릴 함수를 등록한다. 메모리 체크포인터가 만들어질 때 checkpoint.py가 부른다."""
    if deleter not in _memory_deleters:
        _memory_deleters.append(deleter)


def owner_hash(session_id: str | None, user_id: str | None) -> str | None:
    """요청 주인의 해시(앞 16자). 로그인했으면 사용자, 아니면 session_id 기준이다. 둘 다 없으면 None(스레드를 만들 수 없다)."""
    name = quota.owner_key(session_id, user_id)
    if not name:
        return None
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:OWNER_LEN]


def thread_id(session_id: str | None, user_id: str | None, bl_no: str) -> str | None:
    """"verdict:{owner}:{cargo}". 주인이나 화물 지문을 만들 수 없으면(열쇠 없음) None이고, 부르는 쪽은 체크포인터 없이 돈다 (EC-01).
    사용자가 다르면 B/L이 같아도 스레드가 다르다(격리). 한 사용자의 B/L이 여럿이면 스레드도 여럿이다."""
    owner = owner_hash(session_id, user_id)
    fingerprint = cargo.cargo_key(bl_no)
    if not owner or not fingerprint:
        return None
    return f"{THREAD_PREFIX}:{owner}:{fingerprint[:CARGO_LEN]}"


def user_prefix(user_id: str) -> str:
    """로그인 사용자의 모든 스레드가 공유하는 접두사 "verdict:{owner}:". 계정·기록 삭제가 이것으로 지운다."""
    return f"{THREAD_PREFIX}:{owner_hash(None, user_id)}:"


def delete_user_threads(conn, user_id: str) -> None:
    """그 사용자의 멈춘 스레드를 모두 지운다 (EC-13, AC-14). 계정 삭제·DELETE /api/history가 같은 트랜잭션 안에서 부른다.
    로그인하지 않은 세션의 스레드는 사용자를 알 수 없어 TTL(graph_pause_ttl_hours)과 정리 함수가 맡는다.

    표가 아직 없는 DB(마이그레이션 032 전)는 건너뛴다: 안쪽 트랜잭션(저장점)에서 실패를 되돌려 바깥 삭제 전체가 무너지지 않게 한다.
    그 밖의 오류는 그대로 올려 부르는 쪽이 삭제 실패로 알리게 한다(지운 줄 알았는데 남아 있으면 안 된다)."""
    import psycopg

    like = user_prefix(user_id).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        with atomic(conn):  # 바깥 트랜잭션 안에서는 저장점이 된다
            for table in CHECKPOINT_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE thread_id LIKE %s", [like])
    except psycopg.errors.UndefinedTable:
        log_failure("graph_checkpoint", "tables_missing")
    prefix = user_prefix(user_id)
    for deleter in list(_memory_deleters):
        deleter(prefix)
