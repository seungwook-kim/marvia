"""판정 그래프 조립. 근거: docs/specs/v2-langgraph.md 2절 엣지 그림, 10절 H2.

책임: 노드(nodes.py)와 갈림길(routes.py)을 StateGraph로 이어 컴파일한다.
H2의 모양: guard_input -> load_memory -> load_saved -> (follow_up | fetch_customs -> (mrn_fallback | scope_gate -> identify
-> collect -> compute_signals -> (gate_no_position | gate_no_eta | table_verdict -> judge_loop)) -> decorate) -> END. 후속 질문(follow_up)은 H5까지 v1 함수를 한 번에 부르는 굵은 노드다.
기억 읽기(load_memory)를 화물 줄 읽기(load_saved)보다 먼저 두는 것은 v1 _run의 순서를 지키려는 것이다:
기억 읽기가 실패하면 화물 줄 읽기는 DB에 다시 붙지 않는다 (10-08 QA B7, test_qa_1008_verdict.py). spec 표의 순서와 다르다.
체크포인터는 H7에서 붙인다. H2는 None이라 노드 사이 값은 메모리에서만 움직이고 저장되지 않는다.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend.app.graph import nodes, routes
from backend.app.graph.state import AgentState

# 그래프에 올리는 노드 이름. 함수 이름과 같다 (nodes.py)
NODE_NAMES = ("guard_input", "load_memory", "load_saved", "follow_up", "fetch_customs", "mrn_fallback",
              "scope_gate", "identify", "collect", "compute_signals", "gate_no_position", "gate_no_eta",
              "table_verdict", "judge_loop", "decorate")


def build_graph(checkpointer=None, *, entry: str = "guard_input"):
    """판정 그래프를 만들어 컴파일한다. checkpointer=None이면 상태를 저장하지 않는다 (H2).

    entry: 시작 노드. 기본은 guard_input(사용자 조회 전체). "fetch_customs"는 판매 예정일 검사, 기억·화물 줄 읽기를 건너뛰고
    세관 조회부터 시작하는 판정만의 그래프로, v1 `_assess`와 같은 범위다 (지켜보기 재판정이 쓴다, facade.assess_graph).

    조건부 엣지의 도착지 목록(path_map)을 명시한다. 함수가 돌려줄 수 있는 이름을 한눈에 보이게 하고,
    그림(get_graph)에도 모든 갈래가 나오게 하려는 것이다."""
    graph = StateGraph(AgentState)
    for name in NODE_NAMES:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, entry)
    graph.add_conditional_edges("guard_input", routes.after_guard, {"load_memory": "load_memory", END: END})
    graph.add_edge("load_memory", "load_saved")
    graph.add_conditional_edges("load_saved", routes.after_saved,
                                {"follow_up": "follow_up", "fetch_customs": "fetch_customs"})
    graph.add_edge("follow_up", END)
    graph.add_conditional_edges("fetch_customs", routes.after_customs,
                                {"scope_gate": "scope_gate", "mrn_fallback": "mrn_fallback", END: END})
    graph.add_edge("mrn_fallback", END)
    graph.add_conditional_edges("scope_gate", routes.after_scope, {"decorate": "decorate", "identify": "identify"})
    graph.add_conditional_edges("identify", routes.after_identify, {"decorate": "decorate", "collect": "collect"})
    graph.add_edge("collect", "compute_signals")
    graph.add_conditional_edges("compute_signals", routes.after_signals,
                                {"gate_no_position": "gate_no_position", "gate_no_eta": "gate_no_eta",
                                 "table_verdict": "table_verdict"})
    graph.add_edge("gate_no_position", "decorate")
    graph.add_edge("gate_no_eta", "decorate")
    graph.add_edge("table_verdict", "judge_loop")
    graph.add_edge("judge_loop", "decorate")
    graph.add_edge("decorate", END)
    return graph.compile(checkpointer=checkpointer)
