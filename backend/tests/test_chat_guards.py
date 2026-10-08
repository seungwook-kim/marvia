"""채팅 울타리 테스트 (services/chat.py).

사람 이름 되풀이 막기: 화물·배와 상관없는 사람 이름("호준이", "민수 어디야?")에 모델이 이름을 따라 쓴 답은 버리고 고정 문장으로 되묻는다.
"이 배" 위치 돌려 보내기: 화면에 화물이 있고 화면 배와 같은 MMSI로 위치·거리 도구를 부르면 그 호출은 ask_cargo가 대신 답한다.
LLM·MCP는 부르지 않는다 (가짜 chat_agent). 이름·배·MMSI·B/L은 모두 모의값이다.
"""
import pytest

from backend.app.routers.agent import _response
from backend.app.schemas.agent import AgentRequest
from backend.app.services import chat as svc
from backend.app.services import llm, validate
from backend.tests.test_chat_agent import _agent, _mcp, _no_classic, _out, agent_path, web_off  # noqa: F401 (fixture)

KNOWN = llm.skills_reference(llm.CHAT_AGENT_SKILLS) + svc.OTHER_REPLY + validate.GENERAL_TERMS


# ── 사람 이름 되풀이 막기 ──
@pytest.mark.parametrize("message,name", [("호준이", "호준"), ("민수 어디야?", "민수"), ("김대리한테 전달해야 돼", "김대리"),
                                          ("민수야", "민수"), ("김대리", "김대리")])
def test_person_like_words_are_found(message, name):
    assert name in svc.person_names(message, KNOWN)


@pytest.mark.parametrize("message", ["아 짜증나", "ㅋㅋㅋ", "망했다", "힘들다", "B/L 번호는 어디서 찾아?", "통관 며칠 걸려?",
                                     "이 배 어디야?", "D/O가 뭐야?"])
def test_ordinary_messages_have_no_person_name(message):
    assert svc.person_names(message, KNOWN) == []


def test_echo_is_detected_only_when_the_reply_writes_the_name():
    assert svc.echoes_person_name("호준이", "호준이가 누구인지 알려 주세요.", KNOWN)
    assert not svc.echoes_person_name("호준이", "누구를 말씀하시는지 알려 주세요.", KNOWN)


def test_guard_replaces_name_echo_with_fixed_question(monkeypatch, agent_path, web_off):  # noqa: F811
    _no_classic(monkeypatch)
    _agent(monkeypatch, [], [("호준이가 누구인지 알려 주세요.", "unrelated", "")])
    r = svc.handle("호준이", "s1", None, lambda req: pytest.fail("조회하면 안 된다"))
    assert r.reply == svc.PERSON_ASK and "호준" not in r.reply


def test_guard_keeps_a_reply_without_the_name(monkeypatch, agent_path, web_off):  # noqa: F811
    _no_classic(monkeypatch)
    _agent(monkeypatch, [], [("누구를 말씀하시는지 알려 주세요.", "unrelated", "")])
    r = svc.handle("호준이", "s1", None, lambda req: pytest.fail("조회하면 안 된다"))
    assert r.reply == "누구를 말씀하시는지 알려 주세요."


def test_memo_question_is_not_replaced_by_person_ask(monkeypatch, agent_path, web_off):  # noqa: F811
    """저장 메모를 묻는 문장이면 거래처 이름이 답에 나와도 이름 되풀이 막기가 나서지 않는다."""
    _no_classic(monkeypatch)
    _agent(monkeypatch, [], [("삼성 전화번호는 저장된 게 없어요.", "unrelated", "")])
    r = svc.handle("삼성 전화번호 뭐였지", "s1", None, lambda req: None)
    assert r.reply != svc.PERSON_ASK


def test_bl_and_ship_text_is_not_a_person_name():
    """B/L 자리표시자와 영문 배 이름은 한글 낱말만 보는 이름 찾기에 걸리지 않는다."""
    assert svc.person_names("[BL1] 어디야?", KNOWN) == []
    assert svc.person_names("HONGKONG VOYAGER 어디야?", KNOWN) == []


# ── "이 배" 위치 돌려 보내기 ──
CONTEXT = AgentRequest(bl_no="TESTBL01", bl_type="HOUSE", session_id="s1")
SCREEN = {"vessel": {"ship_name": "SAMPLE ORCHID", "flag": "LR", "mmsi": 440999001, "lat": 34.2, "lng": 128.9, "destination": "BUSAN"}}
TOOLS = [{"type": "function", "function": {"name": n, "description": "x", "parameters": {"type": "object", "properties": {}}}}
         for n in ("get_live_position", "estimate_distance")]


def _fake(monkeypatch, script):
    """on_screen을 받는 가짜 chat_agent. script의 도구를 부르고 결과를 모은다."""
    done = []

    def fake(masked, refs, has_cargo, today, history, tools, run, *, model=None, feedback=None, **extra):
        calls = [{"name": n, "arguments": a, "result": run(n, a)} for n, a in script]
        done.extend(calls)
        return llm.LLMAnswer(output=_out(("", "tool", "")), llm=None, prompt_version="test", tool_calls=calls)
    monkeypatch.setattr(llm, "chat_agent", fake)
    return done


def _run(monkeypatch, script, context=CONTEXT, screen=SCREEN):
    seen, run_tool = _mcp(monkeypatch, {"mmsi": 440999001, "lat": 34.9, "lng": 129.3, "source": "shipfinder",
                                        "fetched_at": "2026-10-08T00:00:00+00:00"})
    monkeypatch.setattr(svc.mcp_client, "read_only_tools", lambda: TOOLS)  # 위치·거리 둘이 있는 목록
    asked = []
    done = _fake(monkeypatch, script)

    def run_agent(req):
        asked.append(req.follow_up_question)
        return _response(status="ok", answer="배는 부산 앞바다에 있어요.")
    r = svc.handle("이 배 어디야?", "s1", context, run_agent, run_tool=run_tool, on_screen=screen)
    return r, seen, asked, done


def test_own_vessel_position_call_is_rerouted_to_ask_cargo(monkeypatch, agent_path, web_off):  # noqa: F811
    r, seen, asked, done = _run(monkeypatch, [("get_live_position", {"mmsi": 440999001}),
                                              ("estimate_distance", {"lat": 34.9, "lng": 129.3, "sog": 12.4})])
    assert seen == [] and len(asked) == 1  # 위치·거리 도구는 실행하지 않았고 화물 질문이 한 번 돌았다
    assert done[0]["result"]["rerouted_to"] == "ask_cargo" and done[1]["result"]["rerouted_to"] == "ask_cargo"
    assert r.kind == "cargo" and "부산 앞바다" in r.reply


def test_other_vessel_is_not_rerouted(monkeypatch, agent_path, web_off):  # noqa: F811
    _, seen, asked, _ = _run(monkeypatch, [("get_live_position", {"mmsi": 440999004})])
    assert seen == [("get_live_position", {"mmsi": 440999004})] and asked == []


def test_no_cargo_on_screen_is_not_rerouted(monkeypatch, agent_path, web_off):  # noqa: F811
    _, seen, asked, _ = _run(monkeypatch, [("get_live_position", {"mmsi": 440999001})], context=None)
    assert seen == [("get_live_position", {"mmsi": 440999001})] and asked == []
