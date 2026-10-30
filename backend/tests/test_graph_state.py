"""H1: 그래프 State와 체크포인트 암호화 직렬화기 (docs/specs/v2-langgraph.md 2절 State, 4절 체크포인트, 10절 H1, AC-09).

키도 DB도 없이 돈다. 암호화 열쇠는 테스트 안에서 만든 가짜 Fernet 키를 환경변수로 준다.
B/L, 컨테이너 번호는 가짜 값(TESTBL99 등)만 쓴다 (CLAUDE.md "테스트에 실제 B/L 번호를 쓰지 않는다").
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import types
import typing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional, TypedDict, get_args, get_origin, get_type_hints

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel

from backend.app import crypto
from backend.app.graph import checkpoint
from backend.app.graph.state import PLAINTEXT_FIELDS, AgentState
from backend.app.schemas import agent as agent_schemas
from backend.app.schemas import customs as customs_schemas
from backend.app.schemas import signals as signals_schemas
from backend.app.schemas.agent import (
    AgentRequest,
    AgentResponse,
    AnchorageInfo,
    BerthInfo,
    CustomsInfo,
    EtaChange,
    EtaInfo,
    Evidence,
    Position,
    Reason,
    VesselCandidate,
    VesselInfo,
)
from backend.app.schemas.customs import CustomsDetailRow, CustomsRecord, CustomsStage
from backend.app.schemas.signals import Signal, SignalSet
from backend.app.services.llm import CustomsExplanation, JudgeOutput
from backend.app.services.memory import MemoryContext

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_BL = "TESTBL99"
FAKE_CNTR = "TEST1234567"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _test_key(monkeypatch):
    """B/L 열쇠를 테스트용 가짜 키로 둔다. 실제 열쇠는 쓰지 않는다."""
    monkeypatch.setenv(crypto.BL_KEY_ENV, Fernet.generate_key().decode())


# ── 1. 최상위 평문 허용 목록 (AC-09) ──

_PRIMITIVES = (str, int, float, bool)


def is_plain_primitive(hint) -> bool:
    """타입이 str/int/float/bool(또는 그 Optional, 문자열 Literal)뿐이면 True.

    PostgresSaver는 이런 최상위 값을 checkpoints.checkpoint JSONB에 평문으로 넣는다 (v2-langgraph.md 1절 스파이크 표).
    날짜, 객체, 리스트, dict는 blob으로 가서 암호화되므로 여기서 걸리지 않는다.
    """
    origin = get_origin(hint)
    if origin is Annotated:
        return is_plain_primitive(get_args(hint)[0])
    if origin is Literal:
        return all(isinstance(v, _PRIMITIVES) or v is None for v in get_args(hint))
    if origin in (typing.Union, types.UnionType):
        return all(is_plain_primitive(a) for a in get_args(hint))
    return hint is type(None) or hint in _PRIMITIVES


def plaintext_violations(state_cls, allowed) -> list[str]:
    """허용 목록에 없는데 평문으로 저장될 최상위 필드 이름들."""
    hints = get_type_hints(state_cls)
    return sorted(n for n, h in hints.items() if is_plain_primitive(h) and n not in allowed)


def test_primitive_state_fields_are_all_in_plaintext_allowlist():
    """AgentState의 최상위 str/int/float/bool(Optional 포함) 필드는 PLAINTEXT_FIELDS에 있는 것만 허용한다."""
    assert plaintext_violations(AgentState, PLAINTEXT_FIELDS) == []


def test_detector_catches_new_primitive_field():
    """검사기 자체 확인: 목록에 없는 str, Optional[int], Literal 필드를 만들면 걸리고, 객체·리스트 필드는 안 걸린다."""
    class Bad(TypedDict, total=False):
        free_text: str
        maybe_num: Optional[int]
        kind: Literal["a", "b"]
        lines: list[str]
        when: datetime
        ok: str

    assert plaintext_violations(Bad, {"ok"}) == ["free_text", "kind", "maybe_num"]


def test_plaintext_allowlist_has_no_stale_names():
    """PLAINTEXT_FIELDS의 이름은 모두 AgentState에 실제로 있는 필드다."""
    assert PLAINTEXT_FIELDS <= set(get_type_hints(AgentState))


def test_identifying_fields_are_not_plaintext():
    """B/L, 컨테이너 번호, 메모, LLM·사용자 문장이 담기는 칸은 평문 허용에 없고, 최상위 원시 값 타입도 아니다 (2절 표 "아니오")."""
    hints = get_type_hints(AgentState)
    sensitive = ["request", "judged_request", "memory", "customs", "failure", "candidates", "judged", "previous",
                 "response", "collected", "violations", "judge_feedback", "follow_feedback", "port_note"]
    for name in sensitive:
        assert name not in PLAINTEXT_FIELDS, name
        assert not is_plain_primitive(hints[name]), f"{name}이 평문 컬럼으로 갈 수 있는 타입이다"


def test_path_is_the_only_reducer_field():
    """리듀서는 path 하나뿐이다. 나머지는 덮어쓰기라 지난 턴의 근거가 따라오지 않는다 (2절)."""
    hints = get_type_hints(AgentState, include_extras=True)
    reducers = [n for n, h in hints.items() if get_origin(h) is Annotated]
    assert reducers == ["path"]


def _models_in(hint, found: set[type]) -> None:
    """타입 힌트 안에 든 pydantic 모델과 dataclass를 모은다."""
    for arg in get_args(hint):
        _models_in(arg, found)
    if isinstance(hint, type) and (issubclass(hint, BaseModel) or dataclasses.is_dataclass(hint)):
        found.add(hint)


def test_every_model_in_state_is_in_msgpack_allowlist():
    """State 칸에 쓰인 모델은 전부 직렬화 허용 목록에 있다. 빠지면 재개 때 모델이 dict로 돌아온다."""
    found: set[type] = set()
    for hint in get_type_hints(AgentState).values():
        _models_in(hint, found)
    assert found, "State에서 모델을 찾지 못했다(검사기 오류)"
    assert found <= set(checkpoint.MSGPACK_ALLOWLIST)


@pytest.mark.parametrize("module", [agent_schemas, signals_schemas, customs_schemas])
def test_allowlist_covers_every_contract_model(module):
    """C1~C3, C5 모듈에 정의된 모델은 모두 허용 목록에 있다. 새 모델을 더하면 이 테스트가 목록 갱신을 알려 준다."""
    defined = {c for c in vars(module).values()
               if isinstance(c, type) and issubclass(c, BaseModel) and c.__module__ == module.__name__}
    assert defined
    assert defined <= set(checkpoint.MSGPACK_ALLOWLIST)


# ── 2. 허용 목록 왕복 (strict) ──

def _c1_to_c5_objects() -> dict:
    """C1~C5 모델과 판정 출력, 기억 문맥을 State 모양(최상위 값 + collected dict)으로 모은 가짜 값."""
    request = AgentRequest(bl_no=FAKE_BL, session_id="s-test", sale_due_date=date(2026, 10, 5))
    customs = CustomsRecord(
        ship_name="TEST VESSEL", voyage="0001E", entry_date=NOW, stage_code="01", stage_name="입항",
        discharge_port="KRPUS", container_no=FAKE_CNTR, goods_name="TEST GOODS",
        stage_history=[CustomsStage(code="01", name="입항", at=NOW)],
        display_rows=[CustomsDetailRow(kind="반입신고", weight="123")],
        bl_masked="TE****99", fetched_at=NOW,
    )
    signal_set = SignalSet(
        signals=[Signal(id="position_stale", state="clear", value="1h", threshold="6h", source="shipfinder")],
        freshness="fresh", distance_nm=10.0, calc_eta=NOW, eta_gap_hours=None, computed_at=NOW,
    )
    candidate = VesselCandidate(mmsi=111111111, imo=None, name="TEST VESSEL", flag="KR", dest="KRPUS",
                                last_time=NOW, ship_type=70, match_reasons=["name"])
    anchored = AnchorageInfo(anchored_at=NOW, state="anchored", waited_hours=3)
    eta_moved = EtaChange(from_eta=date(2026, 9, 24), to_eta=date(2026, 9, 26), hours=48)
    response = AgentResponse(
        status="ok", verdict="fine", vessel_match="confirmed", vessel_candidates=[candidate],
        reasons=[Reason(rule="position_stale", value="1h", triggered=False)], days_margin=3, decided_by="rule",
        vessel=VesselInfo(mmsi=111111111, imo=None, name="TEST VESSEL", call_sign="TEST", flag="KR", ship_type=70,
                          voyage_no="0001E"),
        position=Position(lat=35.1, lng=129.0, sog=0.0, cog=0.0, navistat=0, last_time=NOW, freshness="fresh"),
        eta=EtaInfo(ais_eta=None, mof_eta=None, calc_eta=NOW, trusted_eta=NOW, distance_nm=10.0),
        customs=CustomsInfo(stage_code="01", stage_name="입항", plain_explanation="TEST", next_stage=None,
                            needs_broker=False, container_no=FAKE_CNTR),
        berth=BerthInfo(), evidence=[Evidence(source="rule", field="f", value="v", fetched_at=NOW)],
        answer="TEST", trace_id="t-1", anchorage=anchored, eta_change=eta_moved,
    )
    judged = JudgeOutput(verdict="fine", reasons=[], conflicts=[], actions=[], answer="TEST",
                         customs=CustomsExplanation(plain_explanation="TEST", next_stage=None))
    return {
        "request": request, "judged_request": request, "customs": customs, "signal_set": signal_set,
        "candidates": [candidate], "anchored": anchored, "eta_moved": eta_moved, "response": response,
        "failure": response, "previous": response, "judged": judged,
        "memory": MemoryContext(blocks="TEST MEMO", judge_blocks="TEST MEMO 2"),
        "collected": {"anchorage": anchored, "eta_change": eta_moved, "evidence": response.evidence,
                      "vessel": response.vessel, "position": response.position, "eta": response.eta,
                      "berth": response.berth, "vessel_candidates": [candidate]},
        "previous_eta": date(2026, 9, 24), "trusted_eta": NOW, "path": ["guard_input"],
    }


def _assert_same_types(original, restored, name="state"):
    """되살린 값이 원본과 같은 타입(dict, list 안까지)이고 같은 내용인지 본다."""
    assert type(restored) is type(original), f"{name}: {type(restored).__name__} != {type(original).__name__}"
    if isinstance(original, dict):
        assert restored.keys() == original.keys()
        for k in original:
            _assert_same_types(original[k], restored[k], f"{name}.{k}")
    elif isinstance(original, list):
        assert len(restored) == len(original)
        for i, (a, b) in enumerate(zip(original, restored)):
            _assert_same_types(a, b, f"{name}[{i}]")
    elif isinstance(original, BaseModel):
        # display_rows는 exclude=True라 체크포인트로 가지 않는다 (v2-langgraph.md 4절 "저장되는 것"). 나머지는 같아야 한다
        assert restored.model_dump() == original.model_dump(), name
    else:
        assert restored == original, name


def test_round_trip_c1_to_c5_objects_in_strict_mode(monkeypatch):
    """LANGGRAPH_STRICT_MSGPACK 켠 상태에서 C1~C5 객체를 넣고 되살려도 같은 모델(isinstance)로 돌아온다."""
    from langgraph.checkpoint.serde import _msgpack
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", True)
    serde = checkpoint.make_serde()
    state = _c1_to_c5_objects()
    typ, blob = serde.dumps_typed(state)
    assert typ.endswith("+" + checkpoint.CIPHER_NAME)
    restored = serde.loads_typed((typ, blob))

    assert isinstance(restored["request"], AgentRequest)
    assert isinstance(restored["customs"], CustomsRecord)
    assert isinstance(restored["customs"].stage_history[0], CustomsStage)
    assert isinstance(restored["signal_set"], SignalSet)
    assert isinstance(restored["signal_set"].signals[0], Signal)
    assert isinstance(restored["response"], AgentResponse)
    assert isinstance(restored["response"].evidence[0], Evidence)
    assert isinstance(restored["candidates"][0], VesselCandidate)
    assert isinstance(restored["anchored"], AnchorageInfo)
    assert isinstance(restored["eta_moved"], EtaChange)
    assert isinstance(restored["judged"], JudgeOutput)
    assert isinstance(restored["memory"], MemoryContext)
    assert isinstance(restored["collected"]["anchorage"], AnchorageInfo)
    assert isinstance(restored["collected"]["vessel"], VesselInfo)
    assert isinstance(restored["collected"]["vessel_candidates"][0], VesselCandidate)
    _assert_same_types(state, restored)


def test_customs_display_rows_do_not_go_into_checkpoint():
    """화면 전용 처리 이력 원문(중량 등)은 exclude=True라 직렬화되지 않고 되살리면 빈 목록이다 (4절 "저장되는 것", SDD 3.5)."""
    customs = _c1_to_c5_objects()["customs"]
    assert customs.display_rows  # 원본에는 있다
    serde = checkpoint.make_serde()
    restored = serde.loads_typed(serde.dumps_typed({"customs": customs}))["customs"]
    assert isinstance(restored, CustomsRecord)
    assert restored.display_rows == []


def test_removing_a_model_from_allowlist_breaks_round_trip(monkeypatch):
    """허용 목록에서 모델 하나를 일부러 빼면 되살릴 때 차단되어 dict로 돌아온다 (허용 목록이 실제로 일하고 있다는 증거)."""
    from langgraph.checkpoint.serde import _msgpack
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", True)
    state = _c1_to_c5_objects()
    for removed in (AgentRequest, CustomsRecord, SignalSet, AgentResponse, JudgeOutput, MemoryContext, VesselCandidate):
        narrowed = tuple(c for c in checkpoint.MSGPACK_ALLOWLIST if c is not removed)
        assert len(narrowed) == len(checkpoint.MSGPACK_ALLOWLIST) - 1
        serde = checkpoint.make_serde(narrowed)
        restored = serde.loads_typed(serde.dumps_typed(state))
        key = {AgentRequest: "request", CustomsRecord: "customs", SignalSet: "signal_set", AgentResponse: "response",
               JudgeOutput: "judged", MemoryContext: "memory"}.get(removed)
        if key:
            assert not isinstance(restored[key], removed), removed.__name__
            assert isinstance(restored[key], dict), removed.__name__
        else:  # VesselCandidate는 리스트 안에 든 모델이라 리스트 원소가 dict가 된다
            assert not isinstance(restored["candidates"][0], removed)


def test_strict_env_var_blocks_unlisted_types_but_not_ours():
    """실제 환경변수 LANGGRAPH_STRICT_MSGPACK=true로 새 프로세스를 띄워 확인한다.
    목록 없는 기본 직렬화기는 AgentRequest를 막고(dict), make_serde()는 모델로 되살린다."""
    code = (
        "from datetime import date\n"
        "from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer\n"
        "from backend.app.graph.checkpoint import make_serde\n"
        "from backend.app.schemas.agent import AgentRequest\n"
        f"r = AgentRequest(bl_no='{FAKE_BL}', session_id='s')\n"
        "plain = JsonPlusSerializer()\n"
        "assert isinstance(plain.loads_typed(plain.dumps_typed(r)), dict), 'strict mode is not on'\n"
        "s = make_serde()\n"
        "assert isinstance(s.loads_typed(s.dumps_typed(r)), AgentRequest)\n"
        "print('ok')\n"
    )
    env = {**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true"}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("ok")


# ── 3. 암호화 blob에 식별 값이 없다 ──

def test_encrypted_blob_has_no_bl_or_container_number():
    """암호화 직렬화기가 만든 바이트에는 가짜 B/L, 컨테이너 번호, 메모 문장이 보이지 않는다. 통제군(암호화 없음)에는 보인다."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    state = _c1_to_c5_objects()
    _, plain_blob = JsonPlusSerializer(allowed_msgpack_modules=list(checkpoint.MSGPACK_ALLOWLIST)).dumps_typed(state)
    assert FAKE_BL.encode() in plain_blob  # 통제군: 이 값이 평문 직렬화에는 들어간다

    _, blob = checkpoint.make_serde().dumps_typed(state)
    for secret in (FAKE_BL, FAKE_CNTR, "TEST MEMO", "TEST GOODS"):
        assert secret.encode() not in blob, secret


def test_blob_cannot_be_opened_with_another_key(monkeypatch):
    """다른 열쇠로는 열리지 않고 예외가 난다(조용히 빈 값이 되지 않는다)."""
    serde = checkpoint.make_serde()
    data = serde.dumps_typed(_c1_to_c5_objects())
    monkeypatch.setenv(crypto.BL_KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(checkpoint.CheckpointCryptoError):
        serde.loads_typed(data)


def test_tampered_blob_is_rejected():
    """저장된 바이트가 바뀌면 위조 검사에서 걸린다."""
    serde = checkpoint.make_serde()
    typ, blob = serde.dumps_typed({"request": _c1_to_c5_objects()["request"]})
    bad = blob[:-2] + (b"AA" if blob[-2:] != b"AA" else b"BB")
    with pytest.raises(checkpoint.CheckpointCryptoError):
        serde.loads_typed((typ, bad))


def test_no_key_never_writes_plaintext(monkeypatch):
    """열쇠가 없으면 평문으로 저장하지 않고 예외를 던진다 (4절 3, EC-01의 마지막 방어선)."""
    monkeypatch.delenv(crypto.BL_KEY_ENV, raising=False)
    with pytest.raises(checkpoint.CheckpointCryptoError):
        checkpoint.make_serde().dumps_typed({"request": _c1_to_c5_objects()["request"]})


def test_unknown_cipher_name_is_rejected():
    """모르는 암호 이름이 붙은 blob은 열지 않는다."""
    with pytest.raises(checkpoint.CheckpointCryptoError):
        checkpoint.FernetCipher().decrypt("aes", b"x")


# ── crypto.py 바이트용 함수 ──

def test_encrypt_bytes_round_trip_and_not_plain():
    token = crypto.encrypt_bytes(FAKE_BL.encode())
    assert token is not None and FAKE_BL.encode() not in token
    assert crypto.decrypt_bytes(token) == FAKE_BL.encode()
    assert crypto.encrypt_bytes(FAKE_BL.encode()) != token  # Fernet은 매번 다르게 잠근다


def test_bytes_functions_return_none_without_key_or_with_wrong_key(monkeypatch):
    token = crypto.encrypt_bytes(b"x")
    monkeypatch.setenv(crypto.BL_KEY_ENV, Fernet.generate_key().decode())
    assert crypto.decrypt_bytes(token) is None
    monkeypatch.delenv(crypto.BL_KEY_ENV)
    assert crypto.encrypt_bytes(b"x") is None
    assert crypto.decrypt_bytes(token) is None


def test_bytes_functions_use_the_key_named_by_key_env(monkeypatch):
    """key_env 인자로 다른 열쇠(PII)를 고를 수 있고, B/L 열쇠와 서로 열리지 않는다(기존 encrypt/decrypt와 같다)."""
    monkeypatch.setenv(crypto.PII_KEY_ENV, Fernet.generate_key().decode())
    token = crypto.encrypt_bytes(b"x", crypto.PII_KEY_ENV)
    assert crypto.decrypt_bytes(token, crypto.PII_KEY_ENV) == b"x"
    assert crypto.decrypt_bytes(token) is None
