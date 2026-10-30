"""H2: AGENT_GRAPH=off(v1)와 on(그래프)의 응답과 부수 효과가 같다 (docs/specs/v2-langgraph.md 10절 H2, AC-01).

test_routers.py의 시나리오(정상, 되묻기, unsupported, 반출, failed, MRN 대체, partial)와 AC-01이 더 든 시나리오
(위치 없음, 도착일 없음, 판정 포기)를 같은 가짜로 두 번 돌려 비교한다. 키도 DB도 필요 없다.

비교에서 빼는 칸은 trace_id 하나뿐이다 (스펙 H2 완료 기준 3). 이 테스트는 요청 밖에서 run_agent를 직접 부르므로 두 모드 모두 빈 값이라
실제로는 같지만, 스펙이 허용한 예외만 빼는 규칙을 지키려고 명시한다. 그 밖의 모든 칸이 같아야 한다.
부수 효과도 같이 본다: 조회 이력(_logged)과 화물 표·기억 쓰기(remember_later)에 넘어간 인자가 두 모드에서 같다.
"""
from __future__ import annotations

from datetime import date

import pytest

from backend.app.graph.build import build_graph
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest
from backend.app.schemas.customs import CustomsRecord, mask_bl
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, NOW, POSITION, _candidate, _fake_follow, _request, _signals, fake,
)

# 스펙이 두 모드에서 달라도 된다고 정한 칸 (H2 완료 기준 3)
ALLOWED_DIFF = {"trace_id"}

# 호출한 순간의 벽시계를 그대로 찍는 근거 카드의 fetched_at. 스펙의 허용 목록(trace_id)에는 없지만 두 번 호출하면 반드시 달라지는 값이다.
# rule 카드는 services/evidence.build가 datetime.now로, searoute 카드는 가짜로 바꾸지 않는 실제 거리 계산이 호출 때 찍는다.
# 카드의 field, value, source 등 나머지 칸은 그대로 비교한다
WALL_CLOCK_SOURCES = {"rule", "searoute"}


def _req(**over) -> AgentRequest:
    return AgentRequest(**_request(**over))


def _dump(response) -> dict:
    """비교용 사전. trace_id를 빼고, 벽시계 stamp(WALL_CLOCK_SOURCES 카드의 fetched_at)를 비운다."""
    data = response.model_dump(mode="json", exclude=ALLOWED_DIFF)
    data["evidence"] = [{**e, "fetched_at": None} if e["source"] in WALL_CLOCK_SOURCES else e for e in data["evidence"]]
    return data


def _record_side_effects(monkeypatch) -> list:
    """조회 이력(_logged)과 기억 쓰기(remember_later)가 받은 인자를 순서대로 모은다. 이력은 응답을 그대로 돌려준다."""
    calls: list = []

    def logged(request, response, user_id=None):
        calls.append(("logged", request.model_dump(mode="json"), _dump(response), user_id))
        return response

    def remember(user_id, request, response, released=False):
        calls.append(("remember", user_id, request.model_dump(mode="json"), _dump(response), released))

    monkeypatch.setattr(agent_router, "_logged", logged)
    monkeypatch.setattr(agent_router.memory_service, "remember_later", remember)
    return calls


def _run_both(monkeypatch, setup, request: AgentRequest, user_id: str | None = None):
    """같은 설정으로 off, on 순서대로 run_agent를 돌린다. (응답, 부수 효과) 두 쌍을 돌려준다.
    setup은 모드마다 다시 불러 호출 횟수를 세는 가짜가 새로 시작하게 한다."""
    results = []
    for mode in ("off", "on"):
        monkeypatch.setenv("AGENT_GRAPH", mode)
        setup()
        effects = _record_side_effects(monkeypatch)
        response = agent_router.run_agent(request.model_copy(deep=True), user_id=user_id)
        results.append((response, effects))
    return results


def _assert_same(monkeypatch, setup, request: AgentRequest, user_id: str | None = None):
    (off, off_effects), (on, on_effects) = _run_both(monkeypatch, setup, request, user_id)
    a, b = _dump(off), _dump(on)
    # 어긋난 칸 이름과 값을 실패 메시지에 싣는다. 응답이 커서 기본 diff로는 어디가 다른지 찾기 어렵다
    differing = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    assert not differing, f"off와 on의 응답이 다르다: {differing}"
    assert on_effects == off_effects
    return off


def _path(request: AgentRequest, user_id: str | None = None) -> list[str]:
    """그래프를 직접 한 번 돌려 State path(지나간 노드 이름)를 얻는다. 퍼사드와 같은 입력 모양이다."""
    out = build_graph().invoke({"request": request, "user_id": user_id, "bl_masked": mask_bl(request.bl_no), "path": []})
    return out["path"]


CUSTOMS_OK = ["guard_input", "load_memory", "load_saved", "fetch_customs"]


# ── 시나리오: 응답과 부수 효과가 같다 ──

def test_normal_judged(fake, monkeypatch):
    """정상: 후보 1척, 위치 있음, 판정 LLM 통과. 등급은 표(watch), 문장은 LLM 것."""
    def setup():
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "ok" and response.verdict == "watch" and response.customs_detail is not None


def test_normal_path(fake, monkeypatch):
    """정상 경로의 State path. 기억 읽기가 화물 줄 읽기보다 앞이다 (build.py 머리말)."""
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    assert _path(_req()) == [*CUSTOMS_OK, "scope_gate", "identify", "assess_rest", "decorate"]


def test_ask_back(fake, monkeypatch):
    """되묻기: 같은 이름 2척, LLM이 못 고름 -> insufficient + 후보."""
    def setup():
        fake(candidates=[_candidate(440999001), _candidate(440999002, call_sign="TST02")])
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "insufficient" and response.vessel_match == "uncertain"
    assert _path(_req()) == [*CUSTOMS_OK, "scope_gate", "identify", "decorate"]


def test_ask_back_resolved_mmsi(fake, monkeypatch):
    """되묻기 답으로 후보를 골라 같은 B/L을 다시 보낸 요청 (v1은 처음부터 다시 돈다)."""
    def setup():
        fake(candidates=[_candidate(440999001), _candidate(440999002, call_sign="TST02")],
             position={**POSITION, "mmsi": 440999002}, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
    response = _assert_same(monkeypatch, setup, _req(resolved_mmsi=440999002))
    assert response.vessel_match == "confirmed"


def test_ask_back_resolved_outside_candidates(fake, monkeypatch):
    """사용자가 고른 배가 후보에 없으면 바꿔 끼우지 않고 되묻는다 (rejected)."""
    def setup():
        fake(candidates=[_candidate(440999001), _candidate(440999002, call_sign="TST02")])
    response = _assert_same(monkeypatch, setup, _req(resolved_mmsi=440999777))
    assert response.status == "insufficient" and response.answer == agent_router.RESOLVED_NOT_CANDIDATE


def test_unsupported_port(fake, monkeypatch):
    """부산 밖: 배를 찾지 않고 unsupported. 화면 전용 칸은 붙지만 통관 소요 시간은 안 붙는다."""
    def setup():
        fake(customs=CustomsRecord(ship_name="TEST STAR", discharge_port="ICN", bl_masked="TEST****0001", fetched_at=NOW),
             candidates=[_candidate(440999001)], judge=JUDGED)
        monkeypatch.setattr(agent_router, "get_vessel_candidates",
                            lambda **k: pytest.fail("부산 외 화물인데 선박 검색을 불렀다"))
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "unsupported"
    assert _path(_req()) == [*CUSTOMS_OK, "scope_gate", "decorate"]


def test_released(fake, monkeypatch):
    """반출: 배도 LLM도 부르지 않고 규칙 fine. remember_later에 released=True가 넘어간다."""
    def setup():
        fake(customs=CustomsRecord(ship_name="TEST STAR", discharge_port="KRPUS", stage_name="반출신고",
                                   shed_name="모의 CY", bl_masked="TEST****0001", fetched_at=NOW),
             candidates=[_candidate(440999001)], judge=JUDGED)
        monkeypatch.setattr(agent_router, "get_vessel_candidates", lambda **k: pytest.fail("반출인데 배를 찾았다"))
        monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("반출인데 LLM을 불렀다"))
    response = _assert_same(monkeypatch, setup, _req(sale_due_date="2026-09-01"))
    assert response.verdict == "fine" and response.decided_by == "rule"
    (_, effects) = _run_both(monkeypatch, setup, _req(sale_due_date="2026-09-01"))[1]
    assert effects[-1][-1] is True  # remember_later(released=True)
    assert _path(_req()) == [*CUSTOMS_OK, "scope_gate", "decorate"]


def test_failed_no_cargo(fake, monkeypatch):
    """failed: 마스터·하우스 둘 다 없음. 화면 전용 칸을 붙이지 않고 decorate를 거치지 않는다."""
    def setup():
        fake(customs=None)
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "failed" and response.answer == agent_router.NO_CARGO
    assert _path(_req()) == CUSTOMS_OK


def test_failed_customs_outage_is_not_replaced_by_mrn(monkeypatch):
    """세관 장애(문구가 다름)는 MRN이 있어도 대체하지 않는다 (mrn-fallback.md EC-04)."""
    def setup():
        monkeypatch.setattr(agent_router, "_fetch_customs",
                            lambda req: (None, agent_router._response(status="failed", answer="세관 장애"), None))
        monkeypatch.setattr(agent_router, "_saved_row", lambda user_id, bl_no: None)
        monkeypatch.setattr(agent_router.memory_service, "load_context", lambda user_id, bl_no: None)
    response = _assert_same(monkeypatch, setup, _req(mrn="26TEST0001I"))
    assert response.status == "failed" and response.answer == "세관 장애"
    assert _path(_req(mrn="26TEST0001I")) == CUSTOMS_OK


@pytest.mark.parametrize("outcome", ["alternative", "not_found", "lookup_failed"])
def test_mrn_fallback(fake, monkeypatch, outcome):
    """MRN 대체: 세관에 없고 MRN이 있으면 해수부 입항 신고로 답한다. 대체 성공, 해수부에도 없음, 해수부 장애 셋 다 같다."""
    from backend.app.services import mrn_fallback as svc

    def assess(request):
        if outcome == "lookup_failed":
            raise svc.MrnLookupFailed("해수부 장애")
        return agent_router._response(status="partial", answer="대체") if outcome == "alternative" else None

    def setup():
        fake(customs=None)
        monkeypatch.setattr(svc, "assess", assess)
    response = _assert_same(monkeypatch, setup, _req(mrn="26TEST0001I"))
    assert response.status in ("partial", "failed")
    assert _path(_req(mrn="26TEST0001I")) == [*CUSTOMS_OK, "mrn_fallback"]


def test_partial_when_llm_fails_table_verdict(fake, monkeypatch):
    """partial: 판정 LLM이 실패하면 표 등급 + 고정 문구."""
    def setup():
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=None)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "partial" and response.answer.startswith("판정 표 기준으로")


def test_partial_when_three_violations(fake, monkeypatch):
    """partial: 세 번 모두 검증 위반이면 표 등급 + 고정 문구. 두 모드가 LLM에 같은 피드백으로 세 번 묻는다."""
    import os

    from backend.app.services import llm

    feedback_by_mode: dict[str, list] = {}
    bad = {**JUDGED, "answer": "10월 3일에 들어옵니다."}  # 3은 근거에 없는 숫자 (V7)

    def setup():
        calls = feedback_by_mode.setdefault(os.environ["AGENT_GRAPH"], [])
        fake(candidates=[_candidate(440999001)], position=POSITION)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
        monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: (calls.append(k.get("feedback")), llm.LLMAnswer(
            output=llm.JudgeOutput.model_validate(bad), llm=llm.LLMResult(content="", model="test/model"),
            prompt_version="test"))[1])
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "partial"
    assert len(feedback_by_mode["on"]) == 3 and feedback_by_mode["on"] == feedback_by_mode["off"]
    assert "JSON only" in feedback_by_mode["on"][2]  # 두 번째 재시도의 강화 문구


def test_partial_cached_position(fake, monkeypatch):
    """캐시 위치로 판정하면 판정은 두고 status만 partial."""
    def setup():
        fake(candidates=[_candidate(440999001)], position={**POSITION, "cached_at": NOW.isoformat()}, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
    assert _assert_same(monkeypatch, setup, _req()).status == "partial"


def test_partial_vessel_tool_failed(fake, monkeypatch):
    """partial: 선박 검색 도구가 실패하면 되묻지 않고 partial."""
    def setup():
        fake(candidates=[], judge=JUDGED)
        monkeypatch.setattr(agent_router, "get_vessel_candidates", lambda **k: {"error": {"code": "timeout"}})
    assert _assert_same(monkeypatch, setup, _req()).status == "partial"


def test_position_missing(fake, monkeypatch):
    """위치 없음: LLM 없이 보류하고 포워더 입항 예정일을 묻는다."""
    def setup():
        fake(candidates=[_candidate(440999001)], judge=JUDGED)
        monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("위치 없이 LLM을 불렀다"))
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "insufficient" and response.missing_inputs == ["forwarder_eta"]


def test_no_arrival_date(fake, monkeypatch):
    """도착일 없음: 판매일은 있는데 믿을 도착일이 없으면 LLM 없이 보류."""
    def setup():
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED,
             schedule={"planned_arrival": None, "actual_arrival": None, "fetched_at": NOW.isoformat()})
        monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("도착일 없이 LLM을 불렀다"))
        _signals(monkeypatch, {"sales_date_exceeded": "unknown"})
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "insufficient"


def test_gray_zone_llm_failure_has_no_verdict(fake, monkeypatch):
    """판정 포기: 회색 지대에서 LLM이 실패하면 등급 없이 끝난다."""
    def setup():
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=None)
    response = _assert_same(monkeypatch, setup, _req())
    assert response.status == "partial"


# ── 입력 가드, 저장 화물, 후속 질문 ──

def test_sale_due_out_of_range_ends_at_guard(fake, monkeypatch):
    """판매 예정일 범위 밖: 외부 조회 없이 첫 노드에서 끝나고, 조회 이력만 남고 기억 쓰기는 없다."""
    def setup():
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
        monkeypatch.setattr(agent_router, "UnipassClient", lambda *a, **k: pytest.fail("범위 밖인데 UNI-PASS를 불렀다"))
    request = _req(sale_due_date="1900-01-01")
    response = _assert_same(monkeypatch, setup, request)
    assert response.status == "insufficient" and response.missing_inputs == ["sale_due_date"]
    assert _path(request) == ["guard_input"]
    effects = _run_both(monkeypatch, setup, request)[1][1]
    assert [e[0] for e in effects] == ["logged"]


def test_saved_row_fills_type_due_and_previous_eta(fake, monkeypatch):
    """저장 화물: 구분과 판매 예정일을 채우고, 응답 끝에 저장 날짜 한 줄을 붙이며, 이력에는 채운 요청을 남긴다."""
    row = {"bl_type": "HOUSE", "sale_due_date": date(2026, 10, 5), "last_eta": date(2026, 10, 1)}

    def setup():
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
        monkeypatch.setattr(agent_router, "_saved_row", lambda user_id, bl_no: row)
    request = AgentRequest(bl_no="TESTMBL0000001", session_id="sess-1")  # 구분·판매 예정일 없음
    response = _assert_same(monkeypatch, setup, request, user_id="user-1")
    assert response.answer.endswith(agent_router.SAVED_SALE_DUE_NOTE.format(day=date(2026, 10, 5)))
    effects = _run_both(monkeypatch, setup, request, user_id="user-1")[1][1]
    assert effects[0][1]["sale_due_date"] == "2026-10-05"  # 이력에는 채운 요청
    assert effects[1][2]["bl_type"] == "HOUSE" and effects[1][2]["sale_due_date"] is None  # 화물 표에는 원래 판매일 입력


def test_memory_read_failure_skips_later_db_steps(fake, monkeypatch):
    """기억 읽기가 실패하면 화물 줄 읽기와 기억 쓰기를 건너뛴다 (10-08 QA B7). 기억 읽기가 먼저인 이유."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    tries: list = []

    def setup():
        tries.clear()
        fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
        _signals(monkeypatch, {"eta_conflict": "triggered"})
        monkeypatch.setattr(agent_router.memory_service, "load_context", lambda user_id, bl_no: None)
        monkeypatch.setattr(agent_router, "connect", lambda *a, **k: tries.append(1) or pytest.fail("DB에 다시 붙었다"))
    (off, off_effects), (on, on_effects) = _run_both(monkeypatch, setup, _req(), user_id="user-1")
    assert _dump(on) == _dump(off) and tries == []
    assert [e[0] for e in on_effects] == [e[0] for e in off_effects] == ["logged"]  # remember_later는 건너뜀


def test_follow_up_uses_saved_result(fake, monkeypatch):
    """후속 질문: 저장된 지난 결과 안에서 답한다. 화물 줄 읽기를 하지 않고 이력에는 원래 요청을 남긴다."""
    saved = agent_router._response(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5, answer="지난 답")

    def setup():
        fake(judge=JUDGED)
        monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: saved)
        monkeypatch.setattr(agent_router, "_saved_row", lambda *a: pytest.fail("후속 질문이 화물 줄을 읽었다"))
        _fake_follow(monkeypatch, {"in_scope": True, "answer": "여유는 5일입니다."})
    request = _req(follow_up_question="여유 며칠이야?")
    response = _assert_same(monkeypatch, setup, request)
    assert response.answer == "여유는 5일입니다." and response.verdict == "watch"


def test_follow_up_path(fake, monkeypatch):
    """후속 질문 경로: 세관 조회로 가지 않고 follow_up 노드에서 끝난다 (H5까지 굵은 노드)."""
    saved = agent_router._response(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5, answer="지난 답")
    fake(judge=JUDGED)
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: saved)
    _fake_follow(monkeypatch, {"in_scope": True, "answer": "여유는 5일입니다."})
    assert _path(_req(follow_up_question="여유 며칠이야?")) == ["guard_input", "load_memory", "load_saved", "follow_up"]


# ── 플래그 ──

def test_flag_off_never_touches_the_graph(fake, monkeypatch):
    """AGENT_GRAPH가 꺼져 있으면(기본, 빈 값 포함) 퍼사드를 부르지 않는다."""
    from backend.app.graph import facade

    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    monkeypatch.setattr(facade, "run_graph", lambda *a, **k: pytest.fail("꺼져 있는데 그래프를 돌렸다"))
    for value in (None, "", "off", "no"):
        if value is None:
            monkeypatch.delenv("AGENT_GRAPH", raising=False)
        else:
            monkeypatch.setenv("AGENT_GRAPH", value)
        assert agent_router.graph_enabled() is False
        assert agent_router.run_agent(_req(), user_id=None).status in ("ok", "partial")


@pytest.mark.parametrize("value", ["on", "ON", " true ", "1"])
def test_flag_on_calls_the_facade(fake, monkeypatch, value):
    """AGENT_GRAPH가 켜지면 run_agent가 퍼사드를 부른다. 대소문자와 공백은 무시한다 (WATCH_ENABLED와 같은 읽는 법)."""
    from backend.app.graph import facade

    seen = []
    monkeypatch.setenv("AGENT_GRAPH", value)
    monkeypatch.setattr(facade, "run_graph", lambda request, user_id: seen.append((request.bl_no, user_id)) or "응답")
    assert agent_router.run_agent(_req(), user_id="user-1") == "응답"
    assert seen == [("TESTMBL0000001", "user-1")]
