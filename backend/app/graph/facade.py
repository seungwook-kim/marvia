"""판정 그래프 퍼사드: run_agent가 AGENT_GRAPH=on일 때 부르는 진입점. 근거: docs/specs/v2-langgraph.md 2절 원칙, 3절 퍼사드, 10절 H2.

책임: 그래프를 한 번 돌려 AgentResponse를 얻고, v1 _run이 응답 뒤에 하던 일(조회 이력 _logged, 저장 판매 예정일 한 줄,
화물 표·기억 쓰기 remember_later)을 똑같이 한다. 요청 단위 DB 쓰기를 노드가 아니라 여기서 하는 것은 재개한 노드가 쓰기를 두 번
하지 않게 하려는 설계다 (2절 원칙).

HTTP 일(로그인, 토큰 한도 429, 요청 루트 span, session_scope, DB 상태 ContextVar)은 이 함수를 부른 run_agent가 이미 열어 두었다.
그래서 request_id ContextVar와 Langfuse 루트 span은 v1과 같고, 여기서 새로 열지 않는다.

H3: previous_eta, trusted_vessel, db_failed는 State 입력으로 들어가 노드 사이를 State로 건너간다 (2절 ContextVar 이전).
이 파일이 ContextVar와 State 사이의 경계다: 들어올 때 호출한 쪽이 꽂아 둔 ContextVar(trusted_vessel, _db_state)를 State 입력으로 옮기고,
나갈 때 State의 db_failed를 호출한 쪽의 _db_state에 되돌려 놓는다. 지켜보기 재판정은 assess_graph로 같은 값을 State로 넘긴다.

H2는 체크포인터가 없다(None). thread_id, 멈춘 그래프 이어 가기, GraphRecursionError 처리는 H7·H4의 일이다.
"""
from __future__ import annotations

import functools

from backend.app.graph.build import build_graph
from backend.app.observability.logging import log_failure
from backend.app.routers import agent
from backend.app.schemas.agent import AgentRequest, AgentResponse
from backend.app.schemas.customs import mask_bl


@functools.lru_cache(maxsize=1)
def _compiled():
    """체크포인터 없는 판정 그래프를 한 번만 컴파일한다. 노드가 agent의 이름을 호출 시점에 읽으므로 캐시해도 테스트의 패치가 먹는다."""
    return build_graph(None)


@functools.lru_cache(maxsize=1)
def _compiled_assess():
    """세관 조회부터 시작하는 판정만의 그래프(v1 _assess와 같은 범위)를 한 번만 컴파일한다."""
    return build_graph(None, entry="fetch_customs")


def assess_graph(request: AgentRequest, memory: str | None = None) -> AgentResponse:
    """v1 `routers.agent._assess(request, memory)`와 같은 일을 그래프로 한다. 지켜보기 재판정(services/watch._check_one)이 쓴다.

    지켜보기는 저장해 둔 확정 배(trusted_vessel)와 지난 신뢰 ETA(previous_eta)를 호출 쪽(_check_one)이 ContextVar에 꽂아 두고 부른다
    (기존 테스트가 그 ContextVar를 읽는다). 이 함수가 그 값을 State 입력으로 옮겨, 그래프 안에서는 State로만 흐르게 한다 (2절 ContextVar 이전).
    판매 예정일 검사, 기억·화물 줄 읽기, 이력·기억 쓰기는 하지 않는다: _assess가 안 하는 일이다. 기억 블록은 지켜보기가 늘 None이라 받지 않는다.
    run_agent 밖이라 _db_state가 None이고, db_scope는 그때 실패를 기억하지 않는다(화물 하나의 DB 실패가 다른 화물로 번지지 않게)."""
    if memory is not None:
        raise ValueError("assess_graph는 기억 블록을 받지 않는다(지켜보기는 None)")
    out = _compiled_assess().invoke({"request": request, "bl_masked": mask_bl(request.bl_no), "path": [],
                                     "trusted_vessel": agent.trusted_vessel.get(), "previous_eta": agent.previous_eta.get()})
    return out["response"]


def run_graph(request: AgentRequest, user_id: str | None) -> AgentResponse:
    """_run과 같은 일을 그래프로 한다. 입력은 요청과 사용자 ID, 출력은 같은 AgentResponse.

    State에는 B/L 원문을 request 객체 안에만 두고 최상위 문자열에는 가린 값(bl_masked)만 둔다 (SDD 3.5, 4절 마스킹).
    """
    out = _compiled().invoke({"request": request, "user_id": user_id, "bl_masked": mask_bl(request.bl_no), "path": [],
                              "trusted_vessel": agent.trusted_vessel.get(), "db_failed": agent._db_failed()})
    response = out["response"]
    if out.get("db_failed"):
        # 노드가 State로 되받은 DB 실패를 이 요청의 _db_state에 되돌려 놓는다. 아래 _logged와 기억 쓰기가 그것을 본다
        agent._mark_db_failed()

    # 판매 예정일이 범위 밖이라 첫 노드에서 끝난 실행: v1은 이력만 남기고 화물 표·기억은 쓰지 않는다 (_run의 첫 return)
    if out["path"] == ["guard_input"]:
        return agent._logged(request, response, user_id)

    # 저장된 판매 예정일로 판정했으면 답 끝에 그 사실을 한 줄 붙인다. 후속 질문 갈래는 v1도 붙이지 않는다 (saved_due가 없다)
    if out.get("saved_due"):
        response = agent._with_saved_due_note(response, out["saved_due"])
    # 이력에는 저장 날짜를 채운 요청(judged_request)을 남긴다. 후속 질문은 원래 요청이다 (v1 _run)
    response = agent._logged(out.get("judged_request") or out["request"], response, user_id)
    if agent._db_failed():
        # DB가 이미 안 붙는 요청이다. 뒤 작업(화물 표 쓰기·기억 뽑기)도 붙지 못하고 일꾼 자리만 잡는다
        log_failure("memory_write", "skipped_after_db_failure")
        return response
    # 화물 표에는 실제로 찾은 구분을 적는다. 다음 조회가 그 구분으로 바로 간다 (services/cargo.upsert_assessment)
    agent.memory_service.remember_later(user_id, agent.matched_request(out["request"], response), response,
                                        released=agent.is_released(response))
    return response
