"""10-08 QA E2 회귀 테스트: 선박 자세히 보기 (services/vessel_detail.py, GET /api/vessels/detail). 키·DB 없이 돈다.

- E2-1: 후보 상세 조회를 rules.md vessel_detail_lookups척으로 줄여 부른다
- E2-2: 도구가 실패한 결과는 캐시하지 않고, 외부 호출이 모두 실패하면 하루 횟수를 돌려준다
- E2-3: 같은 배 동시 요청은 외부를 한 번만 부르고 횟수도 한 번만 쓴다
- E2-4: 호출부호 맞춤이 캐시 열쇠와 배 고르기에서 같다, 공백뿐인 값은 400, 응답 선명은 이번 요청 값
- E2-5: 최근 신호 없는 배만 있으면 stale_only
- E2-9: 서버를 다시 띄워도 방금 본 배는 DB 기록으로 횟수를 안 쓴다, 응답 remaining
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import vessel_detail as svc
from backend.app.services.rules import rule

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)  # 인증 꺼짐: 모두 한 몫(anonymous)
    svc._cache.clear()
    svc._inflight.clear()
    svc.reset_daily()
    yield
    svc._cache.clear()
    svc._inflight.clear()
    svc.reset_daily()


def _fake(monkeypatch, *, cands=None, stale_count=0, mof_error=False, cand_error=False, delay=0.0):
    """도구 넷을 가짜로. calls에 부른 도구와 인자를 남긴다."""
    calls = []
    rows = cands if cands is not None else [{"mmsi": 440999001, "name": "TEST SHIP", "call_sign": "TST01", "flag": "KR"}]

    def schedule(**kw):
        calls.append(("mof", kw))
        time.sleep(delay)
        if mof_error:
            return {"error": {"code": "timeout", "message": "느림", "retryable": True}}
        return {"planned_arrival": None, "actual_arrival": None, "facility": "신선대", "control_events": []}

    def candidates(**kw):
        calls.append(("cand", kw))
        if cand_error:
            return {"error": {"code": "timeout", "message": "느림", "retryable": True}}
        return {"candidates": rows, "stale_count": stale_count}

    monkeypatch.setattr(svc, "get_port_schedule", schedule)
    monkeypatch.setattr(svc, "get_vessel_candidates", candidates)
    monkeypatch.setattr(svc, "get_live_position", lambda mmsi: calls.append(("pos", mmsi)) or {
        "mmsi": mmsi, "lat": 33.0, "lng": 128.0, "sog": 12.0, "last_time": (NOW - timedelta(hours=1)).isoformat()})
    monkeypatch.setattr(svc, "estimate_distance", lambda lat, lng, sog: {"distance_nm": 120.0, "calc_hours_at_sog": 10.0})
    monkeypatch.setattr(svc, "route_points", lambda lat, lng: [[lat, lng], [35.1, 129.0]])
    return calls


def _limit(monkeypatch, n):
    real = svc.rule
    monkeypatch.setattr(svc, "rule", lambda key: n if key == "vessel_detail_daily_limit" else real(key))


def _count(calls, kind):
    return len([c for c in calls if c[0] == kind])


# ── E2-1 ──

def test_candidates_called_with_detail_lookups_rule(monkeypatch):
    calls = _fake(monkeypatch)
    svc.detail(None, "TEST SHIP")
    (_, kw), = [c for c in calls if c[0] == "cand"]
    assert kw["detail_limit"] == rule("vessel_detail_lookups") == 1


# ── E2-2 ──

def test_tool_failure_is_not_cached(monkeypatch):
    calls = _fake(monkeypatch, mof_error=True)
    first = svc.detail("TST01", None)
    assert [e["source"] for e in first["errors"]] == ["mof"] and first["vessel"]
    svc.detail("TST01", None)
    assert _count(calls, "cand") == 2  # 실패가 섞인 결과는 캐시하지 않아 다시 부른다


def test_pick_result_is_cached(monkeypatch):
    calls = _fake(monkeypatch, cands=[{"mmsi": 1, "name": "A", "call_sign": "TST01"}, {"mmsi": 2, "name": "B", "call_sign": "TST01"}])
    assert svc.detail("TST01", None)["errors"][0]["code"] == "ambiguous"
    svc.detail("TST01", None)
    assert _count(calls, "cand") == 1  # 못 고른 것은 사실이라 캐시한다


def test_all_external_failed_gives_slot_back(monkeypatch):
    _fake(monkeypatch, mof_error=True, cand_error=True)
    _limit(monkeypatch, 1)
    with TestClient(app) as client:
        r = client.get("/api/vessels/detail", params={"call_sign": "TST01"})
        assert r.status_code == 200 and {e["source"] for e in r.json()["errors"]} == {"mof", "shipfinder"}
        assert r.json()["remaining"] == 1  # 돌려받았다
        assert client.get("/api/vessels/detail", params={"call_sign": "TST02"}).status_code == 200


def test_partial_failure_keeps_slot_used(monkeypatch):
    _fake(monkeypatch, mof_error=True)  # ShipFinder는 받았다
    _limit(monkeypatch, 1)
    with TestClient(app) as client:
        r = client.get("/api/vessels/detail", params={"call_sign": "TST01"})
        assert r.status_code == 200 and r.json()["remaining"] == 0
        assert client.get("/api/vessels/detail", params={"call_sign": "TST02"}).status_code == 429


# ── E2-3 ──

def test_concurrent_same_vessel_calls_tools_once(monkeypatch):
    calls = _fake(monkeypatch, delay=0.3)
    _limit(monkeypatch, 3)
    results, errors = [], []

    def one():
        try:
            results.append(svc.lookup("TST01", None, "u1"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 8
    assert _count(calls, "cand") == 1 and _count(calls, "mof") == 1
    assert svc._daily.get("u1") == 1  # 같이 받은 요청은 횟수를 안 쓴다
    assert not svc._inflight


# ── E2-4 ──

def test_call_sign_key_matches_vessel_pick_normalization(monkeypatch):
    calls = _fake(monkeypatch)
    _limit(monkeypatch, 1)
    with TestClient(app) as client:
        assert client.get("/api/vessels/detail", params={"call_sign": "TST01"}).status_code == 200
        r = client.get("/api/vessels/detail", params={"call_sign": " tst 01 "})
        assert r.status_code == 200  # 같은 배라 캐시에서 답한다 (전에는 429)
    assert _count(calls, "cand") == 1
    assert calls[0][1]["call_sign"] == "TST01"  # 도구에도 맞춘 값을 넘긴다


@pytest.mark.parametrize("params", [{"call_sign": "   "}, {"ship_name": "   "}, {"call_sign": " ", "ship_name": "\t"}])
def test_blank_values_are_400(monkeypatch, params):
    calls = _fake(monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/vessels/detail", params=params).status_code == 400
    assert calls == []


def test_cached_response_echoes_this_request(monkeypatch):
    _fake(monkeypatch)
    svc.detail("TST01", "FIRST NAME")
    again = svc.detail("tst01", "  other   name ")
    assert again["call_sign"] == "TST01" and again["ship_name"] == "other name"


# ── E2-5 ──

def test_only_stale_candidates_is_stale_only(monkeypatch):
    _fake(monkeypatch, cands=[], stale_count=2)
    r = svc.detail("TST01", None)
    assert [e["code"] for e in r["errors"]] == ["stale_only"]
    assert str(rule("vessel_recent_days")) in r["errors"][0]["message"]
    _fake(monkeypatch, cands=[], stale_count=0)
    svc._cache.clear()
    assert [e["code"] for e in svc.detail("TST01", None)["errors"]] == ["not_found"]


# ── E2-9 ──

class _UsageDB:
    """vessel_detail_usage 한 표(횟수 + 최근 본 배)를 흉내 낸다. SQL 문장의 첫 낱말로 갈래를 고른다."""

    def __init__(self):
        self.rows: dict = {}  # (owner, day) -> [count, recent]

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        row = None
        if sql is svc.USAGE_SQL:
            owner, day, limit = params
            cur = self.rows.setdefault((owner, day), [0, {}])
            if cur[0] < limit:
                cur[0] += 1
                row = (cur[0],)
        elif sql is svc.READ_SQL:
            got = self.rows.get(tuple(params))
            row = (got[0], dict(got[1])) if got else None
        elif sql is svc.REMEMBER_SQL:
            owner, day, entry, _ttl = params
            self.rows.setdefault((owner, day), [0, {}])[1].update(json.loads(entry))
        elif sql is svc.GIVE_BACK_SQL:
            cur = self.rows.get(tuple(params))
            if cur and cur[0] > 0:
                cur[0] -= 1
        else:
            raise AssertionError(sql)
        return type("Cur", (), {"fetchone": lambda _self: row})()


def test_recently_seen_vessel_is_free_after_restart(monkeypatch):
    db = _UsageDB()
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(svc, "connect", db)
    calls = _fake(monkeypatch)
    _limit(monkeypatch, 1)
    first = svc.lookup("TST01", None, "u1")
    assert first["remaining"] == 0
    svc._cache.clear()  # 서버를 다시 띄운 것처럼 메모리 캐시를 비운다
    svc.reset_daily()
    again = svc.lookup("tst01", None, "u1")  # 다 썼지만 방금 본 배라 횟수를 안 쓴다
    assert again["vessel"] and again["remaining"] == 0 and _count(calls, "cand") == 2
    with pytest.raises(svc.DailyLimitReached):
        svc.lookup("TST02", None, "u1")  # 다른 배는 여전히 막힌다


def test_recent_record_expires_with_cache_lifetime(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(seconds=svc.CACHE_SEC + 5)).isoformat()
    assert not svc._seen_recently({"TST01": old}, svc._key("TST01", None))
    assert svc._seen_recently({"TST01": datetime.now(timezone.utc).isoformat()}, svc._key("TST01", None))
    assert svc._seen_recently({"name:TEST SHIP": datetime.now(timezone.utc).isoformat()}, svc._key(None, " test  ship "))


def test_remember_sql_prunes_expired_keys():
    # 수명이 지난 열쇠를 지우는 조건과 새 열쇠를 합치는 부분이 문장에 있다 (029 recent 칸이 끝없이 크지 않게)
    assert "make_interval(secs => %s)" in svc.REMEMBER_SQL and "|| EXCLUDED.recent" in svc.REMEMBER_SQL


def test_remaining_reported_on_cache_hit(monkeypatch):
    _fake(monkeypatch)
    _limit(monkeypatch, 3)
    with TestClient(app) as client:
        assert client.get("/api/vessels/detail", params={"call_sign": "TST01"}).json()["remaining"] == 2
        assert client.get("/api/vessels/detail", params={"call_sign": "TST01"}).json()["remaining"] == 2
