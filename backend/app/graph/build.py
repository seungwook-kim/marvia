"""판정 그래프 조립. 근거: docs/specs/v2-langgraph.md 2절 엣지 그림, 10절 H2~H4.

책임: 노드(nodes.py)와 갈림길(routes.py)을 StateGraph로 이어 컴파일한다.
모양: guard_input -> load_memory -> load_saved -> (load_previous -> ... | fetch_customs -> (mrn_fallback | scope_gate -> identify
-> collect -> compute_signals -> (gate_no_position | gate_no_eta | table_verdict -> judge <-> validate -> (finalize | fallback_partial)))
-> decorate) -> END.
후속 질문(H5): load_saved -> load_previous -> (지난 결과 있음: guard_follow_up | 없음: fetch_customs부터 판정을 다시 돌린 뒤 끝 자리마다
guard_follow_up으로 합류) -> answer_follow_up <-> validate_follow_up -> END. 합류하는 끝 자리는 decorate, 세관 조회 실패(fetch_customs),
mrn_fallback이다: v1은 `_previous_response(...) or _assess(...)`의 결과가 조회 실패여도 그 위에 후속 답을 만든다.
judge <-> validate 루프(H4): judge가 LLM을 한 번 부르고, 호출이 실패하면 fallback_partial로, 아니면 validate로 간다.
validate 뒤 엣지 함수(routes.after_validate)가 통과면 finalize, 다시 묻기면 judge, 접기(상한·시간 예산)면 fallback_partial로 보낸다.
기억 읽기(load_memory)를 화물 줄 읽기(load_saved)보다 먼저 두는 것은 v1 _run의 순서를 지키려는 것이다:
기억 읽기가 실패하면 화물 줄 읽기는 DB에 다시 붙지 않는다 (10-08 QA B7, test_qa_1008_verdict.py). spec 표의 순서와 다르다.
체크포인터는 H7에서 붙인다. H2는 None이라 노드 사이 값은 메모리에서만 움직이고 저장되지 않는다.

간선 표(FIXED, BRANCHES)를 한 곳에 두고, 시작 노드(entry)에서 닿는 노드만 그래프에 올린다. 판정만의 그래프(entry="fetch_customs")에
guard_input, load_memory, load_saved, load_previous처럼 닿지 않는 노드가 따라 들어가지 않게 하려는 것이다.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend.app.graph import nodes, routes
from backend.app.graph.state import AgentState

# 갈림길 없는 간선: 노드 -> 다음 노드(또는 END)
FIXED: dict[str, str] = {
    "load_memory": "load_saved", "collect": "compute_signals",
    "gate_no_position": "decorate", "gate_no_eta": "decorate", "table_verdict": "judge",
    "finalize": "decorate", "fallback_partial": "decorate",
}

# 조건부 간선: 노드 -> (엣지 함수, {함수가 돌려주는 이름: 도착 노드}).
# 도착지 목록(path_map)을 명시한다. 함수가 돌려줄 수 있는 이름을 한눈에 보이게 하고, 그림(get_graph)에도 모든 갈래가 나오게 하려는 것이다
BRANCHES: dict[str, tuple] = {
    "guard_input": (routes.after_guard, {"load_memory": "load_memory", END: END}),
    "load_saved": (routes.after_saved, {"load_previous": "load_previous", "fetch_customs": "fetch_customs"}),
    "load_previous": (routes.after_previous, {"guard_follow_up": "guard_follow_up", "fetch_customs": "fetch_customs"}),
    "fetch_customs": (routes.after_customs, {"scope_gate": "scope_gate", "mrn_fallback": "mrn_fallback",
                                             "guard_follow_up": "guard_follow_up", END: END}),
    "mrn_fallback": (routes.after_mrn_fallback, {"guard_follow_up": "guard_follow_up", END: END}),
    "scope_gate": (routes.after_scope, {"decorate": "decorate", "identify": "identify"}),
    "identify": (routes.after_identify, {"decorate": "decorate", "collect": "collect"}),
    "compute_signals": (routes.after_signals, {"gate_no_position": "gate_no_position", "gate_no_eta": "gate_no_eta",
                                               "table_verdict": "table_verdict"}),
    "judge": (routes.after_judge, {"validate": "validate", "give_up": "fallback_partial"}),
    "validate": (routes.after_validate, {"pass": "finalize", "retry": "judge", "give_up": "fallback_partial"}),
    "decorate": (routes.after_decorate, {"guard_follow_up": "guard_follow_up", END: END}),
    "guard_follow_up": (routes.after_guard_follow_up, {"answer_follow_up": "answer_follow_up", END: END}),
    "answer_follow_up": (routes.after_answer_follow_up, {"validate_follow_up": "validate_follow_up", END: END}),
    "validate_follow_up": (routes.after_validate_follow_up, {"answer_follow_up": "answer_follow_up", END: END}),
}

# 그래프에 올릴 수 있는 노드 이름. 함수 이름과 같다 (nodes.py)
NODE_NAMES = ("guard_input", "load_memory", "load_saved", "load_previous", "fetch_customs", "mrn_fallback",
              "scope_gate", "identify", "collect", "compute_signals", "gate_no_position", "gate_no_eta",
              "table_verdict", "judge", "validate", "finalize", "fallback_partial", "decorate",
              "guard_follow_up", "answer_follow_up", "validate_follow_up")


def _reachable(entry: str) -> list[str]:
    """entry에서 간선을 따라 닿는 노드 이름을 NODE_NAMES 순서로 돌려준다."""
    seen: set[str] = set()
    todo = [entry]
    while todo:
        name = todo.pop()
        if name == END or name in seen:
            continue
        seen.add(name)
        if name in FIXED:
            todo.append(FIXED[name])
        if name in BRANCHES:
            todo.extend(BRANCHES[name][1].values())
    return [name for name in NODE_NAMES if name in seen]


def build_graph(checkpointer=None, *, entry: str = "guard_input"):
    """판정 그래프를 만들어 컴파일한다. checkpointer=None이면 상태를 저장하지 않는다 (H2).

    entry: 시작 노드. 기본은 guard_input(사용자 조회 전체). "fetch_customs"는 판매 예정일 검사, 기억·화물 줄 읽기를 건너뛰고
    세관 조회부터 시작하는 판정만의 그래프로, v1 `_assess`와 같은 범위다 (지켜보기 재판정이 쓴다, facade.assess_graph).
    그 시작 노드에서 닿지 않는 노드는 올리지 않는다."""
    graph = StateGraph(AgentState)
    reachable = _reachable(entry)
    for name in reachable:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, entry)
    for name in reachable:
        if name in FIXED:
            graph.add_edge(name, FIXED[name])
        if name in BRANCHES:
            route, path_map = BRANCHES[name]
            graph.add_conditional_edges(name, route, path_map)
    return graph.compile(checkpointer=checkpointer)
