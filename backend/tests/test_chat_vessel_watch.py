"""채팅으로 배 한 척 지켜보기 테스트 (services/chat.py watch_vessel·vessel_target·is_action_request, routers/chat._vessel_hooks).

LLM·DB·ShipFinder는 부르지 않는다. llm.chat_agent를 가짜로 끼우고 배 저장소는 가짜 VesselHooks로 둔다.
배 이름·호출부호·MMSI는 모두 모의값이다.
"""
import pytest

from backend.app.services import chat as svc
from backend.app.services import llm
from backend.tests.test_chat_agent import _agent, agent_path, web_off  # noqa: F401 (fixture)

USER = "user-1"
ON_SCREEN = {"arrivals": [{"ship_name": "TEST STAR", "flag": "Panama", "eta": "2026-10-09T03:00:00", "call_sign": "TST01"},
                          {"ship_name": "TEST MOON", "flag": "Liberia", "call_sign": "TST02", "selected": True}]}
RESOLVED = 440999002  # 호출부호로 찾았다고 치는 MMSI


class Vessels:
    """가짜 배 저장소. 라우터의 훅처럼 MMSI가 없으면 호출부호로 찾은 셈 친다 (resolve)."""

    def __init__(self, resolve=None, add_error=None):
        self.added, self.removed, self.resolved = [], [], []
        self._resolve, self._add_error = resolve or (lambda sign, name: {"mmsi": RESOLVED}), add_error

    def _mmsi(self, mmsi, call_sign, name):
        if mmsi:
            return {"mmsi": mmsi}
        self.resolved.append((call_sign, name))
        return self._resolve(call_sign, name)

    def hooks(self):
        def add(mmsi, call_sign, name, flag, dest, eta):
            found = self._mmsi(mmsi, call_sign, name)
            if "error" in found:
                return found
            self.added.append((found["mmsi"], name, flag, eta, call_sign))
            return {"error": self._add_error} if self._add_error else found

        def remove(mmsi, call_sign, name):
            found = self._mmsi(mmsi, call_sign, name)
            if "error" not in found:
                self.removed.append(found["mmsi"])
            return found
        return svc.WatchHooks(tool=lambda bl, on: {"on": on}, state=lambda bl: {"watch": False}, worry=lambda bl: None,
                              vessel=svc.VesselHooks(add=add, remove=remove))


def _vessel_llm(monkeypatch, args, tools=None):
    """watch_vessel 하나를 부르는 가짜 chat_agent. 도구 결과를 seen에 모은다."""
    monkeypatch.setattr(svc.mcp_client, "read_only_tools", lambda: tools or [])
    seen = []

    def fake(masked, refs, has_cargo, today, history, tool_list, run, *, model=None, feedback=None, **extra):
        seen.append(run("watch_vessel", args))
        return llm.LLMAnswer(output=llm.ChatAgentOutput(reply="", answer_kind="tool", skill_quote=""), llm=None,
                             prompt_version="test", tool_calls=[])
    monkeypatch.setattr(llm, "chat_agent", fake)
    return seen


def _say(msg, store, on_screen=ON_SCREEN, user=USER, run_tool=None):
    return svc.handle(msg, "s1", None, lambda req: None, user_id=user, watch=store.hooks() if store else None,
                      on_screen=on_screen, run_tool=run_tool)


# ── 요청 문장에는 웹 검색을 다시 시키지 않는다 ──

@pytest.mark.parametrize("msg,expected", [("지금 지도에 보이는 배 추적현황에 넣어줘", True), ("이 배 감시 켜 줘", True), ("알림 꺼 줘", True),
                                          ("B/L 번호는 어디에 넣어요?", False), ("하우스 b/l은 누가 만들어?", False),
                                          ("B/L 어디서 찾는지 알려줘", False)])
def test_is_action_request(msg, expected):
    assert svc.is_action_request(msg) is expected


def test_action_request_gets_no_web_nudge(monkeypatch):
    searched = []
    monkeypatch.setattr(llm, "web_search", lambda q: searched.append(q) or [])
    monkeypatch.delenv("WEB_SEARCH", raising=False)
    calls = _agent(monkeypatch, [], [("", "not_in_skills", "")])
    r = svc.handle("지금 지도에 보이는 배 추적현황에 넣어줘", "s1", None, lambda req: None)
    assert len(calls) == 1 and searched == [] and r.reply == svc.NOT_IN_SKILLS


# ── 배 지켜보기 ──

def test_selected_arrival_is_watched_by_call_sign(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    store = Vessels()
    r = _say("지금 지도에 보이는 배 추적현황에 넣어줘", store)
    assert store.resolved == [("TST02", "TEST MOON")] and store.added[0][:2] == (RESOLVED, "TEST MOON")
    assert r.reply == svc.VESSEL_ON_REPLY.format(name="TEST MOON") + "\n" + svc.WATCH_PAGE_LINE
    assert r.watch_vessel_set == {"mmsi": RESOLVED, "name": "TEST MOON", "on": True} and r.watch_set is None


def test_turning_off_removes_vessel(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": False, "ship_name": None})
    store = Vessels()
    r = _say("이 배 그만 지켜봐 줘", store)
    assert store.removed == [RESOLVED] and r.reply == svc.VESSEL_OFF_REPLY.format(name="TEST MOON")


def test_single_ship_on_screen_is_used_without_selection(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    store = Vessels()
    _say("이 배 지켜봐 줘", store, {"arrivals": [{"ship_name": "TEST STAR", "call_sign": "TST01"}]})
    assert store.added[0][1] == "TEST STAR"


def test_cargo_vessel_with_mmsi_skips_resolve(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    store = Vessels()
    _say("이 배 지켜봐 줘", store, {"vessel": {"ship_name": "TEST SUN", "mmsi": 440999001}})
    assert store.resolved == [] and store.added[0][0] == 440999001


def test_unclear_ship_asks_which_one(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    store = Vessels()
    two = {"arrivals": [{"ship_name": "A ONE", "call_sign": "A1"}, {"ship_name": "B TWO", "call_sign": "B2"}]}
    assert _say("이 배 지켜봐 줘", store, two).reply == svc.VESSEL_ASK_WHICH and store.added == []
    assert _say("이 배 지켜봐 줘", store, None).reply == svc.VESSEL_ASK_WHICH


def test_typed_name_matches_screen_row_but_invented_name_is_refused(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": True, "ship_name": "test star"})
    store = Vessels()
    _say("test star 지켜봐 줘", store)
    assert store.resolved == [("TST01", "TEST STAR")]
    _vessel_llm(monkeypatch, {"on": True, "ship_name": "INVENTED ONE"})
    store2 = Vessels()
    assert _say("지켜봐 줘", store2).reply == svc.VESSEL_ASK_WHICH and store2.added == []


def test_typed_name_uses_candidates_and_requires_one_pick(monkeypatch, agent_path, web_off):  # noqa: F811
    tools = [{"type": "function", "function": {"name": "get_vessel_candidates", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    _vessel_llm(monkeypatch, {"on": True, "ship_name": "OTHER SHIP"}, tools)
    one = {"candidates": [{"mmsi": 440999123, "name": "OTHER SHIP", "flag": "Korea", "call_sign": "X1", "dest": "BUSAN"}]}
    store = Vessels()
    r = _say("OTHER SHIP 지켜봐 줘", store, None, run_tool=lambda n, a: one)
    assert store.added[0][:2] == (440999123, "OTHER SHIP") and r.watch_vessel_set["mmsi"] == 440999123
    two = {"candidates": [{"mmsi": 1, "name": "OTHER SHIP"}, {"mmsi": 2, "name": "OTHER SHIP"}]}
    store2 = Vessels()
    assert _say("OTHER SHIP 지켜봐 줘", store2, None, run_tool=lambda n, a: two).reply == svc.VESSEL_MANY and store2.added == []


def test_not_logged_in_and_no_watch_words_change_nothing(monkeypatch, agent_path, web_off):  # noqa: F811
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    store = Vessels()
    assert _say("이 배 지켜봐 줘", store, user=None).reply == svc.VESSEL_NEED_LOGIN and store.added == []
    seen = _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    r = _say("이 배 어디야?", store)
    assert store.added == [] and seen[0]["error"]["code"] == "not_requested" and r.watch_vessel_set is None


def test_errors_use_router_messages(monkeypatch, agent_path, web_off):  # noqa: F811
    from backend.app.routers import watch as watch_router
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    assert _say("이 배 지켜봐 줘", Vessels(add_error="limit")).reply == watch_router._VESSEL_ERRORS["limit"][1]
    unknown = Vessels(resolve=lambda s, n: {"error": "unknown_vessel"})
    assert _say("이 배 지켜봐 줘", unknown).reply == watch_router._VESSEL_ERRORS["unknown_vessel"][1]


def test_ship_words_with_arrivals_on_screen_are_not_taken_as_cargo_watch(monkeypatch, agent_path, web_off):  # noqa: F811
    """화면에 화물과 입항 목록이 함께 있을 때 "이 배 지켜봐 줘"는 화물 지켜보기로 가로채지 않고 배 지켜보기로 간다."""
    from backend.app.schemas.agent import AgentRequest
    _vessel_llm(monkeypatch, {"on": True, "ship_name": None})
    store = Vessels()
    r = svc.handle("이 배 지켜봐 줘", "s1", AgentRequest(bl_no="TESTBL01", session_id="s1"), lambda req: None,
                   user_id=USER, watch=store.hooks(), on_screen=ON_SCREEN)
    assert r.watch_vessel_set and r.watch_set is None


def test_screen_passes_call_sign_and_selected():
    out = svc._masked_screen(ON_SCREEN)
    assert out["arrivals"][1]["call_sign"] == "TST02" and out["arrivals"][1]["selected"] is True and "selected" not in out["arrivals"][0]


def test_request_schema_accepts_call_sign_and_selected():
    from backend.app.routers.chat import ChatResponse, ChatScreenArrival
    row = ChatScreenArrival(ship_name="TEST STAR", call_sign="TST01", selected=True)
    assert row.model_dump()["selected"] is True and ChatScreenArrival(ship_name="X").selected is False
    resp = ChatResponse(kind="message", reply="x", watch_vessel_set={"mmsi": RESOLVED, "name": "TEST MOON", "on": True})
    assert resp.watch_vessel_set.mmsi == RESOLVED


def test_router_hooks_resolve_by_call_sign_and_check_db_first(monkeypatch):
    """라우터 훅의 켜기 순서: DB 사전 검사 → 호출부호로 MMSI 찾기 → 위치 받아 두기 → 저장 (POST /api/watch/vessels와 같다)."""
    from contextlib import contextmanager

    from backend.app.routers import chat as chat_router
    order = []

    @contextmanager
    def fake_conn():
        yield object()
    monkeypatch.setattr(chat_router.ops, "open_conn", fake_conn)
    vs = chat_router.vessel_watch_service
    monkeypatch.setattr(vs, "precheck", lambda conn, uid, mmsi, name: order.append(("precheck", mmsi)))
    monkeypatch.setattr(vs, "resolve_mmsi", lambda sign, name: order.append(("resolve", sign)) or RESOLVED)
    monkeypatch.setattr(vs, "fetch_position", lambda mmsi: order.append(("position", mmsi)) or True)
    monkeypatch.setattr(vs, "add_vessel", lambda conn, uid, mmsi, name, flag, dest, eta: order.append(("add", mmsi)) or {"mmsi": mmsi})
    hooks = chat_router._vessel_hooks("user-1")
    assert hooks.add(None, "TST02", "TEST MOON", None, None, None) == {"mmsi": RESOLVED}
    assert order == [("precheck", None), ("resolve", "TST02"), ("position", RESOLVED), ("add", RESOLVED)]

    def limit(*a):
        raise vs.VesselWatchError("limit")
    monkeypatch.setattr(vs, "precheck", limit)
    assert hooks.add(None, "TST02", "TEST MOON", None, None, None) == {"error": "limit"}


def test_usage_guide_mentions_ship_watch():
    assert "이 배 지켜봐 줘" in svc.USAGE_GUIDE and "감시 화물" in svc.USAGE_GUIDE


def test_prompt_has_watch_vessel_lines():
    text = open("backend/app/prompts/marvia-chat-agent.md", encoding="utf-8").read()
    assert "- watch_vessel:" in text and "selected: true" in text and "call_sign" in text
