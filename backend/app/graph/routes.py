"""판정 그래프의 조건부 엣지 함수. 근거: docs/specs/v2-langgraph.md 2절 원칙, 엣지 그림.

책임: State를 보고 다음 노드 이름만 돌려준다. 호출, 저장, 숫자 비교 상수는 두지 않는다(판단만).
H2의 갈림길은 모두 "끝낼 응답이 이미 State에 있는가"나 요청의 모양에서 정해진다.
"""
from __future__ import annotations

from langgraph.graph import END

from backend.app.graph.state import AgentState
from backend.app.observability.logging import log_failure
from backend.app.routers import agent


def after_guard(state: AgentState) -> str:
    """판매 예정일 범위 검사에서 응답이 나왔으면 끝, 아니면 기억 읽기로."""
    return END if state.get("response") is not None else "load_memory"


def _end_or_follow_up(state: AgentState) -> str:
    """응답이 정해져 끝낼 자리. 후속 질문이면(지난 결과가 없어 판정을 다시 돌린 연쇄) 그 응답을 지난 결과로 삼아 후속 답으로 이어 가고,
    아니면 END다. v1은 어떤 응답이든(조회 실패 포함) 후속 질문이면 그 결과를 두고 후속 답을 만든다 (_follow_up의 `or _assess`)."""
    return "guard_follow_up" if state["request"].follow_up_question else END


def after_saved(state: AgentState) -> str:
    """후속 질문이면 지난 결과 읽기로, 아니면 UNI-PASS 조회로 (v1 _run의 갈림길)."""
    return "load_previous" if state["request"].follow_up_question else "fetch_customs"


def after_previous(state: AgentState) -> str:
    """지난 결과가 있으면 바로 후속 답으로, 없으면 세관 조회부터 판정을 다시 돌린다 (v1 `_previous_response(...) or _assess(...)`)."""
    return "guard_follow_up" if state.get("previous") is not None else "fetch_customs"


def after_customs(state: AgentState) -> str:
    """세관 조회가 성공했으면 범위 확인으로. 실패했고 '못 찾음 + MRN 있음'이면 MRN 대체로, 그 밖의 실패는 그 응답으로 끝
    (세관 장애는 대체하지 않는다, mrn-fallback.md EC-04). 후속 질문의 연쇄면 끝 자리에서 후속 답으로 이어진다."""
    failure = state.get("failure")
    if failure is None:
        return "scope_gate"
    request = state.get("judged_request") or state["request"]
    return "mrn_fallback" if agent.is_not_found(failure) and request.mrn else _end_or_follow_up(state)


def after_decorate(state: AgentState) -> str:
    """화면 전용 칸을 붙인 뒤: 후속 질문의 연쇄면 후속 답으로, 아니면 끝."""
    return _end_or_follow_up(state)


def after_mrn_fallback(state: AgentState) -> str:
    """MRN 대체 응답 뒤: 후속 질문의 연쇄면 후속 답으로, 아니면 끝."""
    return _end_or_follow_up(state)


def after_guard_follow_up(state: AgentState) -> str:
    """주입 모양이라 막은 응답이 나왔으면 끝, 아니면 후속 답 작성으로."""
    return END if state.get("response") is not None else "answer_follow_up"


def after_answer_follow_up(state: AgentState) -> str:
    """후속 답 LLM 호출이 실패(LLMError)해 실패 응답이 나왔으면 끝, 답이나 다시 묻기 사유가 있으면 검증으로."""
    return END if state["follow_route"] == "give_up" else "validate_follow_up"


def after_validate_follow_up(state: AgentState) -> str:
    """후속 답 검증 뒤: 통과도 포기도 끝(응답은 validate_follow_up이 이미 정했다). 위반이 있고 상한 전이면 다시 답 작성으로.
    상한은 판정과 같은 judge_max_attempts다 (agent.JUDGE_ATTEMPTS). 포기 판단은 validate_follow_up이 한다."""
    return "answer_follow_up" if state["follow_route"] == "retry" else END


def after_scope(state: AgentState) -> str:
    """부산 밖이거나 반출이라 응답이 정해졌으면 화면 전용 칸 붙이기로, 아니면 선박 식별로."""
    return "decorate" if state.get("response") is not None else "identify"


def after_identify(state: AgentState) -> str:
    """배를 못 정해 되묻기·partial 응답이 나왔으면 화면 전용 칸 붙이기로, 확정이면 위치·일정 수집으로."""
    return "decorate" if state.get("response") is not None else "collect"


def after_signals(state: AgentState) -> str:
    """신호 계산 뒤 갈림길 (SDD 4.1). 쓸 위치가 없으면 위치 보류로, 판매일은 있는데 여유 일수를 못 셌으면 도착일 보류로,
    그 밖에는 판정 표로. 위치 보류가 먼저다(v1 순서)."""
    if not state["has_position"]:
        return "gate_no_position"
    request = state.get("judged_request") or state["request"]
    if request.sale_due_date and state["days_margin"] is None:
        return "gate_no_eta"
    return "table_verdict"


def after_judge(state: AgentState) -> str:
    """판정 LLM 호출이 실패(LLMError)했으면 다시 묻지 않고 폴백으로, 답이나 형식 오류면 검증으로 (SDD 5.7)."""
    return "give_up" if state["judge_route"] == "give_up" else "validate"


def after_validate(state: AgentState) -> str:
    """judge <-> validate 루프의 포기 판단은 이 함수 한 곳에서만 한다 (v2-langgraph.md 2절 루프). v1 _judge_stage와 같은 순서다.
    위반이 없으면 pass. 위반이 있으면: 시도 횟수가 상한이면 give_up, 그 뒤 "그동안 쓴 시간 + 방금 한 번에 걸린 시간"이 예산을 넘으면
    give_up(다음 시도를 시작하지 않는다), 아니면 retry. 상한과 예산은 호출할 때 agent의 별칭(rules.md judge_max_attempts, judge_budget_sec)을 읽는다.
    포기 사유는 v1과 같은 문장으로 로그에 남긴다."""
    if state["judge_route"] == "pass":
        return "pass"
    attempt = state["judge_attempt"]
    if attempt >= agent.JUDGE_ATTEMPTS:
        log_failure("judge", f"gave up after {agent.JUDGE_ATTEMPTS} attempts")
        return "give_up"
    if state["judge_spent_sec"] + state["judge_last_sec"] > agent.JUDGE_BUDGET_SEC:
        log_failure("judge", f"time budget exceeded after {attempt} attempts")
        return "give_up"
    return "retry"
