"""QA 수정 뒤 갈래 사이에 남았던 빈틈 (10-08).

- 조회 때 쌓는 위치 점(services/cargo._keep_position)도 미래 시각·(0,0) 좌표를 버린다. 워커·aisstream·첫 점과 같은 규칙(E1-6)
- 지켜보기 재판정은 범위 밖으로 저장된 옛 판매 예정일(예 1900-01-01)을 쓰지 않는다(B2를 워커 경로에도)
DB·키 없이 돈다.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app.services import cargo
from backend.app.services import watch as svc


class _Conn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, list(params)))
        return SimpleNamespace(rowcount=1, fetchone=lambda: None, fetchall=lambda: [])


def _response(last_time, lat=35.1, lng=129.0):
    return SimpleNamespace(position=SimpleNamespace(last_time=last_time, lat=lat, lng=lng, sog=10.0, cog=90.0, navistat=0))


def test_keep_position_drops_future_and_zero_and_floors_seconds():
    now = datetime.now(timezone.utc)
    conn = _Conn()
    cargo._keep_position(conn, 440999001, _response(now + timedelta(days=3)))  # 미래 시각
    cargo._keep_position(conn, 440999001, _response(now - timedelta(hours=1), lat=0, lng=0))  # (0,0)
    assert conn.calls == []
    past = (now - timedelta(hours=1)).replace(microsecond=654321)
    cargo._keep_position(conn, 440999001, _response(past))
    assert conn.calls and conn.calls[0][1][1] == past.replace(microsecond=0)


def test_rejudge_ignores_out_of_range_stored_sale_date(monkeypatch):
    from backend.app import crypto
    from backend.app.routers import agent

    monkeypatch.setattr(crypto, "decrypt", lambda token: "QATEST000001")
    seen = {}

    def assess(request, memory):
        seen["sale_due"] = request.sale_due_date
        raise RuntimeError("stop here")  # 요청 모양만 확인한다
    row = ("u1", "key", b"enc", "QAT****0001", None, "HOUSE", date(1900, 1, 1), None, None, 440999001, "watch", None)
    with pytest.raises(Exception):
        svc._check_one(_Conn(), row, datetime(2026, 9, 23, tzinfo=timezone.utc), assess)
    assert "sale_due" in seen and seen["sale_due"] is None  # 판정까지 갔고, 날짜 없이 갔다
