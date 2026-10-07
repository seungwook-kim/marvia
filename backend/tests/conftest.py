"""backend/tests 공통 픽스처.

테스트는 키도 DB도 없이 돈다 (CLAUDE.md "테스트는 키 없이 돈다").
"""
import psycopg
import pytest


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch):
    """테스트에서 실제 DB 접속은 기다리지 않고 바로 실패하게 한다.

    많은 테스트가 DATABASE_URL을 가짜 주소로 두고 쓰는 곳의 connect만 바꿔 끼운다. 바꿔 끼우지 않은 길
    (main._migrate, auth._suspended)은 가짜 호스트에 붙으려다 이름 찾기 실패까지 수 초를 기다린다.
    두 길 모두 접속 실패를 잡아 DB 없이 계속하므로, 실패가 바로 오면 동작은 같고 기다림만 없어진다.
    테스트가 connect·open_conn을 가짜로 바꿔 끼운 곳은 여기까지 오지 않아 영향이 없다.
    """
    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("테스트에서는 실제 DB에 붙지 않는다 (backend/tests/conftest.py)")

    monkeypatch.setattr(psycopg, "connect", refuse)
    # 앞 테스트의 연결 실패 기억(db.connect의 짧은 차단, 10-08 QA A1)이 다음 테스트로 넘어가지 않게 지운다
    from backend.app.db import reset_connection_state
    reset_connection_state()


@pytest.fixture(autouse=True)
def _fixed_sale_due_today(monkeypatch):
    """판매 예정일 범위 검사(routers/agent.sale_due_out_of_range)의 "오늘"을 고정한다 (10-08 QA B2).
    많은 테스트가 "2026-10-05" 같은 고정 판매 예정일을 쓴다. 실제 오늘을 쓰면 몇 주 뒤에 범위 밖이 되어
    관계없는 테스트가 깨진다. 범위 검사 자체를 보는 테스트는 today_kst를 다시 바꿔 끼우거나 today 인자를 준다."""
    from datetime import date

    from backend.app.routers import agent

    monkeypatch.setattr(agent, "today_kst", lambda: date(2026, 9, 23))


@pytest.fixture(autouse=True)
def _forget_tool_rejections():
    """후속 질문 도구 거절 기억(services/llm.tools_rejected)은 프로세스 메모리다. 한 테스트가 남긴 기억이
    다음 테스트의 후속 질문을 도구 없이 묻게 하지 않도록 테스트마다 비운다 (10-08 QA B8)."""
    from backend.app.services import llm

    llm._tools_rejected_at.clear()
    yield
    llm._tools_rejected_at.clear()


@pytest.fixture(autouse=True)
def _chat_agent_breaker_closed():
    """채팅 에이전트 회로 차단(services/chat._agent_breaker)을 테스트마다 닫힌 상태로 시작한다 (10-08 QA C1).
    프로세스 메모리 상태라 앞 테스트의 에이전트 실패가 쌓이면 뒤 테스트가 에이전트를 건너뛴다."""
    from backend.app.services import chat  # noqa: PLC0415 - 이 픽스처에서만 쓴다
    chat._agent_breaker.reset()
    yield
    chat._agent_breaker.reset()
