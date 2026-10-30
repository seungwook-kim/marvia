"""판정 그래프의 노드. 근거: docs/specs/v2-langgraph.md 2절 노드 표, 10절 H2·H3.

책임: 기존 routers/agent.py의 함수를 호출 시점에 모듈 속성으로 불러(agent._fetch_customs처럼) 그래프 노드로 감싼다.
판정 문장, 임계값, 검증 규칙은 여기서 새로 쓰지 않는다. 노드는 자기가 바꾸는 State 칸만 돌려준다(부분 갱신).

왜 모듈 속성으로 부르나: 기존 테스트가 routers/agent의 이름(_fetch_customs, UnipassClient, llm, _logged 등)을 monkeypatch한다.
함수를 import해 두면 패치가 안 먹으므로 `agent.이름`으로 그때그때 읽는다. 함수를 graph/로 옮기는 일은 H10이다.

왜 이 파일이 routers.agent를 맨 위에서 import해도 순환이 안 되나: routers/agent.py는 graph를 모듈 맨 위에서 import하지 않는다.
run_agent가 AGENT_GRAPH=on일 때만 함수 안에서 graph.facade를 불러 온다(순환 방지와 꺼져 있을 때 langgraph를 읽지 않으려는 것).

판정 뒤쪽은 collect, compute_signals, gate_no_position, gate_no_eta, table_verdict와 judge <-> validate 루프(judge, validate, finalize,
fallback_partial)로 나뉜다. 시도 횟수, 피드백, 시간 예산은 State가 들고 다음 시도를 할지는 routes.after_validate가 정한다 (H4).

되묻기(H7): identify가 배를 못 정해 insufficient 응답을 만들면 ask_prepare가 "멈출 수 있는 실행인가"를 State(awaiting)에 적고,
decorate 뒤에 awaiting이면 ask_wait가 interrupt()로 멈춘다. 재개하면 ask_wait가 새 요청의 정보(resolved_mmsi 등)를 request에 합치고
identify로 돌아간다 (v2-langgraph.md 3절). interrupt가 있는 노드는 재개 때 처음부터 다시 도므로 ask_wait에는 interrupt 한 줄과
State 갱신만 두고 외부 호출·DB 쓰기는 앞 노드에 둔다 (2절 원칙).

ContextVar 이전 (2절): previous_eta, trusted_vessel, db_failed는 State로 흐른다. 기존 도우미가 아직 ContextVar를 읽으므로 노드가
들어갈 때 State 값을 ContextVar에 맞추고 나올 때 되돌린다(db_scope, identify). 도우미가 인자를 받게 고치는 일은 H10이다.
"""
from __future__ import annotations

import functools
import os

from langgraph.types import interrupt

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
def load_previous(state: AgentState) -> dict:
    """후속 질문이 답할 지난 결과를 DB에서 읽는다 (SDD 4.4, v1 _follow_up의 첫 줄). 없거나 DB에 못 붙거나 입력이 바뀌었으면 None이고,
    그러면 route(after_previous)가 세관 조회부터 다시 돌려 그 결과로 답하게 한다 (DB 없이도 돌아야 한다)."""
    return _step("load_previous", previous=agent._previous_response(state["request"], state.get("user_id")))


def guard_follow_up(state: AgentState) -> dict:
    """답할 지난 결과(previous)를 정하고 프롬프트 주입 모양 질문을 LLM 없이 막는다 (SDD 7절).
    지난 결과가 DB에 있었으면 previous가 이미 있고, 없어서 판정을 다시 돌렸으면 그 판정 응답(response)이 지난 결과가 된다.
    막으면 지난 판정·근거 그대로 답만 FOLLOW_UP_BLOCKED로 response에 둬서 끝낸다. 통과하면 response를 비워 답 작성으로 보낸다."""
    previous = state.get("previous") or state["response"]
    if agent.guard.looks_like_injection(state["request"].follow_up_question):
        log_failure("follow_up", "blocked prompt injection pattern")
        return _step("guard_follow_up", previous=previous, response=agent._follow_up_blocked(previous))
    progress.step("answer")
    return _step("guard_follow_up", previous=previous, response=None)


def answer_follow_up(state: AgentState) -> dict:
    """후속 답 LLM을 한 번 부른다 (SDD 4.4). 도구(읽기 전용 MCP)를 고르는 라운드는 llm.answer_follow_up 안에서 돈다.
    기억 블록은 후속 LLM에 그대로 준다. 시도 횟수는 State(follow_attempt)가 센다.
    - 형식 오류(LLMFormatError): 무엇이 틀렸는지를 피드백으로 남기고 다시 묻는다(validate_follow_up이 상한을 본다)
    - 도구를 붙인 요청의 거절(bad_request): 거절을 기억하고 피드백 그대로 다시 묻는다. 다음 시도는 도구 없이 간다 (agent._follow_up_tools)
    - 그 밖의 호출 실패(LLMError): 다시 묻지 않고 지난 결과 + FOLLOW_UP_FAILED로 끝낸다
    후보 검색 도구를 부른 횟수는 시도를 넘어 State(follow_candidates_used)로 센다: 한 질문에 한 번이라는 상한이 다시 묻기로 풀리지 않게."""
    request = state["request"]
    previous = state["previous"]
    memory = state.get("memory")
    attempt = state.get("follow_attempt", 0) + 1
    feedback = state.get("follow_feedback")
    tools = agent._follow_up_tools(previous)
    used = {"candidates": state.get("follow_candidates_used", 0)}
    update: dict = {"follow_attempt": attempt, "follow_answer": None, "follow_tool_calls": []}
    try:
        answer = agent.llm.answer_follow_up(request.follow_up_question, previous,
                                            feedback="\n".join(feedback) if feedback is not None else None, tools=tools,
                                            run_tool=agent._capped_follow_up_tool(previous, used),
                                            memory=memory.blocks if memory else None)
    except agent.llm.LLMFormatError as e:
        log_failure("follow_up", f"{e.kind} attempt {attempt}")
        update.update(follow_route="retry", follow_feedback=[e.detail])
    except agent.llm.LLMError as e:
        if tools and e.kind == "bad_request":
            log_failure("follow_up", "tools rejected, retry without tools")
            agent.llm.remember_tools_rejected()  # 다음 질문부터는 이 모델에 도구를 붙이지 않는다 (llm.TOOLS_REJECT_TTL_SEC 동안)
            update.update(follow_route="retry")
        else:
            log_failure("follow_up", agent._llm_error(e))
            update.update(follow_route="give_up", response=agent._follow_up_failed(previous))
    else:
        # 여기서 pass는 호출이 답을 줬다는 뜻일 뿐이다. 검증은 validate_follow_up이 한다
        update.update(follow_route="pass", follow_answer=[answer.output.answer], follow_tool_calls=answer.tool_calls)
    update["follow_candidates_used"] = used["candidates"]
    return _step("answer_follow_up", **update)


def validate_follow_up(state: AgentState) -> dict:
    """후속 답을 검증하고 다시 물을지 접을지 정한다 (SDD 4.4, 5.7). 규칙 집합은 agent._check_follow_up_answer 한 곳이다
    (FOLLOW_UP_RULES + V9 + V10 + V12, 컨테이너 번호표는 검증엔 빠지고 화면 답엔 되돌아온다).
    통과하면 그 응답을 response에 두고 pass. 위반(또는 답이 오지 않은 시도)이 있으면 상한(judge_max_attempts)에 닿았을 때만 접는다:
    지난 판정·근거 그대로 답만 FOLLOW_UP_FAILED로 response에 두고 give_up. 아니면 위반 내용을 피드백으로 남기고 retry다.
    후속 질문에는 시간 예산이 없다 (v1과 같다)."""
    attempt = state["follow_attempt"]
    previous = state["previous"]
    answer = state.get("follow_answer")
    update: dict = {}
    if answer is not None:
        memory = state.get("memory")
        response, violations = agent._check_follow_up_answer(
            answer[0], state.get("follow_tool_calls", []), state["request"].follow_up_question, previous,
            memory.blocks if memory else None, attempt)
        if response is not None:
            return _step("validate_follow_up", follow_route="pass", response=response)
        update["follow_feedback"] = [agent._follow_up_feedback_text(violations)]
    if attempt >= agent.JUDGE_ATTEMPTS:
        log_failure("follow_up", f"gave up after {agent.JUDGE_ATTEMPTS} attempts")
        return _step("validate_follow_up", follow_route="give_up", response=agent._follow_up_failed(previous), **update)
    return _step("validate_follow_up", follow_route="retry", **update)


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


def ask_prepare(state: AgentState) -> dict:
    """배를 못 정해 identify가 insufficient(되묻기) 응답을 만들었을 때 거치는 노드. 이 실행이 멈출 수 있는지를 State(awaiting)에 적는다.
    멈출 수 있다 = 체크포인터가 붙어 있고(퍼사드가 resumable로 알린다) 후속 질문이 아니다. 후속 질문 연쇄는 스레드를 만들지 않으므로 멈추지 않고
    지금처럼 응답으로 이어 간다. 응답 자체는 identify가 이미 v1과 같게 만들어 State response에 두었다 (spec의 ask_prepare가 하는 응답 만들기를
    identify 안에서 하는 것은 H2부터 그대로다). 멈춤 자체(interrupt)는 decorate 뒤 ask_wait가 한다 (v2-langgraph.md 3절, Q6)."""
    return _step("ask_prepare", awaiting=bool(state.get("resumable")) and not state["request"].follow_up_question)


# 재개 값으로 새 요청에서 꺼내 합치는 칸 (v2-langgraph.md 3절 "재개 값"). 값이 있는 칸만 덮어쓴다: 사용자가 이미 준 값을 지우지 않는다
RESUME_FIELDS = ("resolved_mmsi", "voyage_no", "forwarder_eta", "sale_due_date")


def ask_wait(state: AgentState) -> dict:
    """되묻기 응답을 내놓고 멈춘다. 중단 값은 응답(model_dump, JSON)이고 체크포인트에서는 암호화된 blob이 된다 (3절 "중단 값").
    재개하면 interrupt()가 재개 값(RESUME_FIELDS 칸)을 돌려주고, 이 노드가 처음부터 다시 돌아 그 값을 request에 합친다(새 값이 우선, v1과 같다).
    판정에 쓰는 judged_request도 같은 규칙으로 다시 만든다: 판매 예정일이 이번에 없으면 처음 읽어 둔 저장 날짜(saved_due)로 채운다 (v1 _run).
    응답은 비우고(identify가 새로 정한다) awaiting을 내리며 resumed를 올린다: 재개한 실행의 decorate가 처리 이력을 다시 받는다 (EC-15, Q12).
    이 노드에는 외부 호출과 DB 쓰기를 두지 않는다 (재개 때 처음부터 다시 도는 노드라서)."""
    resume = interrupt({"response": state["response"].model_dump(mode="json")})
    request = state["request"].model_copy(update={f: resume[f] for f in RESUME_FIELDS if resume.get(f) is not None})
    saved_due = state.get("saved_due") if request.sale_due_date is None else None
    judged = request.model_copy(update={"sale_due_date": saved_due}) if saved_due else request
    return _step("ask_wait", request=request, judged_request=judged, saved_due=saved_due, response=None, awaiting=False, resumed=True)


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


def judge(state: AgentState) -> dict:
    """판정 LLM을 한 번 부른다 (SDD 5.7 한 시도). LLM이 쓰는 것은 설명 칸뿐이고 등급, 여유 일수, 신호는 State의 코드 값을 넘긴다.
    기억 블록은 품명 없는 판정용(for_judge)이고 회색 지대면 버린다. 시도 횟수는 State(judge_attempt)가 센다.
    - 형식 오류(LLMFormatError): 위반처럼 본다. 무엇이 틀렸는지를 피드백으로 남기고 validate를 거쳐 엣지가 다시 물을지 정한다
    - 호출 실패(LLMError): 다시 묻지 않는다. judge_route를 give_up으로 두고 fallback_partial로 바로 간다
    걸린 시간은 judge_last_sec에 적는다(validate가 자기 시간을 더해 judge_spent_sec에 쌓는다). 시계는 v1과 같은 agent.time이다."""
    attempt = state.get("judge_attempt", 0) + 1
    if attempt == 1:
        progress.step("judge")  # v1은 반복문 앞에서 한 번만 알린다
    gray = state["gray"]
    memory = state.get("memory")
    judge_memory = agent._judge_memory(memory.for_judge() if memory else None, gray)
    feedback = state.get("judge_feedback")
    request = _working_request(state)
    started = agent.time.monotonic()
    update: dict = {"judge_attempt": attempt, "violations": []}
    try:
        judged = agent.llm.judge(state["signal_set"], state["customs"], state["days_margin"],
                                 feedback="\n".join(feedback) if feedback is not None else None,
                                 trusted_eta=state["trusted_eta"], sale_due_date=request.sale_due_date,
                                 table_verdict=state["table_verdict"], verdict_mode="raise_only" if gray else "fixed",
                                 memory=judge_memory, facts=agent._judge_facts(state["anchored"], state["eta_moved"]))
    except agent.llm.LLMFormatError as e:
        log_failure("judge", f"{e.kind} attempt {attempt}")
        update.update(judged=None, judge_route="retry", judge_feedback=[agent._with_strengthen(e.detail, attempt)])
    except agent.llm.LLMError as e:
        log_failure("judge", agent._llm_error(e))
        update.update(judged=None, judge_route="give_up")
    else:
        # 검증은 validate가 한다. 여기서 "pass"는 호출이 답을 줬다는 뜻일 뿐이다
        update.update(judged=judged.output, judge_route="pass")
    update["judge_last_sec"] = agent.time.monotonic() - started
    return _step("judge", **update)


def validate(state: AgentState) -> dict:
    """LLM 판정 1회분을 검증한다 (SDD 5.7). 규칙 집합과 순서는 v1과 같은 agent._check_judge_output 한 곳이다(V8, V1~V7, V9·V10, V12, V15).
    위반이 없으면 응답을 State response에 두고 judge_route를 pass로, 있으면 retry로 두며 피드백(위반 내용, 둘째 재시도면 강화 문장)을 쓴다.
    다시 물을지 접을지는 엣지 함수(routes.after_validate)가 정한다. 형식 오류로 judged가 없으면 judge가 이미 피드백을 썼으니 검사 없이 지나간다.
    judge와 이 노드가 한 번 도는 데 걸린 시간을 judge_last_sec에 두고 judge_spent_sec에 쌓는다 (시간 예산, 2절)."""
    started = agent.time.monotonic()
    attempt = state["judge_attempt"]
    out = state.get("judged")
    update: dict = {}
    if out is None:
        update["judge_route"] = "retry"  # 형식 오류: 위반으로 본다 (피드백은 judge가 남겼다)
    else:
        memory = state.get("memory")
        judge_memory = agent._judge_memory(memory.for_judge() if memory else None, state["gray"])
        response, violations = agent._check_judge_output(
            out, attempt, _working_request(state), state["customs"], judge_memory, state["signal_set"],
            state["collected"], state["from_cache"], state["table_verdict"], state["gray"])
        if response is not None:
            update.update(judge_route="pass", violations=[], response=response)
        else:
            update.update(judge_route="retry", violations=[{"rule": r, "detail": d} for r, d in violations],
                          judge_feedback=[agent._with_strengthen(agent._judge_feedback_text(violations), attempt)])
    last = state["judge_last_sec"] + (agent.time.monotonic() - started)
    return _step("validate", judge_last_sec=last, judge_spent_sec=state.get("judge_spent_sec", 0.0) + last, **update)


def finalize(state: AgentState) -> dict:
    """검증을 통과한 판정 응답에 신호별 확인처 줄(다음 행선지)을 붙인다. LLM이 쓴 문장이 아니라 코드 문장이라 검사 대상이 아니다 (services/referrals.py)."""
    response = state["response"]
    return _step("finalize", response=response.model_copy(
        update={"actions": agent.referrals.with_signal_lines(response.actions, state["signal_set"])}))


def fallback_partial(state: AgentState) -> dict:
    """판정 LLM이 끝내 실패했을 때(호출 실패, 세 번 위반, 시간 예산 초과)의 응답 (SDD 5.7, 8절).
    status는 늘 partial이고 등급은 판정 표 것이다. 회색 지대면 등급 없이 LLM_FAILED 문구다 (agent._llm_gave_up)."""
    return _step("fallback_partial", response=agent._llm_gave_up(
        state["signal_set"], state["days_margin"], state["table_verdict"], state["gray"], state["customs"], state["collected"]))


def refetch_display_rows(request, bl_type_used: str | None) -> list:
    """재개한 실행이 화면 전용 처리 이력(customs_detail)을 다시 받는다 (EC-15, Q12 A). 체크포인트에는 이력 원문(중량·관세법인명)을 두지 않으므로
    (CustomsRecord.display_rows는 exclude=True) 되살린 State에는 비어 있다. 처음 실행이 찾은 구분(bl_type_used) 하나로 UNI-PASS를 한 번 부른다.
    못 가져오면(장애, 구분 모름, 조회 결과 없음) 빈 목록이고 로그에 종류만 남긴다. 이력이 비는 것뿐 판정은 그대로다."""
    if bl_type_used is None:
        return []
    try:
        record = agent.UnipassClient().fetch(request.bl_no, bl_type_used)
    except agent.CustomsError as e:
        log_failure("customs_detail", e.code)
        return []
    return list(record.display_rows) if record is not None else []


@db_scope
def decorate(state: AgentState) -> dict:
    """화면 전용 칸을 붙인다: 처리 이력 원문, 찾은 B/L 구분, 통관 소요 시간과 그 근거 카드 (v1 _assess 끝부분).
    세관 조회에 성공한 뒤의 모든 응답에만 붙는다. 조회 실패·MRN 대체 응답은 이 노드를 거치지 않는다.
    두 가지가 H7에서 더해졌다 (v2-langgraph.md 3, 4절):
    - 재개한 실행(resumed)에서 이력이 비어 있으면 UNI-PASS를 구분 하나로 한 번 불러 채운다(refetch_display_rows). 멈출 응답이면 건너뛴다(아래).
    - 멈출 응답(awaiting)에서는 customs_detail을 비운다. 이 응답은 State와 중단 값으로 체크포인트(DB)에 가는데, 이력 원문에는 중량·관세법인명이 있어
      SDD 3.5가 DB 저장을 막는다. HTTP로 나가는 응답의 이력은 퍼사드가 메모리에 있는 State(customs.display_rows)로 다시 채운다 (facade.restore_detail)."""
    customs = state["customs"]
    if state.get("resumed") and not state.get("awaiting") and not customs.display_rows:
        customs = customs.model_copy(update={"display_rows": refetch_display_rows(_working_request(state), state.get("bl_type_used"))})
    response = agent._with_display_fields(state["response"], customs, state.get("bl_type_used"))
    if state.get("awaiting"):
        response = response.model_copy(update={"customs_detail": []})
    return _step("decorate", response=response)
