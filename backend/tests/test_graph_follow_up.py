"""H5: 후속 질문 분기를 노드(load_previous, guard_follow_up, answer_follow_up, validate_follow_up)로 나눈 그래프가
v1 _follow_up과 같게 움직인다 (docs/specs/v2-langgraph.md 10절 H5, SDD 4.4).

run_agent를 AGENT_GRAPH=off/on으로 두 번 돌려 응답과 후속 LLM 호출(횟수, 피드백 글, 도구를 줬는지)이 같은지 비교하고,
그래프를 직접 돌려 노드가 지나간 길(State path)을 본다. 키도 DB도 필요 없다. 가짜 UNI-PASS·도구는 test_routers.py의 fake를 같이 쓴다.
"""
from __future__ import annotations

import pytest

from backend.app.graph import routes
from backend.app.graph.build import build_graph
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest
from backend.app.schemas.customs import mask_bl
from backend.app.services import llm
from backend.app.services.memory import MemoryContext
from backend.app.services.rules import rule
from backend.tests.test_guard import ATTACKS
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, POSITION, _candidate, _request, _signals, fake,
)

BAD = "10월 3일에 들어옵니다."           # 3은 근거에 없는 숫자라 V7 위반
GOOD = "여유는 5일입니다."               # 지난 결과의 days_margin 5와 같은 숫자라 통과
QUESTION = "여유가 며칠이야?"
WATCH = {"eta_conflict": "triggered"}

ANSWER_LOOP = ["answer_follow_up", "validate_follow_up"]


def _saved(**over):
    """DB에 저장돼 있던 지난 결과(가공값). 여유 5일, 등급 watch."""
    base = dict(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5, answer="지난 답")
    return agent_router._response(**{**base, **over})


def _req(**over) -> AgentRequest:
    return AgentRequest(**_request(follow_up_question=QUESTION, **over))


def _script(monkeypatch, steps: list, tool_calls: list | None = None, call_tools: list[str] | None = None) -> list:
    """후속 LLM 가짜. 부를 때마다 steps를 차례로 쓰고(끝나면 마지막 것을 되풀이) 받은 값을 기록한다.
    step이 문장이면 그 답, 예외 객체면 던진다. call_tools가 있으면 부를 때마다 run_tool로 그 도구들을 부른다."""
    calls: list[dict] = []

    def follow(question, response, feedback=None, tools=None, run_tool=None, memory=None, **kwargs):
        calls.append({"question": question, "feedback": feedback, "tools": bool(tools), "memory": memory})
        for name in call_tools or []:
            run_tool(name, {})
        step = steps[min(len(calls), len(steps)) - 1]
        if isinstance(step, Exception):
            raise step
        return llm.LLMAnswer(output=llm.FollowUpOutput(in_scope=True, answer=step),
                             llm=llm.LLMResult(content="", model="test/model"), prompt_version="test",
                             tool_calls=tool_calls or [])
    monkeypatch.setattr(agent_router.llm, "answer_follow_up", follow)
    return calls


def _use_saved(monkeypatch, previous) -> None:
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: previous)
    monkeypatch.setattr(agent_router, "_saved_row", lambda *a: pytest.fail("후속 질문이 화물 줄을 읽었다"))


def _run_both(fake, monkeypatch, setup):
    """setup(): 가짜를 새로 꽂고 후속 LLM 호출 기록을 돌려준다. v1(off)과 그래프(on)로 차례로 돌려 [(응답, 호출 기록)] 두 쌍을 돌려준다."""
    out = []
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        fake(judge=JUDGED)
        calls = setup()
        out.append((agent_router.run_agent(_req(), user_id=None), calls))
    return out


def _same(off, on):
    (r_off, c_off), (r_on, c_on) = off, on
    a, b = r_off.model_dump(mode="json", exclude={"trace_id"}), r_on.model_dump(mode="json", exclude={"trace_id"})
    for d in (a, b):
        d["evidence"] = [{**e, "fetched_at": None} if e["source"] in {"rule", "searoute"} else e for e in d["evidence"]]
    assert a == b
    assert c_off == c_on


def _path(request: AgentRequest) -> list[str]:
    out = build_graph().invoke({"request": request, "user_id": None, "bl_masked": mask_bl(request.bl_no), "path": []})
    return out["path"]


# ── 주입 모양: LLM 0회 ──

def test_injection_shape_makes_no_llm_call_and_keeps_previous(fake, monkeypatch):
    """주입 모양 질문은 후속 LLM을 부르지 않고 FOLLOW_UP_BLOCKED로 답한다. 지난 판정·근거는 그대로다. 길은 guard_follow_up에서 끝난다."""
    def setup():
        _use_saved(monkeypatch, _saved(verdict="act"))
        monkeypatch.setattr(agent_router.llm, "answer_follow_up", lambda *a, **k: pytest.fail("주입 모양 질문이 LLM에 갔다"))
        return []
    monkeypatch.setenv("AGENT_GRAPH", "on")
    fake(judge=JUDGED)
    setup()
    request = AgentRequest(**_request(follow_up_question=ATTACKS[0][:300]))
    response = agent_router.run_agent(request, user_id=None)
    assert response.answer == agent_router.FOLLOW_UP_BLOCKED
    assert (response.status, response.verdict) == ("ok", "act")
    assert _path(request) == ["guard_input", "load_memory", "load_saved", "load_previous", "guard_follow_up"]


# ── 정상 / 세 번 위반 ──

def test_normal_answer_path_and_parity(fake, monkeypatch):
    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [GOOD])
    off, on = _run_both(fake, monkeypatch, setup)
    assert on[0].answer == GOOD and on[0].verdict == "watch"
    assert len(on[1]) == 1
    _same(off, on)
    _use_saved(monkeypatch, _saved())
    _script(monkeypatch, [GOOD])
    assert _path(_req()) == ["guard_input", "load_memory", "load_saved", "load_previous", "guard_follow_up", *ANSWER_LOOP]


def test_three_violations_give_follow_up_failed_and_keep_previous(fake, monkeypatch):
    """세 번 모두 위반이면 틀린 문장을 내지 않고 FOLLOW_UP_FAILED. 지난 판정은 유지. 호출 3회, 피드백은 v1과 같다."""
    def setup():
        _use_saved(monkeypatch, _saved(verdict="act"))
        return _script(monkeypatch, [BAD])
    off, on = _run_both(fake, monkeypatch, setup)
    response, calls = on
    assert len(calls) == agent_router.JUDGE_ATTEMPTS == 3
    assert response.answer == agent_router.FOLLOW_UP_FAILED and response.verdict == "act"
    assert calls[0]["feedback"] is None and "V7" in calls[1]["feedback"] and "V7" in calls[2]["feedback"]
    assert "Return JSON only" not in calls[2]["feedback"]  # 후속 질문은 강화 문장을 붙이지 않는다 (판정과 다르다)
    _same(off, on)
    _use_saved(monkeypatch, _saved(verdict="act"))
    _script(monkeypatch, [BAD])
    assert _path(_req())[4:] == ["guard_follow_up", *ANSWER_LOOP * 3]


def test_violation_then_fixed_answer_is_used(fake, monkeypatch):
    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [BAD, GOOD])
    off, on = _run_both(fake, monkeypatch, setup)
    assert on[0].answer == GOOD and len(on[1]) == 2 and "V7" in on[1][1]["feedback"]
    _same(off, on)


def test_attempt_cap_is_the_judge_cap_read_at_call_time(fake, monkeypatch):
    """상한은 판정과 같은 judge_max_attempts다. 별칭을 패치하면 두 길 모두 따른다."""
    monkeypatch.setattr(agent_router, "JUDGE_ATTEMPTS", 2)

    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [BAD])
    off, on = _run_both(fake, monkeypatch, setup)
    assert len(off[1]) == len(on[1]) == 2
    _same(off, on)


# ── 호출 실패, 형식 오류, 도구 거절 ──

def test_llm_error_is_not_retried(fake, monkeypatch):
    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [llm.LLMError("테스트: 호출 실패")])
    off, on = _run_both(fake, monkeypatch, setup)
    assert len(on[1]) == 1 and on[0].answer == agent_router.FOLLOW_UP_FAILED and on[0].verdict == "watch"
    _same(off, on)
    _use_saved(monkeypatch, _saved())
    _script(monkeypatch, [llm.LLMError("테스트: 호출 실패")])
    assert _path(_req())[4:] == ["guard_follow_up", "answer_follow_up"]  # validate를 거치지 않는다


def test_format_error_retries_with_detail(fake, monkeypatch):
    err = llm.LLMFormatError("테스트: 형식", detail="Missing field: answer")

    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [err, GOOD])
    off, on = _run_both(fake, monkeypatch, setup)
    assert [c["feedback"] for c in on[1]] == [None, "Missing field: answer"] and on[0].answer == GOOD
    _same(off, on)


def test_format_error_three_times_gives_failed(fake, monkeypatch):
    err = llm.LLMFormatError("테스트: 형식", detail="Missing field: answer")

    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [err])
    off, on = _run_both(fake, monkeypatch, setup)
    assert len(on[1]) == 3 and on[0].answer == agent_router.FOLLOW_UP_FAILED
    _same(off, on)


def test_tools_rejected_retries_without_tools(fake, monkeypatch):
    """도구를 붙인 요청이 bad_request로 거절되면 도구 없이 다시 묻는다. 후속 답은 막히지 않는다."""
    tool = {"type": "function", "function": {"name": "get_live_position", "parameters": {}}}
    rejected = llm.LLMError("HTTP 404 no endpoint", code=404, kind="bad_request")

    def setup():
        llm._tools_rejected_at.clear()
        monkeypatch.setattr(agent_router, "read_only_tools", lambda: [tool])
        _use_saved(monkeypatch, _saved())

        calls: list[dict] = []

        def follow(question, response, feedback=None, tools=None, run_tool=None, **kwargs):
            calls.append({"feedback": feedback, "tools": bool(tools)})
            if tools:
                raise rejected
            return llm.LLMAnswer(output=llm.FollowUpOutput(in_scope=True, answer=GOOD),
                                 llm=llm.LLMResult(content="", model="test/model"), prompt_version="test")
        monkeypatch.setattr(agent_router.llm, "answer_follow_up", follow)
        return calls
    off, on = _run_both(fake, monkeypatch, setup)
    assert [c["tools"] for c in on[1]] == [True, False] and on[0].answer == GOOD
    _same(off, on)
    llm._tools_rejected_at.clear()


def test_candidates_tool_cap_survives_a_retry(fake, monkeypatch):
    """get_vessel_candidates는 한 질문에 한 번이다. 위반으로 다시 물어도 두 번째 시도에서 도구를 또 부르지 못한다 (State follow_candidates_used)."""
    tools = [{"type": "function", "function": {"name": agent_router.CANDIDATES_TOOL, "parameters": {}}}]
    ran: list[int] = []

    def setup():
        monkeypatch.setattr(agent_router, "read_only_tools", lambda: tools)
        monkeypatch.setattr(agent_router, "_follow_up_tool", lambda name, arguments: ran.append(1) or {"candidates": []})
        _use_saved(monkeypatch, _saved(vessel=None))
        return _script(monkeypatch, [BAD, GOOD], call_tools=[agent_router.CANDIDATES_TOOL])
    off, on = _run_both(fake, monkeypatch, setup)
    assert len(on[1]) == 2
    assert len(ran) == 2  # off 한 번 + on 한 번. 각 모드에서 실제 도구 실행은 한 번뿐이다
    _same(off, on)


# ── 컨테이너 번호표 ──

def test_container_token_is_not_validated_but_restored_on_screen(fake, monkeypatch):
    """번호표 [CNTR1]은 검증에서 빠지고(V7·V9에 걸리지 않고), 화면 답에는 원래 번호로 되돌아온다. LLM에는 번호표만 간다."""
    from backend.app.schemas.agent import CustomsInfo
    customs = CustomsInfo(stage_code="", stage_name="반입신고", plain_explanation="반입신고 단계입니다.", next_stage=None,
                          needs_broker=False, container_no="TSTU1234560", goods_name="TEST GOODS")

    def setup():
        _use_saved(monkeypatch, _saved(customs=customs))
        return _script(monkeypatch, ["컨테이너 번호는 [CNTR1]입니다."])
    off, on = _run_both(fake, monkeypatch, setup)
    assert on[0].answer == "컨테이너 번호는 TSTU1234560입니다." and len(on[1]) == 1
    _same(off, on)


def test_tool_evidence_card_is_kept_on_the_answer(fake, monkeypatch):
    """도구에서 받은 숫자를 쓴 답은 근거 카드 덕분에 통과하고, 응답에 그 카드가 남는다."""
    calls = [{"name": "get_live_position", "arguments": {"mmsi": 440999001},
              "result": {"sog": 12.3, "fetched_at": "2026-10-02T01:00:00Z"}}]

    def setup():
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, ["지금 속력은 12.3노트입니다."], tool_calls=calls)
    off, on = _run_both(fake, monkeypatch, setup)
    assert on[0].answer == "지금 속력은 12.3노트입니다."
    assert any(e.field == "get_live_position.sog" for e in on[0].evidence)
    _same(off, on)


# ── 기억 블록 ──

def test_memory_blocks_go_to_the_follow_up_llm(fake, monkeypatch):
    """후속 LLM에는 기억 블록(blocks)이 간다 (판정 LLM용 품명 없는 블록이 아니다). v1과 같다."""
    def setup():
        monkeypatch.setattr(agent_router.memory_service, "load_context",
                            lambda user_id, bl_no: MemoryContext(blocks="BLOCKS", judge_blocks="JUDGE BLOCKS"))
        _use_saved(monkeypatch, _saved())
        return _script(monkeypatch, [GOOD])
    off, on = _run_both(fake, monkeypatch, setup)
    assert on[1][0]["memory"] == "BLOCKS"
    _same(off, on)


# ── 연쇄: 지난 결과가 없으면 판정을 다시 돌린 뒤 합류 ──

def _chain_setup(fake, monkeypatch, steps, states=WATCH, judge=JUDGED):
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=judge)
    _signals(monkeypatch, states)
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: None)
    return _script(monkeypatch, steps)


def test_no_previous_result_assesses_again_then_answers(fake, monkeypatch):
    """지난 결과가 없으면 세관 조회부터 판정을 다시 돌리고, 그 결과(decorate까지)를 지난 결과로 삼아 후속 답을 만든다."""
    out = []
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        calls = _chain_setup(fake, monkeypatch, [GOOD])
        out.append((agent_router.run_agent(_req(), user_id=None), calls))
    off, on = out
    assert on[0].answer == GOOD and on[0].verdict == "watch" and on[0].customs_detail is not None
    assert len(on[1]) == 1
    _same(off, on)
    _chain_setup(fake, monkeypatch, [GOOD])
    path = _path(_req())
    assert path == ["guard_input", "load_memory", "load_saved", "load_previous", "fetch_customs", "scope_gate", "identify",
                    "collect", "compute_signals", "table_verdict", "judge", "validate", "finalize", "decorate",
                    "guard_follow_up", *ANSWER_LOOP]


def test_chain_after_customs_failure_still_answers_on_the_failed_result(fake, monkeypatch):
    """조회 실패 응답도 v1은 그 위에 후속 답을 만든다(`_previous_response(...) or _assess(...)`). 그래프도 fetch_customs 끝에서 합류한다."""
    out = []
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        calls = _chain_setup(fake, monkeypatch, ["조회 결과가 없습니다."])
        monkeypatch.setattr(agent_router, "_fetch_customs",
                            lambda req: (None, agent_router._response(status="failed", answer=agent_router.NO_CARGO), None))
        out.append((agent_router.run_agent(_req(), user_id=None), calls))
    off, on = out
    assert on[0].status == "failed" and len(on[1]) == 1
    _same(off, on)
    _chain_setup(fake, monkeypatch, ["조회 결과가 없습니다."])
    monkeypatch.setattr(agent_router, "_fetch_customs",
                        lambda req: (None, agent_router._response(status="failed", answer=agent_router.NO_CARGO), None))
    assert _path(_req())[3:] == ["load_previous", "fetch_customs", "guard_follow_up", *ANSWER_LOOP]


def test_chain_injection_blocks_after_the_verdict_branch(fake, monkeypatch):
    """연쇄에서도 주입 모양이면 판정 응답을 지난 결과로 두고 LLM 없이 막는다."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    _chain_setup(fake, monkeypatch, [GOOD])
    monkeypatch.setattr(agent_router.llm, "answer_follow_up", lambda *a, **k: pytest.fail("주입 모양 질문이 LLM에 갔다"))
    request = AgentRequest(**_request(follow_up_question=ATTACKS[0][:300]))
    response = agent_router.run_agent(request, user_id=None)
    assert response.answer == agent_router.FOLLOW_UP_BLOCKED and response.verdict == "watch"


# ── 엣지 함수 단독 ──

def test_edge_functions():
    from langgraph.graph import END
    assert routes.after_saved({"request": _req()}) == "load_previous"
    assert routes.after_previous({"previous": _saved()}) == "guard_follow_up"
    assert routes.after_previous({"previous": None}) == "fetch_customs"
    assert routes.after_guard_follow_up({"response": _saved()}) == END
    assert routes.after_guard_follow_up({"response": None}) == "answer_follow_up"
    assert routes.after_answer_follow_up({"follow_route": "give_up"}) == END
    assert routes.after_answer_follow_up({"follow_route": "retry"}) == "validate_follow_up"
    assert routes.after_answer_follow_up({"follow_route": "pass"}) == "validate_follow_up"
    assert routes.after_validate_follow_up({"follow_route": "retry"}) == "answer_follow_up"
    assert routes.after_validate_follow_up({"follow_route": "pass"}) == END
    assert routes.after_validate_follow_up({"follow_route": "give_up"}) == END
    # 질문이 없는 요청은 끝 자리에서 그대로 끝난다
    plain = AgentRequest(**_request())
    assert routes.after_decorate({"request": plain}) == END and routes.after_mrn_fallback({"request": plain}) == END


def test_follow_feedback_is_a_list_in_state(fake, monkeypatch):
    _use_saved(monkeypatch, _saved())
    _script(monkeypatch, [BAD])
    request = _req()
    seen = {}
    for chunk in build_graph().stream({"request": request, "user_id": None, "bl_masked": mask_bl(request.bl_no), "path": []},
                                      stream_mode="updates"):
        for name, update in chunk.items():
            seen[name] = update
    feedback = seen["validate_follow_up"]["follow_feedback"]
    assert isinstance(feedback, list) and len(feedback) == 1 and feedback[0].startswith("V7")
    assert seen["answer_follow_up"]["follow_answer"] == [BAD]


# ── 그래프 상한 (graph_recursion_limit) 다시 센 값 ──

def test_longest_path_after_h5_is_below_the_recursion_limit(fake, monkeypatch):
    """H5 뒤 가장 긴 경로: 지난 결과 없음 + 판정 세 번 위반(폴백) + 후속 답 세 번 위반이다. 센 값은 25이고 rules.md 한도(40)보다 작다."""
    _chain_setup(fake, monkeypatch, [BAD], judge={**JUDGED, "answer": BAD})
    final = build_graph().invoke({"request": _req(), "user_id": None, "bl_masked": "x", "path": []},
                                 {"recursion_limit": rule("graph_recursion_limit")})
    assert len(final["path"]) == 25
    assert final["response"].answer == agent_router.FOLLOW_UP_FAILED
    assert rule("graph_recursion_limit") > 25


def test_recursion_limit_hit_in_follow_up_keeps_previous(fake, monkeypatch):
    """후속 답 루프 중간에 상한에 걸려도 failed가 아니라 지난 결과 + FOLLOW_UP_FAILED다 (EC-12)."""
    from backend.app.graph import facade
    monkeypatch.setenv("AGENT_GRAPH", "on")
    fake(judge=JUDGED)
    _use_saved(monkeypatch, _saved(verdict="act"))
    _script(monkeypatch, [BAD])
    monkeypatch.setattr(facade, "rule", lambda key: 6 if key == "graph_recursion_limit" else rule(key))
    response = agent_router.run_agent(_req(), user_id=None)
    assert response.answer == agent_router.FOLLOW_UP_FAILED and (response.status, response.verdict) == ("ok", "act")
