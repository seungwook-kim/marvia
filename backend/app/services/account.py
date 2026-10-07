"""회원 탈퇴 업무 규칙 (개인정보보호법 제37조: 동의 철회 시 지체 없이 파기).

순서가 중요하다. 우리 DB 기록을 먼저 지우고, 그다음 Supabase 계정을 지운다.
두 곳은 따로 처리되어 중간에 한쪽만 실패할 수 있다.
  - 계정을 먼저 지우고 기록 삭제가 실패하면, 로그인을 못 해 다시 탈퇴할 수 없고 기록만 주인 없이 남는다
  - 기록을 먼저 지우면, 계정 삭제가 실패해도 로그인 상태라 탈퇴를 다시 누를 수 있다

한계: 이미 받은 로그인 토큰은 남은 기한(보통 1시간) 동안 auth.py를 통과한다.
계정 삭제 여부를 Supabase에 매번 묻지 않기 때문이다. 화면이 탈퇴 직후 로그아웃시킨다.
"""
from __future__ import annotations

import os

from backend.app.clients.supabase_admin import AdminError, delete_account
from backend.app.db import atomic, connect, delete_user_queries
from backend.app.services import quota
from backend.app.services.conversations import delete_all as delete_conversations
from backend.app.services.kakao import delete as delete_kakao_link
from backend.app.services.memory import forget_user
from backend.app.services.profile import delete as delete_profile


class WithdrawError(Exception):
    """탈퇴를 끝내지 못했다. stage: 어느 단계에서(account_records 기록 / account_delete 계정), reason: 오류 종류만."""

    def __init__(self, stage: str, reason: str):
        super().__init__(f"{stage} {reason}")
        self.stage, self.reason = stage, reason


def withdraw(user_id: str) -> int:
    """기록을 지우고 계정을 지운다. 지운 기록 수를 돌려준다. 어느 단계든 실패하면 WithdrawError."""
    # 1) 우리 기록 먼저. 못 지우면 계정은 건드리지 않는다
    if not os.environ.get("DATABASE_URL"):
        raise WithdrawError("account_records", "not_configured")
    try:
        with connect() as conn, atomic(conn):  # 한 트랜잭션. 중간에 실패하면 하나도 안 지워진다
            deleted = delete_user_queries(conn, user_id)
            delete_profile(conn, user_id)  # 회원 정보(이름·전화·주소, 암호화 저장)
            forget_user(conn, user_id)  # 화물 표·메모장 (암호화된 B/L 포함)
            delete_conversations(conn, user_id)  # 대화 목록. 말풍선은 대화가 지워질 때 같이 지워진다 (migrations/018)
            # 카카오 알림 토큰은 줄만 지운다. 카카오 서버 연결 해제는 부르지 않는다
            # (외부 실패가 탈퇴를 막으면 안 되고, 토큰이 없으면 더 보낼 수 없다)
            delete_kakao_link(conn, user_id)
            quota.forget_user(conn, user_id)  # 하루 LLM 토큰 사용량 (migrations/016)
    except Exception as e:  # 연결·SQL 실패 모두. 오류 종류만 남긴다
        raise WithdrawError("account_records", type(e).__name__) from None

    # 2) 계정. 실패해도 기록은 이미 지워졌다. 다시 부르면 이어서 끝난다
    try:
        delete_account(user_id)
    except AdminError as e:
        raise WithdrawError("account_delete", f"{e.kind} {e.code}" if e.code else e.kind) from None
    return deleted
