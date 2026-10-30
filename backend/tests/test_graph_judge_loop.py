"""H4: judge <-> validate 재시도 루프를 State로 옮긴 그래프가 v1 _judge_stage와 같게 움직인다 (docs/specs/v2-langgraph.md 10절 H4, SDD 5.7).

그래프를 직접 돌려 노드가 지나간 길(State path)과 노드별 State 쓰기를 보고, run_agent를 AGENT_GRAPH=off/on으로 두 번 돌려
응답과 판정 LLM 호출(횟수, 피드백 글)이 같은지 비교한다. 키도 DB도 필요 없다. 가짜 UNI-PASS·도구는 test_routers.py의 fake를 같이 쓴다.
시계는 test_judge_budget.py처럼 가짜로 바꿔 끼워 실제로 기다리지 않는다.
"""
from __future__ import annotations

import pytest

from backend.app.graph import routes
from backend.app.graph.build import build_graph
from backend.app.graph import facade
from backend.app.routers import agent as agent_router
from backend.app.schemas.customs import mask_bl
from backend.app.services import llm
from backend.app.services.rules import rule
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, POSITION, _candidate, _request, _signals, fake,
)
from backend.app.schemas.agent import AgentRequest

BAD = {**JUDGED, "answer": "10월 3일에 들어옵니다."}  # 3은 근거에 없는 숫자라 V7 위반
LOW = {**JUDGED, "verdict": "fine", "reasons": []}      # 표(watch)보다 낮은 등급이라 V8 위반
GRAY_RAISE = {**JUDGED, "reasons": [{"rule": "anchor_wait", "value": "지속 시간 미확인", "triggered": False}]}

WATCH = {"eta_conflict": "triggered"}        # 표: watch, 회색 지대 아님
GRAY = {"anchor_wait": "unknown"}            # 표: fine, 회색 지대

# judge 아래의 노드 이름. 한 시도 = judge 다음 validate
ATTEMPT = ["judge", "validate"]


class _Clock:
    """time.monotonic 대신. 판정 LLM 가짜가 부를 때마다 정해 둔 초만큼 앞으로 간다."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def _script(monkeypatch, steps: list, seconds: float = 0.0) -> list:
    """판정 LLM 가짜. 부를 때마다 steps를 차례로 쓰고(끝나면 마지막 것을 되풀이) 받은 feedback을 모아 돌려준다.
    step이 dict면 그 판정, 예외 객체면 던진다. seconds가 있으면 호출마다 가짜 시계가 그만큼 간다."""
    clock, calls = _Clock(), []
    monkeypatch.setattr(agent_router, "time", clock)

    def judge(signal_set, customs, margin, feedback=None, **kwargs):
        calls.append(feedback)
        clock.now += seconds
        step = steps[min(len(calls), len(steps)) - 1]
        if isinstance(step, Exception):
            raise step
        return llm.LLMAnswer(output=llm.JudgeOutput.model_validate(step),
                             llm=llm.LLMResult(content="", model="test/model"), prompt_version="test")
    monkeypatch.setattr(agent_router.llm, "judge", judge)
    return calls


def _req(**over) -> AgentRequest:
    return AgentRequest(**_request(**over))


def _stream(request: AgentRequest) -> tuple[list[str], dict[str, dict], dict]:
    """판정만의 그래프(세관 조회부터)를 돌려 (지나간 노드 이름 순서, 노드별 마지막 State 쓰기, 최종 State)를 돌려준다."""
    graph = build_graph(entry="fetch_customs")
    order: list[str] = []
    writes: dict[str, dict] = {}
    final: dict = {}
    for mode, chunk in graph.stream({"request": request, "bl_masked": mask_bl(request.bl_no), "path": []},
                                    stream_mode=["updates", "values"]):
        if mode == "values":
            final = chunk
            continue
        for name, update in chunk.items():
            order.append(name)
            writes[name] = update or {}
    return order, writes, final


def _setup(fake, monkeypatch, states: dict[str, str], steps: list, seconds: float = 0.0) -> list:
    """후보 1척, 위치 있음, 신호는 states, 판정 LLM은 steps. 부른 feedback 목록을 돌려준다."""
    fake(candidates=[_candidate(440999001)], position=POSITION)
    _signals(monkeypatch, states)
    return _script(monkeypatch, steps, seconds)


def _run_both(fake, monkeypatch, states, steps, seconds=0.0):
    """같은 설정으로 v1(off)과 그래프(on)를 차례로 돌려 [(응답, 판정 호출 feedback 목록)] 두 쌍을 돌려준다."""
    out = []
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        calls = _setup(fake, monkeypatch, states, steps, seconds)
        response = agent_router.run_agent(_req(), user_id=None)
        out.append((response, calls))
    return out


def _same(off, on):
    """응답(trace_id와 벽시계로 찍는 카드 시각만 빼고)과 판정 호출 feedback이 두 모드에서 같다."""
    (r_off, c_off), (r_on, c_on) = off, on
    drop = {"trace_id"}
    a, b = r_off.model_dump(mode="json", exclude=drop), r_on.model_dump(mode="json", exclude=drop)
    for d in (a, b):
        d["evidence"] = [{**e, "fetched_at": None} if e["source"] in {"rule", "searoute"} else e for e in d["evidence"]]
    assert a == b
    assert c_off == c_on


# ── 상한과 값의 위치 ──

def test_attempt_limit_and_budget_come_from_rules():
    """상한(3)과 시간 예산(60)은 rules.md에서 읽고, agent의 이름은 그 값을 가리키는 별칭이다. 값은 H4 전과 같다."""
    assert (rule("judge_max_attempts"), rule("judge_budget_sec")) == (3, 60)
    assert agent_router.JUDGE_ATTEMPTS == rule("judge_max_attempts")
    assert agent_router.JUDGE_BUDGET_SEC == float(rule("judge_budget_sec"))


def test_aliases_are_read_at_call_time(fake, monkeypatch):
    """별칭을 패치하면 두 길(v1, 그래프) 모두 바뀐 값으로 돈다. 상한 2면 두 번 묻고 접는다."""
    monkeypatch.setattr(agent_router, "JUDGE_ATTEMPTS", 2)
    off, on = _run_both(fake, monkeypatch, WATCH, [BAD])
    assert len(off[1]) == len(on[1]) == 2
    _same(off, on)


# ── 길 (path) ──

def test_normal_path_is_one_attempt(fake, monkeypatch):
    """정상: judge -> validate -> finalize -> decorate. 폴백 노드는 지나지 않는다."""
    calls = _setup(fake, monkeypatch, WATCH, [JUDGED])
    order, _, final = _stream(_req())
    assert order[-5:] == ["table_verdict", "judge", "validate", "finalize", "decorate"]
    assert len(calls) == 1
    assert final["response"].status == "ok" and final["response"].verdict == "watch"
    assert final["judge_attempt"] == 1 and final["judge_route"] == "pass"


def test_three_violations_path(fake, monkeypatch):
    """세 번 위반: (judge, validate)가 세 번 돌고 fallback_partial로 끝난다. finalize는 지나지 않는다."""
    _setup(fake, monkeypatch, WATCH, [BAD])
    order, _, final = _stream(_req())
    assert order[order.index("table_verdict"):] == ["table_verdict", *ATTEMPT * 3, "fallback_partial", "decorate"]
    assert final["judge_attempt"] == 3


def test_llm_error_goes_straight_to_fallback(fake, monkeypatch):
    """호출 실패(LLMError)는 validate를 거치지 않고 다시 묻지도 않는다."""
    calls = _setup(fake, monkeypatch, WATCH, [llm.LLMError("테스트: 호출 실패")])
    order, writes, final = _stream(_req())
    assert order[order.index("table_verdict"):] == ["table_verdict", "judge", "fallback_partial", "decorate"]
    assert len(calls) == 1
    assert writes["judge"]["judge_route"] == "give_up"
    assert final["response"].status == "partial"


# ── 세 번 위반 -> partial ──

def test_three_violations_give_table_verdict_with_fixed_text(fake, monkeypatch):
    """세 번 모두 위반이면 표 등급 + TABLE_FALLBACK, decided_by=rule, partial. v1과 호출·응답이 같다."""
    off, on = _run_both(fake, monkeypatch, WATCH, [BAD])
    response, calls = on
    assert len(calls) == agent_router.JUDGE_ATTEMPTS == 3
    assert (response.status, response.verdict, response.decided_by) == ("partial", "watch", "rule")
    assert response.answer == agent_router.TABLE_FALLBACK.format(label=agent_router.VERDICT_LABEL["watch"])
    _same(off, on)


def test_gray_zone_three_violations_give_no_verdict(fake, monkeypatch):
    """회색 지대에서 세 번 위반이면 등급 없이 LLM_FAILED. 표 등급(fine)은 최소치라 확정처럼 내지 않는다."""
    off, on = _run_both(fake, monkeypatch, GRAY, [{**BAD, "reasons": GRAY_RAISE["reasons"]}])
    response, calls = on
    assert len(calls) == 3
    assert (response.status, response.verdict, response.answer) == ("partial", None, agent_router.LLM_FAILED)
    _same(off, on)


# ── 호출 실패와 형식 오류 ──

def test_llm_error_is_not_retried(fake, monkeypatch):
    """LLMError는 한 번 부르고 partial. 재시도 없음."""
    off, on = _run_both(fake, monkeypatch, WATCH, [llm.LLMError("테스트: 호출 실패")])
    assert len(on[1]) == 1
    assert (on[0].status, on[0].verdict) == ("partial", "watch")
    _same(off, on)


def test_llm_format_error_retries_like_a_violation(fake, monkeypatch):
    """LLMFormatError는 위반처럼 본다: 무엇이 틀렸는지를 붙여 다시 묻고, 고친 답이 오면 그것을 쓴다."""
    err = llm.LLMFormatError("테스트: 형식", detail="Missing field: answer")
    off, on = _run_both(fake, monkeypatch, WATCH, [err, JUDGED])
    response, calls = on
    assert calls == [None, "Missing field: answer"]
    assert (response.status, response.answer) == ("ok", JUDGED["answer"])
    _same(off, on)


def test_llm_format_error_three_times_gives_table_fallback(fake, monkeypatch):
    err = llm.LLMFormatError("테스트: 형식", detail="Missing field: answer")
    off, on = _run_both(fake, monkeypatch, WATCH, [err])
    assert len(on[1]) == 3 and on[0].status == "partial" and on[0].verdict == "watch"
    _same(off, on)


# ── 피드백 ──

def test_feedback_carries_violation_and_second_retry_is_strengthened(fake, monkeypatch):
    """첫 재시도는 위반 내용만, 둘째(마지막) 재시도는 거기에 "Return JSON only"를 덧붙인다 (SDD 5.7). v1과 글자까지 같다."""
    off, on = _run_both(fake, monkeypatch, WATCH, [BAD])
    calls = on[1]
    assert calls[0] is None
    assert "V7" in calls[1] and "Return JSON only" not in calls[1]
    assert "V7" in calls[2] and calls[2].endswith("Return JSON only and follow every rule above.")
    _same(off, on)


def test_feedback_is_a_list_of_one_sentence_in_state(fake, monkeypatch):
    """State의 judge_feedback은 list[str] | None이다 (문장 하나가 원소 하나, 평문 칸에 문장이 가지 않게, 2절)."""
    _setup(fake, monkeypatch, WATCH, [BAD])
    _, writes, _ = _stream(_req())  # writes는 노드별 마지막 쓰기: 세 번째(마지막) 시도라 강화 문장은 없다
    feedback = writes["validate"]["judge_feedback"]
    assert isinstance(feedback, list) and len(feedback) == 1 and feedback[0].startswith("V7")
    assert "judge_feedback" not in writes["judge"]  # 정상 호출에서 judge는 피드백을 쓰지 않는다


# ── V8, 회색 지대 ──

def test_v8_lower_grade_than_table_is_retried(fake, monkeypatch):
    """표(watch)보다 낮은 등급(fine)은 V8 위반이라 다시 묻는다. 고친 답이 오면 그것을 쓴다."""
    off, on = _run_both(fake, monkeypatch, WATCH, [LOW, JUDGED])
    response, calls = on
    assert len(calls) == 2 and calls[1].startswith("V8: verdict must be watch (the judgment table decided watch).")
    assert (response.status, response.verdict, response.decided_by) == ("ok", "watch", "rule")
    _same(off, on)


def test_gray_zone_raise_is_allowed_and_marked_llm(fake, monkeypatch):
    """회색 지대에서만 표(fine)보다 높은 등급(watch)을 받아 준다. decided_by=llm. 기억 블록은 회색 지대라 주지 않는다."""
    off, on = _run_both(fake, monkeypatch, GRAY, [GRAY_RAISE])
    response, calls = on
    assert len(calls) == 1
    assert (response.status, response.verdict, response.decided_by) == ("ok", "watch", "llm")
    _same(off, on)


def test_gray_zone_cannot_lower(fake, monkeypatch):
    """회색 지대라도 표(watch)보다 낮출 수 없다. 세 번 모두 fine이면 등급 없이 partial."""
    off, on = _run_both(fake, monkeypatch, {**GRAY, **WATCH}, [LOW])
    assert (on[0].status, on[0].verdict) == ("partial", None) and len(on[1]) == 3
    _same(off, on)


def test_raise_is_not_allowed_outside_gray_zone(fake, monkeypatch):
    """회색 지대가 아니면 표보다 높이는 것도 V8 위반이다 (고정 등급). 세 번 모두 act면 표 등급(watch) 폴백."""
    off, on = _run_both(fake, monkeypatch, WATCH, [{**JUDGED, "verdict": "act"}])
    assert (on[0].status, on[0].verdict, on[0].decided_by) == ("partial", "watch", "rule") and len(on[1]) == 3
    _same(off, on)


# ── 시간 예산 ──

def test_budget_stops_before_the_second_attempt(fake, monkeypatch, caplog):
    """한 번에 40초면 한 번 더 80초라 예산(60초)을 넘는다. 두 번째 시도를 시작하지 않고 표 등급으로 끝낸다. 로그 문장도 v1과 같다."""
    logs = {}
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        calls = _setup(fake, monkeypatch, WATCH, [BAD], seconds=40)
        caplog.clear()
        with caplog.at_level("WARNING", logger="marvia.failure"):
            response = agent_router.run_agent(_req(), user_id=None)
        logs[mode] = (response, calls, [(r.stage, r.error) for r in caplog.records if r.name == "marvia.failure"])
    for response, calls, failures in logs.values():
        assert len(calls) == 1
        assert (response.status, response.verdict) == ("partial", "watch")
        assert ("judge", "time budget exceeded after 1 attempts") in failures
    assert logs["off"][2] == logs["on"][2]


def test_budget_stops_before_the_third_call(fake, monkeypatch):
    """25초씩: 두 번째는 50초 예상이라 묻고 세 번째는 75초 예상이라 묻지 않는다."""
    off, on = _run_both(fake, monkeypatch, WATCH, [BAD], seconds=25)
    assert len(on[1]) == 2
    _same(off, on)


def test_budget_alias_is_read_at_call_time(fake, monkeypatch):
    """JUDGE_BUDGET_SEC를 패치하면 그래프도 따른다 (기존 패치가 계속 먹는다)."""
    monkeypatch.setattr(agent_router, "JUDGE_BUDGET_SEC", 5.0)
    off, on = _run_both(fake, monkeypatch, WATCH, [BAD], seconds=3)
    assert len(on[1]) == len(off[1]) == 1
    _same(off, on)


# ── 엣지 함수 단독 ──

def _loop_state(**over) -> dict:
    return {"judge_route": "retry", "judge_attempt": 1, "judge_spent_sec": 1.0, "judge_last_sec": 1.0, **over}


def test_edge_pass_retry_give_up(monkeypatch):
    monkeypatch.setattr(agent_router, "time", _Clock())
    assert routes.after_validate(_loop_state(judge_route="pass")) == "pass"
    assert routes.after_validate(_loop_state()) == "retry"
    assert routes.after_validate(_loop_state(judge_attempt=agent_router.JUDGE_ATTEMPTS)) == "give_up"
    # 쓴 시간 + 방금 한 번이 예산을 넘기면 접는다. 딱 예산이면 접지 않는다 (v1은 '넘기면')
    assert routes.after_validate(_loop_state(judge_spent_sec=30.0, judge_last_sec=30.0)) == "retry"
    assert routes.after_validate(_loop_state(judge_spent_sec=30.0, judge_last_sec=30.5)) == "give_up"


def test_edge_after_judge():
    assert routes.after_judge({"judge_route": "give_up"}) == "give_up"
    assert routes.after_judge({"judge_route": "pass"}) == "validate"
    assert routes.after_judge({"judge_route": "retry"}) == "validate"  # 형식 오류도 validate로 가서 위반처럼 다뤄진다


# ── 코드 값은 LLM이 바꾸지 못한다 ──

def test_llm_output_cannot_overwrite_code_values(fake, monkeypatch):
    """LLM 출력에 등급(표와 다른 값), days_margin, 신호, trusted_eta를 실어도 응답과 State의 코드 값은 그대로다 (SDD 5.8, AC-03).
    days_margin, signal_set, trusted_eta를 쓰는 노드는 compute_signals뿐이고, 판정 루프의 노드는 이 칸을 쓰지 않는다."""
    forged = {**JUDGED, "days_margin": 99, "trusted_eta": "2030-01-01T00:00:00+00:00", "signals": [], "signal_set": {"signals": []}}
    _setup(fake, monkeypatch, WATCH, [forged])
    order, writes, final = _stream(_req())
    code_fields = {"days_margin", "signal_set", "trusted_eta", "table_verdict", "gray"}
    loop_nodes = {"judge", "validate", "finalize", "fallback_partial"}
    assert [n for n in order if n in loop_nodes and set(writes[n]) & code_fields] == []
    response = final["response"]
    assert response.days_margin == final["days_margin"] != 99
    assert response.verdict == final["table_verdict"] == "watch"
    assert final["trusted_eta"].isoformat() != "2030-01-01T00:00:00+00:00"


# ── 그래프 상한 (graph_recursion_limit) ──

def test_recursion_limit_in_rules_is_above_the_longest_counted_path(fake, monkeypatch):
    """rules.md graph_recursion_limit은 지금 가장 긴 경로(사용자 조회 전체, 판정 세 번 위반)의 노드 수보다 크다.
    그 경로의 노드 수는 path 길이로 센다. 값을 직접 센 결과는 rules.md 주석에 있다(17)."""
    _setup(fake, monkeypatch, WATCH, [BAD])
    graph = build_graph()
    final = graph.invoke({"request": _req(), "user_id": None, "bl_masked": "x", "path": []},
                         {"recursion_limit": rule("graph_recursion_limit")})
    longest = len(final["path"])
    assert longest == 17
    assert rule("graph_recursion_limit") > longest


def test_recursion_limit_hit_ends_as_partial_with_table_verdict(fake, monkeypatch, caplog):
    """EC-12: 상한에 걸려도 failed가 아니라 partial로 끝난다. 표 등급이 정해진 뒤(회색 지대 아님)면 판정 폴백과 같은 응답이다.
    판정 루프 중간(table_verdict 뒤)에서 걸리게 상한을 낮춘다."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    _setup(fake, monkeypatch, WATCH, [JUDGED])
    full = len(build_graph().invoke({"request": _req(), "user_id": None, "bl_masked": "x", "path": []})["path"])
    cut = full - 3  # table_verdict(9번째) 뒤, 루프 도중
    monkeypatch.setattr(facade, "rule", lambda key: cut if key == "graph_recursion_limit" else rule(key))
    with caplog.at_level("WARNING", logger="marvia.failure"):
        response = agent_router.run_agent(_req(), user_id=None)
    assert (response.status, response.verdict, response.decided_by) == ("partial", "watch", "rule")
    assert response.answer == agent_router.TABLE_FALLBACK.format(label=agent_router.VERDICT_LABEL["watch"])
    assert response.customs_detail is not None  # 화면 전용 칸도 붙는다 (decorate와 같은 것)
    assert ("graph", "recursion_limit") in [(r.stage, r.error) for r in caplog.records if r.name == "marvia.failure"]


def test_recursion_limit_one_ends_as_partial_without_verdict(fake, monkeypatch):
    """상한을 1로 낮추면 표 등급도 정해지기 전에 걸린다. 그래도 failed가 아니라 LLM_FAILED 문구의 partial이다."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    _setup(fake, monkeypatch, WATCH, [JUDGED])
    monkeypatch.setattr(facade, "rule", lambda key: 1 if key == "graph_recursion_limit" else rule(key))
    response = agent_router.run_agent(_req(), user_id=None)
    assert (response.status, response.verdict, response.answer) == ("partial", None, agent_router.LLM_FAILED)


def test_recursion_limit_in_gray_zone_gives_no_verdict(fake, monkeypatch):
    """회색 지대면 상한에 걸려도 등급 없이 LLM_FAILED (표 등급은 최소치라 확정처럼 내지 않는다)."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    _setup(fake, monkeypatch, GRAY, [JUDGED])
    full = len(build_graph().invoke({"request": _req(), "user_id": None, "bl_masked": "x", "path": []})["path"])
    monkeypatch.setattr(facade, "rule", lambda key: full - 3 if key == "graph_recursion_limit" else rule(key))
    response = agent_router.run_agent(_req(), user_id=None)
    assert (response.status, response.verdict, response.answer) == ("partial", None, agent_router.LLM_FAILED)


# ── 판정만의 그래프는 닿는 노드만 올린다 ──

def test_assess_graph_has_only_reachable_nodes():
    """entry="fetch_customs"에는 판매 예정일 검사, 기억·화물 줄 읽기, 지난 결과 읽기가 올라가지 않는다.
    후속 답 노드는 판정 끝 자리에서 이어질 수 있어 올라간다(질문이 없으면 지나지 않는다)."""
    names = set(build_graph(entry="fetch_customs").get_graph().nodes) - {"__start__", "__end__"}
    assert not names & {"guard_input", "load_memory", "load_saved", "load_previous"}
    assert {"fetch_customs", "judge", "validate", "finalize", "fallback_partial", "decorate"} <= names
    full = set(build_graph().get_graph().nodes) - {"__start__", "__end__"}
    assert full == names | {"guard_input", "load_memory", "load_saved", "load_previous"}
