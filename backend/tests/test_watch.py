"""화물 지켜보기 서비스 테스트 (services/watch.py).

DB·카카오·LLM·ShipFinder는 부르지 않는다. user_cargo 표를 흉내 내는 가짜 연결, 판정 함수(assess) 가짜,
카카오 보내기 가짜, 테스트용 열쇠를 쓴다. B/L은 모의값이다(실제 번호 아님).
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from backend.app import crypto
from backend.app.clients.kakao import KakaoError
from backend.app.services import cargo
from backend.app.services import watch as svc
from backend.app.services.kakao import KakaoLinkError

NOW = datetime(2026, 10, 5, 3, tzinfo=timezone.utc)
USER = "user-123"
# 알림 보류 칸 (10-08 QA E1-2). 보류·재발송이 없는 실행의 기본값
QUIET = {"notify_held": 0, "renotified": 0, "notify_dropped": 0}
BL = "MOCKBL20261005"  # 모의 B/L


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://web.example.com/, http://localhost:3000")


class _Cur:
    def __init__(self, rows=None, rowcount=0):
        self._rows, self.rowcount = rows or [], rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """user_cargo 한 표를 메모리에 둔다. 문장 앞부분으로 어떤 SQL인지 가른다 (test_kakao_service.FakeConn과 같은 방식).
    10-08 QA E1: 시도 기록(watch_attempt_at·watch_fail_count), 알림 보류(notify_pending), 도착 예정 변화 칸,
    조건부 켜기 UPDATE를 같이 흉내 낸다."""

    def __init__(self):
        self.rows: list[dict] = []
        self.positions: dict[tuple, tuple] = {}  # watch_positions (mmsi, ts) → 넣은 값
        self.sql: list[str] = []

    def add(self, bl=BL, user=USER, **over) -> dict:
        row = {"user_id": user, "cargo_key": cargo.cargo_key(bl), "bl_encrypted": crypto.encrypt(bl),
               "bl_masked": "MOC*******1005", "label": None, "bl_type": "HOUSE", "sale_due_date": date(2026, 10, 20),
               "forwarder_eta": None, "voyage_no": None, "vessel_mmsi": 440123456, "last_verdict": "watch",
               "last_checked_at": datetime(2026, 10, 4, tzinfo=timezone.utc), "status": "active", "watch": False,
               "changed_from": None, "changed_at": None, "change_seen": True,
               "watch_attempt_at": None, "watch_fail_count": 0, "notify_pending": None, "notify_pending_at": None,
               "eta_changed_from": None, "eta_changed_to": None, "eta_change_hours": None, **over}
        self.rows.append(row)
        return row

    def row(self, bl=BL, user=USER) -> dict:
        return next(r for r in self.rows if r["user_id"] == user and r["cargo_key"] == cargo.cargo_key(bl))

    def _one(self, user, key):
        return [r for r in self.rows if (r["user_id"], r["cargo_key"]) == (user, key)]

    @staticmethod
    def _paused(r) -> bool:
        return svc.is_paused(r["watch_fail_count"], r["watch_attempt_at"], r["last_checked_at"])

    def _active_watch_count(self, user):
        return sum(1 for r in self.rows if r["user_id"] == user and r["watch"] and r["status"] == "active")

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        self.sql.append(q)
        if q.startswith("SELECT status, watch FROM user_cargo"):
            return _Cur([(r["status"], r["watch"]) for r in self._one(*params)])
        if q.startswith("UPDATE user_cargo SET watch = true, watch_fail_count = 0"):
            # 조건부 켜기: 아직 꺼져 있고 지금 켠 수가 한도 미만일 때만 (services/watch.set_watch)
            user, key, _, limit = params
            if self._active_watch_count(user) >= limit:
                return _Cur(rowcount=0)
            found = [r for r in self._one(user, key) if not r["watch"]]
            for r in found:
                r.update(watch=True, watch_fail_count=0)
            return _Cur(rowcount=len(found))
        if q.startswith("UPDATE user_cargo SET watch = false"):
            for r in self._one(*params):
                r["watch"] = False
            return _Cur(rowcount=1)
        if q.startswith("SELECT bl_masked, label, changed_from, last_verdict, changed_at"):
            found = [r for r in self.rows if r["user_id"] == params[0] and r["watch"] and not r["change_seen"]]
            found.sort(key=lambda r: r["changed_at"], reverse=True)
            return _Cur([(r["bl_masked"], r["label"], r["changed_from"], r["last_verdict"], r["changed_at"],
                          r.get("last_days_margin"), r.get("last_stage"), r["eta_changed_from"], r["eta_changed_to"],
                          r["eta_change_hours"]) for r in found])
        if q.startswith("UPDATE user_cargo SET change_seen = true"):
            found = [r for r in self.rows if r["user_id"] == params[0] and not r["change_seen"]]
            for r in found:
                r["change_seen"] = True
            return _Cur(rowcount=len(found))
        if q.startswith("SELECT count(*) FROM user_cargo WHERE watch AND status = 'active' AND vessel_mmsi IS NULL"):
            return _Cur([(sum(1 for r in self.rows if r["watch"] and r["status"] == "active"
                              and r["vessel_mmsi"] is None),)])
        if q.startswith("SELECT count(*) FROM user_cargo WHERE watch AND status = 'active' AND vessel_mmsi IS NOT NULL"):
            return _Cur([(sum(1 for r in self.rows if r["watch"] and r["status"] == "active"
                              and r["vessel_mmsi"] is not None and not self._paused(r)),)])
        if q.startswith("SELECT user_id, cargo_key, bl_encrypted"):
            found = [r for r in self.rows if r["watch"] and r["status"] == "active" and not self._paused(r)
                     and ("vessel_mmsi IS NOT NULL" not in q or r["vessel_mmsi"] is not None)]

            def since(r):  # GREATEST(last_checked_at, watch_attempt_at) ASC NULLS FIRST
                seen = [t for t in (r["last_checked_at"], r["watch_attempt_at"]) if t]
                return (bool(seen), max(seen) if seen else NOW)
            found.sort(key=since)
            cols = ("user_id", "cargo_key", "bl_encrypted", "bl_masked", "label", "bl_type", "sale_due_date",
                    "forwarder_eta", "voyage_no", "vessel_mmsi", "last_verdict", "last_eta")
            return _Cur([tuple(r.get(c) for c in cols) for r in found[:params[1]]])
        if q.startswith("UPDATE user_cargo SET watch_attempt_at = %s, watch_fail_count = 0"):
            now, user, key = params
            for r in self._one(user, key):
                r.update(watch_attempt_at=now, watch_fail_count=0)
            return _Cur(rowcount=1)
        if q.startswith("UPDATE user_cargo SET watch_fail_count = CASE"):
            now, user, key = params
            for r in self._one(user, key):
                fresh = r["watch_attempt_at"] is not None and r["last_checked_at"] and r["last_checked_at"] > r["watch_attempt_at"]
                r.update(watch_fail_count=1 if fresh else r["watch_fail_count"] + 1, watch_attempt_at=now)
            return _Cur(rowcount=1)
        if q.startswith("UPDATE user_cargo SET watch_attempt_at = %s WHERE"):
            now, user, key = params
            for r in self._one(user, key):
                r["watch_attempt_at"] = now
            return _Cur(rowcount=1)
        if q.startswith("UPDATE user_cargo SET changed_from = CASE"):
            vc, old, e1, eta_from, e2, eta_to, e3, hours, at, user, key = params
            for r in self._one(user, key):
                seen = r["change_seen"]
                r["changed_from"] = old if vc else (None if seen else r["changed_from"])
                r["eta_changed_from"] = eta_from if e1 else (None if seen else r["eta_changed_from"])
                r["eta_changed_to"] = eta_to if e2 else (None if seen else r["eta_changed_to"])
                r["eta_change_hours"] = hours if e3 else (None if seen else r["eta_change_hours"])
                r.update(changed_at=at, change_seen=False)
            return _Cur(rowcount=1)
        if q.startswith("UPDATE user_cargo SET notify_pending = NULL"):
            for r in self._one(*params):
                r.update(notify_pending=None, notify_pending_at=None)
            return _Cur(rowcount=1)
        if q.startswith("UPDATE user_cargo SET notify_pending = %s"):
            text, since, user, key = params
            for r in self._one(user, key):
                r.update(notify_pending=text, notify_pending_at=since)
            return _Cur(rowcount=1)
        if q.startswith("SELECT user_id, cargo_key, notify_pending, notify_pending_at"):
            return _Cur([(r["user_id"], r["cargo_key"], r["notify_pending"], r["notify_pending_at"])
                         for r in self.rows if r["notify_pending"] is not None and r["watch"]])
        if q.startswith("SELECT vessel_mmsi FROM user_cargo"):
            return _Cur([(r["vessel_mmsi"],) for r in self._one(*params)])
        if q.startswith("INSERT INTO watch_positions"):
            # 첫 위치 점 (services/watch.seed_position). (mmsi, ts)가 같으면 넣지 않는다 (ON CONFLICT DO NOTHING)
            if (params[0], params[1]) in self.positions:
                return _Cur(rowcount=0)
            self.positions[(params[0], params[1])] = params
            return _Cur(rowcount=1)
        raise AssertionError(f"예상하지 못한 SQL: {q}")


def _resp(verdict, stage="통관진행"):
    """AgentResponse처럼 verdict와 customs.stage_name만 가진 가짜 응답."""
    return SimpleNamespace(verdict=verdict, customs=SimpleNamespace(stage_name=stage))


@pytest.fixture
def upserts(monkeypatch):
    """cargo.upsert_assessment를 가짜로 바꾼다. 마지막 판정을 덮고 반출이면 done으로 옮기는 것만 흉내 낸다."""
    calls = []

    def fake(conn, user_id, request, response, *, released, now=None):
        calls.append({"user_id": user_id, "request": request, "response": response, "released": released, "now": now})
        row = conn.row(request.bl_no, user_id)
        row.update(last_verdict=response.verdict, last_checked_at=now, status="done" if released else "active")
        return True

    monkeypatch.setattr(svc.cargo, "upsert_assessment", fake)
    return calls


@pytest.fixture
def sent(monkeypatch):
    """kakao.send_to_user를 가짜로 바꾼다. 보낸 (user_id, text, link)를 모은다."""
    log = []
    monkeypatch.setattr(svc.kakao, "send_to_user", lambda conn, user_id, text, link: log.append((user_id, text, link)))
    return log


# ── set_watch ──

def test_set_watch_on_and_off():
    conn = FakeConn()
    conn.add()
    assert svc.set_watch(conn, USER, BL, True) is True and conn.row()["watch"] is True
    assert svc.set_watch(conn, USER, BL.lower(), False) is False and conn.row()["watch"] is False  # 소문자도 같은 화물


def test_set_watch_not_saved_when_row_missing():
    with pytest.raises(svc.WatchError) as e:
        svc.set_watch(FakeConn(), USER, BL, True)
    assert e.value.kind == "not_saved"


def test_set_watch_done_cargo_cannot_be_watched():
    conn = FakeConn()
    conn.add(status="done")
    with pytest.raises(svc.WatchError) as e:
        svc.set_watch(conn, USER, BL, True)
    assert e.value.kind == "done" and conn.row()["watch"] is False


def test_set_watch_limit_is_five_per_user():
    conn = FakeConn()
    for i in range(5):
        conn.add(bl=f"MOCKBL2026100{i}", watch=True)
    conn.add()
    conn.add(user="other", watch=True)  # 다른 사용자의 감시는 세지 않는다
    with pytest.raises(svc.WatchError) as e:
        svc.set_watch(conn, USER, BL, True)
    assert e.value.kind == "limit" and conn.row()["watch"] is False
    assert svc.set_watch(conn, USER, "MOCKBL20261000", True) is True  # 이미 켠 것을 다시 켜는 것은 한도에 걸리지 않는다
    svc.set_watch(conn, USER, "MOCKBL20261000", False)
    assert svc.set_watch(conn, USER, BL, True) is True  # 하나 끄면 다시 켤 수 있다


def test_set_watch_limit_ignores_finished_cargo():
    conn = FakeConn()
    for i in range(5):
        conn.add(bl=f"MOCKBL2026100{i}", watch=True, status="done")  # 반출 끝난 화물은 자리를 차지하지 않는다
    conn.add()
    assert svc.set_watch(conn, USER, BL, True) is True


def test_set_watch_off_is_always_fine():
    conn = FakeConn()
    assert svc.set_watch(conn, USER, BL, False) is False  # 줄이 없어도
    conn.add(status="done", watch=True)
    assert svc.set_watch(conn, USER, BL, False) is False and conn.row()["watch"] is False  # 끝난 화물도


def test_set_watch_without_key(monkeypatch):
    """열쇠가 없으면 어느 줄인지 몰라 켜지도 끄지도 못한다. 끄기도 "꺼졌다"고 거짓으로 답하지 않는다 (10-08 QA E1-4)."""
    monkeypatch.delenv("BL_ENCRYPTION_KEY")
    for on in (True, False):
        with pytest.raises(svc.WatchError) as e:
            svc.set_watch(FakeConn(), USER, BL, on)
        assert e.value.kind == "no_key"


# ── watch_state ──

# ── 켤 때 첫 위치 점 (services/watch.seed_position) ──

def _pos(mmsi=440123456, last_time=datetime(2026, 10, 6, 3, tzinfo=timezone.utc)):
    """ShipFinderClient.position이 캐시에 남기는 값 모양 (모의 좌표)."""
    return SimpleNamespace(mmsi=mmsi, last_time=last_time, fetched_at=datetime(2026, 10, 6, 3, 5, tzinfo=timezone.utc),
                           lat=35.0, lng=129.0, sog=12.5, cog=60.0, navistat=0)


@pytest.fixture
def pos_cache(monkeypatch):
    """테스트마다 빈 ShipFinder 캐시. 클래스 변수라 다른 테스트가 담은 위치가 섞이지 않게 바꿔 끼운다."""
    from backend.app.clients.cache import ResponseCache
    from backend.app.clients.shipfinder import ShipFinderClient
    cache = ResponseCache()
    monkeypatch.setattr(ShipFinderClient, "cache", cache)
    return cache


def test_set_watch_on_seeds_cached_position_once(pos_cache):
    conn = FakeConn()
    conn.add()
    pos_cache.set("shipfinder:pos:440123456", _pos())
    svc.set_watch(conn, USER, BL, True)
    assert list(conn.positions) == [(440123456, datetime(2026, 10, 6, 3, tzinfo=timezone.utc))]
    # 마지막 칸은 위치 출처 seed: 켤 때 저장해 둔(캐시) 위치라 몇 시간 전일 수 있다 (migrations/031, 10-08 사용자 결정)
    assert next(iter(conn.positions.values()))[-1] == "seed"
    svc.set_watch(conn, USER, BL, True)  # 다시 켜도 같은 보고 시각은 한 줄
    svc.set_watch(conn, USER, BL, False)
    svc.set_watch(conn, USER, BL, True)
    assert len(conn.positions) == 1 and conn.row()["watch"] is True


def test_set_watch_on_without_cache_or_vessel_has_no_point(pos_cache):
    conn = FakeConn()
    conn.add()
    assert svc.set_watch(conn, USER, BL, True) is True and conn.positions == {}  # 캐시 없음
    pos_cache.set("shipfinder:pos:440123456", _pos(last_time=None))
    svc.set_watch(conn, USER, BL, True)
    assert conn.positions == {}  # 보고 시각 없음
    other = FakeConn()
    other.add(vessel_mmsi=None)
    assert svc.set_watch(other, USER, BL, True) is True and other.positions == {}  # 배를 못 정한 화물


def test_set_watch_on_still_works_when_seed_fails(pos_cache, monkeypatch):
    conn = FakeConn()
    conn.add()
    pos_cache.set("shipfinder:pos:440123456", _pos())
    real = conn.execute

    def broken(sql, params=()):
        if "INSERT INTO watch_positions" in sql:
            raise RuntimeError("db down")
        return real(sql, params)
    monkeypatch.setattr(conn, "execute", broken)
    assert svc.set_watch(conn, USER, BL, True) is True and conn.row()["watch"] is True


def test_watch_state():
    conn = FakeConn()
    assert svc.watch_state(conn, USER, BL) == {"saved": False, "watch": False}
    conn.add()
    assert svc.watch_state(conn, USER, BL) == {"saved": True, "watch": False}
    conn.row()["watch"] = True
    assert svc.watch_state(conn, USER, BL) == {"saved": True, "watch": True}
    conn.row()["status"] = "done"  # 반출이 끝나면 더 지켜보지 않는다
    assert svc.watch_state(conn, USER, BL) == {"saved": True, "watch": False}


def test_watch_state_without_key(monkeypatch):
    monkeypatch.delenv("BL_ENCRYPTION_KEY")
    assert svc.watch_state(FakeConn(), USER, BL) == {"saved": False, "watch": False}


# ── pending_changes / mark_seen ──

def test_pending_changes_and_mark_seen():
    conn = FakeConn()
    conn.add(bl="MOCKBL20261001", label="고추", last_verdict="act", changed_from="watch", change_seen=False,
             changed_at=datetime(2026, 10, 4, tzinfo=timezone.utc), watch=True)
    conn.add(bl="MOCKBL20261002", last_verdict="fine", changed_from="act", change_seen=False, changed_at=NOW, watch=True)
    conn.add(bl="MOCKBL20261003", watch=True)  # 변화 없음
    conn.add(bl="MOCKBL20261004", user="other", change_seen=False, changed_at=NOW, watch=True)  # 남의 변화
    changes = svc.pending_changes(conn, USER)
    assert [c["from_verdict"] for c in changes] == ["act", "watch"]  # 최근 것부터
    assert changes[1] == {"bl_masked": "MOC*******1005", "label": "고추", "kind": "verdict", "from_verdict": "watch",
                          "to_verdict": "act", "changed_at": "2026-10-04T00:00:00+00:00",
                          "eta_from": None, "eta_to": None, "eta_change_hours": None,
                          "reason": None}  # 저장된 값이 없으면 이유도 없다
    assert svc.mark_seen(conn, USER) == 2
    assert svc.pending_changes(conn, USER) == [] and svc.mark_seen(conn, USER) == 0
    assert conn.row("MOCKBL20261004", "other")["change_seen"] is False  # 남의 줄은 그대로


def test_pending_changes_only_for_watched_cargo():
    """지켜보기를 끈 화물의 띠는 보이지 않는다 (10-08 QA E1-8). 다시 켜면 안 본 기록이 그대로 보인다."""
    conn = FakeConn()
    conn.add(last_verdict="act", changed_from="watch", change_seen=False, changed_at=NOW, watch=True)
    assert len(svc.pending_changes(conn, USER)) == 1
    svc.set_watch(conn, USER, BL, False)
    assert svc.pending_changes(conn, USER) == []
    svc.set_watch(conn, USER, BL, True)
    assert [c["to_verdict"] for c in svc.pending_changes(conn, USER)] == ["act"]


def test_pending_changes_reason_from_stored_margin_and_stage():
    """이유는 화물 표에 저장된 마지막 여유 일수·통관 단계만으로 코드가 쓴다. 한쪽만 있어도 있는 것만 쓴다."""
    conn = FakeConn()
    conn.add(bl="MOCKBL20261001", change_seen=False, changed_at=NOW, last_days_margin=-2, last_stage="수입신고", watch=True)
    conn.add(bl="MOCKBL20261002", change_seen=False, changed_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
             last_days_margin=3, watch=True)
    reasons = [c["reason"] for c in svc.pending_changes(conn, USER)]
    assert reasons == ["마지막 확인 기준 판매 예정일보다 2일 늦음, 통관 수입신고", "마지막 확인 기준 여유 3일"]


# ── run_watch ──

def _watched(conn, bl=BL, **over):
    return conn.add(bl=bl, watch=True, **over)


def test_unchanged_records_no_change_but_refreshes(upserts, sent):
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert out == {"checked": 1, "changed": 0, "notified": 0, "failed": 0, "unjudged": 0, **QUIET}
    row = conn.row()
    assert row["changed_from"] is None and row["change_seen"] is True and sent == []
    assert row["last_checked_at"] == NOW and len(upserts) == 1  # 마지막 판정은 늘 갱신한다


def test_changed_records_change_and_notifies(upserts, sent):
    conn = FakeConn()
    _watched(conn, label="고추", last_verdict="watch")
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("act"))
    assert out == {"checked": 1, "changed": 1, "notified": 1, "failed": 0, "unjudged": 0, **QUIET}
    row = conn.row()
    assert (row["changed_from"], row["changed_at"], row["change_seen"], row["last_verdict"]) == ("watch", NOW, False, "act")
    [(user_id, text, link)] = sent
    assert user_id == USER and link == "https://web.example.com/"
    assert text == ("[Marvia] 고추 화물 판정이 '주의'에서 '긴급'(으)로 바뀌었습니다. "
                    "Marvia에서 이유와 할 일을 확인하세요.")
    assert BL not in text


def test_message_uses_masked_number_without_label(upserts, sent):
    conn = FakeConn()
    _watched(conn, last_verdict="act")
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("fine"))
    assert "MOC*******1005" in sent[0][1] and BL not in sent[0][1]


def test_request_is_built_like_a_user_lookup(upserts, sent):
    """저장된 값으로 같은 AgentRequest를 만들고, 기억 블록(memory)은 넣지 않는다."""
    conn = FakeConn()
    _watched(conn, bl_type="MASTER", forwarder_eta=date(2026, 10, 9), voyage_no="V12")
    seen = []
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: seen.append((req, mem)) or _resp("watch"))
    [(req, mem)] = seen
    assert (req.bl_no, req.bl_type, req.sale_due_date, req.forwarder_eta, req.voyage_no, req.resolved_mmsi) == \
        (BL, "MASTER", date(2026, 10, 20), date(2026, 10, 9), "V12", 440123456)
    assert req.session_id == "watch-20261005" and mem is None
    assert upserts[0]["request"] is req and upserts[0]["now"] == NOW and upserts[0]["user_id"] == USER


def test_first_judgement_without_previous_verdict_is_not_a_change(upserts, sent):
    conn = FakeConn()
    _watched(conn, last_verdict=None)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("act"))
    assert out["changed"] == 0 and sent == [] and conn.row()["last_verdict"] == "act"


def test_empty_verdict_keeps_previous_baseline(upserts, sent):
    """판정이 없는 결과(되묻기·일시 실패)는 화물 표를 덮지 않는다. 기준 등급이 지워지면 다음 변화를 못 잡는다."""
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp(None))
    assert out["unjudged"] == 1 and out["failed"] == 0 and out["changed"] == 0
    assert upserts == [] and conn.row()["last_verdict"] == "watch"


def test_not_linked_is_silent(upserts, monkeypatch):
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    failures = []
    monkeypatch.setattr(svc, "log_failure", lambda stage, error: failures.append((stage, error)))

    def not_linked(*a):
        raise KakaoLinkError("not_linked")

    monkeypatch.setattr(svc.kakao, "send_to_user", not_linked)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("act"))
    assert (out["changed"], out["notified"], out["failed"]) == (1, 0, 0)
    assert failures == []  # 조용히 넘어간다
    assert conn.row()["change_seen"] is False  # 화면 띠용 기록은 남는다


@pytest.mark.parametrize("error", [KakaoLinkError("relink"), KakaoLinkError("no_key"), KakaoError("no_consent"),
                                   KakaoError("failed")])
def test_other_notify_errors_log_kind_only(upserts, monkeypatch, error):
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    failures = []
    monkeypatch.setattr(svc, "log_failure", lambda stage, kind: failures.append((stage, kind)))

    def boom(*a):
        raise error

    monkeypatch.setattr(svc.kakao, "send_to_user", boom)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("act"))
    assert (out["changed"], out["notified"], out["failed"]) == (1, 0, 0)  # 알림 실패는 화물 실패가 아니다
    assert failures == [("watch_notify", error.kind)]


def test_one_cargo_failing_does_not_stop_others(upserts, sent, monkeypatch):
    conn = FakeConn()
    for i, day in enumerate((1, 2, 3)):
        _watched(conn, bl=f"MOCKBL2026100{i}", last_checked_at=datetime(2026, 10, day, tzinfo=timezone.utc))
    failures = []
    monkeypatch.setattr(svc, "log_failure", lambda stage, kind: failures.append((stage, kind)))

    def assess(req, mem):
        if req.bl_no == "MOCKBL20261000":
            raise RuntimeError(f"upstream said {req.bl_no}")  # 예외 글에 B/L이 있어도 로그에는 종류만 간다
        return _resp("act")

    out = svc.run_watch(conn, now=NOW, assess=assess)
    assert (out["checked"], out["failed"], out["changed"]) == (2, 1, 2)
    assert failures == [("watch_cargo", "RuntimeError")]
    assert "MOCKBL" not in str(failures)


def test_cargo_with_unreadable_bl_counts_as_failed(upserts, sent, monkeypatch):
    conn = FakeConn()
    _watched(conn, bl_encrypted=None)
    failures = []
    monkeypatch.setattr(svc, "log_failure", lambda stage, kind: failures.append((stage, kind)))
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: pytest.fail("원문 없이 조회하면 안 된다"))
    assert out["failed"] == 1 and failures == [("watch_cargo", "no_bl")]


def test_limit_and_oldest_first(upserts, sent):
    conn = FakeConn()
    for i in range(7):
        _watched(conn, bl=f"MOCKBL2026100{i}", last_checked_at=datetime(2026, 10, 7 - i, tzinfo=timezone.utc))
    _watched(conn, bl="MOCKBL20261009", last_checked_at=None)  # 한 번도 안 본 줄이 가장 먼저
    conn.add(bl="MOCKBL20261008", watch=False, last_checked_at=None)  # 안 지켜보는 줄은 안 본다
    conn.add(bl="MOCKBL20261007", watch=True, status="done", last_checked_at=None)  # 끝난 줄도 안 본다
    seen = []
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: seen.append(req.bl_no) or _resp("watch"))
    assert out["checked"] == 5
    assert seen == ["MOCKBL20261009", "MOCKBL20261006", "MOCKBL20261005", "MOCKBL20261004", "MOCKBL20261003"]
    assert svc.run_watch(conn, limit=2, now=NOW, assess=lambda req, mem: _resp("watch"))["checked"] == 2


def test_released_cargo_becomes_done(upserts, sent):
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("fine", stage="반출신고"))
    assert upserts[0]["released"] is True and conn.row()["status"] == "done"
    assert out["changed"] == 1  # 주의 → 순항도 등급이 바뀐 것이다
    # 다음 실행부터는 끝난 화물이라 보지 않는다
    assert svc.run_watch(conn, now=NOW, assess=lambda req, mem: pytest.fail("끝난 화물")) ["checked"] == 0


def test_default_assess_is_the_user_lookup_path(upserts, sent, monkeypatch):
    """assess를 안 주면 조회 화면과 같은 routers.agent._assess를 부른다 (판정 길이 하나여야 두 답이 어긋나지 않는다)."""
    from backend.app.routers import agent

    # 이 테스트는 v1 기본(_assess)을 본다. AGENT_GRAPH=on이면 기본이 그래프 판정(facade.assess_graph)으로 바뀌므로 꺼 둔다.
    # 켠 쪽의 기본은 test_graph_state_flow.py가 본다 (v2-langgraph.md 10절 H3)
    monkeypatch.setenv("AGENT_GRAPH", "off")
    conn = FakeConn()
    _watched(conn)
    seen = []
    monkeypatch.setattr(agent, "_assess", lambda req, mem=None: seen.append((req.bl_no, mem)) or _resp("watch"))
    assert svc.run_watch(conn, now=NOW)["checked"] == 1 and seen == [(BL, None)]


# ── 하루 토큰 한도. 재판정도 그 사용자의 LLM 사용량이다 ──

def test_rejudge_is_scoped_to_the_cargo_owner(upserts, sent, monkeypatch):
    """session_scope에 user_id를 넘겨 토큰이 그 사람 몫으로 쌓인다 (services/quota.py owner_var)."""
    from backend.app.services import quota
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    owners = []
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: owners.append(quota.owner_var.get()) or _resp("watch"))
    assert owners == [f"user:{USER}"]


def test_rejudge_skipped_when_owner_is_over_quota(upserts, sent, monkeypatch):
    from backend.app.services import quota
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    failures = []
    monkeypatch.setattr(svc, "log_failure", lambda stage, kind: failures.append((stage, kind)))

    def over(session_id, user_id):
        raise quota.QuotaExceeded(user_id)

    monkeypatch.setattr(quota, "ensure_allowed", over)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: pytest.fail("한도를 다 쓴 사람의 화물은 재판정하지 않는다"))
    assert out["failed"] == 1 and failures == [("watch_cargo", "quota")] and upserts == []


def test_known_vessel_only_skips_cargo_without_vessel(upserts, sent):
    """워커 예산 안에서 돌 때(known_vessel_only)는 배를 모르는 화물을 다시 판정하지 않고 skipped_no_vessel로 센다.
    배를 모르면 후보 검색으로 ShipFinder를 최대 11회 써서 화물당 1회로 떼어 둔 예산을 깬다."""
    conn = FakeConn()
    _watched(conn, bl="MOCKBL20261001", vessel_mmsi=None, last_checked_at=None)
    _watched(conn, bl="MOCKBL20261002", vessel_mmsi=440999001)
    seen = []
    out = svc.run_watch(conn, now=NOW, known_vessel_only=True,
                        assess=lambda req, mem: seen.append((req.bl_no, req.resolved_mmsi)) or _resp("watch"))
    assert seen == [("MOCKBL20261002", 440999001)]
    assert out["checked"] == 1 and out["skipped_no_vessel"] == 1
    assert svc.known_vessel_due_count(conn) == 1


def test_default_run_watch_still_checks_cargo_without_vessel(upserts, sent):
    """python -m worker.watch(예산 없음)는 지금처럼 배를 모르는 화물도 다시 판정한다. 결과 모양도 그대로다."""
    conn = FakeConn()
    _watched(conn, vessel_mmsi=None)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert out == {"checked": 1, "changed": 0, "notified": 0, "failed": 0, "unjudged": 0, **QUIET}


# ── 도착 예정 변화 알림 (port_facts.eta_change) ──

def _moved(verdict, change):
    """판정과 도착 예정 변화를 가진 가짜 응답."""
    return SimpleNamespace(verdict=verdict, customs=SimpleNamespace(stage_name="통관진행"), eta_change=change)


def test_eta_only_change_notifies_without_verdict_change(upserts, sent):
    from backend.app.schemas.agent import EtaChange
    conn = FakeConn()
    _watched(conn, label="고추", last_verdict="watch", last_eta=date(2026, 10, 5))
    seen = []

    def assess(req, mem):
        from backend.app.routers.agent import previous_eta
        seen.append(previous_eta.get())  # 저장된 지난 ETA가 판정 흐름에 넘어간다
        return _moved("watch", EtaChange(from_eta=date(2026, 10, 5), to_eta=date(2026, 10, 7), hours=48))

    out = svc.run_watch(conn, now=NOW, assess=assess)
    assert seen == [date(2026, 10, 5)]
    assert out == {"checked": 1, "changed": 0, "notified": 1, "failed": 0, "unjudged": 0, **QUIET}
    assert conn.row()["changed_from"] is None  # 등급 변화 기록(이전 등급)은 남기지 않는다
    # 도착 예정만 바뀐 것도 띠에 남는다 (10-08 QA E1-7). 카카오를 안 쓰는 사용자도 다음 접속 때 본다
    [band] = svc.pending_changes(conn, USER)
    assert (band["kind"], band["from_verdict"], band["to_verdict"]) == ("eta", "watch", "watch")
    assert (band["eta_from"], band["eta_to"], band["eta_change_hours"]) == ("2026-10-05", "2026-10-07", 48)
    assert band["reason"].startswith("도착 예정이 10월 5일에서 10월 7일로 약 48시간 늦어졌습니다.")
    [(_, text, _)] = sent
    assert text == ("[Marvia] 고추 화물의 도착 예정이 10월 5일에서 10월 7일로 약 48시간 늦어졌습니다. "
                    "Marvia에서 이유와 할 일을 확인하세요.")


def test_verdict_and_eta_change_in_one_message(upserts, sent):
    from backend.app.schemas.agent import EtaChange
    conn = FakeConn()
    _watched(conn, label="고추", last_verdict="watch", last_eta=date(2026, 10, 7))
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: _moved(
        "act", EtaChange(from_eta=date(2026, 10, 7), to_eta=date(2026, 10, 6), hours=-24)))
    [(_, text, _)] = sent
    assert "판정이 '주의'에서 '긴급'(으)로 바뀌었습니다. 도착 예정이 10월 7일에서 10월 6일로 약 24시간 당겨졌습니다." in text


def test_no_eta_change_no_extra_notice(upserts, sent):
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _moved("watch", None))
    assert out["notified"] == 0 and sent == []


# ── 10-08 QA E1: 굶김·자동 확인 멈춤 (E1-1) ──

def test_failing_cargo_moves_back_and_does_not_starve_others(upserts, sent):
    """실패한 화물도 시도 시각이 남아 다음 실행에서는 뒤로 간다. 예전에는 늘 맨 앞에 서서 다른 화물을 굶겼다."""
    conn = FakeConn()
    _watched(conn, bl="MOCKBL20261001", last_checked_at=datetime(2026, 9, 1, tzinfo=timezone.utc))  # 가장 오래됐고 늘 실패
    _watched(conn, bl="MOCKBL20261002", last_checked_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    _watched(conn, bl="MOCKBL20261003", last_checked_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    order = []

    def assess(req, mem):
        order.append(req.bl_no)
        if req.bl_no == "MOCKBL20261001":
            raise RuntimeError("mcp down")
        return _resp("watch")

    for hour in range(3):
        svc.run_watch(conn, limit=1, now=NOW.replace(hour=hour + 4), assess=assess)
    assert order == ["MOCKBL20261001", "MOCKBL20261002", "MOCKBL20261003"]
    assert conn.row("MOCKBL20261001")["watch_fail_count"] == 1


def test_consecutive_failures_pause_until_user_looks_again(upserts, sent):
    """rules.md watch_pause_after_failures번 잇달아 실패(판정 없음 포함)하면 고르지 않는다. 사용자가 다시 조회하면 풀린다."""
    from datetime import timedelta
    conn = FakeConn()
    _watched(conn)
    n = svc.pause_after()
    for i in range(n):
        out = svc.run_watch(conn, now=NOW + timedelta(hours=i), assess=lambda req, mem: _resp(None))
        assert out["unjudged"] == 1
    assert conn.row()["watch_fail_count"] == n
    calls = []
    out = svc.run_watch(conn, now=NOW + timedelta(hours=n), assess=lambda req, mem: calls.append(1) or _resp("watch"))
    assert out["checked"] == 0 and calls == [] and svc.known_vessel_due_count(conn) == 0
    # 사용자가 화면에서 다시 조회하면 last_checked_at이 마지막 시도보다 새로워져 풀린다 (cargo.upsert_assessment)
    conn.row()["last_checked_at"] = NOW + timedelta(hours=n, minutes=30)
    out = svc.run_watch(conn, now=NOW + timedelta(hours=n + 1), assess=lambda req, mem: _resp(None))
    assert out["unjudged"] == 1 and conn.row()["watch_fail_count"] == 1  # 다시 1부터 센다
    out = svc.run_watch(conn, now=NOW + timedelta(hours=n + 2), assess=lambda req, mem: _resp("watch"))
    assert out["checked"] == 1 and conn.row()["watch_fail_count"] == 0  # 성공하면 0


def test_is_paused_matches_rule():
    n = svc.pause_after()
    assert svc.is_paused(n, NOW, NOW.replace(hour=1)) is True
    assert svc.is_paused(n, NOW, None) is True
    assert svc.is_paused(n, NOW.replace(hour=1), NOW) is False  # 시도 뒤에 사용자가 조회했다
    assert svc.is_paused(n - 1, NOW, None) is False


def test_turning_watch_on_again_resets_failures():
    conn = FakeConn()
    conn.add(watch_fail_count=7, watch_attempt_at=NOW)
    svc.set_watch(conn, USER, BL, True)
    assert conn.row()["watch_fail_count"] == 0


def test_quota_failure_moves_back_but_is_not_counted(upserts, sent, monkeypatch):
    """그 사용자의 오늘 LLM 한도는 화물 탓이 아니다. 시도 시각만 남기고 연속 실패로 세지 않는다."""
    from backend.app.services import quota
    conn = FakeConn()
    _watched(conn)

    def over(session_id, user_id):
        raise quota.QuotaExceeded(user_id)
    monkeypatch.setattr(quota, "ensure_allowed", over)
    for _ in range(5):
        svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert conn.row()["watch_fail_count"] == 0 and conn.row()["watch_attempt_at"] == NOW


def test_attempt_record_failure_does_not_break_run(upserts, sent, monkeypatch):
    conn = FakeConn()
    _watched(conn)
    real = conn.execute

    def broken(sql, params=()):
        if "watch_attempt_at = %s" in sql:
            raise RuntimeError("db down")
        return real(sql, params)
    monkeypatch.setattr(conn, "execute", broken)
    assert svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))["checked"] == 1


# ── 알림 보류와 다시 보내기 (E1-2) ──

def test_network_error_holds_notice_and_next_run_resends(upserts, monkeypatch):
    """httpx.ConnectError처럼 예상 밖 예외도 잡아 보낼 글을 남기고, 다음 실행에서 다시 보낸다. 실행은 failed가 아니다."""
    import httpx
    conn = FakeConn()
    _watched(conn, label="양파", last_verdict="fine")
    failures, sent = [], []
    monkeypatch.setattr(svc, "log_failure", lambda stage, kind: failures.append((stage, kind)))

    def down(conn_, user_id, text, link):
        raise httpx.ConnectError("kakao down")
    monkeypatch.setattr(svc.kakao, "send_to_user", down)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("act"))
    assert (out["changed"], out["notified"], out["notify_held"], out["failed"]) == (1, 0, 1, 0)
    assert ("watch_notify", "ConnectError") in failures and "kakao down" not in str(failures)
    held = conn.row()["notify_pending"]
    assert held.startswith("[Marvia] 양파 화물 판정이") and BL not in held and conn.row()["notify_pending_at"] == NOW
    assert conn.row()["change_seen"] is False  # 띠 기록은 그대로

    monkeypatch.setattr(svc.kakao, "send_to_user", lambda conn_, user_id, text, link: sent.append(text))
    out = svc.run_watch(conn, now=NOW.replace(hour=9), assess=lambda req, mem: _resp("act"))  # 이번엔 등급 그대로
    assert out["renotified"] == 1 and sent == [held]
    assert conn.row()["notify_pending"] is None and conn.row()["notify_pending_at"] is None


def test_kakao_failed_kind_is_held_but_relink_is_not(upserts, monkeypatch):
    conn = FakeConn()
    _watched(conn, last_verdict="fine")

    def failed(*a):
        raise KakaoError("failed")
    monkeypatch.setattr(svc.kakao, "send_to_user", failed)
    assert svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("act"))["notify_held"] == 1

    def relink(*a):
        raise KakaoLinkError("relink")
    monkeypatch.setattr(svc.kakao, "send_to_user", relink)
    out = svc.run_watch(conn, now=NOW.replace(hour=9), assess=lambda req, mem: _resp("act"))
    assert out["renotified"] == 0 and conn.row()["notify_pending"] is None  # 다시 연결해야 하는 실패는 쌓지 않는다


def test_stale_held_notice_is_dropped(upserts, sent):
    """rules.md watch_notify_retry_hours보다 오래된 보류 글은 보내지 않고 지운다 (낡은 알림). 끈 화물은 보내지 않는다."""
    from datetime import timedelta
    conn = FakeConn()
    hours = float(svc.rule("watch_notify_retry_hours"))
    _watched(conn, notify_pending="[Marvia] old", notify_pending_at=NOW - timedelta(hours=hours + 1))
    _watched(conn, bl="MOCKBL20261009", notify_pending="[Marvia] fresh", notify_pending_at=NOW - timedelta(hours=1))
    conn.add(bl="MOCKBL20261008", notify_pending="[Marvia] off", notify_pending_at=NOW)
    out = svc.run_watch(conn, limit=0, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert (out["notify_dropped"], out["renotified"]) == (1, 1)
    assert [t for _, t, _ in sent] == ["[Marvia] fresh"]
    assert conn.row()["notify_pending"] is None and conn.row("MOCKBL20261008")["notify_pending"] == "[Marvia] off"


# ── 도착 예정 변화 띠 (E1-7) ──

def test_seen_verdict_change_is_not_repeated_by_eta_change(upserts, sent):
    """이미 본 등급 변화는 새 도착 예정 변화 띠에 다시 붙지 않는다. 안 본 상태에서 등급도 바뀌면 둘 다 남는다(kind both)."""
    from backend.app.schemas.agent import EtaChange
    conn = FakeConn()
    _watched(conn, last_verdict="act", changed_from="watch", changed_at=NOW, change_seen=True)
    move = EtaChange(from_eta=date(2026, 10, 7), to_eta=date(2026, 10, 8), hours=24)
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: _moved("act", move))
    [band] = svc.pending_changes(conn, USER)
    assert band["kind"] == "eta" and conn.row()["changed_from"] is None
    svc.run_watch(conn, now=NOW.replace(hour=9), assess=lambda req, mem: _moved("fine", None))
    [band] = svc.pending_changes(conn, USER)
    assert (band["kind"], band["from_verdict"], band["to_verdict"], band["eta_change_hours"]) == ("both", "act", "fine", 24)


# ── 켜기 동시성·안내 (E1-10, E1-9) ──

def test_conditional_update_refuses_when_count_changed_in_between():
    """한도 세기와 켜기 사이에 다른 요청이 자리를 채웠으면 조건부 UPDATE가 0줄이라 limit이다."""
    conn = FakeConn()
    for i in range(4):
        conn.add(bl=f"MOCKBL2026100{i}", watch=True)
    conn.add()
    real = conn.execute

    def racing(sql, params=()):
        if sql.lstrip().startswith("UPDATE user_cargo SET watch = true"):
            conn.add(bl="MOCKBL20261009", watch=True)  # 다른 요청이 먼저 다섯 번째를 켰다
        return real(sql, params)
    conn.execute = racing
    with pytest.raises(svc.WatchError) as e:
        svc.set_watch(conn, USER, BL, True)
    assert e.value.kind == "limit" and conn.row()["watch"] is False


def test_set_watch_takes_user_lock_inside_transaction():
    """트랜잭션을 여는 연결(psycopg)이면 사용자별 advisory 잠금을 잡고 그 안에서 센다. 첫 위치 점은 잠금 밖이다."""
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
            self.events.append(" ".join(sql.split())[:30])
            return super().execute(sql, params)

    conn = TxConn()
    conn.add()
    assert svc.set_watch(conn, USER, BL, True) is True
    assert conn.events[:2] == ["begin", ("lock", f"cargo_watch:{USER}")]
    assert conn.events.index("commit") < conn.events.index("SELECT vessel_mmsi FROM user_c")


def test_vessel_known():
    conn = FakeConn()
    conn.add()
    conn.add(bl="MOCKBL20261001", vessel_mmsi=None)
    assert svc.vessel_known(conn, USER, BL) is True
    assert svc.vessel_known(conn, USER, "MOCKBL20261001") is False
    assert svc.vessel_known(conn, USER, "MOCKBL29999999") is False  # 줄 없음


# ── 위치 시각 (E1-6) ──

def test_usable_position_time_drops_future_and_truncates():
    from datetime import timedelta
    tol = float(svc.rule("position_future_tolerance_minutes"))
    assert svc.usable_position_time(NOW + timedelta(minutes=tol + 1), NOW) is None
    assert svc.usable_position_time(NOW + timedelta(minutes=tol - 1), NOW) == NOW + timedelta(minutes=tol - 1)
    assert svc.usable_position_time(NOW.replace(microsecond=987654), NOW) == NOW
    assert svc.usable_position_time(None, NOW) is None
    naive = datetime(2026, 10, 5, 2, 59, 59, 500000)  # 시간대 없는 값은 UTC로 읽는다
    assert svc.usable_position_time(naive, NOW) == datetime(2026, 10, 5, 2, 59, 59, tzinfo=timezone.utc)


def test_seed_skips_future_and_null_island(pos_cache):
    conn = FakeConn()
    pos_cache.set("shipfinder:pos:440123456", _pos(last_time=datetime(2099, 1, 1, tzinfo=timezone.utc)))
    assert svc.seed_position(conn, 440123456) is False
    zero = _pos()
    zero.lat, zero.lng = 0.0, 0.0
    pos_cache.set("shipfinder:pos:440123456", zero)
    assert svc.seed_position(conn, 440123456) is False and conn.positions == {}
