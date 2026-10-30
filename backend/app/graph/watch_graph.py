"""지켜보기 재판정 그래프. 근거: docs/specs/v2-langgraph.md 6절, 10절 H8, SDD 5.10.

책임: 지켜보는 화물의 재판정을 "고르기 -> 화물마다 병렬 판정(Send) -> 차례로 반영" 그래프로 돈다. 체크포인터는 붙이지 않는다.
모양: START -> select_due -> (Send 화물마다) assess_one -> apply_results -> END. 고른 화물이 없으면 select_due에서 apply_results로 바로 간다.

노드 책임:
- select_due (순차): 못 보낸 알림 다시 보내기(start_run)와 재판정 대상 고르기(_due_rows). 대상 수(limit)가 여기서 정해지므로 Send는 그 수만큼만 생기고,
  ShipFinder 호출 상한(하루 한도, 한 번 실행 예산, 건수 = min(대기, 예산 // 화물당 호출 수))은 병렬화로 바뀌지 않는다.
- assess_one (병렬, 건별): services/watch.assess_cargo로 요청을 만들고(B/L 복호화, 오늘 토큰 한도 확인) 판정 함수를 부른다. DB 연결을 쓰지 않는다.
  노드 전체를 try/except로 감싸 예외를 ItemResult로 돌려준다: 예외가 노드 밖으로 나가면 실행 전체가 멈추므로 이것이 건별 격리다 (EC-10).
  한도 소진(WatchError "quota")은 error_kind로 표시해 연속 실패로 세지 않게 한다 (EC-11).
- apply_results (순차): 결과를 select_due가 고른 순서로 돌며 화물 표 반영, 변화 기록, 알림, 시도 기록을 한다(apply_assessment 등).
  공유 DB 연결은 이 노드에서만 쓴다. psycopg 연결을 여러 스레드가 동시에 쓰지 않게 하려는 것이다.

State에는 B/L 원문을 두지 않는다. 화물 줄(rows)에는 잠근 B/L(bl_encrypted)과 가린 번호만 있고, 열어 만든 요청 객체(AgentRequest, B/L 원문 포함)는
State 밖의 실행 지역 딕셔너리(requests)에 두었다가 반영 때 같은 객체로 쓴다 (AC-09의 취지, SDD 3.5).
병렬 폭은 rules.md watch_parallel_max를 config["max_concurrency"]로 준다. 1이면 고른 순서대로 한 건씩 돌아 순차 길과 결과가 같다.
건별 ContextVar(trusted_vessel, previous_eta)는 assess_cargo가 노드를 돌리는 스레드의 문맥에만 꽂는다. 건마다 문맥이 달라 서로 섞이지 않는다.
"""
from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from backend.app.graph.observe import node_span
from backend.app.services import watch
from backend.app.services.rules import rule


class WatchItem(TypedDict):
    """Send로 assess_one에 주는 입력. index는 select_due가 고른 순서, row는 화물 한 줄(user_cargo 칸 13개)."""
    index: int
    row: tuple


class ItemResult(TypedDict):
    """화물 한 건의 판정 결과. 실패하면 ok=False, error_kind에 종류 이름만, response는 None. 요청(B/L 포함)은 담지 않는다."""
    index: int
    ok: bool
    error_kind: str | None
    response: object


class WatchState(TypedDict, total=False):
    """지켜보기 그래프 State. results는 병렬 노드들이 각자 돌려준 목록을 리듀서(operator.add)로 이어 붙인다."""
    rows: list
    summary: dict
    results: Annotated[list, operator.add]


def build_watch_graph(conn, *, limit: int, now: datetime, assess, known_vessel_only: bool, requests: dict | None = None):
    """이번 실행의 연결·시각·판정 함수를 노드가 닫아 쓰는 그래프를 만들어 컴파일한다. 실행마다 새로 만든다(컴파일이 가볍다).
    requests: assess_one이 만든 요청 객체를 index별로 두는 실행 지역 딕셔너리. 테스트가 State에 B/L이 없음을 볼 수 있게 밖에서 받을 수도 있다."""
    requests = {} if requests is None else requests

    def select_due(state: WatchState) -> dict:
        """실행 시작 처리와 재판정 대상 고르기. 순차이고 DB 연결을 쓴다. 대상 수가 여기서 정해진다 (6절 ShipFinder 호출 상한)."""
        summary = watch.start_run(conn, now, known_vessel_only)
        return {"summary": summary, "rows": watch._due_rows(conn, limit, known_vessel_only)}

    def fan_out(state: WatchState):
        """고른 화물마다 Send 하나. 하나도 없으면 반영 노드로 바로 간다."""
        sends = [Send("assess_one", {"index": i, "row": row}) for i, row in enumerate(state.get("rows", []))]
        return sends or "apply_results"

    def assess_one(item: WatchItem) -> dict:
        """화물 한 건 판정(병렬). 어떤 예외도 밖으로 내보내지 않고 ItemResult로 돌려준다 (건별 격리, EC-10).
        요청 객체는 State에 넣지 않고 requests[index]에 둔다(B/L 원문이 들어 있다)."""
        index = item["index"]
        try:
            request, response = watch.assess_cargo(item["row"], now, assess)
        except Exception as e:  # 한 건 실패가 다른 건의 판정과 반영을 막지 않는다. 종류 이름만 남긴다
            return {"results": [{"index": index, "ok": False, "error_kind": watch.error_kind(e), "response": None}]}
        requests[index] = request
        return {"results": [{"index": index, "ok": True, "error_kind": None, "response": response}]}

    def apply_results(state: WatchState) -> dict:
        """결과를 고른 순서(index)대로 반영한다(순차). 건별 실패 처리는 순차 길과 같다: 반영 중 DB 오류도 그 건의 실패로 센다."""
        summary = state.get("summary", {})
        rows = state.get("rows", [])
        for res in sorted(state.get("results", []), key=lambda r: r["index"]):
            row = rows[res["index"]]
            if not res["ok"]:
                watch.count_failure(conn, row, now, summary, res["error_kind"])
                continue
            try:
                judged, changed, notice = watch.apply_assessment(conn, row, now, res["response"], requests.pop(res["index"], None))
            except Exception as e:  # 반영 도중 실패도 그 건만 실패로 센다
                watch.count_failure(conn, row, now, summary, watch.error_kind(e))
                continue
            watch.count_result(conn, row, now, summary, judged, changed, notice)
        return {"summary": summary}

    graph = StateGraph(WatchState)
    for name, fn in (("select_due", select_due), ("assess_one", assess_one), ("apply_results", apply_results)):
        graph.add_node(name, node_span(name)(fn))
    graph.add_edge(START, "select_due")
    graph.add_conditional_edges("select_due", fan_out, ["assess_one", "apply_results"])
    graph.add_edge("assess_one", "apply_results")
    graph.add_edge("apply_results", END)
    return graph.compile()


def parallel_width() -> int:
    """동시에 판정할 최대 화물 수. rules.md watch_parallel_max, 1 미만은 1로 본다 (6절)."""
    return max(1, int(rule("watch_parallel_max")))


def run_watch_graph(conn, *, limit: int, now: datetime, assess, known_vessel_only: bool = False) -> dict:
    """services/watch.run_watch의 그래프 길. 요약 딕셔너리(checked, changed, ...)를 순차 길과 같은 뜻으로 돌려준다."""
    graph = build_watch_graph(conn, limit=limit, now=now, assess=assess, known_vessel_only=known_vessel_only)
    out = graph.invoke({}, config={"max_concurrency": parallel_width()})
    return out["summary"]
