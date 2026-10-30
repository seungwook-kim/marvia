"""H8: 지켜보기 재판정 그래프 (graph/watch_graph.py). 근거: docs/specs/v2-langgraph.md 6절, 10절 H8, AC-11.

DB·카카오·LLM·ShipFinder는 부르지 않는다. test_watch.py의 가짜 연결(FakeConn), 가짜 화물 표 쓰기(upserts), 가짜 알림(sent)을 그대로 쓴다.
판정 함수(assess)는 이 파일의 가짜다. 가짜 ShipFinder는 "건당 호출 수"를 세는 가짜 판정 함수로 대신한다. B/L은 모의값이다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.graph import watch_graph
from backend.app.routers import agent as agent_router
from backend.app.services import quota
from backend.app.services import watch as svc
from backend.tests.test_watch import BL, NOW, QUIET, USER, FakeConn, _resp, _watched, env, sent, upserts  # noqa: F401  (pytest 픽스처 포함)

BLS = [f"MOCKBL2026100{i}" for i in range(1, 6)]  # 모의 B/L 5건


@pytest.fixture
def graph_on(monkeypatch):
    monkeypatch.setenv("AGENT_GRAPH", "on")


def _five(conn: FakeConn, **over) -> list[dict]:
    """5건을 만든다. 마지막 확인이 오래된 순서가 BLS 순서(select_due 순서)가 되도록 시각을 벌린다."""
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return [_watched(conn, bl=bl, last_verdict="watch", last_checked_at=base + timedelta(hours=i),
                     vessel_mmsi=440000000 + i, **over) for i, bl in enumerate(BLS)]


def _snapshot(conn: FakeConn) -> list[tuple]:
    """반영 결과 비교용: 화물별 마지막 판정, 확인 시각, 실패 횟수, 변화 기록."""
    return [(r["cargo_key"], r["last_verdict"], r["last_checked_at"], r["watch_fail_count"], r["watch_attempt_at"],
             r["changed_from"], r["change_seen"]) for r in conn.rows]


def _by_bl(verdicts: dict[str, str]):
    return lambda req, mem: _resp(verdicts[req.bl_no])


# ── 어느 길로 도는가 ──

def test_graph_path_runs_only_when_agent_graph_is_on(monkeypatch, upserts, sent):
    """AGENT_GRAPH=off면 순차 길(그래프를 부르지 않는다), on이면 그래프 길이다."""
    calls = []
    real = watch_graph.run_watch_graph
    monkeypatch.setattr(watch_graph, "run_watch_graph", lambda *a, **k: calls.append(1) or real(*a, **k))
    conn = FakeConn()
    _watched(conn, last_verdict="watch")
    monkeypatch.setenv("AGENT_GRAPH", "off")
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert calls == []
    monkeypatch.setenv("AGENT_GRAPH", "on")
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert calls == [1]


def test_default_assess_is_the_graph_assess_when_on(monkeypatch, graph_on):
    """assess를 주지 않으면 켠 때 graph.facade.assess_graph가 기본이다(H3 그대로)."""
    from backend.app.graph import facade

    seen = []
    monkeypatch.setattr(watch_graph, "run_watch_graph", lambda conn, **k: seen.append(k["assess"]) or {})
    svc.run_watch(FakeConn(), now=NOW)
    assert seen == [facade.assess_graph]


# ── 병렬 결과 = 순차 결과 ──

def test_parallel_result_equals_sequential_result(monkeypatch, upserts, sent):
    """같은 5건을 순차 길과 그래프 길로 돌리면 요약, 화물 표, 알림, 반영 순서가 같다."""
    verdicts = dict(zip(BLS, ["watch", "act", "fine", "watch", "act"]))  # 일부는 등급이 바뀐다
    outs, snaps, notices, order = {}, {}, {}, {}
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        conn = FakeConn()
        _five(conn)
        sent.clear()
        upserts.clear()
        outs[mode] = svc.run_watch(conn, now=NOW, assess=_by_bl(verdicts))
        snaps[mode], notices[mode], order[mode] = _snapshot(conn), list(sent), [c["request"].bl_no for c in upserts]
    assert outs["off"] == outs["on"] and outs["on"]["checked"] == 5 and outs["on"]["changed"] == 3
    assert snaps["off"] == snaps["on"] and notices["off"] == notices["on"] and order["off"] == order["on"] == BLS


def test_width_one_matches_sequential_call_order(monkeypatch, graph_on, upserts, sent):
    """watch_parallel_max가 1이면 판정 호출 순서가 순차 길과 같다(select_due 순서)."""
    monkeypatch.setattr(watch_graph, "rule", lambda key: 1 if key == "watch_parallel_max" else None)
    conn = FakeConn()
    _five(conn)
    called = []
    svc.run_watch(conn, now=NOW, assess=lambda req, mem: called.append(req.bl_no) or _resp("watch"))
    assert called == BLS


# ── 건별 격리 (EC-10, EC-11, AC-11) ──

def test_one_raising_item_is_isolated_and_applied_in_input_order(monkeypatch, graph_on, upserts, sent):
    """5건 중 1건이 예외: failed=1, 나머지 4건은 반영되고 실패 건만 watch_fail_count가 오른다.
    앞선 건일수록 오래 걸리게 해서(완료 순서가 거꾸로) 반영 순서가 완료 순서가 아니라 입력 순서임을 본다."""
    conn = FakeConn()
    _five(conn)
    failures = []
    monkeypatch.setattr(svc, "log_failure", lambda stage, kind: failures.append((stage, kind)))

    def assess(req, mem):
        time.sleep(0.05 * (len(BLS) - BLS.index(req.bl_no)))
        if req.bl_no == BLS[2]:
            raise RuntimeError("boom")
        return _resp("act")

    out = svc.run_watch(conn, now=NOW, assess=assess)
    assert out["checked"] == 4 and out["failed"] == 1 and out["changed"] == 4
    assert failures == [("watch_cargo", "RuntimeError")]
    assert [c["request"].bl_no for c in upserts] == [b for b in BLS if b != BLS[2]]
    counts = {r["cargo_key"]: r["watch_fail_count"] for r in conn.rows}
    failed_key = conn.row(BLS[2])["cargo_key"]
    assert counts[failed_key] == 1 and sum(counts.values()) == 1
    assert conn.row(BLS[2])["watch_attempt_at"] == NOW  # 시도 시각은 남겨 뒤로 보낸다


def test_quota_item_is_skipped_and_not_counted_as_failure(monkeypatch, graph_on, upserts, sent):
    """한 건은 예외, 한 건은 하루 토큰 한도 소진: 나머지 3건은 반영되고 한도 건은 연속 실패에 세지 않는다 (AC-11)."""
    conn = FakeConn()
    _five(conn)
    limited = conn.row(BLS[1])["user_id"]
    other = "user-limited"
    conn.row(BLS[1])["user_id"] = other  # 한 건만 다른 사용자

    def ensure(_conn, user_id):
        if user_id == other:
            raise quota.QuotaExceeded(user_id)

    monkeypatch.setattr(quota, "ensure_allowed", ensure)
    monkeypatch.setattr(svc, "log_failure", lambda *a: None)

    def assess(req, mem):
        if req.bl_no == BLS[3]:
            raise ValueError("bad")
        return _resp("act")

    out = svc.run_watch(conn, now=NOW, assess=assess)
    assert out["checked"] == 3 and out["failed"] == 2 and [c["request"].bl_no for c in upserts] == [BLS[0], BLS[2], BLS[4]]
    assert limited == USER
    assert conn.row(BLS[1], other)["watch_fail_count"] == 0 and conn.row(BLS[1], other)["watch_attempt_at"] == NOW
    assert conn.row(BLS[3])["watch_fail_count"] == 1


def test_apply_failure_counts_only_that_item(monkeypatch, graph_on, upserts, sent):
    """반영 도중 DB 오류도 그 건만 실패로 센다(순차 길과 같다). 나머지는 이어서 반영된다."""
    conn = FakeConn()
    _five(conn)
    real = svc.cargo.upsert_assessment

    def flaky(conn_, user_id, request, response, **k):
        if request.bl_no == BLS[0]:
            raise RuntimeError("db down")
        return real(conn_, user_id, request, response, **k)

    monkeypatch.setattr(svc.cargo, "upsert_assessment", flaky)
    monkeypatch.setattr(svc, "log_failure", lambda *a: None)
    out = svc.run_watch(conn, now=NOW, assess=lambda req, mem: _resp("watch"))
    assert out["failed"] == 1 and out["checked"] == 4 and conn.row(BLS[0])["watch_fail_count"] == 1


# ── ShipFinder 호출 상한 (6절) ──

def test_calls_never_exceed_limit_and_one_per_cargo(graph_on, upserts, sent):
    """가짜 ShipFinder(판정 함수가 세는 호출)는 건당 1회이고, 합이 limit을 넘지 않는다. 마지막 확인이 오래된 순서로 limit건만 고른다."""
    conn = FakeConn()
    _five(conn)
    calls: dict[str, int] = {}
    lock = threading.Lock()

    def assess(req, mem):
        with lock:
            calls[req.bl_no] = calls.get(req.bl_no, 0) + 1
        return _resp("watch")

    out = svc.run_watch(conn, limit=3, now=NOW, assess=assess)
    assert out["checked"] == 3 and sum(calls.values()) == 3 and max(calls.values()) == 1
    assert sorted(calls) == BLS[:3]


def test_unknown_vessel_cargo_gets_zero_calls_when_known_vessel_only(graph_on, upserts, sent):
    """배를 모르는 화물은 known_vessel_only에서 판정 호출 0회이고 skipped_no_vessel로 센다."""
    conn = FakeConn()
    _five(conn)
    conn.row(BLS[0])["vessel_mmsi"] = None
    calls = []
    out = svc.run_watch(conn, now=NOW, known_vessel_only=True, assess=lambda req, mem: calls.append(req.bl_no) or _resp("watch"))
    assert BLS[0] not in calls and len(calls) == 4 and out["skipped_no_vessel"] == 1 and out["checked"] == 4


# ── 동시 실행 폭 (rules.md watch_parallel_max) ──

@pytest.mark.parametrize("width", [1, 2, 3])
def test_concurrency_peak_does_not_exceed_rule_value(monkeypatch, graph_on, upserts, sent, width):
    """가짜 판정이 동시 실행 수를 센다. 최댓값이 watch_parallel_max 이하이고, 2 이상이면 실제로 겹쳐 돈다."""
    monkeypatch.setattr(watch_graph, "rule", lambda key: width if key == "watch_parallel_max" else None)
    conn = FakeConn()
    _five(conn)
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def assess(req, mem):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.1)
        with lock:
            state["now"] -= 1
        return _resp("watch")

    svc.run_watch(conn, now=NOW, assess=assess)
    assert state["peak"] <= width
    if width > 1:
        assert state["peak"] == width


def test_width_comes_from_rules_not_code(monkeypatch):
    """폭은 rules.md의 watch_parallel_max에서 읽고 1 미만은 1로 본다."""
    from backend.app.services.rules import rule

    assert watch_graph.parallel_width() == rule("watch_parallel_max") >= 1
    monkeypatch.setattr(watch_graph, "rule", lambda key: 0)
    assert watch_graph.parallel_width() == 1


# ── ContextVar 섞임 없음, State에 B/L 없음 ──

def test_context_vars_do_not_mix_between_parallel_items(graph_on, upserts, sent):
    """서로 다른 배(trusted_vessel)와 지난 ETA(previous_eta)를 가진 화물 둘이 동시에 돌 때 각자 자기 값만 본다.
    두 판정이 같은 시각에 겹치도록 장벽(Barrier)을 건다."""
    conn = FakeConn()
    a = _watched(conn, bl=BLS[0], vessel_mmsi=440111111, last_eta=datetime(2026, 10, 11).date())
    b = _watched(conn, bl=BLS[1], vessel_mmsi=440222222, last_eta=datetime(2026, 10, 22).date())
    a["last_checked_at"], b["last_checked_at"] = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc)
    barrier = threading.Barrier(2, timeout=5)
    seen = {}

    def assess(req, mem):
        barrier.wait()  # 둘이 함께 여기 도착해야 지난다. 직렬이면 시간 초과로 실패한다
        seen[req.bl_no] = (agent_router.trusted_vessel.get(), agent_router.previous_eta.get(), req.resolved_mmsi)
        barrier.wait()
        return _resp("watch")

    svc.run_watch(conn, now=NOW, assess=assess)
    assert seen[BLS[0]] == (440111111, datetime(2026, 10, 11).date(), 440111111)
    assert seen[BLS[1]] == (440222222, datetime(2026, 10, 22).date(), 440222222)
    assert agent_router.trusted_vessel.get() is None and agent_router.previous_eta.get() is None  # 부른 쪽 문맥은 그대로


def test_state_has_no_plaintext_bl_and_requests_are_released(upserts, sent):
    """그래프 State(최종값)에는 B/L 원문이 없고, 요청 객체를 둔 실행 지역 딕셔너리는 반영 뒤 비어 있다. 응답 객체에도 가린 번호만 담긴다."""
    conn = FakeConn()
    _five(conn)
    requests: dict = {}
    graph = watch_graph.build_watch_graph(conn, limit=5, now=NOW, assess=lambda req, mem: _resp("watch"),
                                          known_vessel_only=False, requests=requests)
    out = graph.invoke({}, config={"max_concurrency": 2})
    dumped = repr(out)
    assert all(bl not in dumped for bl in BLS)
    assert set(out) == {"rows", "summary", "results"} and requests == {}
    assert [r["index"] for r in out["results"]] and out["summary"]["checked"] == 5


def test_nothing_due_goes_straight_to_apply(upserts, sent):
    """고른 화물이 없으면 판정 호출 없이 요약만 돌려준다(Send 0개). 요약 칸은 순차 길과 같다."""
    out = watch_graph.run_watch_graph(FakeConn(), limit=5, now=NOW, known_vessel_only=False,
                                      assess=lambda req, mem: pytest.fail("대상이 없는데 판정을 불렀다"))
    assert out == {"checked": 0, "changed": 0, "notified": 0, "failed": 0, "unjudged": 0, **QUIET}


def test_summary_keys_match_sequential_for_known_vessel_only(monkeypatch, upserts, sent):
    """known_vessel_only면 두 길 모두 skipped_no_vessel 칸이 들어 있고 값이 같다."""
    outs = {}
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        conn = FakeConn()
        _five(conn)
        conn.row(BLS[4])["vessel_mmsi"] = None
        outs[mode] = svc.run_watch(conn, now=NOW, known_vessel_only=True, assess=lambda req, mem: _resp("watch"))
    assert outs["off"] == outs["on"] and outs["on"]["skipped_no_vessel"] == 1
