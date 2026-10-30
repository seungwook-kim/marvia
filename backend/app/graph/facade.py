"""판정 그래프 퍼사드: run_agent가 AGENT_GRAPH=on일 때 부르는 진입점. 근거: docs/specs/v2-langgraph.md 2절 원칙, 3절 퍼사드, 10절 H2.

책임: 그래프를 한 번 돌려 AgentResponse를 얻고, v1 _run이 응답 뒤에 하던 일(조회 이력 _logged, 저장 판매 예정일 한 줄,
화물 표·기억 쓰기 remember_later)을 똑같이 한다. 요청 단위 DB 쓰기를 노드가 아니라 여기서 하는 것은 재개한 노드가 쓰기를 두 번
하지 않게 하려는 설계다 (2절 원칙).

HTTP 일(로그인, 토큰 한도 429, 요청 루트 span, session_scope, DB 상태 ContextVar)은 이 함수를 부른 run_agent가 이미 열어 두었다.
그래서 request_id ContextVar와 Langfuse 루트 span은 v1과 같고, 여기서 새로 열지 않는다.

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


def run_graph(request: AgentRequest, user_id: str | None) -> AgentResponse:
    """_run과 같은 일을 그래프로 한다. 입력은 요청과 사용자 ID, 출력은 같은 AgentResponse.

    State에는 B/L 원문을 request 객체 안에만 두고 최상위 문자열에는 가린 값(bl_masked)만 둔다 (SDD 3.5, 4절 마스킹).
    """
    out = _compiled().invoke({"request": request, "user_id": user_id, "bl_masked": mask_bl(request.bl_no), "path": []})
    response = out["response"]

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
