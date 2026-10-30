"""판정 그래프의 조건부 엣지 함수. 근거: docs/specs/v2-langgraph.md 2절 원칙, 엣지 그림.

책임: State를 보고 다음 노드 이름만 돌려준다. 호출, 저장, 숫자 비교 상수는 두지 않는다(판단만).
H2의 갈림길은 모두 "끝낼 응답이 이미 State에 있는가"나 요청의 모양에서 정해진다.
"""
from __future__ import annotations

from langgraph.graph import END

from backend.app.graph.state import AgentState
from backend.app.routers import agent


def after_guard(state: AgentState) -> str:
    """판매 예정일 범위 검사에서 응답이 나왔으면 끝, 아니면 기억 읽기로."""
    return END if state.get("response") is not None else "load_memory"


def after_saved(state: AgentState) -> str:
    """후속 질문이면 후속 노드로, 아니면 UNI-PASS 조회로 (v1 _run의 갈림길)."""
    return "follow_up" if state["request"].follow_up_question else "fetch_customs"


def after_customs(state: AgentState) -> str:
    """세관 조회가 성공했으면 범위 확인으로. 실패했고 '못 찾음 + MRN 있음'이면 MRN 대체로, 그 밖의 실패는 그 응답으로 끝
    (세관 장애는 대체하지 않는다, mrn-fallback.md EC-04)."""
    failure = state.get("failure")
    if failure is None:
        return "scope_gate"
    request = state.get("judged_request") or state["request"]
    return "mrn_fallback" if agent.is_not_found(failure) and request.mrn else END


def after_scope(state: AgentState) -> str:
    """부산 밖이거나 반출이라 응답이 정해졌으면 화면 전용 칸 붙이기로, 아니면 선박 식별로."""
    return "decorate" if state.get("response") is not None else "identify"


def after_identify(state: AgentState) -> str:
    """배를 못 정해 되묻기·partial 응답이 나왔으면 화면 전용 칸 붙이기로, 확정이면 남은 판정으로."""
    return "decorate" if state.get("response") is not None else "assess_rest"
