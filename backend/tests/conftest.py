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
