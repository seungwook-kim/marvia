"""10-08 QA A갈래(공통 DB·인증·계정·운영자) 회귀 테스트.

A1 DB 연결 상한·실패 기억, A3 탈퇴 계정 차단, A4 정지·탈퇴 계정의 삭제 요청 허용, A6 회원 정보 미입력 차단,
A7 정지 순서, A8 강제 탈퇴 이력, A9 운영자 이메일, A10 API 문서·보안 헤더.
진짜 DB·Supabase는 부르지 않는다. test_admin.FakeConn과 가짜 psycopg.connect를 끼운다.
"""
from contextlib import contextmanager

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import auth, db as db_module, main
from backend.app.clients.supabase_admin import AdminError
from backend.app.main import app
from backend.app.routers import admin as admin_router
from backend.app.services import account as account_service
from backend.app.services import customers as svc
from backend.app.services import operators as ops
from backend.app.services import profile as profile_svc
from backend.app.services.account import WithdrawResult
from backend.tests.test_admin import OPERATOR, FakeConn, _login

USERS = {"u1": {"id": "u1", "email": "testuser1@marvia.test", "user_metadata": {}}}
REASON = "고객 전화 문의로 본인 확인이 필요함"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("PII_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    conn = FakeConn()
    ops.grant(conn, OPERATOR, "bootstrap", None)
    monkeypatch.setattr(ops, "open_conn", lambda: conn)
    monkeypatch.setattr(svc, "get_user", lambda user_id: USERS.get(user_id))
    return conn


def _profile(db, user_id="user-123"):
    profile_svc.save(db, user_id, profile_svc.clean("테스트", "010-1111-2222", "서울특별시 중구 세종대로 1", True))


# ── A1: 연결 상한과 실패 기억 ──

class _Conn:
    pass


def _fake_psycopg(monkeypatch, outcomes):
    """psycopg.connect를 가짜로. outcomes에서 차례로 꺼내 예외면 던지고 아니면 연결을 돌려준다. 불린 인자를 기록한다."""
    calls = []

    def connect(conninfo, **kwargs):
        calls.append(kwargs)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return _Conn()

    monkeypatch.setattr(psycopg, "connect", connect)
    return calls


def test_connect_sets_default_timeouts(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    monkeypatch.delenv("DB_CONNECT_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("DB_STATEMENT_TIMEOUT_MS", raising=False)
    calls = _fake_psycopg(monkeypatch, [None, None, None, None])
    db_module.connect()
    assert calls[-1]["connect_timeout"] == 3 and calls[-1]["options"] == "-c statement_timeout=15000"
    db_module.connect(statement_timeout_ms=0)  # 마이그레이션: 문장 상한 없음
    assert "options" not in calls[-1] and calls[-1]["connect_timeout"] == 3
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SEC", "7")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "abc")  # 잘못 적은 값은 기본값
    db_module.connect()
    assert calls[-1]["connect_timeout"] == 7 and calls[-1]["options"] == "-c statement_timeout=15000"
    db_module.connect(url="postgresql://h/db?connect_timeout=9")  # 주소에 적은 값은 덮지 않는다
    assert "connect_timeout" not in calls[-1]


def test_connect_failure_is_remembered_briefly(monkeypatch):
    """한 번 못 붙으면 잠깐 동안은 기다리지 않고 바로 실패한다. 한 요청의 여러 단계가 대기를 반복하지 않는다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    calls = _fake_psycopg(monkeypatch, [psycopg.OperationalError("timeout expired"), None])
    for _ in range(3):
        with pytest.raises(psycopg.OperationalError):
            db_module.connect()
    assert len(calls) == 1  # 두 번째, 세 번째는 붙어 보지도 않았다
    db_module.reset_connection_state()  # 차단 시간이 지난 것과 같다
    db_module.connect()
    assert len(calls) == 2


def test_connect_cooldown_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    monkeypatch.setenv("DB_DOWN_COOLDOWN_SEC", "0")
    calls = _fake_psycopg(monkeypatch, [psycopg.OperationalError("x"), psycopg.OperationalError("x")])
    for _ in range(2):
        with pytest.raises(psycopg.OperationalError):
            db_module.connect()
    assert len(calls) == 2


def test_connect_falls_back_when_pooler_rejects_options(monkeypatch):
    """트랜잭션 풀러가 options 시작 인자를 거절하면 options 없이 다시 붙고, 그 뒤로는 보내지 않는다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    calls = _fake_psycopg(monkeypatch, [psycopg.OperationalError("unsupported startup parameter: options"), None, None])
    db_module.connect()
    db_module.connect()
    assert "options" in calls[0] and "options" not in calls[1] and "options" not in calls[2]


def test_auth_does_not_reconnect_while_db_is_down(monkeypatch):
    """DB가 응답하지 않을 때 로그인 확인이 요청마다 연결 대기를 반복하지 않는다 (10-08 QA A1: 요청마다 25초+)."""
    headers = _login(monkeypatch, "kim@example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    monkeypatch.setattr(main, "_migrate", lambda: None)
    calls = _fake_psycopg(monkeypatch, [psycopg.OperationalError("timeout expired")] * 5)
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/api/me", headers=headers).status_code == 200  # 열린 쪽으로 실패
    assert len(calls) == 1


# ── A3·A4: 탈퇴·정지 계정 ──

def test_withdrawn_user_is_blocked_but_can_finish_withdrawal(db, monkeypatch):
    db.withdrawn.add("user-123")
    headers = _login(monkeypatch, "kim@example.com")
    monkeypatch.setattr("backend.app.routers.account.withdraw", lambda user_id: WithdrawResult(0, True))
    with TestClient(app) as client:
        r = client.get("/api/me", headers=headers)
        assert r.status_code == 403 and r.json()["detail"] == auth.WITHDRAWN
        assert client.get("/api/history", params={"session_id": "s"}, headers=headers).status_code == 403
        assert client.put("/api/profile", json={"name": "a", "phone": "010-1234-5678", "address": "서울특별시 중구",
                                                "consent": True}, headers=headers).status_code == 403
        r = client.delete("/api/account", headers=headers)  # 계정 삭제가 남았으면 마저 끝낼 수 있다
        assert r.status_code == 200 and r.json()["resumed"] is True


def test_suspended_user_may_delete_history_and_account(db, monkeypatch):
    """정지 중에도 개인정보 삭제권(내 기록 삭제·탈퇴)은 막지 않는다 (10-08 QA A4)."""
    db.suspended["user-123"] = ("op", REASON)
    headers = _login(monkeypatch, "kim@example.com")
    deleted = []
    monkeypatch.setattr("backend.app.routers.account.withdraw", lambda user_id: deleted.append(user_id) or WithdrawResult(4, False))
    with TestClient(app) as client:
        assert client.get("/api/me", headers=headers).json()["detail"] == auth.SUSPENDED
        assert client.post("/api/agent", headers=headers, json={"bl_no": "TESTBL01", "session_id": "s"}).status_code == 403
        assert client.delete("/api/history", headers=headers).status_code != 403  # 여기는 DB가 가짜라 503이어도 된다
        r = client.delete("/api/account", headers=headers)
        assert r.status_code == 200 and r.json()["deleted_records"] == 4 and deleted == ["user-123"]


# ── A6: 회원 정보 미입력 ──

def test_llm_routes_need_profile_when_login_is_on(db, monkeypatch):
    headers = _login(monkeypatch, "kim@example.com")
    with TestClient(app) as client:
        for path, body in [("/api/chat", {"message": "안녕", "session_id": "s"}),
                           ("/api/chat/stream", {"message": "안녕", "session_id": "s"}),
                           ("/api/agent", {"bl_no": "TESTBL01", "session_id": "s"})]:
            r = client.post(path, json=body, headers=headers)
            assert r.status_code == 409 and r.json()["detail"] == auth.PROFILE_REQUIRED, path
        # LLM을 부르지 않는 요청은 막지 않는다 (화면이 입력 화면을 띄우려면 /api/me가 돼야 한다)
        me = client.get("/api/me", headers=headers)
        assert me.status_code == 200 and me.json()["profile_complete"] is False


def test_profile_gate_passes_after_input_and_fails_open_without_db(db, monkeypatch):
    _profile(db)
    assert auth._account_block("user-123", ("POST", "/api/chat")) is None
    assert auth._account_block("nobody", ("POST", "/api/document")) == "profile_required"
    assert auth._account_block("nobody", ("GET", "/api/history")) is None

    def broken():
        raise psycopg.OperationalError("down")
    monkeypatch.setattr(ops, "open_conn", broken)
    assert auth._account_block("nobody", ("POST", "/api/chat")) is None  # DB를 못 읽으면 막지 않는다 (/api/me와 같다)


def test_profile_gate_skipped_when_login_is_off(monkeypatch):
    """로그인이 꺼진 환경(평가·키 없는 데모)은 회원 정보를 묻지 않는다."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(ops, "open_conn", lambda: pytest.fail("로그인이 꺼졌으면 계정 확인을 하지 않는다"))
    assert auth.current_user(authorization=None) is None


# ── A7: 정지·해제 순서 ──

def test_suspend_writes_db_before_ban_and_undoes_ban_when_commit_fails(db, monkeypatch):
    order, bans = [], []
    real_execute = db.execute

    def execute(sql, params=()):
        if sql.startswith("INSERT INTO suspended_users"):
            order.append("db")
        return real_execute(sql, params)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(svc, "set_ban", lambda user_id, banned: order.append("ban") or bans.append(banned))
    svc.suspend(db, "u1", "a", OPERATOR, REASON)
    assert order == ["db", "ban"]  # DB 기록 먼저
    db.suspended.clear()

    @contextmanager
    def commit_fails():
        yield
        raise RuntimeError("commit failed")
    monkeypatch.setattr(db, "transaction", commit_fails)
    bans.clear()
    with pytest.raises(RuntimeError):
        svc.suspend(db, "u1", "a", OPERATOR, REASON)
    assert bans == [True, False]  # 확정이 실패하면 Supabase 차단을 되돌린다


def test_ban_failure_rolls_back_db(db, monkeypatch):
    def broken(user_id, banned):
        raise AdminError("failed", 500)
    monkeypatch.setattr(svc, "set_ban", broken)
    with pytest.raises(AdminError):
        svc.suspend(db, "u1", "a", OPERATOR, REASON)
    assert not svc.is_suspended(db, "u1") and db.actions == []
    db.suspended["u1"] = ("a", REASON)
    with pytest.raises(AdminError):
        svc.resume(db, "u1", "a", OPERATOR)
    assert svc.is_suspended(db, "u1")  # 해제도 Supabase가 실패하면 DB는 그대로(다시 누를 수 있다)


def test_resume_repairs_supabase_only_ban(db, monkeypatch):
    """정지 표에는 없는데 Supabase에만 차단이 남은 계정도 해제로 풀 수 있다 (예전 순서의 잔재)."""
    bans = []
    monkeypatch.setattr(svc, "set_ban", lambda user_id, banned: bans.append((user_id, banned)))
    monkeypatch.setattr(svc, "get_user", lambda user_id: {**USERS["u1"], "banned_until": "2125-01-01T00:00:00Z"})
    svc.resume(db, "u1", "a", OPERATOR)
    assert bans == [("u1", False)] and db.actions[-1][0] == "customer_resume"
    monkeypatch.setattr(svc, "get_user", lambda user_id: {**USERS["u1"], "banned_until": None})
    with pytest.raises(svc.CustomerError) as e:
        svc.resume(db, "u1", "a", OPERATOR)
    assert e.value.kind == "not_suspended"


def test_customer_list_shows_supabase_ban(db, monkeypatch):
    monkeypatch.setattr(admin_router, "list_users", lambda: [
        {"id": "u1", "email": "testuser1@marvia.test", "user_metadata": {}, "banned_until": "2125-01-01T00:00:00Z"}])
    headers = _login(monkeypatch, OPERATOR)
    with TestClient(app) as client:
        item = client.get("/api/admin/customers", headers=headers).json()["items"][0]
    assert item["supabase_banned"] is True and item["is_suspended"] is False  # 어긋남이 보인다


# ── A8: 강제 탈퇴 이력 ──

def _withdraw_env(monkeypatch, db, records_fail=False, account_fail=False):
    monkeypatch.setattr(account_service, "connect", lambda: db)

    def records(conn, user_id):
        if records_fail:
            raise RuntimeError("db down")
        return 2
    monkeypatch.setattr(account_service, "delete_user_queries", records)
    for name in ("delete_profile", "forget_user", "delete_conversations", "delete_kakao_link"):
        monkeypatch.setattr(account_service, name, lambda conn, user_id: 0)
    monkeypatch.setattr(account_service.quota, "forget_user", lambda conn, user_id: None)
    monkeypatch.setattr(account_service, "_close_account", lambda conn, user_id: False)

    def delete_account(user_id):
        if account_fail:
            raise AdminError("failed", 500)
    monkeypatch.setattr(account_service, "delete_account", delete_account)


def test_force_delete_records_history_in_same_transaction(db, monkeypatch):
    _withdraw_env(monkeypatch, db, records_fail=True)
    headers = _login(monkeypatch, OPERATOR)
    with TestClient(app) as client:
        r = client.delete("/api/admin/customers/u1", headers=headers)
        assert r.status_code == 503 and "아무것도 바뀌지 않았" in r.json()["detail"]
        assert db.actions == []  # 기록을 못 지웠으면 이력도 없다

        _withdraw_env(monkeypatch, db, account_fail=True)
        r = client.delete("/api/admin/customers/u1", headers=headers)
        # 기록은 지웠고 이력도 남았다. 문구가 그 사실과 같다
        assert r.status_code == 503 and "계정 삭제가 끝나지 않았" in r.json()["detail"]
        assert [a[0] for a in db.actions] == ["customer_delete"]

        _withdraw_env(monkeypatch, db)
        r = client.delete("/api/admin/customers/u1", headers=headers)
        assert r.status_code == 200 and r.json()["deleted_records"] == 2


# ── A9: 운영자 이메일 ──

def test_operator_email_length_and_short_mask():
    with pytest.raises(ops.OperatorError) as e:
        ops.normalize_email("a" * 245 + "@marvia.test")  # 257자
    assert e.value.kind == "invalid_email"
    assert ops.normalize_email("a" * 242 + "@marvia.test")  # 254자는 받는다
    assert ops.mask_email("abc@gmail.com") == "a***@g***.com"  # 3자 아이디는 1자만
    assert ops.mask_email("abcd@gmail.com") == "ab***@g***.com"


def test_long_operator_email_is_400(db, monkeypatch):
    headers = _login(monkeypatch, OPERATOR)
    with TestClient(app) as client:
        r = client.post("/api/admin/operators", json={"email": "a" * 300 + "@marvia.test"}, headers=headers)
    assert r.status_code == 400


# ── A10: API 문서와 보안 헤더 ──

@pytest.mark.parametrize("env, expected", [
    ({}, True),
    ({"RAILWAY_ENVIRONMENT": "production"}, False),
    ({"RAILWAY_ENVIRONMENT_NAME": "production"}, False),
    ({"RAILWAY_ENVIRONMENT": "production", "EXPOSE_DOCS": "true"}, True),
    ({"EXPOSE_DOCS": "off"}, False),
])
def test_docs_enabled(monkeypatch, env, expected):
    for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "EXPOSE_DOCS"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert main.docs_enabled() is expected


def test_docs_routes_disappear_when_closed(monkeypatch):
    monkeypatch.setenv("EXPOSE_DOCS", "0")
    closed = FastAPI(**main._docs_kwargs())
    with TestClient(closed) as client:
        assert all(client.get(p).status_code == 404 for p in ("/docs", "/redoc", "/openapi.json"))


def test_security_headers_on_every_response(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        for r in (client.get("/health"), client.get("/api/nowhere"),
                  client.options("/api/account", headers={"Origin": "http://localhost:3000",
                                                          "Access-Control-Request-Method": "DELETE"})):
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "no-referrer"
