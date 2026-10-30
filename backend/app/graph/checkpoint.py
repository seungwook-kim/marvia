"""체크포인트 암호화 직렬화기와 허용 목록. 근거: docs/specs/v2-langgraph.md 4절 "마스킹과 암호화" 1.

책임: 그래프 State의 blob(객체로 둔 값, 쓰기 대기 값, 중단 값)을 직렬화하고, 되살릴 때 허용 목록에 있는 타입만 모델로 돌려준다.
H1 범위는 직렬화기뿐이다. 체크포인터 만들기(off/memory/postgres)와 thread_id는 H7에서 이 파일에 더한다.

왜 이렇게 했나:
- 암호화: EncryptedSerializer가 직렬화 결과(bytes)를 FernetCipher로 잠근다. 열쇠는 B/L 열쇠(BL_ENCRYPTION_KEY, crypto.py)를 쓴다.
  langgraph 기본 AES 암호는 pycryptodome이 필요해 쓰지 않는다 (2절 스파이크).
- 열쇠가 없으면 잠글 수 없다. 이때 평문으로 저장하지 않고 예외를 던진다. 열쇠가 없을 때 체크포인터 자체를 만들지 않는 것은 H7의 일이고,
  여기 예외는 그 판단이 새어도 평문이 DB에 가지 않게 하는 마지막 방어선이다 (4절 3, EC-01).
- 허용 목록: 허용하지 않은 타입은 되살릴 때 차단된다. 허용 목록을 명시해 넘기면 LANGGRAPH_STRICT_MSGPACK 환경변수와 상관없이
  목록 밖 타입이 막힌다. 목록은 C1~C3, C5 모델과 판정 출력(JudgeOutput), 기억 문맥(MemoryContext)뿐이고 새 모델은 일부러 더해야 한다.
  C4(mcp_server/schemas.py)는 State에서 dict 그대로 다니므로 넣지 않는다 (state.py position, schedule, distance).
"""
from __future__ import annotations

from langgraph.checkpoint.serde.base import CipherProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from backend.app import crypto
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


def make_serde(allowlist: tuple[type, ...] = MSGPACK_ALLOWLIST) -> EncryptedSerializer:
    """체크포인터에 꽂을 암호화 직렬화기를 만든다 (4절 1).

    allowlist 인자는 테스트가 "모델 하나를 빼면 되살리기가 깨진다"를 보이려고 둔다. 운영 코드는 기본값만 쓴다.
    """
    return EncryptedSerializer(FernetCipher(), JsonPlusSerializer(allowed_msgpack_modules=list(allowlist)))
