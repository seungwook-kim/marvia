"""화물 비서 기억 창구 테스트 (routers/memory.py, schemas/memory.py, docs/specs/memory.md "API").

DB 없이 돈다. connect와 서비스 함수를 가짜로 끼운다. B/L은 모의값이다.
"""
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.routers import memory as memory_router
from backend.tests.test_routers import _login

BL = "MOCKBL20261002"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(memory_router, "connect", lambda *a, **k: FakeConn())


@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/memory", None), ("delete", "/api/memory", None), ("delete", "/api/memory/items/c1", None),
])
def test_login_required_everywhere(monkeypatch, db, method, path, body):
    """로그인 꺼진 환경(EC-08)과 헤더 없는 요청은 401. 서비스까지 가지 않는다."""
    monkeypatch.setattr(memory_router.memory, "view", lambda *a: pytest.fail("로그인 없이 읽으면 안 된다"))
    monkeypatch.setattr(memory_router.memory, "delete_all", lambda *a: pytest.fail("로그인 없이 지우면 안 된다"))
    kwargs = {"json": body} if body else {}
    with TestClient(app) as client:
        assert getattr(client, method)(path, **kwargs).status_code == 401  # 로그인 꺼짐
    _login(monkeypatch)
    with TestClient(app) as client:
        assert getattr(client, method)(path, **kwargs).status_code == 401  # 로그인 켜짐, 헤더 없음


def test_view_hides_raw_bl_and_key(monkeypatch, db):
    headers = _login(monkeypatch)
    row = {"cargo_key": "secret-key", "bl_masked": "MOC*******1002", "bl_type": "HOUSE", "label": "고추",
           "status": "active", "sale_due_date": None, "forwarder_eta": None, "original_bl": "required",
           "to_order_bank": None, "freight_term": "collect", "incoterm": None, "last_verdict": "watch",
           "last_checked_at": None, "last_days_margin": 3, "last_eta": None, "last_stage": "반입신고",
           "pending": [{"field": "sale_due_date", "value": "2026-10-03", "noted_at": "2026-10-02"}],
           "vessel_mmsi": 440999001}
    item = {"id": "c1", "section": "cargo", "cargo_key": "secret-key", "kind": "todo",
            "fields": {"text": "은행에서 원본 B/L 받기", "done": False}, "noted_at": "2026-09-28"}
    monkeypatch.setattr(memory_router.memory, "view", lambda conn, user_id: {
        "markdown": "# 화물별\n## 고추 (MOC*******1002)\n- 할 일: 은행에서 원본 B/L 받기 [ ] (9/28)",
        "items": [item], "cargo": [row], "full": True, "pruned_count": 2, "pruned_at": None, "can_undo": True})
    with TestClient(app) as client:
        body = client.get("/api/memory", headers=headers).json()
    assert body["available"] is True and body["full"] is True
    assert body["used_chars"] == len(body["markdown"]) and body["max_chars"] == 3000  # 메모장 N/3000자 (M5)
    assert body["items"][0]["cargo_name"] == "고추 (MOC*******1002)" and "[c1]" not in body["markdown"]
    assert body["cargo"][0]["pending"][0]["value"] == "2026-10-03"
    assert body["pruned_count"] == 2 and body["can_undo"] is True  # 10-08 QA F11·F12
    # 화물 지문은 확인 대기 적용·무시 주소용 cargo_ref 칸에만 있다 (F5). 원본 B/L·MMSI는 여전히 없다
    assert body["cargo"][0]["cargo_ref"] == "secret-key"
    text = str({**body, "cargo": [{k: v for k, v in c.items() if k != "cargo_ref"} for c in body["cargo"]]})
    assert "secret-key" not in text and BL not in text and "440999001" not in text


def test_undo_endpoint(monkeypatch, db):
    """10-08 QA F12: 되돌리기. 직전 버전이 없으면 409, DB 실패는 503."""
    headers = _login(monkeypatch)
    answers = iter(["restored", "none"])
    monkeypatch.setattr(memory_router.memory, "undo", lambda conn, user_id: next(answers))
    with TestClient(app) as client:
        assert client.post("/api/memory/undo", headers=headers).json() == {"restored": True}
        assert client.post("/api/memory/undo", headers=headers).status_code == 409
        assert client.post("/api/memory/undo").status_code == 401


def test_pending_endpoints(monkeypatch, db):
    """10-08 QA F5: 확인 대기 적용·무시. 없는 대기 404, 검사에 걸린 값 422."""
    headers = _login(monkeypatch)
    seen = []

    def fake(conn, user_id, key, field, *, apply, today):
        seen.append((user_id, key, field, apply))
        return {"label": "applied" if apply else "dismissed", "sale_due_date": "invalid"}.get(field, "missing")
    monkeypatch.setattr(memory_router.memory, "resolve_pending", fake)
    with TestClient(app) as client:
        assert client.post("/api/memory/cargo/k1/pending/label/apply", headers=headers).json() == {"result": "applied"}
        assert client.delete("/api/memory/cargo/k1/pending/label", headers=headers).json() == {"result": "dismissed"}
        assert client.post("/api/memory/cargo/k1/pending/sale_due_date/apply", headers=headers).status_code == 422
        assert client.post("/api/memory/cargo/k1/pending/forwarder_eta/apply", headers=headers).status_code == 404
        assert client.delete("/api/memory/cargo/k1/pending/label").status_code == 401
    assert seen[0] == ("user-123", "k1", "label", True) and seen[1][3] is False


def test_view_without_db_is_unavailable(monkeypatch):
    headers = _login(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        body = client.get("/api/memory", headers=headers).json()
    assert body["available"] is False and body["items"] == []


def test_delete_all_and_one(monkeypatch, db):
    headers = _login(monkeypatch)
    asked = []
    # 메모장 전부 지우기는 메모장만 지운다. 화물 표를 지우는 forget_user는 부르지 않는다
    monkeypatch.setattr(memory_router.memory, "forget_user", lambda *a: pytest.fail("화물 표는 지우지 않는다"))
    monkeypatch.setattr(memory_router.memory, "delete_all", lambda conn, user_id: asked.append(user_id) or 3)
    status = {"c1": "deleted", "c9": "missing", "c5": "conflict"}
    monkeypatch.setattr(memory_router.memory, "delete_item", lambda conn, user_id, item_id: status[item_id])
    with TestClient(app) as client:
        assert client.delete("/api/memory", headers=headers).json() == {"deleted": 3}
        assert client.delete("/api/memory/items/c1", headers=headers).json() == {"deleted": 1}
        assert client.delete("/api/memory/items/c9", headers=headers).status_code == 404
        assert client.delete("/api/memory/items/c5", headers=headers).status_code == 503  # 동시 수정 충돌은 404 아님
    assert asked == ["user-123"]


def test_delete_failure_is_not_hidden(monkeypatch):
    headers = _login(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        assert client.delete("/api/memory", headers=headers).status_code == 503

    def broken(*a, **k):
        raise ConnectionError("테스트")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(memory_router, "connect", broken)
    with TestClient(app) as client:
        assert client.delete("/api/memory/items/c1", headers=headers).status_code == 503


def test_confirm_screen_endpoint_is_gone(monkeypatch, db):
    """확인 화면용 POST /api/memory/document-facts는 없다. 자동 저장은 tests/test_doc_facts.py가 본다."""
    headers = _login(monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/memory/document-facts", json={"bl_no": BL}, headers=headers).status_code in (404, 405)
