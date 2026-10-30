"""체크포인트 암호화 직렬화기와 허용 목록. 근거: docs/specs/v2-langgraph.md 4절 "마스킹과 암호화" 1.

책임: 그래프 State의 blob(객체로 둔 값, 쓰기 대기 값, 중단 값)을 직렬화하고, 되살릴 때 허용 목록에 있는 타입만 모델로 돌려준다.
H1: 직렬화기. H7: 체크포인터 만들기(GRAPH_CHECKPOINT off/memory/postgres/auto), 풀 닫기, 평문 blob 거부. thread_id는 threads.py.

왜 이렇게 했나:
- 암호화: EncryptedSerializer가 직렬화 결과(bytes)를 FernetCipher로 잠근다. 열쇠는 B/L 열쇠(BL_ENCRYPTION_KEY, crypto.py)를 쓴다.
  langgraph 기본 AES 암호는 pycryptodome이 필요해 쓰지 않는다 (2절 스파이크).
- 열쇠가 없으면 잠글 수 없다. 이때 평문으로 저장하지 않고 예외를 던진다. 열쇠가 없을 때 체크포인터 자체를 만들지 않는 것은 H7의 일이고,
  여기 예외는 그 판단이 새어도 평문이 DB에 가지 않게 하는 마지막 방어선이다 (4절 3, EC-01).
- 허용 목록: 허용하지 않은 타입은 되살릴 때 차단된다. 허용 목록을 명시해 넘기면 LANGGRAPH_STRICT_MSGPACK 환경변수와 상관없이
  목록 밖 타입이 막힌다. 목록은 C1~C3, C5 모델과 판정 출력(JudgeOutput), 기억 문맥(MemoryContext)뿐이고 새 모델은 일부러 더해야 한다.
  C4(mcp_server/schemas.py)는 State에서 dict 그대로 다니므로 넣지 않는다 (state.py position, schedule, distance).
- 평문 blob 거부(10절 H7 (11)): 기본 EncryptedSerializer.loads_typed는 타입에 "+"(암호 이름)가 없으면 복호화 없이 그대로 읽는다.
  DB에 누가 평문 blob을 직접 써 넣어도 그래프가 그 State를 되살리게 되므로, 접미사 없는 blob은 예외를 던져 거부한다 (StrictEncryptedSerializer).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from langgraph.checkpoint.serde.base import CipherProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from backend.app import crypto
from backend.app.db import plain_url
from backend.app.graph import threads
from backend.app.observability.logging import log_failure
from backend.app.schemas.agent import (
    AgentRequest,
    AgentResponse,
    AnchorageInfo,
    BerthInfo,
    Conflict,
    CustomsInfo,
    CustomsTiming,
    EtaChange,
    EtaInfo,
    Evidence,
    Position,
    Reason,
    StageElapsed,
    VesselCandidate,
    VesselInfo,
)
from backend.app.schemas.customs import CustomsDetailRow, CustomsRecord, CustomsStage
from backend.app.schemas.signals import Signal, SignalSet
from backend.app.services.llm import CustomsExplanation, JudgeOutput
from backend.app.services.memory import MemoryContext

# crypto.encrypt_bytes가 만든 토큰에 붙이는 암호 이름. 저장 형식은 "<직렬화 종류>+fernet" (EncryptedSerializer)
CIPHER_NAME = "fernet"

# 되살리기를 허용하는 타입 전부 (4절 1). 명시 목록이라 환경변수가 꺼져 있어도 목록 밖 타입은 막힌다.
# State 최상위 값뿐 아니라 collected 같은 dict 안에 든 모델도 같은 목록으로 되살아난다
MSGPACK_ALLOWLIST: tuple[type, ...] = (
    # C2, C1 (schemas/agent.py)
    AgentRequest, AgentResponse, Evidence, Reason, Conflict, VesselCandidate, VesselInfo, Position, EtaInfo,
    CustomsInfo, BerthInfo, AnchorageInfo, StageElapsed, CustomsTiming, EtaChange,
    # C3 (schemas/signals.py)
    Signal, SignalSet,
    # C5 (schemas/customs.py)
    CustomsRecord, CustomsStage, CustomsDetailRow,
    # 판정 LLM 출력과 기억 문맥 (State의 judged, memory)
    JudgeOutput, CustomsExplanation, MemoryContext,
)


class CheckpointCryptoError(RuntimeError):
    """체크포인트를 잠그거나 열지 못했다. 열쇠가 없거나, 다른 열쇠로 잠갔거나, 저장된 바이트가 바뀌었다.
    메시지에는 값을 넣지 않는다(종류만)."""


class FernetCipher(CipherProtocol):
    """B/L 열쇠(BL_ENCRYPTION_KEY)로 blob 바이트를 Fernet으로 잠그고 여는 암호 (4절 1)."""

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        """바이트를 잠가 (암호 이름, 토큰)을 돌려준다. 열쇠가 없으면 평문을 내보내지 않고 예외를 던진다."""
        token = crypto.encrypt_bytes(plaintext)
        if token is None:
            log_failure("graph_checkpoint", "encrypt_failed")
            raise CheckpointCryptoError("checkpoint encrypt failed: no usable key")
        return CIPHER_NAME, token

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        """토큰을 연다. 모르는 암호 이름이거나 열쇠가 다르거나 바뀌었으면 예외를 던진다(조용히 빈 값을 돌려주지 않는다)."""
        if ciphername != CIPHER_NAME:
            log_failure("graph_checkpoint", "unknown_cipher")
            raise CheckpointCryptoError("checkpoint decrypt failed: unknown cipher")
        data = crypto.decrypt_bytes(ciphertext)
        if data is None:
            log_failure("graph_checkpoint", "decrypt_failed")
            raise CheckpointCryptoError("checkpoint decrypt failed: bad key or tampered data")
        return data


class StrictEncryptedSerializer(EncryptedSerializer):
    """암호 이름 접미사("+fernet")가 없는 blob은 되살리지 않고 거부하는 암호화 직렬화기 (10절 H7 (11), 4절 마스킹과 암호화).

    기본 구현은 접미사가 없으면 복호화 없이 그대로 읽는다. 그러면 DB에 평문 blob을 직접 써 넣은 행(손상, 공격)도 State로 되살아난다.
    여기서는 그런 행을 CheckpointCryptoError로 막는다. 읽는 쪽(facade)이 이 예외를 "멈춘 그래프 없음 + 손상 스레드 삭제"로 다룬다 (EC-07)."""

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if "+" not in data[0]:
            log_failure("graph_checkpoint", "plain_blob_refused")
            raise CheckpointCryptoError("checkpoint load refused: blob is not encrypted")
        return super().loads_typed(data)


def make_serde(allowlist: tuple[type, ...] = MSGPACK_ALLOWLIST) -> EncryptedSerializer:
    """체크포인터에 꽂을 암호화 직렬화기를 만든다 (4절 1).

    allowlist 인자는 테스트가 "모델 하나를 빼면 되살리기가 깨진다"를 보이려고 둔다. 운영 코드는 기본값만 쓴다.
    """
    return StrictEncryptedSerializer(FernetCipher(), JsonPlusSerializer(allowed_msgpack_modules=list(allowlist)))


# ── 체크포인터 만들기 (4절 "연결", 3절, EC-01, EC-06) ──
# 모드(GRAPH_CHECKPOINT): off, memory(테스트용 프로세스 메모리), postgres, auto(기본). 모양:
#   off      체크포인터 없음. 되묻기 답은 v1처럼 새 요청이다 (EC-01)
#   memory   InMemorySaver + 같은 암호화 직렬화기. 키 없는 테스트가 재개 흐름을 보려고 쓴다 (열쇠 필요)
#   postgres DATABASE_URL이 필요하다. 열쇠가 없으면 만들지 않는다(원문 저장 금지)
#   auto     DATABASE_URL과 열쇠가 모두 있으면 postgres, 아니면 off. 연결이 안 열려도 off로 계속한다 (EC-06)
# 열쇠가 없으면 어느 값이어도 만들지 않는다 (4절 마스킹 3, EC-01).
MODES = ("auto", "off", "memory", "postgres")
DEFAULT_POOL_MAX = 2          # GRAPH_DB_POOL_MAX 기본값. 무료 풀러 연결 수를 아낀다 (Q8)
POOL_OPEN_TIMEOUT_SEC = 3.0   # 풀을 열 때 첫 연결을 기다리는 상한. db.connect의 연결 대기 상한(DB_CONNECT_TIMEOUT_SEC 기본 3초)과 같은 태도다
POOL_GET_TIMEOUT_SEC = 5.0    # 풀에서 연결 하나를 얻는 상한. 넘으면 PoolTimeout이고 facade가 체크포인터 없이 계속한다 (EC-06)
RETRY_AFTER_FAILURE_SEC = 5.0  # 풀을 못 열었으면 이 시간 동안은 다시 열어 보지 않는다 (db.DB_DOWN_COOLDOWN_SEC와 같은 태도)

_lock = threading.Lock()
_saver: Any = None        # 만들어 둔 체크포인터. None이면 아직이거나 off다
_built_for: str | None = None  # _saver를 만든 모드. 환경이 바뀌면(테스트) 다시 만든다
_pool: Any = None
_failed_until = 0.0


def checkpoint_mode() -> str:
    """GRAPH_CHECKPOINT 값을 읽는다. 비었으면 auto. 모르는 값은 off로 보고 종류만 로그에 남긴다(잘못 적어 저장이 켜지지 않게)."""
    raw = os.environ.get("GRAPH_CHECKPOINT", "").strip().lower() or "auto"
    if raw not in MODES:
        log_failure("graph_checkpoint", "unknown_mode")
        return "off"
    return raw


def _pool_max() -> int:
    """GRAPH_DB_POOL_MAX. 숫자가 아니거나 1 미만이면 기본값."""
    try:
        value = int(os.environ.get("GRAPH_DB_POOL_MAX", "").strip() or DEFAULT_POOL_MAX)
    except ValueError:
        return DEFAULT_POOL_MAX
    return value if value >= 1 else DEFAULT_POOL_MAX


def _effective_mode() -> str:
    """환경에서 실제로 만들 종류: "off", "memory", "postgres". 열쇠·DB 주소가 모자라면 off다 (EC-01)."""
    mode = checkpoint_mode()
    if mode == "off":
        return "off"
    if not crypto.has_key():
        if mode != "auto":
            log_failure("graph_checkpoint", "no_key_checkpointer_off")
        return "off"
    if mode == "memory":
        return "memory"
    if not os.environ.get("DATABASE_URL"):
        if mode == "postgres":
            log_failure("graph_checkpoint", "no_database_url_checkpointer_off")
        return "off"
    return "postgres"


def _open_postgres():
    """풀을 열고 PostgresSaver를 만든다. 열지 못하면 None(로그에 종류만). 풀은 autocommit, prepare_threshold=0, dict_row다(라이브러리 요구).
    레포의 db.connect는 dict_row를 안 쓰고 options를 보내므로 그대로 줄 수 없어 풀을 따로 둔다 (4절 연결)."""
    global _pool
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = None
    try:
        pool = ConnectionPool(
            conninfo=plain_url(os.environ["DATABASE_URL"]), min_size=1, max_size=_pool_max(), timeout=POOL_GET_TIMEOUT_SEC, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "connect_timeout": 3})
        pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_SEC)
        saver = PostgresSaver(pool, serde=make_serde())
    except Exception as e:  # 연결 실패, 풀 시간 초과 모두 같은 처리: 체크포인터 없이 계속한다 (EC-06)
        log_failure("graph_checkpoint", f"pool_open_failed {type(e).__name__}")
        if pool is not None:
            try:
                pool.close()
            except Exception:  # noqa: BLE001 - 닫다가 나는 오류가 진짜 오류를 가리지 않게
                pass
        return None
    _pool = pool
    return saver


def _forget_memory_threads(saver, prefix: str) -> None:
    """메모리 체크포인터에서 prefix로 시작하는 스레드를 지운다. threads.delete_user_threads가 부른다 (AC-14)."""
    for thread in [t for t in list(saver.storage) if t.startswith(prefix)]:
        saver.delete_thread(thread)


def get_checkpointer():
    """체크포인터 하나를 돌려준다. 꺼져 있거나 만들 수 없으면 None. 한 번 만들면 프로세스 안에서 재사용한다.
    환경이 바뀌어 모드가 달라졌으면(테스트) 다시 만든다. 만들다 실패하면 RETRY_AFTER_FAILURE_SEC 동안은 다시 시도하지 않는다."""
    global _saver, _built_for, _failed_until
    mode = _effective_mode()
    with _lock:
        if mode == "off":
            return None
        if _saver is not None and _built_for == mode:
            return _saver
        if mode == "postgres" and time.monotonic() < _failed_until:
            return None
        _close_locked()
        if mode == "memory":
            from langgraph.checkpoint.memory import InMemorySaver

            _saver = InMemorySaver(serde=make_serde())
            threads.on_user_delete(lambda prefix, saver=_saver: _forget_memory_threads(saver, prefix))
        else:
            _saver = _open_postgres()
            if _saver is None:
                _failed_until = time.monotonic() + RETRY_AFTER_FAILURE_SEC
                return None
        _built_for = mode
        return _saver


def _close_locked() -> None:
    """만들어 둔 체크포인터와 풀을 닫는다(_lock을 잡은 채로 부른다)."""
    global _saver, _built_for, _pool
    _saver, _built_for = None, None
    if _pool is not None:
        try:
            _pool.close()
        except Exception as e:  # noqa: BLE001 - 닫기 실패가 종료를 막으면 안 된다
            log_failure("graph_checkpoint", f"pool_close_failed {type(e).__name__}")
        _pool = None


def close_checkpointer() -> None:
    """풀을 닫고 만들어 둔 체크포인터를 버린다. main.py lifespan이 서버가 꺼질 때 부르고, 테스트가 환경을 바꾸며 부른다."""
    global _failed_until
    with _lock:
        _close_locked()
        _failed_until = 0.0
