"""판정 그래프 퍼사드: run_agent가 AGENT_GRAPH=on일 때 부르는 진입점. 근거: docs/specs/v2-langgraph.md 2절 원칙, 3절 퍼사드, 10절 H2.

책임: 그래프를 한 번 돌려 AgentResponse를 얻고, v1 _run이 응답 뒤에 하던 일(조회 이력 _logged, 저장 판매 예정일 한 줄,
화물 표·기억 쓰기 remember_later)을 똑같이 한다. 요청 단위 DB 쓰기를 노드가 아니라 여기서 하는 것은 재개한 노드가 쓰기를 두 번
하지 않게 하려는 설계다 (2절 원칙).

HTTP 일(로그인, 토큰 한도 429, 요청 루트 span, session_scope, DB 상태 ContextVar)은 이 함수를 부른 run_agent가 이미 열어 두었다.
그래서 request_id ContextVar와 Langfuse 루트 span은 v1과 같고, 여기서 새로 열지 않는다.

H3: previous_eta, trusted_vessel, db_failed는 State 입력으로 들어가 노드 사이를 State로 건너간다 (2절 ContextVar 이전).
이 파일이 ContextVar와 State 사이의 경계다: 들어올 때 호출한 쪽이 꽂아 둔 ContextVar(trusted_vessel, _db_state)를 State 입력으로 옮기고,
나갈 때 State의 db_failed를 호출한 쪽의 _db_state에 되돌려 놓는다. 지켜보기 재판정은 assess_graph로 같은 값을 State로 넘긴다.

H4: 노드 실행 횟수의 안전망 recursion_limit을 rules.md graph_recursion_limit에서 읽어 config로 준다. 넘기면 GraphRecursionError가 나고
(EC-12), 퍼사드가 그때까지 쌓인 State로 partial 응답을 만들어 끝낸다: log_failure("graph", "recursion_limit"), 표 등급이 정해져 있고
회색 지대가 아니면 _llm_gave_up과 같은 응답, 그 밖에는 LLM_FAILED 문구. failed는 쓰지 않는다 (SDD 5.7).

H7: 되묻기에서 멈춘 그래프를 이어 간다 (v2-langgraph.md 3, 4절). run_graph가 체크포인터(checkpoint.get_checkpointer)와 thread_id(threads.thread_id)가
있으면 먼저 그 스레드를 읽고, 멈춰 있고(만료 전, 같은 bl_type·mrn, 새 정보 있음)면 Command(resume=...)로 이어 가고, 아니면 스레드를 지우고 새로 돈다.
끝난 실행의 스레드는 바로 지우고 멈춘 실행의 스레드만 남긴다(Q7). 후속 질문은 스레드를 만들지 않는다. 같은 스레드의 요청은 프로세스 안 잠금으로
차례로 처리한다(EC-05). 체크포인트 읽기·쓰기가 실패하면(DB 장애, 풀 시간 초과, 복호화 실패) 로그에 종류만 남기고 체크포인터 없이 처음부터 돌아
응답을 막지 않는다(EC-06, EC-07). 응답 뒤 쓰기(이력, 화물 표·기억)는 이 파일이 한 번만 한다: 재개한 노드가 쓰기를 두 번 하지 않게 하려는 것이다.
"""
from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from backend.app.graph import checkpoint, nodes, threads
from backend.app.graph.build import build_graph
from backend.app.observability.logging import log_failure
from backend.app.routers import agent
from backend.app.schemas.agent import AgentRequest, AgentResponse
from backend.app.schemas.customs import mask_bl
from backend.app.services.rules import rule


@functools.lru_cache(maxsize=1)
def _compiled():
    """체크포인터 없는 판정 그래프를 한 번만 컴파일한다. 노드가 agent의 이름을 호출 시점에 읽으므로 캐시해도 테스트의 패치가 먹는다."""
    return build_graph(None)


@functools.lru_cache(maxsize=1)
def _compiled_assess():
    """세관 조회부터 시작하는 판정만의 그래프(v1 _assess와 같은 범위)를 한 번만 컴파일한다."""
    return build_graph(None, entry="fetch_customs")


# 체크포인터마다 컴파일한 그래프. 키는 체크포인터 객체(풀이 닫혀 다시 만들면 새 객체라 새로 컴파일한다)
_checkpointed: dict[int, tuple[object, object]] = {}
_checkpointed_lock = threading.Lock()


def _compiled_with(saver):
    """체크포인터를 붙인 판정 그래프. 체크포인터 객체당 한 번만 컴파일한다(객체를 붙들고 있어 id가 재사용되지 않는다)."""
    with _checkpointed_lock:
        hit = _checkpointed.get(id(saver))
        if hit is None or hit[0] is not saver:
            hit = (saver, build_graph(saver))
            _checkpointed[id(saver)] = hit
        return hit[1]


def _config(thread: str | None = None) -> dict:
    """invoke에 주는 config. recursion_limit(원본은 rules.md graph_recursion_limit)과, 체크포인터를 쓸 때 thread_id뿐이다.
    user_id, B/L 같은 값은 넣지 않는다: config의 configurable은 checkpoints.metadata(평문 JSONB)에 그대로 들어간다 (4절 마스킹 2)."""
    config: dict = {"recursion_limit": rule("graph_recursion_limit")}
    if thread is not None:
        config["configurable"] = {"thread_id": thread}
    return config


def _stream(graph, state_in, config: dict, *, durable: bool = False) -> tuple[dict, tuple]:
    """그래프를 끝까지(또는 멈출 때까지) 돌려 (마지막 State, 중단 목록)을 돌려준다. 상한(GraphRecursionError)에 걸리면 그때까지의 State로
    partial 응답을 채운다 (EC-12). invoke 대신 stream(values)으로 돌려 매 단계의 전체 State를 붙들어 둔다: 예외로는 중간 State를 얻을 수 없어서다.
    durable이면 durability="exit"로 돌려 체크포인트를 호출당 한 번만 쓴다 (스파이크 확인, 매 노드 쓰기를 피한다).
    멈추면 값 스트림 끝에 {"__interrupt__": (Interrupt, ...)}가 한 번 오는데, 이것은 State가 아니라 중단 목록이다."""
    last = state_in if isinstance(state_in, dict) else {}
    interrupts: tuple = ()
    try:
        for chunk in graph.stream(state_in, config, stream_mode="values", **({"durability": "exit"} if durable else {})):
            if "__interrupt__" in chunk:
                interrupts = tuple(chunk["__interrupt__"])
                continue
            last = chunk
    except GraphRecursionError:
        log_failure("graph", "recursion_limit")
        return {**last, "response": _recursion_response(last), "path": [*last.get("path", []), "recursion_limit"]}, ()
    return last, interrupts


def _run_to_end(graph, state_in: dict) -> dict:
    """체크포인터 없는 그래프를 끝까지 돌려 마지막 State를 돌려준다 (assess_graph, 체크포인터 없는 run_graph)."""
    return _stream(graph, state_in, _config())[0]


def _recursion_response(state: dict) -> AgentResponse:
    """상한에 걸렸을 때의 partial 응답 (EC-12). 표 등급이 정해져 있고 회색 지대가 아니면 판정 폴백과 같은 응답(표 등급 + 고정 문구),
    그 밖에는 LLM_FAILED 문구에 지금까지 모은 값만 싣는다. 후속 질문이 답할 지난 결과가 있으면 그 결과 + FOLLOW_UP_FAILED다. 세관 조회까지 끝났으면 화면 전용 칸(decorate와 같은 것)을 붙인다."""
    if state.get("previous") is not None:
        # 후속 질문이 답할 지난 결과가 이미 있으면 그 결과에 FOLLOW_UP_FAILED만 얹는다 (후속 답을 못 만들었을 때와 같다)
        return agent._follow_up_failed(state["previous"])
    collected = state.get("collected") or {}
    if state.get("table_verdict") is not None and state.get("signal_set") is not None:
        response = agent._llm_gave_up(state["signal_set"], state.get("days_margin"), state["table_verdict"],
                                      state["gray"], state["customs"], collected)
    else:
        response = agent._response(status="partial", answer=agent.LLM_FAILED, **collected)
    if state.get("customs") is not None:
        response = agent._with_display_fields(response, state["customs"], state.get("bl_type_used"))
    return response


def assess_graph(request: AgentRequest, memory: str | None = None) -> AgentResponse:
    """v1 `routers.agent._assess(request, memory)`와 같은 일을 그래프로 한다. 지켜보기 재판정(services/watch._check_one)이 쓴다.

    지켜보기는 저장해 둔 확정 배(trusted_vessel)와 지난 신뢰 ETA(previous_eta)를 호출 쪽(_check_one)이 ContextVar에 꽂아 두고 부른다
    (기존 테스트가 그 ContextVar를 읽는다). 이 함수가 그 값을 State 입력으로 옮겨, 그래프 안에서는 State로만 흐르게 한다 (2절 ContextVar 이전).
    판매 예정일 검사, 기억·화물 줄 읽기, 이력·기억 쓰기는 하지 않는다: _assess가 안 하는 일이다. 기억 블록은 지켜보기가 늘 None이라 받지 않는다.
    run_agent 밖이라 _db_state가 None이고, db_scope는 그때 실패를 기억하지 않는다(화물 하나의 DB 실패가 다른 화물로 번지지 않게)."""
    if memory is not None:
        raise ValueError("assess_graph는 기억 블록을 받지 않는다(지켜보기는 None)")
    out = _run_to_end(_compiled_assess(), {"request": request, "bl_masked": mask_bl(request.bl_no), "path": [],
                                           "trusted_vessel": agent.trusted_vessel.get(), "previous_eta": agent.previous_eta.get()})
    return out["response"]


# ── 되묻기 이어 가기 (3절) ──

# 체크포인트 읽기·쓰기가 실패했다고 볼 오류. 이 오류가 나면 체크포인터 없이 처음부터 돈다 (EC-06, EC-07).
# psycopg.Error에 풀 시간 초과(PoolTimeout)도 들어 있다. CheckpointCryptoError는 복호화 실패·평문 blob 거부다
_INFRA_ERRORS = (psycopg.Error, checkpoint.CheckpointCryptoError)

# 스레드(thread_id)별 프로세스 안 잠금. 같은 사용자가 같은 B/L로 요청 둘을 동시에 보내면(더블 클릭) 차례로 처리한다 (EC-05).
# 뒤 요청은 앞 요청이 스레드를 지운 뒤 새로 돈다. 잠금은 쓰는 요청이 없으면 치워 표가 계속 자라지 않게 한다
_thread_locks: dict[str, list] = {}
_thread_locks_guard = threading.Lock()


@contextmanager
def _thread_lock(thread: str):
    with _thread_locks_guard:
        entry = _thread_locks.setdefault(thread, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _thread_locks_guard:
            entry[1] -= 1
            if entry[1] == 0:
                _thread_locks.pop(thread, None)


def _delete_thread(saver, thread: str) -> None:
    """스레드를 지운다. 못 지워도 응답을 막지 않고 종류만 남긴다(남은 스레드는 TTL과 정리 함수가 치운다)."""
    try:
        saver.delete_thread(thread)
    except Exception as e:  # noqa: BLE001 - DB 장애·풀 시간 초과 모두 같은 처리
        log_failure("graph_checkpoint", f"delete_failed {type(e).__name__}")


def _read_snapshot(graph, saver, thread: str):
    """스레드의 마지막 스냅샷. 없으면 값이 빈 스냅샷, 읽기에 실패하면(DB 장애, 복호화 실패, 평문 blob) None이다.
    복호화 실패·평문 blob은 멈춘 그래프가 없는 것으로 보고 손상된 스레드를 지운다 (EC-07). DB 장애는 지우지 않고 EC-06으로 간다."""
    try:
        return graph.get_state(_config(thread))
    except checkpoint.CheckpointCryptoError:
        log_failure("graph_checkpoint", "read_failed corrupt")
        _delete_thread(saver, thread)
    except _INFRA_ERRORS as e:
        log_failure("graph_checkpoint", f"read_failed {type(e).__name__}")
    return None


def _paused_for_this_request(snap, request: AgentRequest) -> bool:
    """이 요청이 멈춘 그래프를 이어 갈 수 있는가 (3절 퍼사드의 결정, EC-02~04).
    - ask_wait에서 멈춰 있고 중단 값이 있다
    - 멈춘 지 graph_pause_ttl_hours 안이다 (통관 단계는 시간이 지나면 바뀐다, EC-02)
    - bl_type·mrn이 멈춘 요청과 같다 (EC-03). bl_type은 말하지 않았거나(None), 멈춘 요청이 쓴 값, 처음 실행이 찾은 구분(bl_type_used)이면 같은 것으로 본다:
      조회 결과를 화면이 받아 다음 요청에 실어 보내는 값이 그것이라서다
    - 새 정보가 있다: resolved_mmsi, voyage_no, forwarder_eta 중 하나가 멈춘 요청에 없던 값이다 (EC-04, Q9). 같은 질문을 다시 한 것이면 v1처럼 새로 돈다"""
    if not snap.values or snap.next != ("ask_wait",) or not snap.interrupts:
        return False
    created = datetime.fromisoformat(snap.created_at) if snap.created_at else None
    if created is None or datetime.now(timezone.utc) - created > timedelta(hours=rule("graph_pause_ttl_hours")):
        return False
    stored = snap.values["request"]
    if request.mrn != stored.mrn or request.bl_type not in (None, stored.bl_type, snap.values.get("bl_type_used")):
        return False
    return any(getattr(request, f) is not None and getattr(request, f) != getattr(stored, f)
               for f in ("resolved_mmsi", "voyage_no", "forwarder_eta"))


def _fresh_input(request: AgentRequest, user_id: str | None, *, resumable: bool) -> dict:
    """처음부터 돌 때의 State 입력. State에는 B/L 원문을 request 객체 안에만 두고 최상위 문자열에는 가린 값(bl_masked)만 둔다 (SDD 3.5, 4절 마스킹)."""
    return {"request": request, "user_id": user_id, "bl_masked": mask_bl(request.bl_no), "path": [],
            "trusted_vessel": agent.trusted_vessel.get(), "db_failed": agent._db_failed(), "resumable": resumable}


def _resume_command(request: AgentRequest) -> Command:
    """멈춘 그래프를 이어 가는 명령. 재개 값은 새 요청에서 꺼낸 칸들이고(nodes.RESUME_FIELDS), State에는 이번 요청의 DB 실패 기억을 덮어 쓴다
    (지난 요청의 db_failed가 이번 요청에 남지 않게)."""
    return Command(resume={f: getattr(request, f) for f in nodes.RESUME_FIELDS}, update={"db_failed": agent._db_failed()})


def _run_checkpointed(saver, thread: str, request: AgentRequest, user_id: str | None) -> dict:
    """체크포인터와 스레드가 있을 때의 실행. 이을 수 있으면 이어 가고, 아니면 스레드를 지우고 새로 돈다. 반환은 마지막 State에
    "paused"(멈춘 응답, 없으면 None)를 더한 것이다. 인프라 오류는 호출한 쪽이 잡는다."""
    graph = _compiled_with(saver)
    with _thread_lock(thread):
        snap = _read_snapshot(graph, saver, thread)
        if snap is None:
            # 읽지 못했다: 이 스레드는 믿지 않는다. 체크포인터 없이 새로 돈다 (EC-06, EC-07)
            return _run_plain(request, user_id)
        if _paused_for_this_request(snap, request):
            state_in = _resume_command(request)
        else:
            if snap.values:
                _delete_thread(saver, thread)  # 지난 실행이 따라오지 않게 먼저 지운다 (AC-08)
            state_in = _fresh_input(request, user_id, resumable=True)
        out, interrupts = _stream(graph, state_in, _config(thread), durable=True)
        if interrupts:
            return {**out, "paused": _paused_response(out, interrupts)}  # 스레드는 남긴다
        _delete_thread(saver, thread)  # 끝난 실행은 남기지 않는다 (Q7)
        return {**out, "paused": None}


def _paused_response(out: dict, interrupts: tuple) -> AgentResponse:
    """중단 값(응답 JSON)을 AgentResponse로 되살리고, 체크포인트에 두지 않은 처리 이력을 메모리의 State로 다시 채운다 (nodes.decorate 주석).
    이력은 State의 customs.display_rows(메모리에 살아 있다)에서 가져오고, 재개한 실행이라 비어 있으면 UNI-PASS를 구분 하나로 한 번 다시 부른다 (EC-15)."""
    response = AgentResponse.model_validate(interrupts[0].value["response"])
    customs = out.get("customs")
    rows = list(customs.display_rows) if customs is not None else []
    if not rows and out.get("resumed"):
        rows = nodes.refetch_display_rows(out.get("judged_request") or out["request"], out.get("bl_type_used"))
    return response.model_copy(update={"customs_detail": rows})


def _run_plain(request: AgentRequest, user_id: str | None) -> dict:
    """체크포인터 없이 한 번에 돈다 (꺼짐, 후속 질문, 키·DB 없음, 체크포인트 오류 뒤). H2~H6의 길이다."""
    return {**_run_to_end(_compiled(), _fresh_input(request, user_id, resumable=False)), "paused": None}


def run_graph(request: AgentRequest, user_id: str | None) -> AgentResponse:
    """_run과 같은 일을 그래프로 한다. 입력은 요청과 사용자 ID, 출력은 같은 AgentResponse.

    체크포인터가 켜져 있고 thread_id를 만들 수 있으면(후속 질문이 아니다) 되묻기에서 멈춘 그래프를 이어 가거나 멈춘다 (3절).
    체크포인터 오류가 나면 로그에 종류만 남기고 체크포인터 없이 처음부터 다시 돈다 (EC-06)."""
    saver = None if request.follow_up_question else checkpoint.get_checkpointer()
    thread = threads.thread_id(request.session_id, user_id, request.bl_no) if saver is not None else None
    if saver is None or thread is None:
        out = _run_plain(request, user_id)
    else:
        try:
            out = _run_checkpointed(saver, thread, request, user_id)
        except _INFRA_ERRORS as e:
            log_failure("graph_checkpoint", f"run_failed {type(e).__name__} rerun_without_checkpointer")
            _delete_thread(saver, thread)
            out = _run_plain(request, user_id)
    response = out["paused"] or out["response"]
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
