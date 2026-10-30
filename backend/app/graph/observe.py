"""노드 span 데코레이터. 근거: docs/specs/v2-langgraph.md 7절 관측, 10절 H6, SDD 5.11.

책임: 그래프 노드 함수를 감싸 실행마다 Langfuse span "node:<이름>"을 하나 남기고, 그 노드 안에서 여는 LLM generation과 도구 관측이
그 span 아래에 걸리게 한다. span을 열고 닫는 일과 부모 문맥(_node_var)은 observability/langfuse.py의 node_scope가 맡고,
이 파일은 노드가 돌려준 State 갱신에서 "남겨도 안전한 값"만 골라 metadata에 옮긴다.

남기는 값: 루프 노드(judge, validate, answer_follow_up, validate_follow_up)의 시도 번호(judge_attempt, follow_attempt, 정수), 갈림길 이름(judge_route, follow_route: pass, retry, give_up), 응답이 정해졌으면 그 status.
남기지 않는 값: 요청, 세관 기록, 후보 이름, 판정·후속 답 문장, 피드백, 기억처럼 State에 든 나머지 전부. input·output은 span에 비운다.
B/L, 컨테이너 번호, 연락처가 span에 가지 않게 하려는 것이다 (SDD 3.5). 값을 이름으로 골라 읽는 목록(아래 두 상수)에 없는 칸은 읽지 않는다.

키가 없으면 node_scope가 span 없이 지나가므로 이 래퍼는 노드를 그대로 부르는 것과 같다.
"""
from __future__ import annotations

import functools
from typing import Callable

from backend.app.graph.state import AgentState
from backend.app.observability import langfuse as tracing

# 갈림길 이름으로 남겨도 되는 칸. 값은 pass, retry, give_up 세 가지뿐이다 (state.py)
_ROUTE_FIELDS = ("judge_route", "follow_route")
# 시도 번호를 남기는 루프 노드와 그 번호를 세는 State 칸(정수). judge와 answer_follow_up은 갱신에 번호가 있고,
# validate 쪽은 갱신에 없어 State에서 읽는다. 루프 밖 노드(decorate 등)에는 지난 루프의 번호가 State에 남아 있으므로 붙이지 않는다
_ATTEMPT_FIELD = {"judge": "judge_attempt", "validate": "judge_attempt",
                  "answer_follow_up": "follow_attempt", "validate_follow_up": "follow_attempt"}


def _annotate(handle, name: str, state: AgentState, update: dict) -> None:
    """노드가 돌려준 State 갱신(와 시도 번호만 State)에서 안전한 칸만 골라 span metadata 자리(handle.metadata)에 적는다.
    attempt: 판정·후속 루프의 몇 번째 시도인가(루프 노드만). outcome: 갈림길 이름, 없으면 응답 status("status:<값>").
    응답은 status 한 글자열만 읽는다(응답의 다른 칸은 B/L 마스킹 값과 문장을 담고 있으므로 읽지 않는다)."""
    field = _ATTEMPT_FIELD.get(name)
    if field is not None:
        value = update.get(field, state.get(field))
        if isinstance(value, int) and not isinstance(value, bool):
            handle.metadata["attempt"] = value
    for route_field in _ROUTE_FIELDS:
        value = update.get(route_field)
        if value in ("pass", "retry", "give_up"):
            handle.metadata["outcome"] = value
    if "outcome" not in handle.metadata:
        status = getattr(update.get("response"), "status", None)
        if isinstance(status, str):
            handle.metadata["outcome"] = f"status:{status}"


def node_span(name: str) -> Callable:
    """노드 함수 하나를 span "node:<name>"으로 감싸는 데코레이터. build_graph가 노드를 올릴 때 쓴다.
    노드를 build에서 감싸는 이유: nodes.py의 함수는 단위 테스트가 직접 부르므로 그대로 두고, 그래프 안에서 돌 때만 span이 생기게 하려는 것이다.
    functools.wraps로 이름과 타입 표시를 이어 받아 langgraph가 State 스키마를 읽는 데 지장이 없다."""
    def decorate(node: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
        @functools.wraps(node)
        def wrapper(state: AgentState) -> dict:
            with tracing.node_scope(name) as handle:
                update = node(state)
                _annotate(handle, name, state, update or {})
                return update
        return wrapper
    return decorate
