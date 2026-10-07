"""ShipFinder 호출 아끼기 테스트 (clients/shipfinder.py). 하루 50회 한도라 같은 값을 다시 부르지 않는지 본다.

가공 fixture(evals/fixtures/shipfinder/)로 키 없이 돈다. 요청 수는 MockTransport가 센다.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from backend.app.clients.shipfinder import ShipFinderClient, position_from_data
from backend.app.services.rules import rule

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "shipfinder"
POS = json.loads((FIXTURES / "position_busan_anchored.json").read_text(encoding="utf-8"))


def _client(calls: list, flag="Panama", fail_after: int | None = None) -> ShipFinderClient:
    """요청마다 calls에 엔드포인트 이름을 쌓는다. fail_after번 뒤부터는 빈도 초과(23)로 답한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if fail_after is not None and len(calls) > fail_after:
            return httpx.Response(200, json={"status": 23, "msg": "rate limited"})
        if request.url.path.endswith("VesselFlagInfo"):
            return httpx.Response(200, json={"status": 0, "msg": "", "data": {"registry": flag}})
        return httpx.Response(200, json=POS)

    return ShipFinderClient(key="test-key", transport=httpx.MockTransport(handler))


def test_position_reused_within_window():
    calls = []
    client = _client(calls)
    first = client.position(440999001)
    second = client.position(440999001)
    assert calls == ["VesselPositionSingle"] and second.fetched_at == first.fetched_at
    assert client.last_cached_at is None  # 방금 받은 값이다. 라우터가 partial로 알리지 않는다


def test_position_fetched_again_after_window():
    calls = []
    client = _client(calls)
    old = datetime.now(timezone.utc) - timedelta(minutes=rule("position_reuse_minutes") + 1)
    ShipFinderClient.cache.set("shipfinder:pos:440999001", position_from_data(POS["data"]), cached_at=old)
    client.position(440999001)
    assert calls == ["VesselPositionSingle"]


def test_failure_fallback_still_serves_cache_after_window():
    # 재사용 기간이 지난 뒤 빈도 초과면 마지막 성공 값으로 대신하고 그 시각을 알린다 (SDD 5.2)
    calls = []
    client = _client(calls, fail_after=0)
    old = datetime.now(timezone.utc) - timedelta(minutes=rule("position_reuse_minutes") + 1)
    ShipFinderClient.cache.set("shipfinder:pos:440999001", position_from_data(POS["data"]), cached_at=old)
    assert client.position(440999001).mmsi == POS["data"]["mmsi"] and client.last_cached_at == old


def test_flag_is_cache_first():
    calls = []
    client = _client(calls)
    assert client.flag(440999001) == "Panama" and client.flag(440999001) == "Panama"
    assert calls == ["VesselFlagInfo"]
    # 기간이 지난 국적은 다시 받는다
    old = datetime.now(timezone.utc) - timedelta(days=rule("flag_cache_ttl_days") + 1)
    ShipFinderClient.cache.set("shipfinder:flag:440999001", "Panama", cached_at=old)
    client.flag(440999001)
    assert calls == ["VesselFlagInfo", "VesselFlagInfo"]


def test_missing_flag_is_asked_again():
    calls = []
    client = _client(calls, flag="")
    assert client.flag(440999001) is None and client.flag(440999001) is None
    assert calls == ["VesselFlagInfo", "VesselFlagInfo"]


def test_candidates_then_live_position_calls_position_once(monkeypatch):
    """한 요청 안의 후보 상세 조회 → 실시간 위치 조회가 같은 배 위치를 두 번 부르지 않는다 (판정·자세히 보기 공통 길)."""
    from mcp_server.tools import live_position, vessel_candidates

    now = int(datetime.now(timezone.utc).timestamp())
    search = {"status": 0, "msg": "", "data": [
        {"match_type": 1, "mmsi": 440999001, "imo": 9900001, "call_sign": "TST01", "ship_name": "TEST STAR", "last_time": now}]}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "VesselSearch":
            return httpx.Response(200, json=search)
        if name == "VesselFlagInfo":
            return httpx.Response(200, json={"status": 0, "msg": "", "data": {"registry": "Korea"}})
        return httpx.Response(200, json=POS)

    client = ShipFinderClient(key="test-key", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(vessel_candidates, "make_client", lambda: client)
    monkeypatch.setattr(live_position, "make_client", lambda: client)
    cands = vessel_candidates.get_vessel_candidates("TEST STAR", call_sign="TST01")["candidates"]
    pos = live_position.get_live_position(cands[0]["mmsi"])
    assert "error" not in pos and pos["cached_at"] is None
    assert sorted(calls) == ["VesselFlagInfo", "VesselPositionSingle", "VesselSearch"]  # 위치는 한 번만
