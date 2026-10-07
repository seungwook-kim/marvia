"""B/L 구분 자동 재조회와 관련 보강 테스트.

- routers/agent._fetch_customs: 구분을 모르면 하우스, 마스터 순서로, 정했으면 그 구분 먼저 조회한다. 둘 다 없으면 failed
- services/chat._cargo: 어느 구분으로 찾았는지 코드가 쓴 한 줄 (found_type_note)
- services/cargo.upsert_assessment: 찾은 구분과 신뢰 ETA 시각(last_eta_at, 024)을 적는다
- services/vessel_detail.take_daily_slot: DB가 있으면 표에 센다 (024)
- worker/collector: 받아 둔 위치를 다시 쓰는 배는 예산을 쓰지 않는다
키·DB 없이 돈다. B/L은 모의값이다(실제 번호 아님).
"""
from datetime import date, datetime, timezone

import pytest

from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest, EtaInfo
from backend.app.schemas.customs import CustomsRecord
from backend.app.services import cargo
from backend.app.services import chat as chat_svc
from backend.app.services import vessel_detail
from backend.app.services.customs import CustomsError
from backend.tests.test_cargo import FakeConn, bl_key  # noqa: F401 - bl_key는 픽스처

BL = "MOCKBL20261008"  # 모의 B/L
NOW = datetime(2026, 10, 8, 3, tzinfo=timezone.utc)


def _record() -> CustomsRecord:
    return CustomsRecord(ship_name="TEST STAR", ship_nat="PA", stage_name="반입신고", bl_masked="MOC*******1008",
                         fetched_at=NOW)


def _unipass(monkeypatch, found: dict[str, object]) -> list[str]:
    """가짜 UNI-PASS. found는 {구분: 레코드 | None | 예외}. 부른 구분을 순서대로 돌려준다."""
    calls: list[str] = []

    class FakeUnipass:
        def __init__(self, *a, **k):
            pass

        def fetch(self, bl_no, bl_type="MASTER", bl_year=None):
            calls.append(bl_type)
            value = found.get(bl_type)
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(agent_router, "UnipassClient", FakeUnipass)
    return calls


def _req(**over) -> AgentRequest:
    return AgentRequest(**{"bl_no": BL, "session_id": "s1", **over})


# ── 조회 순서 (routers/agent._fetch_customs) ──

def test_unknown_type_house_miss_then_master_hit(monkeypatch):
    calls = _unipass(monkeypatch, {"HOUSE": None, "MASTER": _record()})
    record, failure, used = agent_router._fetch_customs(_req())
    assert calls == ["HOUSE", "MASTER"] and failure is None and used == "MASTER" and record is not None


def test_stated_master_miss_then_house_hit(monkeypatch):
    calls = _unipass(monkeypatch, {"MASTER": None, "HOUSE": _record()})
    _, failure, used = agent_router._fetch_customs(_req(bl_type="MASTER"))
    assert calls == ["MASTER", "HOUSE"] and failure is None and used == "HOUSE"


def test_known_type_hit_is_one_call(monkeypatch):
    calls = _unipass(monkeypatch, {"MASTER": _record()})
    _, failure, used = agent_router._fetch_customs(_req(bl_type="MASTER"))
    assert calls == ["MASTER"] and failure is None and used == "MASTER"


def test_both_miss_is_failed_after_exactly_two_calls(monkeypatch):
    calls = _unipass(monkeypatch, {"HOUSE": None, "MASTER": None})
    response = agent_router._assess(_req())
    assert calls == ["HOUSE", "MASTER"]
    assert response.status == "failed" and response.missing_inputs == [] and response.answer == agent_router.NO_CARGO
    assert agent_router.is_not_found(response) and response.bl_type_used is None


def test_timeout_on_first_try_fails_without_second_call(monkeypatch):
    calls = _unipass(monkeypatch, {"HOUSE": CustomsError("timeout", "느림"), "MASTER": _record()})
    response = agent_router._assess(_req())
    assert calls == ["HOUSE"]  # 장애를 "없음"으로 덮지 않는다
    assert response.status == "failed" and not agent_router.is_not_found(response)


def test_run_agent_reports_and_stores_matched_type(monkeypatch):
    """찾은 구분이 응답(bl_type_used)과 화물 표에 남길 요청(remember_later) 둘 다에 간다."""
    _unipass(monkeypatch, {"HOUSE": None, "MASTER": _record()})
    monkeypatch.setattr(agent_router, "_after_customs", lambda request, customs, memory=None:
                        agent_router._response(status="ok", answer="판정"))
    stored = []
    monkeypatch.setattr(agent_router.memory_service, "remember_later",
                        lambda user_id, request, response, released: stored.append(request))
    response = agent_router.run_agent(_req(), user_id=None)
    assert response.status == "ok" and response.bl_type_used == "MASTER"
    assert stored[0].bl_type == "MASTER"


def test_run_agent_tries_saved_type_first(monkeypatch):
    """화물 표에 지난번 찾은 구분이 있으면 그것부터 본다. 한 번에 찾으면 UNI-PASS는 한 번이다."""
    calls = _unipass(monkeypatch, {"MASTER": _record()})
    monkeypatch.setattr(agent_router, "_saved_row", lambda user_id, bl_no: {"bl_type": "MASTER"})
    monkeypatch.setattr(agent_router, "_after_customs", lambda request, customs, memory=None:
                        agent_router._response(status="ok", answer="판정"))
    monkeypatch.setattr(agent_router.memory_service, "remember_later", lambda *a, **k: None)
    assert agent_router.run_agent(_req(), user_id=None).bl_type_used == "MASTER" and calls == ["MASTER"]


def test_type_order():
    assert agent_router.bl_type_order(None) == ["HOUSE", "MASTER"]
    assert agent_router.bl_type_order("MASTER") == ["MASTER", "HOUSE"]
    assert agent_router.bl_type_order("HOUSE") == ["HOUSE", "MASTER"]


# ── 채팅 안내 (services/chat.found_type_note, _cargo) ──

def test_found_type_note():
    assert chat_svc.found_type_note(None, None, "MASTER") == "(마스터 B/L로 찾았어요.)"
    assert chat_svc.found_type_note("HOUSE", None, "MASTER") == "(말씀하신 하우스 B/L로는 없어 마스터 B/L로 찾았어요.)"
    assert chat_svc.found_type_note("HOUSE", None, "HOUSE") is None  # 말한 대로 찾음
    assert chat_svc.found_type_note(None, "MASTER", "MASTER") is None  # 기억한 구분 그대로
    assert chat_svc.found_type_note(None, None, None) is None  # 못 찾음


def _run(used):
    seen = []

    def run(req):
        seen.append(req)
        return agent_router._response(status="ok", answer="괜찮습니다", bl_type_used=used)
    return seen, run


def test_chat_cargo_says_which_type_matched_and_keeps_it_in_context(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    seen, run = _run("MASTER")
    result = chat_svc._cargo(BL, None, None, "s1", None, run, note=None)
    assert seen[0].bl_type is None  # 구분 모름으로 넘긴다
    assert result.reply == "괜찮습니다\n(마스터 B/L로 찾았어요.)"
    assert result.request.bl_type == "MASTER"  # 다음 문장의 문맥은 찾은 구분


def test_chat_cargo_stated_type_but_other_matched(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    seen, run = _run("MASTER")
    result = chat_svc._cargo(BL, "HOUSE", None, "s1", None, run, note=None)
    assert seen[0].bl_type == "HOUSE" and "말씀하신 하우스 B/L로는 없어 마스터 B/L로 찾았어요." in result.reply


def test_chat_cargo_no_note_when_context_type_matched(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    seen, run = _run("MASTER")
    ctx = AgentRequest(bl_no=BL, bl_type="MASTER", session_id="s1")
    result = chat_svc._cargo(BL, None, None, "s1", ctx, run, note=None)
    assert seen[0].bl_type == "MASTER" and result.reply == "괜찮습니다"


def test_chat_not_found_uses_new_sentence(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = chat_svc._cargo(BL, None, None, "s1", None,
                             lambda req: agent_router._response(status="failed", answer=agent_router.NO_CARGO), note=None)
    assert result.reply == chat_svc.NO_RESULT_REPLY and "둘 다" in result.reply
    assert chat_svc.row_need(result.agent) == (chat_svc.ROW_NOTE_NOT_FOUND, chat_svc.ROW_SAY_NOT_FOUND)


# ── 화물 표 (services/cargo.upsert_assessment) ──

def _values(conn: FakeConn) -> dict:
    sql, params = conn.calls[0]
    names = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    return dict(zip(names, params))


def test_upsert_stores_matched_type_and_eta_time(bl_key):  # noqa: F811
    trusted = datetime(2026, 10, 10, 5, 30, tzinfo=timezone.utc)
    response = agent_router._response(status="ok", bl_type_used="MASTER",
                                      eta=EtaInfo(ais_eta=None, mof_eta=None, calc_eta=None, trusted_eta=trusted, distance_nm=None))
    conn = FakeConn()
    assert cargo.upsert_assessment(conn, "u1", _req(), response, released=False, now=NOW)
    values = _values(conn)
    assert values["bl_type"] == "MASTER" and values["last_eta_at"] == trusted and values["last_eta"] == date(2026, 10, 10)
    assert "bl_type = COALESCE(EXCLUDED.bl_type, user_cargo.bl_type)" in conn.calls[0][0]
    assert "last_eta_at = EXCLUDED.last_eta_at" in conn.calls[0][0]


def test_cargo_line_without_type():
    assert "B/L" not in cargo.cargo_line({"bl_masked": "MOC*******1008", "bl_type": None}).replace("MOC*******1008", "")


def test_migration_024():
    from pathlib import Path
    text = (Path(__file__).parents[1] / "app/db/migrations/024_eta_time_bl_type_vessel_usage.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS last_eta_at TIMESTAMPTZ" in text
    assert "ALTER COLUMN bl_type DROP NOT NULL" in text
    assert "CREATE TABLE IF NOT EXISTS vessel_detail_usage" in text and "PRIMARY KEY (user_key, day)" in text


# ── 선박 자세히 보기 하루 횟수 (services/vessel_detail.take_daily_slot) ──

class _UsageConn:
    """vessel_detail_usage 한 표를 흉내 낸다. 문장 하나로 더하고, 상한이면 줄을 돌려주지 않는다 (USAGE_SQL과 같은 뜻)."""

    def __init__(self, store: dict, fail: bool = False):
        self.store, self.fail = store, fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        if self.fail:
            raise RuntimeError("db down")
        assert "INSERT INTO vessel_detail_usage" in sql and "RETURNING count" in sql
        owner, day, limit = params
        now = self.store.get((owner, day), 0)
        row = None
        if now < limit:
            self.store[(owner, day)] = now + 1
            row = (now + 1,)
        return type("Cur", (), {"fetchone": lambda _self: row})()


def _limit(monkeypatch, n):
    real = vessel_detail.rule
    monkeypatch.setattr(vessel_detail, "rule", lambda key: n if key == "vessel_detail_daily_limit" else real(key))


def test_daily_slot_counts_in_db_and_survives_restart(monkeypatch):
    store: dict = {}
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(vessel_detail, "connect", lambda: _UsageConn(store))
    _limit(monkeypatch, 2)
    vessel_detail.reset_daily()
    assert vessel_detail.take_daily_slot("u1") and vessel_detail.take_daily_slot("u1")
    vessel_detail.reset_daily()  # 서버를 다시 띄운 것처럼 메모리를 지워도
    assert not vessel_detail.take_daily_slot("u1")  # 표에 센 값이 남아 있다
    assert vessel_detail.take_daily_slot(None)  # 로그인 없음은 따로 센다
    assert list(store.values()) == [2, 1]


def test_daily_slot_falls_back_to_memory_on_db_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(vessel_detail, "connect", lambda: _UsageConn({}, fail=True))
    _limit(monkeypatch, 1)
    vessel_detail.reset_daily()
    assert vessel_detail.take_daily_slot("u1") and not vessel_detail.take_daily_slot("u1")


# ── 워커 위치 수집 예산 (worker/collector._spend) ──

def test_reused_positions_do_not_take_budget():
    from backend.tests.test_worker_collector import _FakeSF, _WatchConn
    from worker.collector import Budget, collect_watch_positions
    conn = _WatchConn([1, 2, 3])
    budget = Budget(1)
    result = collect_watch_positions(conn, _FakeSF(reusable={1, 3}), budget)
    assert result["watch_positions_new"] == 3 and budget.remaining == 0  # 1·3은 다시 쓰고, 2만 예산을 썼다


def test_position_reusable_uses_client_window(monkeypatch):
    from backend.app.clients import shipfinder
    from backend.app.clients.cache import ResponseCache
    client = shipfinder.ShipFinderClient.__new__(shipfinder.ShipFinderClient)
    client.cache = ResponseCache()
    assert not client.position_reusable(440999001)
    client.cache.set("shipfinder:pos:440999001", {"lat": 1})
    assert client.position_reusable(440999001)
    monkeypatch.setattr(shipfinder, "position_reuse_window", lambda: shipfinder.timedelta(0))
    assert not client.position_reusable(440999001)  # 다시 쓰기를 끄면 늘 부른다


@pytest.mark.parametrize("bl_type", [None, "MASTER", "HOUSE"])
def test_request_accepts_unknown_type(bl_type):
    assert AgentRequest(bl_no=BL, bl_type=bl_type, session_id="s1").bl_type == bl_type
