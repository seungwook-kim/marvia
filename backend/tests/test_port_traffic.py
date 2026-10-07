"""부산항 입항 추세 (IMF PortWatch) 테스트. 키·네트워크 없이 가공 fixture로 돈다.

- 클라이언트: 응답 파싱(epoch·문자열 날짜), ArcGIS 오류 모양, 타임아웃 → PortWatchError
- 서비스: 7일/28일 평균, 비율과 이름(rules.md 경계), 캐시, 실패 시 마지막 성공값(partial)
- 라우터: GET /api/port/busan/traffic이 로그인 없이 200
"""
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.clients import portwatch, retry
from backend.app.clients.portwatch import DailyPortCalls, PortWatchClient, PortWatchError, parse_day, parse_features
from backend.app.services import port_traffic
from backend.app.services.rules import rule

FIXTURE = json.loads(portwatch.FIXTURE.read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """테스트마다 캐시를 비우고 재시도 대기를 없앤다 (retry.BACKOFF_SEC)."""
    monkeypatch.setattr(retry, "BACKOFF_SEC", 0)
    monkeypatch.delenv("REPLAY_MODE", raising=False)
    port_traffic.reset_cache()
    yield
    port_traffic.reset_cache()


def _client(handler) -> PortWatchClient:
    return PortWatchClient(transport=httpx.MockTransport(handler))


def _fixture_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=FIXTURE)


def test_parse_day_handles_epoch_ms_and_strings():
    assert parse_day(1790726400000) == date(2026, 9, 30)  # 2026-09-30 00:00 UTC
    assert parse_day("2026-09-30") == date(2026, 9, 30)
    assert parse_day("2026-09-30 00:00:00") == date(2026, 9, 30)
    assert parse_day("1790726400000") == date(2026, 9, 30)
    assert parse_day("not a date") is None
    assert parse_day(None) is None
    assert parse_day(True) is None


def test_parse_features_sorts_newest_first_and_skips_bad_rows():
    body = {"features": [
        {"attributes": {"date": "2026-09-01", "portcalls_container": 30}},
        {"attributes": {"date": "garbage", "portcalls_container": 99}},
        {"attributes": {"date": "2026-09-03", "portcalls_container": "x", "portcalls": -1}},
        {"nope": 1},
    ]}
    rows = parse_features(body)
    assert [r.day for r in rows] == [date(2026, 9, 3), date(2026, 9, 1)]
    assert rows[0].portcalls_container is None and rows[0].portcalls is None


def test_arcgis_error_body_is_failure():
    with pytest.raises(PortWatchError) as e:
        parse_features({"error": {"code": 400, "message": "Invalid query"}})
    assert e.value.code == "error"


def test_client_sends_busan_query_and_parses_fixture():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=FIXTURE)

    rows, fetched_at = _client(handler).daily("port1065", 28)
    assert seen["where"] == "portid='port1065'"
    assert seen["orderByFields"] == "date DESC" and seen["resultRecordCount"] == "28" and seen["f"] == "json"
    assert len(rows) == 28 and rows[0].day == date(2026, 9, 30)
    assert fetched_at.tzinfo is not None


def test_client_timeout_retries_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(PortWatchError) as e:
        _client(handler).daily("port1065", 28)
    assert e.value.code == "timeout" and e.value.retryable
    assert len(calls) == 3  # 최초 1회 + 재시도 2회 (SDD 8절)


def test_client_non_json_is_error():
    with pytest.raises(PortWatchError):
        _client(lambda r: httpx.Response(200, text="<html>")).daily("port1065", 28)


def test_replay_mode_reads_fixture(monkeypatch):
    monkeypatch.setenv("REPLAY_MODE", "true")
    rows, _ = PortWatchClient().daily("port1065", 28)
    assert rows[0].portcalls_container == 41


def test_summarize_fixture_numbers():
    rows, _ = _client(_fixture_handler).daily("port1065", 28)
    out = port_traffic.summarize(rows, NOW, today=date(2026, 10, 7))
    # fixture: 최근 7일 합 280(평균 40), 28일 합 996(평균 35.57)
    assert out["avg7"] == 40.0
    assert out["avg28"] == 35.6
    assert out["ratio"] == round(280 / 7 / (996 / 28), 3)
    assert out["level"] == "normal"
    assert out["latest_date"] == "2026-09-30" and out["lag_days"] == 7
    assert out["source"] == "imf_portwatch" and out["attribution"] == "IMF PortWatch"
    assert out["fetched_at"] == NOW.isoformat()


def _rows(recent: int, older: int) -> list[DailyPortCalls]:
    start = date(2026, 9, 30)
    return [DailyPortCalls(start - timedelta(days=i), None, recent if i < 7 else older, None, None) for i in range(28)]


def test_level_follows_rules_thresholds():
    busy = port_traffic.summarize(_rows(50, 30), NOW)
    quiet = port_traffic.summarize(_rows(20, 40), NOW)
    assert busy["ratio"] >= rule("port_traffic_busy_ratio") and busy["level"] == "busy"
    assert quiet["ratio"] <= rule("port_traffic_quiet_ratio") and quiet["level"] == "quiet"
    assert port_traffic.classify(None) is None


def test_missing_container_counts_are_skipped_not_zero():
    rows = _rows(40, 40)
    rows[0] = DailyPortCalls(rows[0].day, None, None, None, None)
    out = port_traffic.summarize(rows, NOW)
    assert out["avg7"] == 40.0 and out["days_used"] == 27


def test_cache_means_one_upstream_call():
    calls = []

    def factory():
        calls.append(1)
        return _client(_fixture_handler)

    first = port_traffic.get_busan_traffic(now=NOW, client_factory=factory)
    second = port_traffic.get_busan_traffic(now=NOW + timedelta(hours=11), client_factory=factory)
    assert first["status"] == second["status"] == "ok"
    assert len(calls) == 1
    port_traffic.get_busan_traffic(now=NOW + timedelta(hours=float(rule("port_traffic_cache_hours")) + 1),
                                   client_factory=factory)
    assert len(calls) == 2


def test_failure_after_success_returns_last_value_as_partial():
    port_traffic.get_busan_traffic(now=NOW, client_factory=lambda: _client(_fixture_handler))

    def broken():
        return _client(lambda r: httpx.Response(503))

    later = NOW + timedelta(hours=13)
    out = port_traffic.get_busan_traffic(now=later, client_factory=broken)
    assert out["status"] == "partial" and out["error"]["code"] == "error"
    assert out["avg7"] == 40.0  # 마지막 성공값 그대로

    # 실패 직후에는 다시 부르지 않는다 (rules.md port_traffic_retry_minutes)
    calls = []

    def counting():
        calls.append(1)
        return _client(_fixture_handler)

    again = port_traffic.get_busan_traffic(now=later + timedelta(minutes=1), client_factory=counting)
    assert again["status"] == "partial" and calls == []


def test_failure_without_success_is_unavailable():
    def broken():
        return _client(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)))

    out = port_traffic.get_busan_traffic(now=NOW, client_factory=broken)
    assert out["status"] == "unavailable"
    assert "avg7" not in out and out["source"] == "imf_portwatch"


def test_empty_features_is_unavailable():
    out = port_traffic.get_busan_traffic(now=NOW, client_factory=lambda: _client(lambda r: httpx.Response(200, json={"features": []})))
    assert out["status"] == "unavailable" and out["error"]["code"] == "empty"


def test_router_returns_200_without_login(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REPLAY_MODE", "true")  # 위로 나가지 않고 fixture를 읽는다
    from backend.app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/port/busan/traffic")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["level"] == "normal" and body["latest_date"] == "2026-09-30"


def test_router_failure_is_still_200(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from backend.app.main import app

    def boom():
        raise PortWatchError("timeout", "slow")

    # 기본 인자 client_factory는 정의 때 묶이므로 함수를 감싸 바꾼다
    original = port_traffic.get_busan_traffic
    monkeypatch.setattr(port_traffic, "get_busan_traffic", lambda: original(client_factory=boom))
    with TestClient(app) as client:
        resp = client.get("/api/port/busan/traffic")
    assert resp.status_code == 200 and resp.json()["status"] == "unavailable"


def test_verdict_side_does_not_import_port_traffic():
    """참고 맥락이라 판정 재료가 아니다. 판정 코드가 이 모듈을 쓰지 않는지 본다."""
    from pathlib import Path

    services = Path(port_traffic.__file__).parent
    for name in ("verdict.py", "signals.py", "validate.py"):
        text = (services / name).read_text(encoding="utf-8")
        assert "port_traffic" not in text and "portwatch" not in text, name


# ── 10-08 QA E2-8 ──

def test_partial_lag_days_is_recounted_from_now():
    """partial로 돌려주는 지난 값의 lag_days는 받은 날이 아니라 지금 기준이다."""
    first = port_traffic.get_busan_traffic(now=NOW, client_factory=lambda: _client(_fixture_handler))
    later = NOW + timedelta(days=3)
    out = port_traffic.get_busan_traffic(now=later, client_factory=lambda: _client(lambda r: httpx.Response(429)))
    assert out["status"] == "partial" and out["lag_days"] == first["lag_days"] + 3


def test_now_argument_sets_lag_days():
    out = port_traffic.get_busan_traffic(now=datetime(2026, 10, 10, 3, tzinfo=timezone.utc),
                                         client_factory=lambda: _client(_fixture_handler))
    assert out["latest_date"] == "2026-09-30" and out["lag_days"] == 10


def test_future_and_duplicate_days_are_dropped():
    rows = _rows(40, 40)
    rows.append(DailyPortCalls(date(2030, 1, 1), None, 999, None, None))  # 미래
    rows += [DailyPortCalls(date(2026, 9, 30), None, 999, None, None)] * 5  # 같은 날 반복
    out = port_traffic.summarize(rows, NOW, today=date(2026, 10, 7))
    assert out["latest_date"] == "2026-09-30" and out["lag_days"] == 7
    assert out["days_used"] == 28 and out["avg28"] < 100


def test_concurrent_requests_get_stale_value_while_one_thread_refreshes():
    """캐시가 지났을 때 원천 호출은 한 스레드만 하고, 그동안 다른 요청은 지난 값을 바로 받는다."""
    port_traffic.get_busan_traffic(now=NOW, client_factory=lambda: _client(_fixture_handler))
    later = NOW + timedelta(hours=float(rule("port_traffic_cache_hours")) + 1)
    calls, started, release = [], threading.Event(), threading.Event()

    def slow(request):
        calls.append(1)
        started.set()
        release.wait(5)
        return httpx.Response(200, json=FIXTURE)

    out = {}
    leader = threading.Thread(target=lambda: out.setdefault("leader", port_traffic.get_busan_traffic(
        now=later, client_factory=lambda: _client(slow))))
    leader.start()
    assert started.wait(5)
    t0 = time.monotonic()
    follower = port_traffic.get_busan_traffic(now=later, client_factory=lambda: _client(slow))
    assert time.monotonic() - t0 < 1 and follower["status"] == "ok" and follower["avg7"] == 40.0
    release.set()
    leader.join(5)
    assert out["leader"]["status"] == "ok" and len(calls) == 1


def test_concurrent_first_requests_share_one_upstream_call():
    """받은 적이 없을 때 동시에 온 요청은 한 호출의 결과를 같이 기다린다 (원천 호출은 한 클라이언트 몫뿐)."""
    calls = []

    def slow(request):
        calls.append(1)
        time.sleep(0.3)
        return httpx.Response(503)

    results = []
    threads = [threading.Thread(target=lambda: results.append(port_traffic.get_busan_traffic(
        now=NOW, client_factory=lambda: _client(slow)))) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(results) == 6 and all(r["status"] == "unavailable" for r in results)
    assert len(calls) == 3  # 최초 1 + 재시도 2 (SDD 8절). 요청 6개가 각자 부르지 않았다
