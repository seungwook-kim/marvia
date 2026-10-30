"""H7: 되묻기 interrupt와 체크포인터 (docs/specs/v2-langgraph.md 3, 4절, 10절 H7, AC-05~AC-08, EC-01~EC-07, EC-13).

메모리 체크포인터(GRAPH_CHECKPOINT=memory, 같은 암호화 직렬화기)로 첫 요청이 멈추고 둘째 요청이 이어 가는 길을 끝까지 돌린다.
v1(AGENT_GRAPH=off)로 같은 두 요청을 돌려 응답과 외부 호출 수를 비교한다. 키도 DB도 필요 없다. 열쇠는 테스트 안에서 만든 가짜 Fernet 키다.
B/L은 가공값(TESTBL99...)이다. 가짜 UNI-PASS·도구는 test_routers.py의 fake를 같이 쓴다.
"""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from backend.app import crypto
from backend.app.graph import checkpoint, facade, threads
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest
from backend.app.schemas.customs import CustomsDetailRow, CustomsRecord
from backend.app.services import llm
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, NOW, POSITION, _candidate, _request, _signals, fake,
)

BL = "TESTBL99XYZ0001"
SHED = "TESTSHED-A"   # 처리 이력 원문에만 있는 글자. 체크포인트에 있으면 안 된다 (SDD 3.5)
WATCH = {"eta_conflict": "triggered"}
CANDIDATES = [_candidate(440999001), _candidate(440999002, call_sign="TST02")]  # 같은 이름 배 2척


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    """B/L 열쇠를 가짜로 두고 체크포인터를 메모리로 켠다 (conftest가 기본으로 꺼 둔 것을 되돌린다)."""
    monkeypatch.setenv(crypto.BL_KEY_ENV, Fernet.generate_key().decode())
    monkeypatch.setenv("GRAPH_CHECKPOINT", "memory")
    checkpoint.close_checkpointer()


class Counters:
    """가짜 UNI-PASS와 DB 읽기가 불린 횟수. 첫 요청과 둘째 요청을 따로 세려고 쓴다."""

    def __init__(self):
        self.fetch: list[str] = []
        self.saved_row = 0
        self.load_context = 0

    def reset(self):
        self.fetch.clear()
        self.saved_row = self.load_context = 0


def _record() -> CustomsRecord:
    """마스터로만 찾아지는 화물. 처리 이력 원문(display_rows)이 있다. 중량 칸이 있어 DB로 가면 안 되는 값이다."""
    return CustomsRecord(ship_name="TEST STAR", ship_nat="PA", stage_name="반입신고", container_no="TSTU1234560", bl_masked="TEST****0001",
                         fetched_at=NOW, display_rows=[CustomsDetailRow(kind="반입신고", shed_name=SHED, weight="1234", weight_unit="KG")])


def _setup(fake, monkeypatch, *, judge=JUDGED, candidates=None) -> Counters:
    """같은 이름 배 2척, 위치 있음, 신호는 watch, 마스터로만 찾아지는 B/L. 불린 횟수를 돌려준다."""
    fake(candidates=candidates or CANDIDATES, position=POSITION, judge=judge)
    _signals(monkeypatch, WATCH)
    counters = Counters()
    record = _record()

    class FakeUnipass:
        def __init__(self, *a, **k):
            pass

        def fetch(self, bl_no, kind="MASTER"):
            counters.fetch.append(kind)
            return record if kind == "MASTER" else None

    monkeypatch.setattr(agent_router, "UnipassClient", FakeUnipass)
    real_saved = agent_router._saved_row
    monkeypatch.setattr(agent_router, "_saved_row", lambda *a, **k: (setattr(counters, "saved_row", counters.saved_row + 1), real_saved(*a, **k))[1])
    monkeypatch.setattr(agent_router.memory_service, "load_context",
                        lambda *a, **k: setattr(counters, "load_context", counters.load_context + 1))
    return counters


def _req(**over) -> AgentRequest:
    return AgentRequest(**_request(bl_no=BL, **{"bl_type": None, **over}))


def _run(mode: str, request: AgentRequest, user_id=None):
    import os

    os.environ["AGENT_GRAPH"] = mode
    return agent_router.run_agent(request, user_id=user_id)


def _dump(response) -> dict:
    """trace_id와 벽시계로 찍는 카드 시각만 뺀 응답 (test_graph_judge_loop._same과 같다)."""
    d = response.model_dump(mode="json", exclude={"trace_id"})
    d["evidence"] = [{**e, "fetched_at": None} if e["source"] in {"rule", "searoute"} else e for e in d["evidence"]]
    return d


def _thread(user_id=None, session_id="sess-1") -> str:
    return threads.thread_id(session_id, user_id, BL)


@pytest.fixture
def env_graph(monkeypatch):
    monkeypatch.setenv("AGENT_GRAPH", "on")


# ── AC-05: 재개는 앞 단계를 다시 하지 않는다 ──

def test_resume_equals_v1_second_response_and_skips_earlier_steps(fake, monkeypatch):
    """첫 요청은 v1과 같은 insufficient(후보 2척, customs_detail 포함). 둘째 요청(resolved_mmsi)은 이어 가서 v1의 둘째 응답과 같다.
    둘째 요청의 UNI-PASS 조회는 마스터 하나 1회이고 하우스 시도·저장 화물 읽기·기억 읽기는 0회다. v1의 둘째 요청은 하우스 -> 마스터 두 번이다."""
    monkeypatch.setenv("GRAPH_CHECKPOINT", "off")  # v1 쪽 비교값은 체크포인터 없이 (꺼져 있어도 v1은 어차피 쓰지 않는다)
    c = _setup(fake, monkeypatch)
    v1_first, v1_second = _run("off", _req()), None
    v1_first_calls = list(c.fetch)
    c.reset()
    v1_second = _run("off", _req(resolved_mmsi=440999001))
    v1_second_calls, v1_second_reads = list(c.fetch), (c.saved_row, c.load_context)
    assert v1_first.status == "insufficient" and v1_first.customs_detail
    assert v1_second.status == "ok" and v1_second.customs_detail
    assert v1_first_calls == v1_second_calls == ["HOUSE", "MASTER"] and v1_second_reads == (1, 1)

    monkeypatch.setenv("GRAPH_CHECKPOINT", "memory")
    checkpoint.close_checkpointer()
    c.reset()
    first = _run("on", _req())
    assert _dump(first) == _dump(v1_first)  # customs_detail까지 같다
    assert c.fetch == ["HOUSE", "MASTER"]
    saver = checkpoint.get_checkpointer()
    assert _thread() in saver.storage  # 멈춘 스레드가 남아 있다

    c.reset()
    second = _run("on", _req(resolved_mmsi=440999001))
    assert _dump(second) == _dump(v1_second)
    assert c.fetch == ["MASTER"] and (c.saved_row, c.load_context) == (0, 0)
    assert _thread() not in saver.storage  # 끝난 실행은 바로 지운다 (Q7)


def test_paused_checkpoint_holds_no_processing_history_text(fake, monkeypatch):
    """멈춘 State와 중단 값에는 처리 이력 원문(중량·장치장)이 없다 (SDD 3.5, EC-15). 응답의 customs_detail과 CustomsRecord.display_rows가 비어 있다."""
    _setup(fake, monkeypatch)
    _run("on", _req())
    saver = checkpoint.get_checkpointer()
    graph = facade._compiled_with(saver)
    snap = graph.get_state({"configurable": {"thread_id": _thread()}})
    assert snap.next == ("ask_wait",) and len(snap.interrupts) == 1
    assert snap.values["response"].customs_detail == [] and snap.values["customs"].display_rows == []
    assert snap.interrupts[0].value["response"]["customs_detail"] == []
    dumped = repr(snap.values) + repr(snap.interrupts)
    assert SHED not in dumped and "weight='1234'" not in dumped and "KG" not in dumped
    # 저장된 blob은 암호화돼 B/L도 컨테이너 번호도 글자로 보이지 않는다
    def all_bytes(x):
        """InMemorySaver의 blobs·writes에서 bytes를 모두 꺼낸다(튜플 깊이는 버전마다 다르다)."""
        if isinstance(x, (bytes, bytearray)):
            yield bytes(x)
        elif isinstance(x, dict):
            for v in x.values():
                yield from all_bytes(v)
        elif isinstance(x, (tuple, list)):
            for v in x:
                yield from all_bytes(v)

    raw = b"".join(all_bytes(saver.blobs)) + b"".join(all_bytes(saver.writes))
    assert len(raw) > 100  # 확인할 blob이 실제로 있다
    assert BL.encode() not in raw and b"TSTU1234560" not in raw and SHED.encode() not in raw


def test_second_pause_after_resume_keeps_history_for_screen(fake, monkeypatch):
    """이어 간 실행이 또 되묻기로 끝나도(고른 배가 후보가 아님) 화면 응답에는 처리 이력이 있다(UNI-PASS를 구분 하나로 한 번 다시 받는다)."""
    c = _setup(fake, monkeypatch)
    _run("on", _req())
    c.reset()
    again = _run("on", _req(resolved_mmsi=440999999))  # 후보에 없는 번호: 확정하지 않고 되묻는다
    assert again.status == "insufficient" and again.answer == agent_router.RESOLVED_NOT_CANDIDATE
    assert again.customs_detail and again.customs_detail[0].shed_name == SHED
    assert c.fetch == ["MASTER"]
    assert _thread() in checkpoint.get_checkpointer().storage  # 다시 멈췄다


# ── AC-06 / EC-01: 체크포인터가 없어도 v1과 같다 ──

@pytest.mark.parametrize("setup_env", ["off", "no_key", "unknown_mode"])
def test_without_checkpointer_two_requests_equal_v1(fake, monkeypatch, setup_env):
    """GRAPH_CHECKPOINT=off, 열쇠 없음, 모르는 값에서는 ask_wait에 닿지 않고(interrupt 없음) 두 요청 모두 v1처럼 처음부터 돈다."""
    c = _setup(fake, monkeypatch)
    v1 = [_run("off", _req()), _run("off", _req(resolved_mmsi=440999001))]
    if setup_env == "off":
        monkeypatch.setenv("GRAPH_CHECKPOINT", "off")
    elif setup_env == "no_key":
        monkeypatch.delenv(crypto.BL_KEY_ENV)
    else:
        monkeypatch.setenv("GRAPH_CHECKPOINT", "banana")
    assert checkpoint.get_checkpointer() is None
    c.reset()
    graph = [_run("on", _req()), _run("on", _req(resolved_mmsi=440999001))]
    assert [_dump(r) for r in graph] == [_dump(r) for r in v1]
    assert c.fetch == ["HOUSE", "MASTER", "HOUSE", "MASTER"]  # 둘째 요청도 처음부터


def test_follow_up_requests_never_make_a_thread(fake, monkeypatch):
    """후속 질문은 스레드를 만들지 않는다 (멈출 일이 없다)."""
    _setup(fake, monkeypatch)
    previous = agent_router._response(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5, answer="지난 답")
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: previous)
    calls = []
    monkeypatch.setattr(checkpoint, "get_checkpointer", lambda: calls.append(1))
    monkeypatch.setattr(agent_router.llm, "answer_follow_up", lambda *a, **k: llm.LLMAnswer(
        output=llm.FollowUpOutput(in_scope=True, answer="여유는 5일입니다."),
        llm=llm.LLMResult(content="", model="test/model"), prompt_version="test"))
    response = _run("on", _req(follow_up_question="여유가 며칠이야?"))
    assert response.answer == "여유는 5일입니다." and calls == []


# ── AC-07: 스레드 격리와 만료, 이을 수 없는 경우 ──

def _pause(fake, monkeypatch, user_id=None, **over):
    c = _setup(fake, monkeypatch)
    first = _run("on", _req(**over), user_id=user_id)
    assert first.status == "insufficient"
    c.reset()
    return c


def test_other_user_does_not_inherit_the_paused_state(fake, monkeypatch):
    """같은 B/L, 같은 세션이어도 사용자가 다르면 스레드가 다르다. 둘째 사용자는 처음부터 돌고 첫 사용자의 스레드는 그대로 남는다."""
    c = _pause(fake, monkeypatch, user_id="user-a")
    saver = checkpoint.get_checkpointer()
    other = _run("on", _req(resolved_mmsi=440999001), user_id="user-b")
    assert other.status == "ok" and c.fetch == ["HOUSE", "MASTER"]  # 이어 가지 않고 새로 돌았다
    assert _thread("user-a") in saver.storage and _thread("user-b") not in saver.storage
    assert _thread("user-a") != _thread("user-b")


def test_other_bl_makes_another_thread(fake, monkeypatch):
    """같은 사용자의 다른 B/L은 다른 스레드다 (EC-08). 멈춘 스레드는 영향이 없다."""
    c = _pause(fake, monkeypatch)
    saver = checkpoint.get_checkpointer()
    other = AgentRequest(**_request(bl_no="TESTBL99XYZ0002", bl_type=None, resolved_mmsi=440999001))
    agent_router.run_agent(other, user_id=None)
    assert c.fetch == ["HOUSE", "MASTER"] and _thread() in saver.storage


@pytest.mark.parametrize("name,over", [
    ("bl_type_differs", {"bl_type": "HOUSE", "resolved_mmsi": 440999001}),
    ("mrn_differs", {"mrn": "26ABCD1234I", "resolved_mmsi": 440999001}),
    ("no_new_information", {}),
    ("same_information_again", {"resolved_mmsi": 440999999}),
])
def test_cannot_resume_runs_fresh_and_clears_old_thread(fake, monkeypatch, name, over):
    """bl_type·mrn이 다르거나(EC-03) 새 정보가 없으면(EC-04) 이을 수 없다. 스레드를 지우고 새로 돈다: UNI-PASS를 처음부터 다시 조회하고(하우스 -> 마스터)
    State의 path에 지난 실행이 따라오지 않는다 (AC-08)."""
    base = {"resolved_mmsi": 440999999} if name == "same_information_again" else {}
    c = _pause(fake, monkeypatch, **base)
    saver = checkpoint.get_checkpointer()
    seen = []
    real_stream = facade._stream
    monkeypatch.setattr(facade, "_stream", lambda g, s, c_, **k: (lambda r: (seen.append(r[0]["path"]), r)[1])(real_stream(g, s, c_, **k)))
    _run("on", _req(**over))
    assert c.fetch[:2] == ["HOUSE", "MASTER"] and len(c.fetch) == 2
    assert seen[-1][0] == "guard_input" and seen[-1].count("guard_input") == 1  # 지난 실행의 path가 앞에 붙지 않았다
    # 새로 돈 실행이 다시 멈췄거나 끝났든, 지난 실행의 State가 남아 있지 않다: 스레드에 guard_input이 한 번만 있다
    graph = facade._compiled_with(saver)
    snap = graph.get_state({"configurable": {"thread_id": _thread()}})
    assert not snap.values or snap.values["path"].count("guard_input") == 1


def test_expired_pause_is_cleared_and_run_fresh(fake, monkeypatch):
    """멈춘 지 graph_pause_ttl_hours를 넘으면 이어 가지 않고 새로 돈다 (EC-02)."""
    c = _pause(fake, monkeypatch)
    real_rule = facade.rule
    monkeypatch.setattr(facade, "rule", lambda key: -1 if key == "graph_pause_ttl_hours" else real_rule(key))
    response = _run("on", _req(resolved_mmsi=440999001))
    assert response.status == "ok" and c.fetch == ["HOUSE", "MASTER"]


def test_pause_ttl_comes_from_rules(monkeypatch):
    from backend.app.services.rules import rule

    assert (rule("graph_pause_ttl_hours"), rule("graph_checkpoint_retention_days")) == (2, 7)


# ── EC-05: 같은 스레드의 요청은 차례로 ──

def test_thread_lock_serialises_same_thread_and_cleans_up():
    import threading
    import time

    order: list[str] = []

    def work(tag):
        with facade._thread_lock("verdict:x:y"):
            order.append(f"{tag}-in")
            time.sleep(0.05)
            order.append(f"{tag}-out")

    workers = [threading.Thread(target=work, args=(t,)) for t in "ab"]
    [w.start() for w in workers]
    [w.join() for w in workers]
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])
    assert facade._thread_locks == {}


# ── EC-06, EC-07: 체크포인트 오류는 응답을 막지 않는다 ──

def test_unreadable_thread_runs_fresh_and_deletes_corrupt_thread(fake, monkeypatch):
    """복호화에 실패한 blob(바이트가 바뀜)은 멈춘 그래프가 없는 것으로 보고 새로 돈다. 손상된 스레드는 지운다 (EC-07)."""
    c = _pause(fake, monkeypatch)
    saver = checkpoint.get_checkpointer()
    for key, (typ, blob) in list(saver.blobs.items()):
        if _thread() == key[0] and typ != "empty":
            saver.blobs[key] = (typ, blob[:-3] + b"xyz")
    response = _run("on", _req(resolved_mmsi=440999001))
    assert response.status == "ok" and c.fetch == ["HOUSE", "MASTER"]


def test_plain_blob_in_thread_is_refused(fake, monkeypatch):
    """접미사("+fernet")가 없는 blob은 되살리지 않고 거부한다. 읽는 쪽은 그 스레드를 믿지 않고 지운 뒤 새로 돈다 (10절 H7 (11))."""
    c = _pause(fake, monkeypatch)
    saver = checkpoint.get_checkpointer()
    plain_serde = checkpoint.JsonPlusSerializer(allowed_msgpack_modules=list(checkpoint.MSGPACK_ALLOWLIST))
    for key, (typ, blob) in list(saver.blobs.items()):
        if key[0] == _thread() and typ != "empty":
            saver.blobs[key] = plain_serde.dumps_typed("평문")  # DB에 누가 평문을 써 넣었다
    response = _run("on", _req(resolved_mmsi=440999001))
    assert response.status == "ok" and c.fetch == ["HOUSE", "MASTER"]


def test_checkpoint_read_failure_continues_without_checkpointer(fake, monkeypatch):
    """체크포인트 읽기가 DB 오류로 실패하면 로그에 종류만 남기고 체크포인터 없이 처음부터 돈다. 응답은 v1과 같다 (EC-06)."""
    import psycopg

    c = _setup(fake, monkeypatch)
    v1 = _run("off", _req())
    c.reset()
    saver = checkpoint.get_checkpointer()
    monkeypatch.setattr(saver, "get_tuple", lambda config: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    response = _run("on", _req())
    assert _dump(response) == _dump(v1) and c.fetch == ["HOUSE", "MASTER"]


def test_checkpoint_write_failure_reruns_without_checkpointer(fake, monkeypatch):
    """멈추며 체크포인트를 쓰다 실패해도(풀 시간 초과 등) 사용자는 같은 응답을 받는다 (EC-06)."""
    import psycopg_pool

    c = _setup(fake, monkeypatch)
    v1 = _run("off", _req())
    c.reset()
    saver = checkpoint.get_checkpointer()
    monkeypatch.setattr(saver, "put", lambda *a, **k: (_ for _ in ()).throw(psycopg_pool.PoolTimeout("no connection")))
    response = _run("on", _req())
    assert _dump(response) == _dump(v1)


def test_delete_failure_does_not_block_the_response(fake, monkeypatch):
    """끝난 실행의 스레드를 못 지워도 응답은 나간다. 남은 스레드는 TTL과 정리 함수가 치운다."""
    c = _pause(fake, monkeypatch)
    saver = checkpoint.get_checkpointer()
    monkeypatch.setattr(saver, "delete_thread", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _run("on", _req(resolved_mmsi=440999001)).status == "ok"


# ── 설정과 이름 ──

def test_config_has_only_thread_id_and_recursion_limit(fake, monkeypatch):
    """graph config에는 thread_id와 recursion_limit뿐이다. user_id, B/L은 checkpoints.metadata(평문)로 가므로 넣지 않는다 (4절 마스킹 2)."""
    _setup(fake, monkeypatch)
    configs = []
    real_stream = facade._stream
    monkeypatch.setattr(facade, "_stream", lambda g, s, config, **k: (configs.append(config), real_stream(g, s, config, **k))[1])
    _run("on", _req(), user_id="user-a")
    assert configs and set(configs[0]) == {"recursion_limit", "configurable"} and set(configs[0]["configurable"]) == {"thread_id"}
    flat = repr(configs)
    assert BL not in flat and "user-a" not in flat and "sess-1" not in flat


def test_thread_id_shape_and_isolation():
    a = threads.thread_id("sess-1", "user-a", BL)
    assert a.startswith("verdict:") and a.count(":") == 2
    owner, cargo = a.split(":")[1:]
    assert (len(owner), len(cargo)) == (threads.OWNER_LEN, threads.CARGO_LEN)
    assert BL not in a and "sess-1" not in a and "user-a" not in a
    assert a == threads.thread_id("sess-2", "user-a", BL)             # 로그인했으면 session_id가 달라도 같은 스레드
    assert a != threads.thread_id("sess-1", "user-b", BL)             # 사용자가 다르면 다르다
    assert a != threads.thread_id("sess-1", "user-a", "TESTBL99XYZ0002")  # B/L이 다르면 다르다
    anon = threads.thread_id("sess-1", None, BL)
    assert anon != threads.thread_id("sess-2", None, BL)              # 비로그인은 같은 session_id에서만 이어진다
    assert anon.startswith(f"verdict:{threads.owner_hash('sess-1', None)}:")


def test_thread_id_needs_key_and_an_owner(monkeypatch):
    assert threads.thread_id(None, None, BL) is None          # 주인을 알 수 없다
    monkeypatch.delenv(crypto.BL_KEY_ENV)
    assert threads.thread_id("sess-1", None, BL) is None      # 화물 지문을 못 만든다 (EC-01)


# ── AC-14, EC-13: 삭제 ──

def test_user_delete_removes_only_that_users_memory_threads(fake, monkeypatch):
    _setup(fake, monkeypatch)
    _run("on", _req(), user_id="user-a")
    _run("on", _req(), user_id="user-b")
    saver = checkpoint.get_checkpointer()
    assert {_thread("user-a"), _thread("user-b")} <= set(saver.storage)

    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append((" ".join(sql.split()), params))

    conn = Conn()
    threads.delete_user_threads(conn, "user-a")
    assert _thread("user-a") not in saver.storage and _thread("user-b") in saver.storage
    assert [s for s, _ in conn.sql] == [f"DELETE FROM {t} WHERE thread_id LIKE %s" for t in threads.CHECKPOINT_TABLES]
    assert all(p == [threads.user_prefix("user-a") + "%"] for _, p in conn.sql)


def test_user_delete_skips_missing_tables(monkeypatch):
    """마이그레이션 032 전의 DB(표 없음)라도 삭제 전체가 무너지지 않는다. 다른 오류는 그대로 올라간다."""
    import psycopg

    class Conn:
        def execute(self, *a, **k):
            raise psycopg.errors.UndefinedTable("no table")

    threads.delete_user_threads(Conn(), "user-a")

    class Broken:
        def execute(self, *a, **k):
            raise psycopg.OperationalError("down")

    with pytest.raises(psycopg.OperationalError):
        threads.delete_user_threads(Broken(), "user-a")


def test_withdraw_and_history_delete_call_thread_delete(monkeypatch):
    """계정 삭제(services/account.withdraw)와 DELETE /api/history가 같은 트랜잭션 안에서 스레드를 지운다."""
    from fastapi.testclient import TestClient

    from backend.app.main import app
    from backend.app.routers import history as history_router
    from backend.app.services import account

    deleted = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(history_router, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(history_router, "delete_user_queries", lambda conn, uid: 3)
    monkeypatch.setattr(history_router, "forget_user", lambda conn, uid: None)
    monkeypatch.setattr(history_router.conversations, "delete_all", lambda conn, uid: None)
    monkeypatch.setattr(history_router, "delete_user_threads", lambda conn, uid: deleted.append(("history", uid)))
    from backend.app.auth import current_user_id

    app.dependency_overrides[current_user_id] = lambda: "user-a"
    try:
        with TestClient(app) as client:
            assert client.delete("/api/history").status_code == 200
    finally:
        app.dependency_overrides.pop(current_user_id, None)
    assert deleted == [("history", "user-a")]

    for name in ("_close_account", "delete_user_queries", "delete_profile", "forget_user", "delete_conversations", "delete_kakao_link"):
        monkeypatch.setattr(account, name, lambda *a, **k: 0)
    monkeypatch.setattr(account, "quota", type("Q", (), {"forget_user": staticmethod(lambda c, u: None)}))
    monkeypatch.setattr(account, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(account, "delete_account", lambda uid: None)
    monkeypatch.setattr(account, "delete_user_threads", lambda conn, uid: deleted.append(("account", uid)))
    account.withdraw("user-a")
    assert deleted[-1] == ("account", "user-a")


# ── 팩토리 ──

def test_checkpointer_factory_modes(monkeypatch):
    monkeypatch.setenv("GRAPH_CHECKPOINT", "memory")
    saver = checkpoint.get_checkpointer()
    assert saver is not None and checkpoint.get_checkpointer() is saver  # 한 번 만든 것을 재사용한다
    monkeypatch.setenv("GRAPH_CHECKPOINT", "off")
    assert checkpoint.get_checkpointer() is None
    monkeypatch.setenv("GRAPH_CHECKPOINT", "auto")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert checkpoint.get_checkpointer() is None              # auto: DB 주소가 없으면 off
    monkeypatch.setenv("GRAPH_CHECKPOINT", "postgres")
    assert checkpoint.get_checkpointer() is None              # postgres인데 DB 주소가 없다
    monkeypatch.setenv("GRAPH_CHECKPOINT", "")
    assert checkpoint.checkpoint_mode() == "auto"


def test_postgres_pool_that_cannot_open_gives_no_checkpointer_and_backs_off(monkeypatch):
    """풀을 못 열면(DB 장애) 체크포인터 없이 계속하고, 잠깐은 다시 열어 보지 않는다 (EC-06)."""
    opened = []

    def fail(*a, **k):
        opened.append(1)
        raise RuntimeError("down")

    monkeypatch.setenv("GRAPH_CHECKPOINT", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    import psycopg_pool

    monkeypatch.setattr(psycopg_pool.ConnectionPool, "open", fail)
    assert checkpoint.get_checkpointer() is None and checkpoint.get_checkpointer() is None
    assert opened == [1]  # 두 번째 호출은 열어 보지 않았다


def test_pool_size_comes_from_env(monkeypatch):
    monkeypatch.setenv("GRAPH_DB_POOL_MAX", "4")
    assert checkpoint._pool_max() == 4
    monkeypatch.setenv("GRAPH_DB_POOL_MAX", "x")
    assert checkpoint._pool_max() == checkpoint.DEFAULT_POOL_MAX


# ── 노드 span: 멈춤은 오류가 아니다 (7절) ──

def test_interrupt_is_not_an_error_span_and_resumed_is_marked(fake, monkeypatch):
    """ask_wait의 멈춤(GraphInterrupt)은 ERROR가 아니라 outcome=interrupted다. 이어 간 실행의 span에는 resumed=True가 붙는다.
    멈춘 요청과 이어 간 요청 모두 노드 span의 input·output이 비어 있고 어떤 span에도 B/L이 없다."""
    import langfuse

    from backend.tests.test_trace_graph_nodes import FakeClient

    client = FakeClient()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(langfuse, "get_client", lambda: client)
    monkeypatch.setattr(llm, "get_prompt", lambda name, label="production": __import__("backend.app.observability.langfuse", fromlist=["x"]).Prompt(
        name=name, text="", version="local"))
    _setup(fake, monkeypatch)

    _run("on", _req())
    waits = client.named("node:ask_wait")
    assert len(waits) == 1 and waits[0].metadata["outcome"] == "interrupted" and "level" not in waits[0].fields
    assert all(s.fields.get("level") != "ERROR" for s in client.nodes())
    assert not any(s.metadata.get("resumed") for s in client.nodes())

    before = len(client.spans)
    _run("on", _req(resolved_mmsi=440999001))
    resumed_spans = [s for s in client.spans[before:] if s.name.startswith("node:")]
    names = [s.name for s in resumed_spans]
    assert names[:2] == ["node:ask_wait", "node:identify"] and "node:decorate" in names
    assert all(s.metadata["resumed"] for s in resumed_spans)
    assert all(s.input is None and "output" not in s.fields for s in client.nodes())
    assert BL not in json.dumps([{"i": s.input, "f": s.fields, "n": s.name} for s in client.spans], default=str)
