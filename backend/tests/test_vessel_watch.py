"""B/L 없이 배만 지켜보기 테스트 (services/vessel_watch.py, routers/watch.py /api/watch/vessels).

DB 없이 돈다. vessel_watch·watch_positions 두 표를 흉내 내는 가짜 연결을 쓴다. 배 번호·이름·좌표는 모의값이다.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import operators as ops
from backend.app.services import vessel_watch as vsvc
from backend.app.services.watch import MAX_WATCH
from backend.tests.test_admin import _login

NOW = datetime(2026, 10, 6, 6, tzinfo=timezone.utc)  # 지난 시각 (미래 보고는 버린다, 10-08 QA E1-6)
USER = "user-123"  # _login이 쓰는 사용자 ID


class _Cur:
    def __init__(self, rows=None, rowcount=0):
        self._rows, self.rowcount = rows or [], rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """vessel_watch 한 표와 watch_positions(배별 최신 먼저 점)를 메모리에 둔다. 문장 앞부분으로 SQL을 가른다."""

    def __init__(self, positions=None):
        self.rows: dict[tuple, dict] = {}
        self.positions = positions or {}
        self.inserted: list = []  # 켤 때 넣은 첫 위치 점 (services/watch.seed_position)

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q.startswith("SELECT 1 FROM vessel_watch"):
            return _Cur([(1,)] if (params[0], params[1]) in self.rows else [])
        if q.startswith("SELECT count(*) FROM vessel_watch"):
            return _Cur([(sum(1 for u, _ in self.rows if u == params[0]),)])
        if q.startswith("INSERT INTO vessel_watch"):
            user, mmsi, name, flag, dest, eta = params
            self.rows[(user, mmsi)] = {"name": name, "flag": flag, "dest": dest, "eta": eta, "created_at": NOW}
            return _Cur()
        if q.startswith("DELETE FROM vessel_watch"):
            return _Cur(rowcount=1 if self.rows.pop((params[0], params[1]), None) else 0)
        if q.startswith("SELECT mmsi, name, flag, dest, eta, created_at FROM vessel_watch"):
            return _Cur([(m, r["name"], r["flag"], r["dest"], r["eta"], r["created_at"])
                         for (u, m), r in self.rows.items() if u == params[0]])
        if q.startswith("INSERT INTO watch_positions"):
            self.inserted.append(params)
            return _Cur(rowcount=1)
        if "FROM watch_positions" in q:
            return _Cur(self.positions.get(params[0], [])[:params[1]])
        raise AssertionError(f"예상 못 한 SQL: {q}")


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def no_shipfinder(monkeypatch):
    """테스트가 진짜 ShipFinder를 부르지 않게 기본 클라이언트를 막고, 위치 캐시를 테스트마다 비운다.
    켜기 전에 위치를 받는 길(fetch_position)이 있어서, 키가 있는 PC에서 mmsi로 올리는 테스트가 실제 호출을 하지 않게 한다.
    클라이언트가 필요한 테스트는 _use로 바꿔 끼운다."""
    from backend.app.clients.cache import ResponseCache
    from backend.app.clients.shipfinder import ShipFinderClient, ShipFinderError
    monkeypatch.setattr(ShipFinderClient, "cache", ResponseCache())
    monkeypatch.setattr(vsvc, "make_client", lambda: (_ for _ in ()).throw(ShipFinderError("bad_key", "테스트")))


@pytest.fixture
def db(monkeypatch):
    conn = FakeConn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(ops, "open_conn", lambda: _Ctx(conn))
    return conn


@pytest.fixture
def me(monkeypatch):
    return _login(monkeypatch, "kim@example.com")


def _call(method, path, body=None, headers=None):
    with TestClient(app) as c:
        return getattr(c, method)(path, headers=headers, **({"json": body} if body is not None else {}))


def _points(n, latest_source="worker"):
    """최신이 먼저 오는 위치 n개. 마지막 칸은 위치 출처(migrations/031), 최신 점만 latest_source"""
    return [(NOW - timedelta(hours=6 * i), 34.0 + i * 0.1, 128.0 - i * 0.1, 12.5, 60.0,
             latest_source if i == 0 else "worker") for i in range(n)]


VESSEL = {"mmsi": 440999901, "name": "TEST STAR", "flag": "KR", "dest": "KRPUS", "eta": "10-12 09:00"}


# ── 서비스 ──

def test_add_is_idempotent_and_updates_shown_values():
    conn = FakeConn()
    assert vsvc.add_vessel(conn, USER, 440999901, " TEST STAR ", "KR", "KRPUS", "10-12") == {"mmsi": 440999901, "watch": True}
    vsvc.add_vessel(conn, USER, 440999901, "TEST STAR", "KR", "KRPUS", "10-13")
    assert len(conn.rows) == 1 and conn.rows[(USER, 440999901)]["eta"] == "10-13"
    assert conn.rows[(USER, 440999901)]["name"] == "TEST STAR"  # 공백은 뗀다


def test_limit_counts_only_new_vessels():
    conn = FakeConn()
    for i in range(MAX_WATCH):
        vsvc.add_vessel(conn, USER, 440000000 + i, f"S{i}")
    with pytest.raises(vsvc.VesselWatchError) as e:
        vsvc.add_vessel(conn, USER, 440009999, "NEW")
    assert e.value.kind == "limit"
    vsvc.add_vessel(conn, USER, 440000000, "S0 RENAMED")  # 이미 있는 배는 한도에 걸리지 않는다
    vsvc.add_vessel(conn, "other-user", 440009999, "NEW")  # 한도는 사용자별이다


def test_invalid_input_rejected_and_text_trimmed():
    conn = FakeConn()
    # MMSI는 9자리만 (10-08 QA E1-5). bool(True는 1)도 받지 않는다
    for mmsi, name in ((0, "X"), (-5, "X"), (123, "X"), (10 ** 19, "X"), (True, "X"), (440999901, "   ")):
        with pytest.raises(vsvc.VesselWatchError) as e:
            vsvc.add_vessel(conn, USER, mmsi, name)
        assert e.value.kind == "invalid"
    vsvc.add_vessel(conn, USER, 440000005, "N", dest="D" * 500)
    assert len(conn.rows[(USER, 440000005)]["dest"]) == vsvc.TEXT_MAX


def test_list_has_last_position_and_no_verdict_fields():
    conn = FakeConn({440999901: _points(3)})
    vsvc.add_vessel(conn, USER, 440999901, "TEST STAR", "KR")
    vsvc.add_vessel(conn, USER, 440999902, "NO POS")
    items = {i["mmsi"]: i for i in vsvc.list_vessels(conn, USER)["items"]}
    a, b = items[440999901], items[440999902]
    assert (a["lat"], a["lng"], a["position_at"]) == (34.0, 128.0, NOW.isoformat()) and len(a["track"]) == 3
    assert b["lat"] is None and b["track"] == []
    assert not {"verdict", "stage", "bl_masked", "days_margin"} & set(a)  # 판정·통관·B/L 칸이 없다
    assert a["position_source"] == "worker" and b["position_source"] is None  # 마지막 점 출처 (migrations/031)


def test_list_position_source_is_latest_point_source():
    """켤 때 첫 점(seed)이 마지막이면 position_source가 seed다. 화면이 속 빈 핀으로 그린다 (10-08 사용자 결정)"""
    conn = FakeConn({440999901: _points(2, latest_source="seed")})
    vsvc.add_vessel(conn, USER, 440999901, "TEST STAR")
    item = vsvc.list_vessels(conn, USER)["items"][0]
    assert item["position_source"] == "seed" and [p["source"] for p in item["track"]] == ["worker", "seed"]


# ── 창구 ──

def test_endpoints_round_trip(db, me):
    r = _call("post", "/api/watch/vessels", VESSEL, me)
    assert r.status_code == 200 and r.json() == {"mmsi": 440999901, "watch": True}
    items = _call("get", "/api/watch/vessels", None, me).json()["items"]
    assert [i["name"] for i in items] == ["TEST STAR"] and items[0]["dest"] == "KRPUS"
    r = _call("delete", "/api/watch/vessels/440999901", None, me)
    assert r.status_code == 200 and r.json() == {"mmsi": 440999901, "watch": False}
    assert _call("get", "/api/watch/vessels", None, me).json() == {"items": []}
    assert _call("delete", "/api/watch/vessels/440999901", None, me).status_code == 200  # 없어도 오류가 아니다


def test_limit_is_409_and_blank_name_is_422(db, me):
    for i in range(MAX_WATCH):
        assert _call("post", "/api/watch/vessels", {**VESSEL, "mmsi": 440000100 + i}, me).status_code == 200
    r = _call("post", "/api/watch/vessels", VESSEL, me)
    assert r.status_code == 409 and "5척" in r.json()["detail"]
    r = _call("post", "/api/watch/vessels", {**VESSEL, "name": " "}, me)
    assert r.status_code == 422


ENDPOINTS = [("get", "/api/watch/vessels", None), ("post", "/api/watch/vessels", VESSEL),
             ("delete", "/api/watch/vessels/440999901", None)]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_login_and_db_conditions(db, monkeypatch, method, path, body):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert _call(method, path, body).status_code == 401  # 로그인 꺼짐
    headers = _login(monkeypatch, "kim@example.com")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert _call(method, path, body, headers).status_code == 503  # DB 없음


def test_db_failure_is_503_without_leaking(monkeypatch, me):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

    def down():
        raise RuntimeError("connection to secret-host failed")

    monkeypatch.setattr(ops, "open_conn", down)
    r = _call("get", "/api/watch/vessels", None, me)
    assert r.status_code == 503 and "secret-host" not in r.text


def test_other_users_vessels_are_not_listed(db, me):
    db.rows[("someone-else", 777)] = {"name": "THEIRS", "flag": None, "dest": None, "eta": None, "created_at": NOW}
    assert _call("get", "/api/watch/vessels", None, me).json() == {"items": []}


# ── 호출부호로 올리기 (입항 예정 목록, MMSI 없음) ──

class _FakeClient:
    """ShipFinderClient 자리. hits는 (mmsi, call_sign) 목록, error를 주면 검색이 그 오류로 실패한다."""

    def __init__(self, hits=(), error=None):
        self.hits, self.error, self.closed, self.queries = hits, error, False, []

    def search(self, keywords, max=10):
        from types import SimpleNamespace
        self.queries.append(keywords)
        if self.error:
            raise self.error
        # 줄은 (mmsi, 호출부호) 또는 (mmsi, 호출부호, IMO, 마지막 신호 며칠 전). 기본은 IMO 없음·방금 신호 (services/vessel_pick.py)
        now = datetime.now(timezone.utc)
        rows = [(*h, None, 0) if len(h) == 2 else h for h in self.hits]
        return SimpleNamespace(hits=[SimpleNamespace(mmsi=m, call_sign=c, ship_name="X", imo=imo,
                                                     last_time=now - timedelta(days=age))
                                     for m, c, imo, age in rows])

    def close(self):
        self.closed = True


def _use(monkeypatch, client):
    monkeypatch.setattr(vsvc, "make_client", lambda: client)
    return client


SIGN_BODY = {"call_sign": "tst01", "name": "TEST STAR", "flag": "KR"}


def test_call_sign_add_resolves_mmsi(db, me, monkeypatch):
    # 예전에 같은 호출부호를 쓴 다른 호출부호 배·같은 배의 중복 줄은 걸러진다
    client = _use(monkeypatch, _FakeClient([(440999901, "TST01"), (440999901, " tst01 "), (111, "OTHER1")]))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 200 and r.json() == {"mmsi": 440999901, "watch": True}
    assert (USER, 440999901) in db.rows and client.queries == ["TST01"] and client.closed


def test_call_sign_without_exact_match_is_422(db, me, monkeypatch):
    _use(monkeypatch, _FakeClient([(111, "TST011"), (222, None)]))  # 비슷하지만 같지 않은 호출부호
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 422 and "선박 번호를 확인하지 못해" in r.json()["detail"] and db.rows == {}
    _use(monkeypatch, _FakeClient([]))
    assert _call("post", "/api/watch/vessels", SIGN_BODY, me).status_code == 422


def test_call_sign_with_several_mmsi_is_422(db, me, monkeypatch):
    _use(monkeypatch, _FakeClient([(1, "TST01"), (2, "TST01")]))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 422 and db.rows == {}


def test_call_sign_reused_by_old_ships_picks_the_recent_one(db, me, monkeypatch):
    # 실제 관찰된 모양: 한 호출부호에 IMO가 다른 3척, 마지막 신호 0일·123일·552일 전 → 지금 배 하나
    _use(monkeypatch, _FakeClient([(440999901, "TST01", 9900001, 0), (440999902, "TST01", 9900002, 123),
                                   (440999903, "TST01", 9900003, 552)]))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 200 and r.json()["mmsi"] == 440999901


def test_call_sign_with_two_recent_ships_is_422(db, me, monkeypatch):
    # 최근 신호를 보낸 배가 둘이면 어느 쪽인지 모른다. 고르지 않는다
    _use(monkeypatch, _FakeClient([(440999901, "TST01", 9900001, 0), (440999902, "TST01", 9900002, 2)]))
    assert _call("post", "/api/watch/vessels", SIGN_BODY, me).status_code == 422 and db.rows == {}


def test_call_sign_lookup_failure_is_503_without_leaking(db, me, monkeypatch):
    from backend.app.clients.shipfinder import ShipFinderError
    _use(monkeypatch, _FakeClient(error=ShipFinderError("timeout", "secret-url 응답 시간 초과")))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 503 and "secret-url" not in r.text and db.rows == {}
    _use(monkeypatch, _FakeClient(error=RuntimeError("boom secret-host")))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 503 and "secret-host" not in r.text
    monkeypatch.setattr(vsvc, "make_client", lambda: (_ for _ in ()).throw(ShipFinderError("bad_key", "키 없음")))  # 키 없음
    assert _call("post", "/api/watch/vessels", SIGN_BODY, me).status_code == 503


def test_call_sign_quota_exhausted_is_503_with_its_own_message(db, me, monkeypatch):
    """ShipFinder 하루 한도 소진은 오늘 다시 눌러도 같다. "잠시 뒤"가 아니라 내일로 안내한다."""
    from backend.app.clients.shipfinder import ShipFinderError
    _use(monkeypatch, _FakeClient(error=ShipFinderError("quota_exhausted", "secret-url 한도 소진", status=38)))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 503 and "한도" in r.json()["detail"] and "내일" in r.json()["detail"]
    assert "secret-url" not in r.text and db.rows == {}
    _use(monkeypatch, _FakeClient(error=ShipFinderError("timeout", "시간 초과")))
    assert "잠시 뒤" in _call("post", "/api/watch/vessels", SIGN_BODY, me).json()["detail"]


def test_mmsi_wins_and_neither_is_422(db, me, monkeypatch):
    client = _use(monkeypatch, _FakeClient([(5, "TST01")]))
    r = _call("post", "/api/watch/vessels", {**VESSEL, "call_sign": "TST01"}, me)
    assert r.json()["mmsi"] == 440999901 and client.queries == []  # mmsi가 있으면 검색하지 않는다
    for body in ({"name": "TEST STAR"}, {"name": "TEST STAR", "call_sign": "  "}):
        assert _call("post", "/api/watch/vessels", body, me).status_code == 422


def test_call_sign_miss_falls_back_to_name_with_same_sign(db, me, monkeypatch):
    # 호출부호 검색 0건, 선명 검색에 같은 호출부호 배가 하나 → 200 (입항 목록에서 지켜보기)
    class ByQuery(_FakeClient):
        def search(self, keywords, max=10):
            self.hits = [] if keywords == "TST01" else [(440999901, "TST01"), (333, "OTHER9")]
            return super().search(keywords, max)
    client = _use(monkeypatch, ByQuery())
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 200 and r.json()["mmsi"] == 440999901
    assert client.queries == ["TST01", "TEST STAR"] and client.closed


def test_name_fallback_without_same_sign_is_still_422(db, me, monkeypatch):
    # 선명이 같아도 호출부호가 다른 배는 고르지 않는다
    class ByQuery(_FakeClient):
        def search(self, keywords, max=10):
            self.hits = [] if keywords == "TST01" else [(333, "OTHER9"), (444, None)]
            return super().search(keywords, max)
    client = _use(monkeypatch, ByQuery())
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 422 and db.rows == {} and client.queries == ["TST01", "TEST STAR"]


def test_add_seeds_cached_position_without_calling_shipfinder(monkeypatch):
    from types import SimpleNamespace
    from backend.app.clients.cache import ResponseCache
    from backend.app.clients.shipfinder import ShipFinderClient
    cache = ResponseCache()
    monkeypatch.setattr(ShipFinderClient, "cache", cache)
    monkeypatch.setattr(vsvc, "make_client", lambda: (_ for _ in ()).throw(AssertionError("ShipFinder를 부르면 안 된다")))
    conn = FakeConn()
    vsvc.add_vessel(conn, USER, 440999901, "TEST STAR")
    assert conn.inserted == []  # 캐시 없음: 점 없이 켜진다
    cache.set("shipfinder:pos:440999901", SimpleNamespace(
        last_time=NOW, fetched_at=NOW, lat=35.0, lng=129.0, sog=12.5, cog=60.0, navistat=0))
    vsvc.add_vessel(conn, USER, 440999901, "TEST STAR")
    # 마지막 칸은 위치 출처 seed (켤 때 저장해 둔 위치, migrations/031)
    assert conn.inserted == [(440999901, NOW, NOW, 35.0, 129.0, 12.5, 60.0, 0, "seed")] and (USER, 440999901) in conn.rows


class _PosClient(_FakeClient):
    """위치 조회도 하는 가짜. 진짜 클라이언트처럼 받은 값을 ShipFinderClient 캐시에 넣는다. fail이면 조회가 실패한다."""

    def __init__(self, hits=(), fail=False):
        super().__init__(hits)
        self.fail, self.positions = fail, []

    def position(self, mmsi):
        from types import SimpleNamespace
        from backend.app.clients.shipfinder import ShipFinderClient, ShipFinderError
        self.positions.append(mmsi)
        if self.fail:
            raise ShipFinderError("timeout", "secret-url 시간 초과")
        pos = SimpleNamespace(mmsi=mmsi, last_time=NOW, fetched_at=NOW, lat=35.0, lng=129.0, sog=12.5, cog=60.0, navistat=0)
        ShipFinderClient.cache.set(f"shipfinder:pos:{mmsi}", pos)
        return pos


def test_watch_without_detail_fetches_position_once_and_seeds_point(db, me, monkeypatch):
    # 상세를 안 열어 캐시가 빈 배: 켜기 전에 위치를 한 번 받아 첫 점으로 넣는다
    client = _use(monkeypatch, _PosClient([(440999901, "TST01")]))
    r = _call("post", "/api/watch/vessels", SIGN_BODY, me)
    assert r.status_code == 200 and client.positions == [440999901]
    assert db.inserted == [(440999901, NOW, NOW, 35.0, 129.0, 12.5, 60.0, 0, "seed")]
    # 다시 켜면 캐시가 있어 위치를 다시 부르지 않는다 (하루 50회 한도)
    again = _use(monkeypatch, _PosClient())
    assert _call("post", "/api/watch/vessels", VESSEL, me).status_code == 200 and again.positions == []


def test_position_failure_still_turns_watch_on_without_point(db, me, monkeypatch):
    client = _use(monkeypatch, _PosClient(fail=True))
    r = _call("post", "/api/watch/vessels", VESSEL, me)
    assert r.status_code == 200 and client.positions == [440999901] and client.closed
    assert db.inserted == [] and (USER, 440999901) in db.rows and "secret-url" not in r.text


# ── 10-08 QA E1 ──

@pytest.mark.parametrize("mmsi", [123, 0, -5, 10 ** 19, True, "440999901", 440999901.5])
def test_bad_mmsi_is_422_not_503(db, me, mmsi):
    """MMSI는 9자리 정수만 받는다 (E1-5). 예전에는 10^19가 DB까지 가서 "저장소 연결 실패"(503)로 잘못 안내됐다."""
    r = _call("post", "/api/watch/vessels", {**VESSEL, "mmsi": mmsi}, me)
    assert r.status_code == 422 and db.rows == {}


@pytest.mark.parametrize("path", ["/api/watch/vessels/123", "/api/watch/vessels/99999999999999999999",
                                  "/api/watch/vessels/abc"])
def test_bad_mmsi_in_delete_path_is_422(db, me, path):
    assert _call("delete", path, None, me).status_code == 422


class _CountingClient(_PosClient):
    """search·position을 부르면 센다. 한도가 찬 사용자의 요청은 하나도 부르면 안 된다."""


def test_full_list_is_refused_before_any_shipfinder_call(db, me, monkeypatch):
    """한도·이름 검사는 DB로 먼저 한다 (E1-3). 한도가 찼으면 호출부호 검색도 위치 조회도 하지 않는다."""
    for i in range(MAX_WATCH):
        db.rows[(USER, 440000100 + i)] = {"name": f"S{i}", "flag": None, "dest": None, "eta": None, "created_at": NOW}
    client = _use(monkeypatch, _CountingClient([(440999901, "TST01")]))
    assert _call("post", "/api/watch/vessels", SIGN_BODY, me).status_code == 409
    assert _call("post", "/api/watch/vessels", VESSEL, me).status_code == 409
    assert client.queries == [] and client.positions == []
    # 이미 지켜보는 배를 다시 올리는 것은 한도에 걸리지 않는다 (보인 값만 갱신)
    r = _call("post", "/api/watch/vessels", {**VESSEL, "mmsi": 440000100, "name": "RENAMED"}, me)
    assert r.status_code == 200 and db.rows[(USER, 440000100)]["name"] == "RENAMED"


def test_blank_name_is_refused_before_shipfinder(db, me, monkeypatch):
    client = _use(monkeypatch, _CountingClient([(440999901, "TST01")]))
    assert _call("post", "/api/watch/vessels", {**SIGN_BODY, "name": "  "}, me).status_code == 422
    assert client.queries == [] and client.positions == []


def test_add_vessel_counts_inside_user_lock():
    """트랜잭션을 여는 연결이면 사용자별 잠금을 잡은 뒤 세고 넣는다 (E1-10과 같은 경쟁). 첫 위치 점은 잠금 밖이다."""
    from contextlib import contextmanager

    class TxConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.events = []

        @contextmanager
        def transaction(self):
            self.events.append("begin")
            yield
            self.events.append("commit")

        def execute(self, sql, params=()):
            if "pg_advisory_xact_lock" in sql:
                self.events.append(("lock", params[0]))
                return _Cur([(None,)])
            self.events.append(" ".join(sql.split()).split(" ")[0])
            return super().execute(sql, params)

    conn = TxConn()
    vsvc.add_vessel(conn, USER, 440999901, "TEST STAR")
    assert conn.events[:2] == ["begin", ("lock", f"vessel_watch:{USER}")]
    assert conn.events.index("INSERT") < conn.events.index("commit")


# ── 워커·마이그레이션 ──

def test_worker_collects_positions_for_vessel_watch_too():
    from worker.collector import SELECT_WATCHED
    sql = " ".join(SELECT_WATCHED.split())
    assert "UNION SELECT mmsi FROM vessel_watch" in sql  # 같은 배는 UNION이 한 번만 센다


def test_migration_023_is_last_and_creates_table():
    from pathlib import Path
    names = sorted(p.name for p in (Path(__file__).parents[1] / "app/db/migrations").glob("*.sql"))
    assert "023_vessel_watch.sql" in names  # 024는 그 뒤에 붙는다 (test_bl_type_retry.py)
    text = (Path(__file__).parents[1] / "app/db/migrations" / "023_vessel_watch.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS vessel_watch" in text and "ENABLE ROW LEVEL SECURITY" in text
