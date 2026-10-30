"""판정 그래프의 노드. 근거: docs/specs/v2-langgraph.md 2절 노드 표, 10절 H2·H3.

책임: 기존 routers/agent.py의 함수를 호출 시점에 모듈 속성으로 불러(agent._fetch_customs처럼) 그래프 노드로 감싼다.
판정 문장, 임계값, 검증 규칙은 여기서 새로 쓰지 않는다. 노드는 자기가 바꾸는 State 칸만 돌려준다(부분 갱신).

왜 모듈 속성으로 부르나: 기존 테스트가 routers/agent의 이름(_fetch_customs, UnipassClient, llm, _logged 등)을 monkeypatch한다.
함수를 import해 두면 패치가 안 먹으므로 `agent.이름`으로 그때그때 읽는다. 함수를 graph/로 옮기는 일은 H10이다.

왜 이 파일이 routers.agent를 맨 위에서 import해도 순환이 안 되나: routers/agent.py는 graph를 모듈 맨 위에서 import하지 않는다.
run_agent가 AGENT_GRAPH=on일 때만 함수 안에서 graph.facade를 불러 온다(순환 방지와 꺼져 있을 때 langgraph를 읽지 않으려는 것).

H3의 굵기: 판정 뒤쪽은 collect, compute_signals, gate_no_position, gate_no_eta, table_verdict, judge_loop(아직 한 노드)로 나뉜다.
H4에서 judge_loop를 judge/validate 루프로 쪼갠다.

ContextVar 이전 (2절): previous_eta, trusted_vessel, db_failed는 State로 흐른다. 기존 도우미가 아직 ContextVar를 읽으므로 노드가
들어갈 때 State 값을 ContextVar에 맞추고 나올 때 되돌린다(db_scope, identify). 도우미가 인자를 받게 고치는 일은 H10이다.
"""
from __future__ import annotations

import functools
import os

from backend.app.graph.state import AgentState
from backend.app.observability.logging import log_failure
from backend.app.routers import agent
from backend.app.services import progress


def _step(name: str, **update) -> dict:
    """노드가 State에 돌려줄 값. 지나간 노드 이름(path)을 항상 붙인다 (State path는 유일한 누적 리듀서, 2절)."""
    return {**update, "path": [name]}


def _working_request(state: AgentState):
    """판정에 쓰는 요청. 저장된 판매 예정일을 채운 judged_request가 있으면 그것, 없으면(후속 질문) 원래 요청.
    v1 _run이 _assess(judged_request, ...)에 넘기던 값과 같다."""
    return state.get("judged_request") or state["request"]


def db_scope(node):
    """노드 래퍼: 이 노드가 도는 동안 기존 도우미(_db_ready, _mark_db_failed)가 읽는 ContextVar(_db_state)를 State의 db_failed에 맞추고,
    끝나면 도우미가 적은 실패를 State로 되받는다 (v2-langgraph.md 2절 ContextVar 이전 표). 이렇게 하면 한 요청 안의 DB 실패 기억이
    State를 따라 노드 사이를 건너가고(B7: 한 번 실패하면 남은 DB 단계는 건너뛴다), 멈춘 뒤 재개해도 같다.
    run_agent 밖에서 부르는 길(지켜보기 재판정)은 _db_state가 None이라 v1처럼 실패를 기억하지 않고 그대로 지나간다:
    한 화물의 DB 실패가 다른 화물로 번지면 안 된다 (routers/agent._db_state 설명)."""
    @functools.wraps(node)
    def wrapper(state: AgentState) -> dict:
        if agent._db_state.get() is None:
            return node(state)
        before = bool(state.get("db_failed"))
        token = agent._db_state.set({"failed": before})
        try:
            update = node(state)
            failed = agent._db_state.get()["failed"]
        finally:
            agent._db_state.reset(token)
        return {**update, "db_failed": failed} if failed != before else update
    return wrapper


@db_scope
def guard_input(state: AgentState) -> dict:
    """판매 예정일이 받아 주는 범위 밖이면 외부 조회·LLM 없이 insufficient로 끝낸다 (10-08 QA B2, v1 _run 첫 단계).
    응답을 State response에 두면 route가 END로 보낸다. 조회 이력 쓰기는 퍼사드가 한다."""
    request = state["request"]
    if agent.sale_due_out_of_range(request.sale_due_date):
        log_failure("input", "sale_due_date_out_of_range")
        return _step("guard_input", response=agent._response(
            status="insufficient", missing_inputs=["sale_due_date"],
            answer=agent.SALE_DUE_OUT_OF_RANGE.format(day=request.sale_due_date.isoformat())))
    return _step("guard_input")


@db_scope
def load_saved(state: AgentState) -> dict:
    """화물 표의 이 화물 줄을 읽어 지난 신뢰 ETA, 저장된 B/L 구분, 저장된 판매 예정일을 준비한다 (v1 _run의 else 갈래).
    후속 질문은 v1도 이 읽기를 하지 않으므로 건너뛴다(DB 호출 수를 v1과 같게 두려는 것).
    request에는 저장된 구분을 채우고, 화물 표 쓰기에는 이 request를 넘긴다(저장 날짜를 이번 입력으로 다시 쓰면 확인 대기가 지워진다, v1 주석)."""
    request = state["request"]
    if request.follow_up_question:
        return _step("load_saved")
    row = agent._saved_row(state.get("user_id"), request.bl_no)
    # 구분을 말하지 않았으면 지난번에 찾은 구분(화물 표 bl_type)부터 조회한다. 하우스·마스터를 매번 둘 다 부르지 않게
    saved_type = (row or {}).get("bl_type")
    if request.bl_type is None and saved_type in agent.OTHER_BL_TYPE:
        request = request.model_copy(update={"bl_type": saved_type})
    # 판매 예정일을 안 줬으면 화물 표에 저장된 날짜로 판정한다 (10-08 QA B4)
    saved_due = agent.saved_sale_due(row) if request.sale_due_date is None else None
    judged = request.model_copy(update={"sale_due_date": saved_due}) if saved_due else request
    return _step("load_saved", request=request, judged_request=judged, saved_due=saved_due,
                 previous_eta=agent._last_eta_of(row))


@db_scope
def load_memory(state: AgentState) -> dict:
    """로그인 사용자의 기억(화물 표·메모장)을 읽는다 (docs/specs/memory.md 읽기 흐름, v1 _run).
    사용자도 DB 주소도 있는데 None이면 읽기가 실패한 것이라 남은 DB 단계를 건너뛰게 표시한다."""
    context = agent.memory_service.load_context(state.get("user_id"), state["request"].bl_no)
    if context is None and state.get("user_id") and os.environ.get("DATABASE_URL"):
        agent._mark_db_failed()
    return _step("load_memory", memory=context)


@db_scope
def follow_up(state: AgentState) -> dict:
    """후속 질문 전체를 v1 함수 한 번으로 답한다 (SDD 4.4). H5에서 load_previous, guard, answer, validate 노드로 쪼갠다.
    기억 블록은 후속 LLM에 그대로, 품명 없는 블록(for_judge)은 지난 결과가 없어 다시 판정할 때 판정 LLM에 준다 (10-08 QA B3)."""
    memory = state.get("memory")
    response = agent._follow_up(state["request"], state.get("user_id"),
                                memory.blocks if memory else None, memory.for_judge() if memory else None)
    return _step("follow_up", response=response)


@db_scope
def fetch_customs(state: AgentState) -> dict:
    """UNI-PASS 조회 (하우스 -> 마스터 순서, 장애는 그 자리에서 failed). 실패 응답은 failure와 response에 같이 둔다.
    실패 뒤 MRN 대체 여부는 route가 정한다 (v1 _assess 앞부분)."""
    progress.step("customs")
    customs, failure, used_type = agent._fetch_customs(_working_request(state))
    if failure is not None:
        return _step("fetch_customs", customs=None, failure=failure, response=failure)
    return _step("fetch_customs", customs=customs, bl_type_used=used_type, failure=None)


@db_scope
def mrn_fallback(state: AgentState) -> dict:
    """세관에 없는 B/L이고 MRN이 있을 때 해수부 입항 신고로 대체한다 (docs/specs/mrn-fallback.md).
    이 노드로 오는 조건(못 찾음 + MRN)은 routes.after_customs가 정한다. 결과 응답에는 화면 전용 칸을 붙이지 않는다."""
    return _step("mrn_fallback", response=agent._customs_failure(_working_request(state), state["failure"]))


@db_scope
def scope_gate(state: AgentState) -> dict:
    """지원 범위 확인: 부산이 아니면 unsupported, 이미 반출됐으면 규칙 fine (SDD 4.1 2-1·2-2). 해당 없으면 아무것도 안 쓴다."""
    early = agent._scope_response(state["customs"])
    return _step("scope_gate", response=early) if early is not None else _step("scope_gate")


@db_scope
def identify(state: AgentState) -> dict:
    """선박 식별 (SDD 4.1 3단계). 확정하지 못하면 되묻기·partial 응답을 response에 둔다 (v1 _after_customs 앞부분).
    spec 노드 표의 ask_prepare가 하는 응답 만들기는 H2에서는 여기서 같이 한다. 되묻기 멈춤(interrupt)은 H7이다."""
    # 지켜보기 재판정이 저장해 둔 확정 배는 State(trusted_vessel)로 오고, _identify는 아직 ContextVar를 읽으므로 이 노드 동안만 꽂는다
    token = agent.trusted_vessel.set(state.get("trusted_vessel"))
    try:
        early, candidates, vessel_match, mmsi, call_sign = agent._identify_stage(state["customs"], _working_request(state))
    finally:
        agent.trusted_vessel.reset(token)
    update = dict(candidates=candidates, vessel_match=vessel_match, mmsi=mmsi, call_sign=call_sign)
    if early is not None:
        update["response"] = early
    return _step("identify", **update)


@db_scope
def collect(state: AgentState) -> dict:
    """위치, 입항 일정, 남은 거리, 위치 이력을 차례로 모은다 (SDD 4.1 4단계). 순서는 v1과 같게 순차다(병렬로 바꾸지 않는다).
    도구 실패는 error 값 그대로 State에 둬서 신호가 unknown으로 계속 가게 한다."""
    position, schedule, distance, history = agent._collect(state["customs"], state["mmsi"], state["call_sign"])
    return _step("collect", position=position, schedule=schedule, distance=distance, position_history=history)


def compute_signals(state: AgentState) -> dict:
    """1층 신호 7종, trusted_eta, 여유 일수, 지난 사실 3종, 근거 카드와 응답 공통 칸을 코드로 계산한다 (SDD 4.1 5·6단계).
    signal_set, trusted_eta, days_margin을 State에 쓰는 노드는 이 노드뿐이다 (AC-03). LLM 칸(judged 등)은 쓰지 않는다.
    지난 신뢰 ETA는 State의 previous_eta를 직접 넘긴다(ContextVar를 거치지 않는다)."""
    sig = agent._signals_stage(_working_request(state), state["customs"], state["vessel_match"], state["candidates"],
                               state["mmsi"], state["call_sign"], state["position"], state["schedule"],
                               state["distance"], state["position_history"], state.get("previous_eta"))
    note = sig["port_note"]
    return _step("compute_signals", signal_set=sig["signal_set"], trusted_eta=sig["trusted"], days_margin=sig["margin"],
                 has_position=sig["has_position"], from_cache=sig["from_cache"], anchored=sig["anchored"],
                 eta_moved=sig["eta_moved"], port_note=[note] if note else None, collected=sig["collected"])


def gate_no_position(state: AgentState) -> dict:
    """쓸 위치가 없으면 LLM 없이 보류한다 (데이터 없음은 insufficient, 도구 고장은 partial). 이 노드로 오는 조건은 routes.after_signals."""
    return _step("gate_no_position",
                 response=agent._no_position(state["position"], _working_request(state), state["collected"]))


def gate_no_eta(state: AgentState) -> dict:
    """판매일은 받았는데 믿을 도착일이 없어 여유 일수를 못 셌으면 LLM 없이 보류하고 대략값만 참고로 보여 준다."""
    return _step("gate_no_eta", response=agent._no_eta(state["signal_set"], state["collected"]))


def table_verdict(state: AgentState) -> dict:
    """판정 표가 등급을 먼저 정하고(table_verdict), 회색 지대인지(gray)를 가른다 (services/verdict.py, rules.md). 코드만 쓴다."""
    return _step("table_verdict", table_verdict=agent.verdict_table.table_verdict(state["signal_set"], state["days_margin"]),
                 gray=agent.verdict_table.is_gray(state["signal_set"]))


def judge_loop(state: AgentState) -> dict:
    """판정 LLM 호출, 검증(V8 등), 재시도, 폴백을 v1 함수 한 번으로 돈다 (SDD 5.7). H4에서 judge, validate 노드와 엣지 함수로 쪼갠다.
    LLM이 쓰는 것은 설명 칸뿐이고 등급, 여유 일수, 신호는 State의 코드 값을 넘긴다. 기억 블록은 품명 없는 판정용(for_judge)이다."""
    memory = state.get("memory")
    response = agent._judge_stage(_working_request(state), state["customs"], memory.for_judge() if memory else None,
                                  state["signal_set"], state["days_margin"], state["trusted_eta"], state["anchored"],
                                  state["eta_moved"], state["collected"], state["from_cache"], state["table_verdict"],
                                  state["gray"])
    return _step("judge_loop", response=response)


@db_scope
def decorate(state: AgentState) -> dict:
    """화면 전용 칸을 붙인다: 처리 이력 원문, 찾은 B/L 구분, 통관 소요 시간과 그 근거 카드 (v1 _assess 끝부분).
    세관 조회에 성공한 뒤의 모든 응답에만 붙는다. 조회 실패·MRN 대체 응답은 이 노드를 거치지 않는다."""
    response = agent._with_display_fields(state["response"], state["customs"], state.get("bl_type_used"))
    return _step("decorate", response=response)
