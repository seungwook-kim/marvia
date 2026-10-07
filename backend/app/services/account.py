"""회원 탈퇴 업무 규칙 (개인정보보호법 제37조: 동의 철회 시 지체 없이 파기).

순서가 중요하다. 우리 DB 기록을 먼저 지우고, 그다음 Supabase 계정을 지운다.
두 곳은 따로 처리되어 중간에 한쪽만 실패할 수 있다.
  - 계정을 먼저 지우고 기록 삭제가 실패하면, 로그인을 못 해 다시 탈퇴할 수 없고 기록만 주인 없이 남는다
  - 기록을 먼저 지우면, 계정 삭제가 실패해도 로그인 상태라 탈퇴를 다시 누를 수 있다

이미 받은 로그인 토큰은 남은 기한(보통 1시간) 동안 서명 확인을 통과한다. 그 사이의 요청은
withdrawn_users(migrations/027)에 남긴 user_id로 auth.current_user가 403으로 막는다 (10-08 QA A3).
탈퇴를 마저 끝내는 DELETE /api/account만 그 표를 보지 않는다 (auth.DELETION_ROUTES).

"내 기록 삭제"(DELETE /api/history)와 범위가 다르다 (10-08 QA A2).
  - 내 기록 삭제: 조회 기록, 화물 표·메모장, 대화 목록만. 회원 정보·카카오 연결·배 지켜보기·사용량은 그대로 두고 계속 쓴다
  - 탈퇴: 위에 더해 회원 정보, 카카오 알림 토큰, 배만 지켜보기(vessel_watch), 하루 토큰·선박 자세히 보기 사용량,
    정지 표 줄까지 지우고 계정을 없앤다. 배만 지켜보기를 남기면 워커가 주인 없는 배의 위치를 계속 모은다
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from backend.app.clients.supabase_admin import AdminError, delete_account
from backend.app.db import atomic, connect, delete_user_queries
from backend.app.services import quota
from backend.app.services.conversations import delete_all as delete_conversations
from backend.app.services.kakao import delete as delete_kakao_link
from backend.app.services.memory import forget_user
from backend.app.services.profile import delete as delete_profile


class WithdrawError(Exception):
    """탈퇴를 끝내지 못했다. stage: 어느 단계에서(account_records 기록 / account_delete 계정), reason: 오류 종류만.

    account_records면 아무것도 지워지지 않았다(한 트랜잭션). account_delete면 기록은 이미 지워졌고 계정만 남았다.
    화면 문구가 이 둘을 구분해 사실대로 알린다 (routers/account.py, routers/admin.py, 10-08 QA A8)."""

    def __init__(self, stage: str, reason: str):
        super().__init__(f"{stage} {reason}")
        self.stage, self.reason = stage, reason


@dataclass(frozen=True)
class WithdrawResult:
    """탈퇴 결과. deleted_records: 이번에 지운 조회 기록 수.
    resumed: 앞선 시도가 기록을 이미 지웠고(계정 삭제만 실패) 이번에 이어서 끝냈다. 이때 deleted_records는 0이 맞다."""

    deleted_records: int
    resumed: bool


def _delete_vessel_watch(conn, user_id: str) -> int:
    """배만 지켜보기(migrations/023) 줄을 지운다. 남기면 worker가 주인 없는 배 위치를 계속 모은다 (10-08 QA A2)."""
    return conn.execute("DELETE FROM vessel_watch WHERE user_id = %s", [user_id]).rowcount


def _delete_vessel_detail_usage(conn, user_id: str) -> int:
    """선박 자세히 보기 하루 횟수(migrations/024). user_key가 로그인 사용자 ID다 (10-08 QA A2)."""
    return conn.execute("DELETE FROM vessel_detail_usage WHERE user_key = %s", [user_id]).rowcount


def _mark_withdrawn(conn, user_id: str) -> bool:
    """탈퇴 표시를 남긴다(migrations/027). 이미 있었으면 True(앞선 시도의 이어하기).
    30일 지난 표시는 같은 김에 지운다. 규칙(30일)은 DB 함수 purge_old_withdrawn_users()에만 있다."""
    conn.execute("SELECT purge_old_withdrawn_users()")
    inserted = conn.execute(
        "INSERT INTO withdrawn_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", [user_id]).rowcount
    return not inserted


def _close_account(conn, user_id: str) -> bool:
    """내 기록 삭제에는 없고 탈퇴에만 있는 정리. 앞선 시도의 이어하기였으면 True (_mark_withdrawn).
    탈퇴 표시를 가장 먼저 남겨 남은 출입증의 요청을 막고(auth.current_user, 10-08 QA A3),
    배만 지켜보기·선박 자세히 보기 횟수(10-08 QA A2)와 정지 줄(10-08 QA A4)을 지운다.
    정지 줄은 지워도 탈퇴 표시가 같은 요청들을 막으므로 빈틈이 없다."""
    resumed = _mark_withdrawn(conn, user_id)
    _delete_vessel_watch(conn, user_id)
    _delete_vessel_detail_usage(conn, user_id)
    conn.execute("DELETE FROM suspended_users WHERE user_id = %s", [user_id])
    return resumed


def withdraw(user_id: str, *, in_transaction: Callable | None = None) -> WithdrawResult:
    """기록을 지우고 계정을 지운다. 어느 단계든 실패하면 WithdrawError.

    in_transaction(conn): 기록 삭제와 같은 트랜잭션에서 더 할 일. 운영자 강제 탈퇴가 이력(admin_actions)을 여기서 남겨
    "기록은 지웠는데 이력은 없다"가 생기지 않게 한다 (services/customers.delete, 10-08 QA A8).
    """
    # 1) 우리 기록 먼저. 못 지우면 계정은 건드리지 않는다
    if not os.environ.get("DATABASE_URL"):
        raise WithdrawError("account_records", "not_configured")
    try:
        with connect() as conn, atomic(conn):  # 한 트랜잭션. 중간에 실패하면 하나도 안 지워진다
            resumed = _close_account(conn, user_id)  # 탈퇴 표시 먼저, 배 지켜보기·사용량·정지 줄 정리
            deleted = delete_user_queries(conn, user_id)
            delete_profile(conn, user_id)  # 회원 정보(이름·전화·주소, 암호화 저장)
            forget_user(conn, user_id)  # 화물 표·메모장 (암호화된 B/L 포함)
            delete_conversations(conn, user_id)  # 대화 목록. 말풍선은 대화가 지워질 때 같이 지워진다 (migrations/018)
            # 카카오 알림 토큰은 줄만 지운다. 카카오 서버 연결 해제는 부르지 않는다
            # (외부 실패가 탈퇴를 막으면 안 되고, 토큰이 없으면 더 보낼 수 없다)
            delete_kakao_link(conn, user_id)
            quota.forget_user(conn, user_id)  # 하루 LLM 토큰 사용량 (migrations/016)
            if in_transaction:
                in_transaction(conn)
    except Exception as e:  # 연결·SQL 실패 모두. 오류 종류만 남긴다
        raise WithdrawError("account_records", type(e).__name__) from None

    # 2) 계정. 실패해도 기록은 이미 지워졌다. 다시 부르면 이어서 끝난다
    try:
        delete_account(user_id)
    except AdminError as e:
        raise WithdrawError("account_delete", f"{e.kind} {e.code}" if e.code else e.kind) from None
    return WithdrawResult(deleted_records=deleted, resumed=resumed)
