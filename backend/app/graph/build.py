"""판정 그래프 조립. 근거: docs/specs/v2-langgraph.md 2절 엣지 그림, 10절 H2.

책임: 노드(nodes.py)와 갈림길(routes.py)을 StateGraph로 이어 컴파일한다.
H2의 모양: guard_input -> load_memory -> load_saved -> (follow_up | fetch_customs -> (mrn_fallback | scope_gate -> identify
-> assess_rest) -> decorate) -> END. 후속 질문(follow_up)은 H5까지 v1 함수를 한 번에 부르는 굵은 노드다.
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
              "scope_gate", "identify", "assess_rest", "decorate")


def build_graph(checkpointer=None):
    """판정 그래프를 만들어 컴파일한다. checkpointer=None이면 상태를 저장하지 않는다 (H2).

    조건부 엣지의 도착지 목록(path_map)을 명시한다. 함수가 돌려줄 수 있는 이름을 한눈에 보이게 하고,
    그림(get_graph)에도 모든 갈래가 나오게 하려는 것이다."""
    graph = StateGraph(AgentState)
    for name in NODE_NAMES:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges("guard_input", routes.after_guard, {"load_memory": "load_memory", END: END})
    graph.add_edge("load_memory", "load_saved")
    graph.add_conditional_edges("load_saved", routes.after_saved,
                                {"follow_up": "follow_up", "fetch_customs": "fetch_customs"})
    graph.add_edge("follow_up", END)
    graph.add_conditional_edges("fetch_customs", routes.after_customs,
                                {"scope_gate": "scope_gate", "mrn_fallback": "mrn_fallback", END: END})
    graph.add_edge("mrn_fallback", END)
    graph.add_conditional_edges("scope_gate", routes.after_scope, {"decorate": "decorate", "identify": "identify"})
    graph.add_conditional_edges("identify", routes.after_identify,
                                {"decorate": "decorate", "assess_rest": "assess_rest"})
    graph.add_edge("assess_rest", "decorate")
    graph.add_edge("decorate", END)
    return graph.compile(checkpointer=checkpointer)
