"""10-08 QA E2 회귀 테스트: MCP 도구 입력 검사·호출 수·오류 모양. 키 없이 가공 응답으로 돈다.

- E2-1: 후보 상세 조회 수(detail_limit, rules.md vessel_candidate_lookups), 호출부호가 여러 척이면 상세 조회 안 함
- E2-5: 최근 신호가 없어 뺀 배 수(stale_count)
- E2-6: ShipFinder "Key time-out" 문구 → bad_key, 영문 원문을 내보내지 않음
- E2-7: 긴 입력·InvalidURL·MMSI 범위·음수 속력·없는 도구·route_failed 문구
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.app.clients.shipfinder import ShipFinderClient, ShipFinderError
from backend.app.services import mcp_client
from backend.app.services.rules import rule
from mcp_server.tools import distance, vessel_candidates
from mcp_server.tools.vessel_candidates import get_vessel_candidates


def _ts(days_ago: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())


def _hit(mmsi: int, call_sign: str, imo: int, days_ago: float, name: str = "QA SHIP") -> dict:
    return {"match_type": 1, "mmsi": mmsi, "imo": imo, "call_sign": call_sign, "ship_name": name, "last_time": _ts(days_ago)}


@pytest.fixture
def counted(monkeypatch):
    """검색 결과를 주고 ShipFinder 엔드포인트별 호출 수를 센다."""
    calls: Counter = Counter()

    def _use(rows: list[dict]) -> Counter:
        def handler(request: httpx.Request) -> httpx.Response:
            endpoint = request.url.path.rsplit("/", 1)[-1]
            calls[endpoint] += 1
            if endpoint == "VesselSearch":
                return httpx.Response(200, json={"status": 0, "msg": "", "data": rows})
            if endpoint == "VesselFlagInfo":
                return httpx.Response(200, json={"status": 0, "msg": "", "data": {"registry": "Panama"}})
            mmsi = int(request.url.params["mmsi"])
            return httpx.Response(200, json={"status": 0, "msg": "", "data": {
                "mmsi": mmsi, "lat": 33.0, "lng": 128.0, "sog": 10, "cog": 90, "last_time": _ts(0.01), "dest": "BUSAN"}})

        client = ShipFinderClient(key="test-key", transport=httpx.MockTransport(handler))
        monkeypatch.setattr(vessel_candidates, "make_client", lambda: client)
        return calls

    return _use


def test_detail_limit_one_looks_up_only_top_candidate(counted):
    """자세히 보기는 1척만 상세 조회한다 (E2-1: 전에는 선명 하나로 11회)."""
    calls = counted([_hit(440000080 + i, f"N{i}", 9700000 + i, 1, name=f"QA NAME{i}") for i in range(7)])
    r = get_vessel_candidates("QA NAME0", detail_limit=1)
    assert len(r["candidates"]) == 7 and r["candidates"][0]["flag"] == "Panama"
    assert all(c["flag"] is None for c in r["candidates"][1:])
    assert calls == Counter({"VesselSearch": 1, "VesselPositionSingle": 1, "VesselFlagInfo": 1})


def test_default_lookups_follow_rule(counted):
    calls = counted([_hit(440000080 + i, f"N{i}", 9700000 + i, 1) for i in range(7)])
    get_vessel_candidates("QA SHIP")
    n = rule("vessel_candidate_lookups")
    assert calls["VesselPositionSingle"] == n and calls["VesselFlagInfo"] == n


def test_ambiguous_call_sign_skips_detail_lookups(counted):
    """같은 호출부호로 최근 신호를 보낸 배가 둘이면 아무도 고르지 않으므로 상세 조회를 하지 않는다 (E2-1)."""
    calls = counted([_hit(440000001, "QAA1", 9100001, 1), _hit(440000002, "QAA1", 9100002, 2)])
    r = get_vessel_candidates("QAA1", call_sign="QAA1")
    assert len(r["candidates"]) == 2
    assert calls == Counter({"VesselSearch": 1})


def test_single_call_sign_still_gets_details(counted):
    calls = counted([_hit(440000011, "QAB1", 9200001, 1), _hit(440000012, "QAB1", 9200001, 3)])  # 같은 IMO → 최신 1척
    r = get_vessel_candidates("QAB1", call_sign="QAB1")
    assert [c["mmsi"] for c in r["candidates"]] == [440000011] and r["candidates"][0]["flag"] == "Panama"
    assert calls["VesselPositionSingle"] == 1


def test_stale_count_reports_dropped_old_signals(counted):
    """최근 rules.md vessel_recent_days일 안에 신호가 없는 배는 빼고 그 수를 알린다 (E2-5)."""
    counted([_hit(440000041, "QAE1", 9500001, 30), _hit(440000042, "QAE1", 9500002, 60)])
    r = get_vessel_candidates("QAE1", call_sign="QAE1")
    assert r["candidates"] == [] and r["stale_count"] == 2


def test_very_long_name_is_bad_request_not_exception(counted):
    """검색 주소가 너무 길어 httpx.InvalidURL이 나도 계약 밖 예외가 아니라 bad_request(재시도 안 함) (E2-7)."""
    counted([])
    r = get_vessel_candidates("A" * 70000)  # 스키마 검사를 거치지 않는 직접 호출
    assert r["error"]["code"] == "bad_request" and r["error"]["retryable"] is False


def test_mcp_schema_limits(monkeypatch):
    """MCP 통로의 입력 검사: 이름 100자, 호출부호 20자, MMSI 1~999999999, 속력 0 이상 (E2-7)."""
    monkeypatch.delenv("MCP_URL", raising=False)
    cases = [("get_vessel_candidates", {"ship_name": "A" * 101}),
             ("get_vessel_candidates", {"ship_name": "A", "call_sign": "C" * 21}),
             ("get_vessel_candidates", {"ship_name": "A", "detail_limit": -1}),
             ("get_live_position", {"mmsi": 10 ** 12}),
             ("get_live_position", {"mmsi": 0}),
             ("estimate_distance", {"lat": 35.0, "lng": 129.0, "sog": -5})]
    for name, args in cases:
        r = mcp_client.call_tool(name, **args)
        assert r["error"]["code"] == "bad_input" and r["error"]["retryable"] is False, (name, args)


def test_unknown_tool_is_not_retryable(monkeypatch):
    monkeypatch.delenv("MCP_URL", raising=False)
    r = mcp_client.call_tool("no_such_tool", x=1)
    assert r["error"]["code"] == "unknown_tool" and r["error"]["retryable"] is False


def test_route_failed_message_hides_exception_text(monkeypatch):
    def boom(*a, **k):
        raise ValueError("C:/secret/path detail")

    monkeypatch.setattr(distance.searoute, "searoute", boom)
    monkeypatch.setattr(distance, "_busan_node", lambda: (35.0, 129.2))
    r = distance.estimate_distance(31.2, 121.9, 12)
    assert r["error"]["code"] == "route_failed" and "secret" not in r["error"]["message"]
    assert "ValueError" in r["error"]["message"]


# ── ShipFinder 상태 문구 (clients/shipfinder.py, E2-6) ──

def _client(body: dict) -> ShipFinderClient:
    return ShipFinderClient(key="test-key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


def test_key_timeout_message_is_bad_key_with_korean_text():
    with pytest.raises(ShipFinderError) as e:
        _client({"status": 47, "msg": "Key time-out"}).search("QA")
    assert e.value.code == "bad_key" and e.value.retryable is False
    assert "Key time-out" not in e.value.message and "키" in e.value.message


def test_unknown_status_hides_english_message():
    with pytest.raises(ShipFinderError) as e:
        _client({"status": 99, "msg": "Something internal happened"}).search("QA")
    assert e.value.code == "error" and "internal" not in e.value.message and "99" in e.value.message
