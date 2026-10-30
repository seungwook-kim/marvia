"""H3: 판정 뒤쪽을 노드로 나눈 그래프와 State로 건너가는 값 (docs/specs/v2-langgraph.md 2절 ContextVar 이전, 10절 H3, AC-03).

키도 DB도 필요 없다. 같은 가짜 도구(test_routers.fake)로 노드를 하나씩, 또 그래프 전체를 돌려
(1) compute_signals가 코드 함수(signals.compute 등)와 같은 값을 State에 쓰고 LLM 칸은 쓰지 않는지,
(2) previous_eta, trusted_vessel, db_failed가 ContextVar가 아니라 State로 건너가는지,
(3) 지켜보기 재판정 경로(assess_graph)가 v1 _assess와 같은 결과를 내는지 본다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.app.graph import nodes
from backend.app.graph.build import build_graph
from backend.app.graph.facade import assess_graph
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest
from backend.app.schemas.customs import CustomsRecord, mask_bl
from backend.app.services import port_facts
from backend.app.services import signals as signals_service
from backend.app.services.evidence import days_margin
from backend.tests.test_graph_parity import _dump, _req
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, NOW, POSITION, _candidate, _signals, fake,
)
from backend.tests.test_watch import FakeConn, _resp, _watched, env, sent, upserts  # noqa: F401  (픽스처)
from backend.tests.test_watch import svc as watch_svc

# LLM이나 판정 표가 쓰는 칸. compute_signals는 이 중 어느 것도 쓰면 안 된다 (AC-03)
NOT_COMPUTE_SIGNALS = {"response", "judged", "judge_attempt", "judge_feedback", "judge_route", "judge_spent_sec",
                       "judge_last_sec", "violations", "table_verdict", "gray"}
CODE_VALUE_FIELDS = {"signal_set", "trusted_eta", "days_margin"}


def _graph_input(request: AgentRequest, **extra) -> dict:
    return {"request": request, "bl_masked": mask_bl(request.bl_no), "path": [], **extra}


def _after_collect_state(request: AgentRequest, **over) -> dict:
    """compute_signals 노드가 읽는 State. 위치 있음, 해수부 입항 예정 있음, 지난 사실 없음."""
    customs = CustomsRecord(ship_name="TEST STAR", ship_nat="PA", stage_name="반입신고", bl_masked="TEST****0001", fetched_at=NOW)
    return {
        "request": request, "customs": customs, "vessel_match": "confirmed", "mmsi": 440999001, "call_sign": "TST01",
        "candidates": agent_router._candidates([_candidate(440999001)]),
        "position": POSITION,
        "schedule": {"planned_arrival": (NOW + timedelta(days=5)).isoformat(), "actual_arrival": None,
                     "fetched_at": NOW.isoformat()},
        "distance": {"distance_nm": 12.0, "source": "searoute", "fetched_at": NOW.isoformat()},
        "position_history": [], "path": [], **over,
    }


# ── compute_signals 노드 단독 (H3 완료 기준 4) ──

def test_compute_signals_writes_the_same_values_as_the_code_functions():
    request = _req()
    state = _after_collect_state(request)
    update = nodes.compute_signals(state)

    expected_set = signals_service.compute(state["position"], state["schedule"], state["distance"], state["customs"],
                                           request.sale_due_date, history=[], forwarder_eta=None)
    expected_trusted = signals_service.trusted_eta(state["position"], state["schedule"], None)
    drop = {"computed_at"}  # 계산한 시각만 호출마다 다르다
    assert update["signal_set"].model_dump(exclude=drop) == expected_set.model_dump(exclude=drop)
    assert update["trusted_eta"] == expected_trusted
    assert update["days_margin"] == days_margin(expected_trusted, request.sale_due_date)
    assert update["has_position"] is True and update["from_cache"] is False
    assert update["path"] == ["compute_signals"]


def test_compute_signals_does_not_write_llm_or_verdict_fields():
    update = nodes.compute_signals(_after_collect_state(_req()))
    assert not set(update) & NOT_COMPUTE_SIGNALS
    assert set(update) == {"signal_set", "trusted_eta", "days_margin", "has_position", "from_cache", "anchored",
                           "eta_moved", "port_note", "collected", "path"}


def test_compute_signals_without_position_has_no_margin():
    """쓸 위치가 없으면 여유 일수도 내지 않는다 (해수부 예정만으로 센 값을 판정 근거처럼 보이지 않게)."""
    update = nodes.compute_signals(_after_collect_state(_req(), position={"error": {"code": "not_found"}}))
    assert update["has_position"] is False and update["days_margin"] is None and update["from_cache"] is False


def test_only_compute_signals_writes_the_code_values_in_a_graph_run(fake, monkeypatch):
    """AC-03: State의 days_margin, signal_set, trusted_eta를 쓰는 노드는 compute_signals뿐이다. 판정 표 노드는 등급 칸만 쓴다."""
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    writers: dict[str, set] = {}
    graph = build_graph(entry="fetch_customs")
    for chunk in graph.stream(_graph_input(_req()), stream_mode="updates"):
        for name, update in chunk.items():
            writers[name] = set(update or {})
    assert [n for n, keys in writers.items() if keys & CODE_VALUE_FIELDS] == ["compute_signals"]
    assert writers["table_verdict"] == {"table_verdict", "gray", "path"}
    # LLM 노드(judge)는 설명 칸(judged)과 시도 상태만 쓴다. 등급, 여유 일수, 신호는 쓰지 않는다 (CODE_VALUE_FIELDS에 없다)
    assert writers["judge"] == {"judge_attempt", "violations", "judged", "judge_route", "judge_last_sec", "path"}
    assert writers["validate"] == {"judge_route", "violations", "response", "judge_last_sec", "judge_spent_sec", "path"}
    assert writers["finalize"] == {"response", "path"}
    assert list(writers) == ["fetch_customs", "scope_gate", "identify", "collect", "compute_signals", "table_verdict",
                             "judge", "validate", "finalize", "decorate"]


# ── 갈림길 ──

def test_gate_paths(fake, monkeypatch):
    """위치 없음, 도착일 없음은 판정 표·LLM을 거치지 않고 보류 노드에서 decorate로 간다."""
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("보류인데 LLM을 불렀다"))
    graph = build_graph(entry="fetch_customs")

    fake(candidates=[_candidate(440999001)])  # position 기본값: not_found
    assert graph.invoke(_graph_input(_req()))["path"] == [
        "fetch_customs", "scope_gate", "identify", "collect", "compute_signals", "gate_no_position", "decorate"]

    fake(candidates=[_candidate(440999001)], position=POSITION,
         schedule={"planned_arrival": None, "actual_arrival": None, "fetched_at": NOW.isoformat()})
    _signals(monkeypatch, {"sales_date_exceeded": "unknown"})
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("보류인데 LLM을 불렀다"))
    assert graph.invoke(_graph_input(_req()))["path"] == [
        "fetch_customs", "scope_gate", "identify", "collect", "compute_signals", "gate_no_eta", "decorate"]


def test_collect_keeps_the_v1_call_order(fake, monkeypatch):
    """위치, 입항 일정, 남은 거리, 위치 이력을 병렬이 아니라 v1과 같은 순서로 부른다 (v1 off와 on이 같다)."""
    orders: dict[str, list] = {}
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        calls = orders.setdefault(mode, [])
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
        monkeypatch.setattr(agent_router, "get_live_position", lambda mmsi: calls.append("position") or POSITION)
        monkeypatch.setattr(agent_router, "get_port_schedule", lambda **k: calls.append("schedule") or
                            {"planned_arrival": (NOW + timedelta(days=5)).isoformat(), "actual_arrival": None,
                             "fetched_at": NOW.isoformat()})
        monkeypatch.setattr(agent_router, "estimate_distance",
                            lambda *a, **k: calls.append("distance") or {"distance_nm": 3.0, "source": "searoute",
                                                                         "fetched_at": NOW.isoformat()})
        monkeypatch.setattr(agent_router, "_position_history", lambda mmsi: calls.append("history") or [])
        agent_router.run_agent(_req(), user_id=None)
    assert orders["on"] == orders["off"] == ["position", "schedule", "distance", "history"]


# ── ContextVar 이전: previous_eta ──

def test_previous_eta_comes_from_state_not_from_the_context_var():
    """State의 previous_eta가 eta_change를 센다. 같은 시각에 ContextVar에 다른 값이 꽂혀 있어도 State가 이긴다."""
    previous = date(2026, 9, 18)  # 도착 예정(9월 28일쯤)과 크게 다르다
    state = _after_collect_state(_req(), previous_eta=previous)
    trusted = signals_service.trusted_eta(state["position"], state["schedule"], None)
    expected = port_facts.eta_change(previous, trusted)
    assert expected is not None  # 이 테스트가 헛돌지 않게: 기준 이상 바뀐 입력이다

    token = agent_router.previous_eta.set(trusted.date())  # 같은 날짜라 ContextVar를 읽었다면 변화 없음이 나온다
    try:
        update = nodes.compute_signals(state)
    finally:
        agent_router.previous_eta.reset(token)
    assert update["eta_moved"] == expected and update["collected"]["eta_change"] == expected


def test_graph_run_leaves_previous_eta_context_var_alone(fake, monkeypatch):
    """그래프는 State로만 건네고 ContextVar를 건드리지 않는다 (H2는 노드 안에서 꽂았다 되돌렸다)."""
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    seen = []
    real = agent_router._signals_stage
    monkeypatch.setattr(agent_router, "_signals_stage",
                        lambda *a, **k: seen.append(agent_router.previous_eta.get()) or real(*a, **k))
    build_graph(entry="fetch_customs").invoke(_graph_input(_req(), previous_eta=date(2026, 9, 18)))
    assert seen == [None]
    assert agent_router.previous_eta.get() is None


# ── ContextVar 이전: trusted_vessel ──

def test_trusted_vessel_in_state_skips_the_candidate_search(fake, monkeypatch):
    """State의 trusted_vessel이 저장된 확정 배를 믿게 한다(후보 검색 없음). ContextVar는 비어 있다."""
    fake()
    monkeypatch.setattr(agent_router, "_saved_cargo_responses", lambda request: [])
    monkeypatch.setattr(agent_router, "get_vessel_candidates", lambda **k: pytest.fail("저장된 확정 배는 다시 찾지 않는다"))
    assert agent_router.trusted_vessel.get() is None
    out = build_graph(entry="fetch_customs").invoke(
        _graph_input(_req(resolved_mmsi=440999009), trusted_vessel=440999009))
    assert out["vessel_match"] == "confirmed" and out["mmsi"] == 440999009
    assert agent_router.trusted_vessel.get() is None  # 노드가 꽂은 값은 되돌려 놓았다


def test_without_trusted_vessel_in_state_the_search_runs(fake, monkeypatch):
    """State에 없으면(화면 요청) 후보와 대조한다. 같은 요청이 State 값 하나로 달라진다."""
    fake(candidates=[_candidate(440999009)])
    monkeypatch.setattr(agent_router, "_saved_cargo_responses", lambda request: [])
    searched = []
    monkeypatch.setattr(agent_router, "get_vessel_candidates",
                        lambda **k: searched.append(1) or {"candidates": [_candidate(440999009)], "source": "shipfinder",
                                                           "fetched_at": NOW.isoformat()})
    build_graph(entry="fetch_customs").invoke(_graph_input(_req(resolved_mmsi=440999009)))
    assert searched == [1]


def test_facade_hands_the_trusted_vessel_context_var_to_state(fake, monkeypatch):
    """run_agent를 부른 쪽이 꽂아 둔 trusted_vessel ContextVar는 퍼사드가 State 입력으로 옮긴다 (기존 테스트들이 이 방식으로 켠다)."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    fake()
    monkeypatch.setattr(agent_router, "_saved_cargo_responses", lambda request: [])
    monkeypatch.setattr(agent_router, "get_vessel_candidates", lambda **k: pytest.fail("저장된 확정 배는 다시 찾지 않는다"))
    token = agent_router.trusted_vessel.set(440999009)
    try:
        response = agent_router.run_agent(_req(resolved_mmsi=440999009), user_id=None)
    finally:
        agent_router.trusted_vessel.reset(token)
    assert response.vessel_match == "confirmed"


# ── ContextVar 이전: db_failed와 db_scope ──

def test_db_scope_passes_through_outside_run_agent():
    """_db_state가 None인 길(지켜보기)은 실패를 기억하지 않는다. 노드가 실패를 적어도 State에 db_failed가 생기지 않는다."""
    assert agent_router._db_state.get() is None

    @nodes.db_scope
    def node(state):
        agent_router._mark_db_failed()
        return {"path": ["x"]}

    assert node({}) == {"path": ["x"]}


def test_db_scope_carries_failure_through_state():
    """노드가 DB 실패를 적으면 State(db_failed)로 되돌려 주고, 다음 노드는 State 값으로 이어받는다. 바깥 ContextVar는 안 건드린다."""
    outer = {"failed": False}
    token = agent_router._db_state.set(outer)
    seen = []

    @nodes.db_scope
    def fails(state):
        seen.append(agent_router._db_failed())
        agent_router._mark_db_failed()
        return {"path": ["fails"]}

    @nodes.db_scope
    def later(state):
        seen.append(agent_router._db_failed())
        return {"path": ["later"]}

    try:
        update = fails({"db_failed": False})
        assert update == {"path": ["fails"], "db_failed": True}
        assert outer == {"failed": False}  # 퍼사드가 그래프가 끝난 뒤에 되돌려 놓는다
        assert later({"db_failed": True}) == {"path": ["later"]}  # 이미 True라 바뀐 것이 없다
    finally:
        agent_router._db_state.reset(token)
    assert seen == [False, True]


def test_facade_copies_db_failed_back_to_the_request_state(fake, monkeypatch):
    """노드에서 난 DB 실패가 퍼사드 뒤쪽(_logged, 기억 쓰기)에 전해져 건너뛴다. 기억 읽기 실패 -> 기억 쓰기 없음."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    monkeypatch.setattr(agent_router.memory_service, "load_context", lambda user_id, bl_no: None)
    wrote = []
    monkeypatch.setattr(agent_router.memory_service, "remember_later", lambda *a, **k: wrote.append(1))
    monkeypatch.setattr(agent_router, "connect", lambda *a, **k: pytest.fail("DB에 다시 붙었다"))
    agent_router.run_agent(_req(), user_id="user-1")
    assert wrote == []


# ── 지켜보기 재판정 경로 ──

def test_assess_graph_equals_assess_with_watch_context_vars(fake, monkeypatch):
    """지켜보기 재판정은 trusted_vessel과 previous_eta를 ContextVar에 꽂고 부른다. assess_graph는 그것을 State로 옮겨
    v1 _assess와 같은 응답을 낸다(저장된 확정 배는 후보 검색을 건너뛰고 지난 ETA 대비 도착 예정 변화가 센다)."""
    monkeypatch.setattr(agent_router, "_saved_cargo_responses", lambda request: [])
    request = _req(resolved_mmsi=440999001)
    results = []
    for runner in (lambda: agent_router._assess(request, None), lambda: assess_graph(request, None)):
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
        monkeypatch.setattr(agent_router, "get_vessel_candidates",
                            lambda **k: pytest.fail("저장된 확정 배는 다시 찾지 않는다"))
        t1 = agent_router.trusted_vessel.set(440999001)
        t2 = agent_router.previous_eta.set(date(2026, 9, 18))
        try:
            results.append(runner())
        finally:
            agent_router.previous_eta.reset(t2)
            agent_router.trusted_vessel.reset(t1)
    v1, graph = results
    assert v1.eta_change is not None  # 이 비교가 헛돌지 않게

    def dump(response) -> dict:
        data = _dump(response)
        # 저장된 확정 배는 후보 검색 없이 지금 시각을 last_time으로 단 후보 한 줄을 만든다 (routers/agent._identify). 벽시계 값이라 뺀다
        data["vessel_candidates"] = [{**c, "last_time": None} for c in data["vessel_candidates"]]
        return data
    assert dump(graph) == dump(v1)


def test_assess_graph_rejects_a_memory_block():
    with pytest.raises(ValueError):
        assess_graph(_req(), "기억 블록")


def test_run_watch_default_assess_follows_the_flag(upserts, sent, monkeypatch):
    """assess를 안 주면 꺼짐에서는 _assess, 켜짐에서는 graph.facade.assess_graph를 부른다. 둘 다 (request, None)으로 불린다."""
    from backend.app.graph import facade

    calls = []
    monkeypatch.setattr(agent_router, "_assess", lambda req, mem=None: calls.append(("v1", mem)) or _resp("watch"))
    monkeypatch.setattr(facade, "assess_graph", lambda req, mem=None: calls.append(("graph", mem)) or _resp("watch"))
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        conn = FakeConn()
        _watched(conn)
        assert watch_svc.run_watch(conn, now=datetime(2026, 10, 5, 3, tzinfo=timezone.utc))["checked"] == 1
    assert calls == [("v1", None), ("graph", None)]
