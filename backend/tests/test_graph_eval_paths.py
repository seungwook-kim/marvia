"""H12: 시나리오별로 판정 그래프가 지나간 노드 순서가 기대 경로와 같다 (docs/specs/v2-langgraph.md 10절 H12, SDD 5.12).

그래프를 stream_mode="updates"로 돌려 모은 노드 순서(스트림 순서)가 State path와 같고, 시나리오별 기대 경로와도 같은지 본다.
정상, 되묻기, 반출, unsupported, 위치 없음, 판정 포기(세 번 위반) 여섯 가지다. 또 평가 실행기가 결과 파일에 적는 engine 값이
AGENT_GRAPH 설정과 맞고(evals/run_eval.py engine_name), 비교 스크립트가 engine을 읽기만 하고 차이 수에 넣지 않는지 본다.
키도 DB도 필요 없다. 가짜 UNI-PASS·도구는 test_routers.py의 fake를 같이 쓴다.
"""
from __future__ import annotations

import json

import pytest

from backend.app.graph.build import build_graph
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest
from backend.app.schemas.customs import CustomsRecord, mask_bl
from backend.app.services import llm
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, NOW, POSITION, _candidate, _request, _signals, fake,
)

CUSTOMS_OK = ["guard_input", "load_memory", "load_saved", "fetch_customs"]


def _req(**over) -> AgentRequest:
    return AgentRequest(**_request(**over))


def _streamed(request: AgentRequest) -> tuple[list[str], list[str]]:
    """전체 그래프를 stream_mode="updates"로 돌려 (스트림에서 모은 노드 순서, 최종 State path)를 돌려준다."""
    graph = build_graph()
    order: list[str] = []
    final: dict = {}
    state = {"request": request, "user_id": None, "bl_masked": mask_bl(request.bl_no), "path": []}
    for mode, chunk in graph.stream(state, stream_mode=["updates", "values"]):
        if mode == "values":
            final = chunk
        else:
            order.extend(chunk)
    return order, final["path"]


def _assert_path(request: AgentRequest, expected: list[str]) -> None:
    order, path = _streamed(request)
    assert order == expected, f"스트림 순서가 기대와 다르다: {order}"
    assert path == expected, f"State path가 기대와 다르다: {path}"


def test_normal_path(fake, monkeypatch):
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    _assert_path(_req(), [*CUSTOMS_OK, "scope_gate", "identify", "collect", "compute_signals",
                          "table_verdict", "judge", "validate", "finalize", "decorate"])


def test_ask_back_path(fake):
    fake(candidates=[_candidate(440999001), _candidate(440999002, call_sign="TST02")])
    _assert_path(_req(), [*CUSTOMS_OK, "scope_gate", "identify", "ask_prepare", "decorate"])


def test_released_path(fake, monkeypatch):
    fake(customs=CustomsRecord(ship_name="TEST STAR", discharge_port="KRPUS", stage_name="반출신고",
                               shed_name="모의 CY", bl_masked="TEST****0001", fetched_at=NOW),
         candidates=[_candidate(440999001)], judge=JUDGED)
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("반출인데 LLM을 불렀다"))
    _assert_path(_req(sale_due_date="2026-09-01"), [*CUSTOMS_OK, "scope_gate", "decorate"])


def test_unsupported_path(fake, monkeypatch):
    fake(customs=CustomsRecord(ship_name="TEST STAR", discharge_port="ICN", bl_masked="TEST****0001", fetched_at=NOW),
         candidates=[_candidate(440999001)], judge=JUDGED)
    monkeypatch.setattr(agent_router, "get_vessel_candidates", lambda **k: pytest.fail("부산 외 화물인데 선박 검색을 불렀다"))
    _assert_path(_req(), [*CUSTOMS_OK, "scope_gate", "decorate"])


def test_no_position_path(fake, monkeypatch):
    fake(candidates=[_candidate(440999001)], judge=JUDGED)
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: pytest.fail("위치 없이 LLM을 불렀다"))
    _assert_path(_req(), [*CUSTOMS_OK, "scope_gate", "identify", "collect", "compute_signals", "gate_no_position", "decorate"])


def test_give_up_after_three_violations_path(fake, monkeypatch):
    """판정 포기: 세 번 위반하면 judge, validate를 세 번 돌고 표 등급 + 고정 문구로 끝난다(fallback_partial)."""
    fake(candidates=[_candidate(440999001)], position=POSITION)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    bad = {**JUDGED, "answer": "10월 3일에 들어옵니다."}  # 3은 근거에 없는 숫자 (V7)
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: llm.LLMAnswer(
        output=llm.JudgeOutput.model_validate(bad), llm=llm.LLMResult(content="", model="test/model"), prompt_version="test"))
    order, path = _streamed(_req())
    assert order == path
    assert path == [*CUSTOMS_OK, "scope_gate", "identify", "collect", "compute_signals", "table_verdict",
                    "judge", "validate", "judge", "validate", "judge", "validate", "fallback_partial", "decorate"]


# ── 평가 실행기의 engine 표시 ──

@pytest.mark.parametrize("value, expected", [("on", "v2-langgraph"), ("off", "v1-skill-mcp")])
def test_run_eval_engine_name_follows_agent_graph(monkeypatch, value, expected):
    from evals import run_eval

    monkeypatch.setenv("AGENT_GRAPH", value)
    assert run_eval.engine_name() == expected


def test_compare_runs_reports_engines_but_does_not_count_them_as_differences(tmp_path, capsys):
    from evals import compare_runs

    row = {"id": "N01", "got": {"status": "ok", "verdict": "fine", "vessel_match": "confirmed", "mmsi": 1,
                                "missing_inputs": [], "decided_by": "rule"}}
    paths = []
    for name, engine in (("old.json", None), ("v1.json", "v1-skill-mcp"), ("v2.json", "v2-langgraph")):
        data = {"rows": [row], **({"engine": engine} if engine else {})}
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        paths.append(str(path))
    assert compare_runs.main([paths[1], paths[2]]) == 0
    out = capsys.readouterr().out
    assert "v1-skill-mcp" in out and "v2-langgraph" in out and "차이 0건" in out
    assert compare_runs.main([paths[0], paths[2]]) == 0  # engine 칸이 없는 옛 파일도 비교된다
    assert "engine 없음" in capsys.readouterr().out
