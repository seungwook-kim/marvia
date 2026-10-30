"""채팅 그래프. 근거: docs/specs/v2-langgraph.md 9절, 10절 H11, SDD 4.5.

책임: services/chat.handle이 한 함수에서 이어 부르던 토막(prepare, guard_reply, plan, agent_attempt, fallback_reply, finish)을 노드로 잇는다.
판단은 토막이 하고 이 파일은 순서와 갈림길만 정한다. 체크포인터는 붙이지 않는다: 대화는 conversations 표가 저장하고,
마스킹 대응표([BL1] -> 원본)는 이 그래프의 State에만 잠깐 있다가 요청이 끝나면 사라진다.

모양:
  START -> mask -> (컨테이너 번호만 못 찾음: END | guard)
  guard -> (주입·범위 밖 주제: finish | plan)
  plan -> (에이전트 시도: agent | 회로 열림·CHAT_AGENT=off: fallback)
  agent -> (답 있음: finish | 첫 도구 전 LLM 실패: fallback)
  fallback -> finish -> END

노드 대응 (9절의 후보와 다른 점):
- `classic`은 `fallback` 노드다. 관세사 고정 안내(BROKER_ONLY)와 의도 분류 방식(_classic)을 함께 맡는다.
- `reply_check`(V7, V9, V10, V13, V14)는 별도 노드가 아니라 `agent` 노드 안(_agent_turn)에 있다. 검사에 실패하면 같은 도구 루프 호출에
  피드백을 붙여 다시 묻고(EC-08의 검색 재시도 포함) 코드 문장으로 접는 흐름이 llm.chat_agent 호출과 얽혀 있어, 노드로 쪼개면 호출 단위가 llm.py 밖으로 샌다 (Q10).
- `finalize`는 `finish` 노드다. [E#] 검사와 서류·카톡·지켜보기 문장은 _agent 안에서 이미 붙으므로 여기서는 기억 뽑기 보내기, 근거 목록 채우기, 고쳐 읽은 B/L 알림만 한다.
- `progress.step("understand")`(화면 단계 소식)는 `plan` 노드가 보낸다. 단계 소식 듣는 쪽은 요청 문맥(ContextVar)이라 노드 스레드에 복사돼 그대로 보인다.
- `mask`는 입력 검사(ChatError)를 포함한다. 빈 문장·긴 문장은 예외로 그래프를 빠져나가 라우터가 400으로 바꾼다 (지금과 같다).

요청 문맥: 노드는 문맥 복사본에서 돈다(노드 안에서 set한 ContextVar는 다음 노드에 보이지 않는다). 그래서 agent·fallback 노드는 route_scope로
기억 뽑기용 가린 문장(memory.user_text)과 B/L 구분(_STORED_TYPES)을 노드 안에서 다시 켜고, 표시판(Turn)은 facade가 만든 한 객체를 State로 건네
어느 노드에서 켜진 fired든 finish가 읽는다. 회로 차단기(_agent_breaker)는 프로세스 전역이라 문맥과 상관없다.
B/L 원문(refs)은 State에만 있고 로그·span에는 가지 않는다(node_span은 State를 읽지 않는다).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.app.graph.observe import node_span
from backend.app.services import chat, memory

NODE_NAMES = ("mask", "guard", "plan", "agent", "fallback", "finish")


class ChatState(TypedDict, total=False):
    """채팅 그래프 State. 모두 한 요청 안에서만 산다(저장하지 않는다)."""
    prep: chat.Prepared        # mask가 만든 가린 문장·B/L 대응표·MRN. refs에 B/L 원문이 들어 있다
    result: chat.ChatResult    # 정해진 답. 없으면 다음 노드가 만든다
    turn: memory.Turn          # 이 턴의 표시판(기억 뽑기를 이미 맡겼나). facade가 만들어 건넨다
    today: str                 # plan이 정한 오늘 날짜(KST, 글자)
    use_agent: bool            # plan이 정한 에이전트 시도 여부


# ── 갈림길 (코드만) ──

def after_mask(state: ChatState) -> str:
    """컨테이너 번호만 못 찾은 문장은 마무리 없이 끝난다(지금 handle과 같다). 그 밖에는 guard."""
    return "end" if state["prep"].early is not None else "guard"


def after_guard(state: ChatState) -> str:
    """고정 문장으로 끝낼 문장이면 finish, 아니면 plan."""
    return "finish" if state.get("result") is not None else "plan"


def after_plan(state: ChatState) -> str:
    """에이전트를 시도하면 agent, 회로가 열려 있거나 끄면 fallback."""
    return "agent" if state.get("use_agent") else "fallback"


def after_agent(state: ChatState) -> str:
    """에이전트가 답을 냈으면 finish, 첫 도구 전에 실패해 답이 없으면 fallback."""
    return "finish" if state.get("result") is not None else "fallback"


def build_chat_graph(*, message: str, session_id: str, context, run_agent, user_id: str | None, history: list[dict],
                     run_tool: Callable | None, watch, kakao, on_screen: dict | None):
    """이번 요청의 입력을 노드가 닫아 쓰는 그래프를 컴파일한다. 요청마다 새로 만든다(컴파일이 가볍다).
    노드는 chat 모듈의 함수를 호출 시점에 모듈 속성으로 부른다(테스트가 chat.llm 등을 바꿔 끼우는 것이 그대로 먹게)."""

    def scope(state: ChatState):
        prep = state["prep"]
        return chat.route_scope(prep.masked, prep.stored_types, state["turn"])

    def mask(state: ChatState) -> dict:
        """입력 검사와 가리기 (chat.prepare). 컨테이너만 못 찾았으면 그 답을 result에 둔다."""
        prep = chat.prepare(message, user_id)
        return {"prep": prep, "result": prep.early}

    def guard(state: ChatState) -> dict:
        """주입 모양·범위 밖 주제는 LLM 없이 고정 문장 (chat.guard_reply)."""
        prep = state["prep"]
        return {"result": chat.guard_reply(prep.masked, prep.refs)}

    def plan(state: ChatState) -> dict:
        """화면에 understand 단계를 알리고 에이전트를 시도할지 정한다 (chat.plan). 오늘 날짜도 여기서 정한다."""
        return {"use_agent": chat.plan(), "today": datetime.now(chat.KST).date().isoformat()}

    def agent(state: ChatState) -> dict:
        """채팅 에이전트 한 턴 (chat.agent_attempt). 도구 루프와 답 검사는 이 안에서 돈다. 첫 도구 전 LLM 실패면 result가 None이다."""
        prep = state["prep"]
        with scope(state):
            result = chat.agent_attempt(prep.masked, prep.refs, prep.mrn, session_id, context, run_agent, user_id, history, run_tool,
                                        state["today"], watch, kakao, on_screen)
        return {"result": result}

    def fallback(state: ChatState) -> dict:
        """대비 경로 (chat.fallback_reply): 관세사 고정 안내 또는 의도 분류 방식."""
        prep = state["prep"]
        with scope(state):
            result = chat.fallback_reply(prep.masked, prep.refs, prep.mrn, session_id, context, run_agent, user_id, state["today"], history)
        return {"result": result}

    def finish(state: ChatState) -> dict:
        """마무리 (chat.finish): 기억 뽑기 보내기, 근거 목록 채우기, 고쳐 읽은 B/L 알림."""
        prep = state["prep"]
        return {"result": chat.finish(state["result"], prep.masked, user_id, state["turn"], prep.repair_notes)}

    graph = StateGraph(ChatState)
    for name, fn in (("mask", mask), ("guard", guard), ("plan", plan), ("agent", agent), ("fallback", fallback), ("finish", finish)):
        graph.add_node(name, node_span(name)(fn))
    graph.add_edge(START, "mask")
    graph.add_conditional_edges("mask", after_mask, {"end": END, "guard": "guard"})
    graph.add_conditional_edges("guard", after_guard, {"finish": "finish", "plan": "plan"})
    graph.add_conditional_edges("plan", after_plan, {"agent": "agent", "fallback": "fallback"})
    graph.add_conditional_edges("agent", after_agent, {"finish": "finish", "fallback": "fallback"})
    graph.add_edge("fallback", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def run_chat_graph(message: str, session_id: str, context, run_agent, user_id: str | None, history: list[dict],
                   run_tool: Callable | None, watch, kakao, on_screen: dict | None) -> chat.ChatResult:
    """services/chat.handle의 그래프 길. 같은 ChatResult를 돌려준다. 빈 문장·긴 문장의 ChatError는 그래프 밖으로 그대로 나간다."""
    graph = build_chat_graph(message=message, session_id=session_id, context=context, run_agent=run_agent, user_id=user_id,
                             history=history, run_tool=run_tool, watch=watch, kakao=kakao, on_screen=on_screen)
    out = graph.invoke({"turn": memory.Turn()})
    return out["result"]
