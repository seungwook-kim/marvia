"""10-08 QA 판정 갈래(B1~B11) 회귀 테스트. 외부 API·DB·LLM 없이 돈다. B/L·MMSI는 전부 모의값이다.

  B1  거절된 resolved_mmsi를 화물 표에 저장하지 않는다, 지켜보기가 믿는 배를 지난 결과로 확인한다, MMSI 범위
  B2  판매 예정일 범위 밖이면 되묻고 저장하지 않는다 (rules.md sale_due_past_days·sale_due_future_days)
  B3  판정 LLM 기억 블록에 품명이 없다
  B4  요청에 판매 예정일이 없으면 저장된 날짜로 판정하고 답에 한 줄
  B5  공백뿐인 후속 질문은 첫 조회, 빈 세션 ID는 422
  B6  재생 모드가 마스터·하우스 구분을 본다
  B7  한 요청에서 DB가 실패하면 남은 DB 단계를 건너뛴다
  B8  후속 질문 도구 거절을 모델별로 기억한다
  B9  LLM reasons의 같은 신호는 첫 번째만
  B11 B/L 하이픈, MRN 대체 문장
"""
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.clients import replay
from backend.app.main import app
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest, VesselCandidate, VesselInfo
from backend.app.services import cargo, llm, memory
from backend.app.services import mrn_fallback
from backend.app.services.customs import UnipassClient
from backend.app.services.rules import rule
from backend.tests.test_cargo import FakeConn
from backend.tests.test_routers import JUDGED, NOW, POSITION, _candidate, _login, _request, _signals, fake  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"


@pytest.fixture
def bl_key(monkeypatch):
    monkeypatch.setenv("BL_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _vessel(mmsi: int) -> VesselInfo:
    return VesselInfo(mmsi=mmsi, imo=None, name="TEST STAR", call_sign=None, flag=None, ship_type=None, voyage_no=None)


def _cand(mmsi: int, reason: str = "선명 유사도 1.0") -> VesselCandidate:
    return VesselCandidate(mmsi=mmsi, imo=None, name="TEST STAR", flag=None, dest=None, last_time=NOW, ship_type=None,
                           match_reasons=[reason])


def _jobs(monkeypatch, context=None):
    """로그인 사용자의 기억 읽기(load_context)를 정해 두고, 응답 뒤 화물 표 쓰기에 넘어간 (요청, 응답)을 모은다."""
    jobs = []
    monkeypatch.setattr(agent_router.memory_service, "load_context", lambda user_id, bl_no=None: context)
    monkeypatch.setattr(agent_router.memory_service, "remember_later",
                        lambda user_id, request, response, released: jobs.append((request, response)))
    return jobs


# ── B1 ──

@pytest.mark.parametrize("bad", [10 ** 20, -1, 0, True, 1_000_000_000])
def test_resolved_mmsi_must_be_a_real_mmsi(bad):
    """MMSI 범위 밖 값과 true/false는 422. 10^20이 화물 표 BIGINT에 들어가다 뒤 작업이 죽었다."""
    with pytest.raises(ValidationError):
        AgentRequest(bl_no="TESTBL01", session_id="s", resolved_mmsi=bad)
    assert AgentRequest(bl_no="TESTBL01", session_id="s", resolved_mmsi=999_999_999).resolved_mmsi == 999_999_999


def test_llm_picked_vessel_is_saved_not_first_candidate():
    """항차로 LLM이 고른 배(두 번째 후보)를 적는다. 예전에는 후보 목록 맨 앞을 적었다."""
    req = AgentRequest(bl_no="TESTBL01", session_id="s", voyage_no="0042E")
    resp = agent_router._response(vessel_match="confirmed", vessel_candidates=[_cand(440999001), _cand(440999002)],
                                  vessel=_vessel(440999002))
    assert cargo._vessel(req, resp) == (440999002, "user")


def test_rejected_pick_is_not_saved_and_clears_old_copy(bl_key):
    """후보가 아니라 거절된 번호는 vessel_mmsi로 적지 않는다. 예전 코드가 저장해 둔 같은 번호는 CASE로 지운다."""
    conn = FakeConn()
    req = AgentRequest(bl_no="TESTBL0001", bl_type="HOUSE", session_id="s", resolved_mmsi=440999205)
    resp = agent_router._response(status="insufficient", vessel_match="uncertain", missing_inputs=["voyage_no"],
                                  vessel_candidates=[_cand(440999208)])
    assert cargo.upsert_assessment(conn, "u1", req, resp, released=False)
    sql, params = conn.calls[0]
    names = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    assert params[names.index("vessel_mmsi")] is None and params[names.index("vessel_source")] is None
    assert params[-3:-1] == [440999205, 440999205]  # vessel_mmsi·vessel_source CASE의 거절 번호
    assert "user_cargo.vessel_mmsi = %s THEN NULL" in sql


def test_confirmed_pick_is_saved_without_clearing(bl_key):
    conn = FakeConn()
    req = AgentRequest(bl_no="TESTBL0001", bl_type="HOUSE", session_id="s", resolved_mmsi=440999208)
    resp = agent_router._response(status="ok", verdict="fine", vessel_match="confirmed",
                                  vessel_candidates=[_cand(440999208)], vessel=_vessel(440999208))
    cargo.upsert_assessment(conn, "u1", req, resp, released=False)
    sql, params = conn.calls[0]
    names = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    assert (params[names.index("vessel_mmsi")], params[names.index("vessel_source")]) == (440999208, "user")
    assert params[-3:-1] == [None, None]


def test_router_rejected_pick_reaches_cargo_write_unconfirmed(fake, monkeypatch):
    """라우터가 후보 밖 번호를 되묻기로 바꾼 응답을 화물 표 쓰기에 넘기면, 화물 표는 그 번호를 배로 적지 않는다."""
    fake(candidates=[_candidate(440999001), _candidate(440999002)])
    monkeypatch.setattr(agent_router, "_saved_cargo_responses", lambda request: [])
    jobs = _jobs(monkeypatch)
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request(resolved_mmsi=440999999), headers=_login(monkeypatch)).json()
    assert body["status"] == "insufficient" and body["answer"] == agent_router.RESOLVED_NOT_CANDIDATE
    request, response = jobs[0]
    assert cargo._vessel(request, response) == (None, None)


def _watch_post(monkeypatch, saved: list[dict]):
    monkeypatch.setattr(agent_router, "_saved_cargo_responses", lambda request: saved)
    monkeypatch.setattr(agent_router, "get_vessel_candidates", lambda **k: pytest.fail("지켜보기 경로는 후보를 다시 찾지 않는다"))
    token = agent_router.trusted_vessel.set(440999009)
    try:
        with TestClient(app) as client:
            return client.post("/api/agent", json=_request(resolved_mmsi=440999009)).json()
    finally:
        agent_router.trusted_vessel.reset(token)


def test_watch_does_not_trust_vessel_other_results_contradict(fake, monkeypatch):
    """지난 결과는 다른 배(440999001)를 확정했는데 화물 표에 440999009가 있으면 거절된 번호가 저장된 줄이다. 판정하지 않는다."""
    fake()
    body = _watch_post(monkeypatch, [{"vessel": {"mmsi": 440999001}, "vessel_candidates": []}])
    assert body["status"] == "insufficient" and body["verdict"] is None and body["vessel_match"] == "uncertain"


def test_watch_trusts_vessel_seen_as_candidate_or_confirmed(fake, monkeypatch):
    fake()
    assert _watch_post(monkeypatch, [{"vessel": None, "vessel_candidates": [{"mmsi": 440999009}]}])["vessel_match"] == "confirmed"
    assert _watch_post(monkeypatch, [{"vessel": {"mmsi": 440999009}}])["vessel_match"] == "confirmed"
    # 배 정보가 있는 이력이 없으면(90일 정리 등) 판단할 근거가 없어 지금처럼 믿는다
    assert _watch_post(monkeypatch, [{"status": "failed"}])["vessel_match"] == "confirmed"


# ── B2 ──

def test_sale_due_range_comes_from_rules():
    today = date(2026, 10, 7)
    past, future = rule("sale_due_past_days"), rule("sale_due_future_days")
    assert agent_router.sale_due_out_of_range(date(1900, 1, 1), today)
    assert agent_router.sale_due_out_of_range(date.fromordinal(today.toordinal() - past - 1), today)
    assert not agent_router.sale_due_out_of_range(date.fromordinal(today.toordinal() - past), today)
    assert not agent_router.sale_due_out_of_range(date.fromordinal(today.toordinal() + future), today)
    assert agent_router.sale_due_out_of_range(date.fromordinal(today.toordinal() + future + 1), today)
    assert not agent_router.sale_due_out_of_range(None, today)


@pytest.mark.parametrize("day", ["1900-01-01", "2099-12-31"])
def test_out_of_range_sale_date_asks_back_without_lookup_or_save(fake, monkeypatch, day):
    """범위 밖 판매 예정일: UNI-PASS도 LLM도 부르지 않고 되묻는다. 화물 표 쓰기도 하지 않아 저장된 정상 값을 덮지 않는다."""
    fake()

    class NoUnipass:
        def __init__(self, *a, **k):
            pytest.fail("범위 밖 날짜로 UNI-PASS를 부르면 안 된다")

    monkeypatch.setattr(agent_router, "UnipassClient", NoUnipass)
    jobs = _jobs(monkeypatch)
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request(sale_due_date=day), headers=_login(monkeypatch)).json()
    assert (body["status"], body["verdict"], body["missing_inputs"]) == ("insufficient", None, ["sale_due_date"])
    assert day in body["answer"] and jobs == []


# ── B3 ──

def test_judge_block_has_no_goods_name():
    row = {"bl_masked": "TE****01", "bl_type": "HOUSE", "goods_name": "RED PEPPER POWDER"}
    assert "RED PEPPER" in memory.compose_blocks([row], [], {})
    assert "RED PEPPER" not in memory.compose_blocks([row], [], {}, goods=False)
    assert "품명" not in cargo.cargo_line(row, goods=False)


def test_load_context_builds_both_blocks(monkeypatch):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    row = {"cargo_key": "k", "bl_masked": "TE****01", "bl_type": "HOUSE", "goods_name": "RED PEPPER POWDER"}
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(memory, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(memory.cargo, "active_rows", lambda conn, user_id: [row])
    monkeypatch.setattr(memory.cargo, "all_rows", lambda conn, user_id: [row])
    monkeypatch.setattr(memory, "load", lambda conn, user_id: ([], 0, None, None))
    context = memory.load_context("u1")
    assert "RED PEPPER" in context.blocks and "RED PEPPER" not in context.for_judge()


def test_judge_gets_block_without_goods_follow_up_gets_full(fake, monkeypatch):
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    _jobs(monkeypatch, memory.MemoryContext(blocks="<user_cargo>품명 있음</user_cargo>",
                                            judge_blocks="<user_cargo>품명 없음</user_cargo>"))
    seen = {}
    real = agent_router.llm.judge
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: (seen.update(judge=k["memory"]), real(*a, **k))[1])

    def follow(question, response, memory=None, **kwargs):
        seen["follow"] = memory
        return llm.LLMAnswer(output=llm.FollowUpOutput(in_scope=True, answer="확인했습니다."),
                             llm=llm.LLMResult(content="", model="test/model"), prompt_version="test")

    monkeypatch.setattr(agent_router.llm, "answer_follow_up", follow)
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: None)
    headers = _login(monkeypatch)
    with TestClient(app) as client:
        client.post("/api/agent", json=_request(), headers=headers)
        assert seen["judge"] == "<user_cargo>품명 없음</user_cargo>"
        seen.clear()
        # 지난 결과가 없어 후속 질문이 다시 판정하는 길도 판정 LLM에는 품명 없는 블록을 준다
        client.post("/api/agent", json=_request(follow_up_question="여유 며칠이야?"), headers=headers)
    assert seen == {"judge": "<user_cargo>품명 없음</user_cargo>", "follow": "<user_cargo>품명 있음</user_cargo>"}


# ── B4 ──

def test_saved_sale_due_is_used_and_noted(fake, monkeypatch):
    """판매 예정일을 안 보낸 재조회는 화물 표에 저장된 날짜로 판정하고, 답 끝에 코드가 한 줄 적는다.
    화물 표 쓰기에는 원래 요청(날짜 없음)을 넘겨 그 칸의 확인 대기가 지워지지 않게 한다."""
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    jobs = _jobs(monkeypatch, memory.MemoryContext(blocks=None))
    monkeypatch.setattr(agent_router, "_saved_row", lambda user_id, bl_no: {"sale_due_date": date(2026, 10, 5)})
    seen = {}
    real = agent_router.llm.judge
    monkeypatch.setattr(agent_router.llm, "judge", lambda *a, **k: (seen.update(k), real(*a, **k))[1])
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request(sale_due_date=None), headers=_login(monkeypatch)).json()
    assert seen["sale_due_date"] == date(2026, 10, 5) and body["days_margin"] is not None
    assert body["answer"] == f"{JUDGED['answer']} 저장된 판매 예정일 10-05 기준입니다."
    assert jobs[0][0].sale_due_date is None


def test_request_date_wins_over_saved(fake, monkeypatch):
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    _jobs(monkeypatch, memory.MemoryContext(blocks=None))
    monkeypatch.setattr(agent_router, "_saved_row", lambda user_id, bl_no: {"sale_due_date": date(2026, 10, 9)})
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request(), headers=_login(monkeypatch)).json()
    assert body["answer"] == JUDGED["answer"]  # 요청 날짜(10-05)로 판정했으니 저장 날짜 안내가 없다


def test_saved_sale_due_out_of_range_is_ignored():
    today = date(2026, 10, 7)
    assert agent_router.saved_sale_due({"sale_due_date": date(1900, 1, 1)}, today) is None
    assert agent_router.saved_sale_due({"sale_due_date": date(2026, 10, 20)}, today) == date(2026, 10, 20)
    assert agent_router.saved_sale_due(None, today) is None


# ── B5 ──

def test_blank_follow_up_is_first_lookup_and_empty_session_rejected():
    assert AgentRequest(bl_no="TESTBL01", session_id="s", follow_up_question=" \n ").follow_up_question is None
    with pytest.raises(ValidationError):
        AgentRequest(bl_no="TESTBL01", session_id="")


# ── B6 ──

@pytest.fixture
def replay_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLAY_MODE", "true")
    monkeypatch.delenv("UNIPASS_KEY", raising=False)
    monkeypatch.setattr(replay, "REPLAY_DIR", tmp_path)
    (tmp_path / "unipass").mkdir()
    return tmp_path


def _house_file(root: Path, house: str, master: str) -> None:
    text = (FIXTURES / "unipass" / "progress_ok.xml").read_text(encoding="utf-8")
    text = text.replace("TESTMBL0000001", master).replace("TESTHBL0000001", house)
    (root / "unipass" / f"{house}.xml").write_text(text, encoding="utf-8")


def test_replay_checks_bl_kind(replay_dir):
    """하우스 번호를 마스터로 물으면 없다고 답하고, 마스터 번호로는 그 화물을 찾는다 (실제 UNI-PASS와 같게)."""
    _house_file(replay_dir, "TESTHB2610001", "TESTMB2610001")
    client = UnipassClient()
    assert client.fetch("TESTHB2610001", "HOUSE") is not None
    assert client.fetch("TESTHB2610001", "MASTER") is None
    assert client.fetch("TESTMB2610001", "MASTER") is not None
    assert client.fetch("TESTMB2610001", "HOUSE") is None


def test_replay_file_named_only_by_number_still_works(replay_dir):
    """파일 안 번호 칸이 이 번호와 상관없는 재생(평가셋 TESTBL01 방식)은 구분과 상관없이 예전처럼 돌려준다."""
    shutil.copy(FIXTURES / "unipass" / "progress_ok.xml", replay_dir / "unipass" / "TESTBL01.xml")
    assert UnipassClient().fetch("TESTBL01", "MASTER") is not None
    assert UnipassClient().fetch("TESTBL01", "HOUSE") is not None


def test_wrong_kind_retries_and_reports_found_kind(replay_dir):
    """마스터라고 잘못 말한 하우스 번호: 마스터 조회는 없음 → 하우스로 다시 조회해 찾고, 찾은 구분(HOUSE)을 돌려준다."""
    _house_file(replay_dir, "TESTHB2610001", "TESTMB2610001")
    record, failure, kind = agent_router._fetch_customs(AgentRequest(bl_no="TESTHB2610001", bl_type="MASTER", session_id="s"))
    assert record is not None and failure is None and kind == "HOUSE"


# ── B7 ──

def test_failed_memory_read_skips_other_db_steps(fake, monkeypatch, bl_key):
    """기억 읽기가 실패한 요청은 화물 표 읽기·위치 이력·이력 쓰기·뒤 작업 모두 DB에 다시 붙지 않는다."""
    real_logged = agent_router._logged
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    monkeypatch.setattr(agent_router, "_logged", real_logged)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    jobs = _jobs(monkeypatch, None)  # 사용자·DB 주소가 있는데 None: 읽기 실패
    tries = []
    monkeypatch.setattr(agent_router, "connect", lambda *a, **k: tries.append(1) or pytest.fail("DB에 다시 붙었다"))
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request(), headers=_login(monkeypatch)).json()
    assert body["verdict"] == "watch" and tries == [] and jobs == []


def test_first_db_failure_stops_later_steps(fake, monkeypatch, bl_key):
    real_logged = agent_router._logged
    fake(candidates=[_candidate(440999001)], position=POSITION, judge=JUDGED)
    monkeypatch.setattr(agent_router, "_logged", real_logged)
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    jobs = _jobs(monkeypatch, memory.MemoryContext(blocks=None))
    tries = []

    def refuse(*a, **k):
        tries.append(1)
        raise OSError("db down")

    monkeypatch.setattr(agent_router, "connect", refuse)
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request(), headers=_login(monkeypatch)).json()
    assert body["verdict"] == "watch" and len(tries) == 1 and jobs == []  # 화물 표 읽기에서 한 번 실패하고 끝


def test_db_state_is_only_per_request():
    """run_agent 밖(지켜보기 재판정이 _assess를 직접 부르는 길)은 실패를 기억하지 않는다. 한 화물의 실패가 다른 화물로 번지지 않게."""
    agent_router._mark_db_failed()
    assert agent_router._db_failed() is False


# ── B8 ──

def test_tool_rejection_is_remembered_per_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test/model-a")
    llm.remember_tools_rejected(now=100.0)
    assert llm.tools_rejected(now=100.0 + llm.TOOLS_REJECT_TTL_SEC - 1)
    assert not llm.tools_rejected("test/model-b", now=101.0)
    assert not llm.tools_rejected(now=100.0 + llm.TOOLS_REJECT_TTL_SEC + 1)  # 지나면 다시 도구를 붙여 본다
    assert not llm.tools_rejected(now=100.0)  # 지난 기억은 지워졌다


def test_second_follow_up_skips_tools_after_rejection(monkeypatch):
    saved = agent_router._response(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5,
                                   answer="한 번 확인이 필요합니다.")
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: saved)
    monkeypatch.setattr(agent_router, "read_only_tools",
                        lambda: [{"type": "function", "function": {"name": "get_live_position", "parameters": {}}}])
    offered = []

    def follow(question, response, feedback=None, tools=None, run_tool=None, **kwargs):
        offered.append(bool(tools))
        if tools:
            raise llm.LLMError("HTTP 404 no endpoint", code=404, kind="bad_request")
        return llm.LLMAnswer(output=llm.FollowUpOutput(in_scope=True, answer="여유는 5일입니다."),
                             llm=llm.LLMResult(content="", model="test/model"), prompt_version="test")

    monkeypatch.setattr(agent_router.llm, "answer_follow_up", follow)
    body = {"bl_no": "TESTBL0000000001", "bl_type": "HOUSE", "session_id": "s-b8", "follow_up_question": "여유가 며칠이야?"}
    with TestClient(app) as client:
        client.post("/api/agent", json=body)
        client.post("/api/agent", json=body)
    assert offered == [True, False, False]  # 두 번째 질문은 처음부터 도구 없이 한 번만


# ── B9 ──

def test_duplicate_reasons_keep_first(fake, monkeypatch):
    reasons = [{"rule": "eta_conflict", "value": "첫 문장", "triggered": True},
               {"rule": "eta_conflict", "value": "같은 신호 다른 문장", "triggered": True},
               {"rule": "anchor_wait", "value": "꺼짐", "triggered": False}]
    fake(candidates=[_candidate(440999001)], position=POSITION, judge={**JUDGED, "reasons": reasons})
    _signals(monkeypatch, {"eta_conflict": "triggered"})
    with TestClient(app) as client:
        body = client.post("/api/agent", json=_request()).json()
    assert [(r["rule"], r["value"]) for r in body["reasons"]] == [("eta_conflict", "첫 문장"), ("anchor_wait", "꺼짐")]


# ── B11 ──

def test_hyphenated_bl_is_accepted():
    assert AgentRequest(bl_no="TEST-BL-01", session_id="s").bl_no == "TESTBL01"
    assert AgentRequest(bl_no=f"test bl{chr(0x2010)}01", session_id="s").bl_no == "TESTBL01"  # 유니코드 하이픈


def _no_customs(monkeypatch):
    monkeypatch.setattr(agent_router, "_fetch_customs",
                        lambda req: (None, agent_router._response(status="failed", answer=agent_router.NO_CARGO), None))


def test_mrn_also_missing_is_said(monkeypatch):
    _no_customs(monkeypatch)
    monkeypatch.setattr(mrn_fallback, "assess", lambda req: None)
    response = agent_router._assess(AgentRequest(bl_no="TESTBL0001", session_id="s", mrn="26TEST0001I"))
    assert response.answer == f"{agent_router.NO_CARGO} {agent_router.MRN_NOT_FOUND}"
    assert agent_router.is_not_found(response)  # 채팅은 여전히 "결과 없음" 갈래로 본다


def test_mof_failure_is_not_called_missing_mrn(monkeypatch):
    _no_customs(monkeypatch)

    def down(req):
        raise mrn_fallback.MrnLookupFailed("timeout")

    monkeypatch.setattr(mrn_fallback, "assess", down)
    response = agent_router._assess(AgentRequest(bl_no="TESTBL0001", session_id="s", mrn="26TEST0001I"))
    assert response.answer.endswith(agent_router.MRN_LOOKUP_FAILED) and "찾지 못했어요. MRN" not in response.answer


def test_mrn_fallback_sentence_without_facility_or_arrival():
    text = mrn_fallback._answer("TEST VICTORY", None, None)
    assert "확인되지 않음입니다" not in text
    assert "입항 예정 시각은 아직 신고되지 않았습니다" in text and "접안 예정지는 아직 정해지지 않았습니다" in text
    text = mrn_fallback._answer("TEST VICTORY", datetime(2026, 10, 3, 0, tzinfo=timezone.utc), "감만부두 4선석")
    assert "TEST VICTORY호가 10월 03일 09:00 부산 입항 예정입니다." in text and "접안 예정지는 감만부두 4선석입니다." in text


def test_mrn_fallback_raises_on_mof_error(monkeypatch):
    from backend.app.clients.mof import MofError

    class Down:
        def sailing_plans_all(self, start, end):
            raise MofError("timeout", "테스트")

        def close(self):
            pass

    monkeypatch.setattr(mrn_fallback, "MofClient", Down)
    with pytest.raises(mrn_fallback.MrnLookupFailed):
        mrn_fallback.assess(AgentRequest(bl_no="TESTBL0001", session_id="s", mrn="26TEST0001I"))
