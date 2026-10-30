"""판정 그래프의 State(AgentState)와 평문 허용 목록(PLAINTEXT_FIELDS). 근거: docs/specs/v2-langgraph.md 2절 State, 4절 체크포인트.

책임: 노드가 주고받는 값의 이름과 타입을 한 곳에 고정한다. 필드 이름은 C1~C5 계약(schemas/*)의 이름을 그대로 쓴다.

왜 평문 허용 목록이 있나:
- PostgresSaver는 State 최상위 값이 None, str, int, float, bool이면 checkpoints.checkpoint(JSONB)에 평문으로 넣고,
  그 밖의 값(객체, 리스트, dict, 날짜)만 checkpoint_blobs에 직렬화기를 거쳐 넣는다. 암호화 직렬화기는 blob만 잠근다 (4절).
- 그래서 B/L, 컨테이너 번호, 메모 문장, LLM이 쓴 문장 같은 식별 값을 최상위 str/int/float/bool 필드에 두면 암호화가 소용없다.
  이런 값은 객체(C1~C5 모델, 위의 Lines 같은 리스트) 안에 둔다.
- 최상위 str/int/float/bool(또는 그 Optional) 필드는 PLAINTEXT_FIELDS에 이름이 있는 것만 허용한다.
  어긴 필드가 생기면 backend/tests/test_graph_state.py가 실패한다 (AC-09).

리듀서는 path 하나뿐이다(operator.add). 나머지는 덮어쓰기라서 지난 턴의 근거가 따라오지 않는다 (2절).
이 파일은 langgraph를 import하지 않는다. TypedDict와 operator.add만 쓰므로 키 없이, 그래프 없이 테스트된다.
"""
from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Annotated, Literal, TypedDict

from backend.app.schemas.agent import (
    AgentRequest,
    AgentResponse,
    AnchorageInfo,
    EtaChange,
    Verdict,
    VesselCandidate,
    VesselMatch,
)
from backend.app.schemas.customs import CustomsRecord
from backend.app.schemas.signals import SignalSet
from backend.app.services.llm import JudgeOutput
from backend.app.services.memory import MemoryContext

# LLM이나 사용자가 쓴 문장 조각을 담는 칸. 최상위 str로 두면 평문 컬럼으로 가므로 리스트로 감싼다(문장 하나면 원소 1개).
# 스펙 표는 judge_feedback, follow_feedback, port_note를 str로 적었지만 "평문 허용 아니오"와 AC-09가 함께 성립하려면 이 모양이어야 한다
Lines = list[str]

# identify가 도구 장애를 알리는 내부 값. 응답으로 나가지 않는다 (routers/agent.TOOL_FAILED, 2절 표 vessel_match 행)
VesselMatchOrFailed = VesselMatch | Literal["tool_failed"]


class AgentState(TypedDict, total=False):
    """판정 그래프 State. total=False라 노드는 자기가 바꾸는 칸만 돌려준다 (부분 갱신).

    칸마다 "누가 쓰나"는 v2-langgraph.md 2절 State 표가 원본이다. 여기서는 타입만 고정한다.
    """

    # ── 입력과 요청 문맥 ──
    request: AgentRequest              # B/L 원본이 들어 있다. 객체라 blob으로 가서 암호화된다
    judged_request: AgentRequest       # 저장된 판매 예정일을 채운 요청(load_saved). 판정과 이력 저장이 쓴다
    saved_due: date | None             # load_saved. 저장된 판매 예정일 한 줄(_with_saved_due_note)용
    user_id: str | None                # Supabase 무작위 UUID. 이미 Langfuse와 DB에 남는 값
    bl_masked: str                     # mask_bl 결과만 (SDD 3.5). 원본 B/L은 여기 두지 않는다
    previous_eta: date | datetime | None  # ContextVar routers/agent.previous_eta에서 옮김
    trusted_vessel: int | None         # ContextVar routers/agent.trusted_vessel에서 옮김. MMSI라 식별 값이 아니다
    db_failed: bool                    # ContextVar routers/agent._db_state에서 옮김 (db_scope가 풀고 되받는다)
    resumable: bool                    # 체크포인터가 붙어 있고 thread_id를 만들 수 있으면 True
    memory: MemoryContext | None       # load_memory. 메모에 전화번호가 있을 수 있어 문자열 칸으로 두지 않는다

    # ── 통관 조회 ──
    customs: CustomsRecord | None      # fetch_customs. 컨테이너 번호, 품명 포함이라 blob 안에서 암호화된다
    bl_type_used: Literal["MASTER", "HOUSE"] | None
    failure: AgentResponse | None      # 세관 조회 실패 응답

    # ── 식별 ──
    vessel_match: VesselMatchOrFailed
    candidates: list[VesselCandidate]
    mmsi: int | None
    call_sign: str | None
    rejected: bool                     # 사용자가 고른 배를 후보에서 확인하지 못함

    # ── 수집과 신호 (C4 도구 출력은 dict 그대로) ──
    position: dict | None
    schedule: dict | None
    distance: dict | None
    position_history: list[dict]
    signal_set: SignalSet              # C3
    trusted_eta: datetime | None
    days_margin: int | None            # 코드만 쓴다. LLM이 낸 값은 덮어쓴다 (SDD 5.8)
    anchored: AnchorageInfo | None     # 코드가 센 지난 사실 3종
    eta_moved: EtaChange | None
    port_note: Lines | None
    collected: dict                    # AgentResponse 칸들(판정이 붙든 안 붙든 같이 나가는 값)
    has_position: bool
    from_cache: bool

    # ── 판정 ──
    table_verdict: Verdict
    gray: bool
    judge_attempt: int                 # 판정 시작 때 0, 호출마다 +1 (덮어쓰기)
    judge_feedback: Lines | None       # LLM이 쓴 문장 조각이 들어 있다
    judge_spent_sec: float             # 시간 예산용 누적 (2절 "시간 예산")
    judge_last_sec: float
    judged: JudgeOutput | None         # LLM이 쓴 설명 칸만
    violations: list[dict]             # {rule, detail}
    judge_route: Literal["pass", "retry", "give_up"]

    # ── 후속 질문 ──
    previous: AgentResponse | None     # 후속 질문이 답할 지난 결과
    follow_attempt: int
    follow_feedback: Lines | None      # LLM이 쓴 문장 조각이 들어 있다
    follow_route: Literal["pass", "retry", "give_up"]
    # 아래 셋은 answer_follow_up이 쓰고 validate_follow_up이 읽는 한 시도분(스펙 State 표에 없는 칸, H5)
    follow_answer: Lines | None        # LLM이 쓴 답 문장(컨테이너 번호표 포함). 문장 하나가 원소 1개
    follow_tool_calls: list[dict]      # LLM이 부른 도구와 결과. 통과한 답의 근거 카드가 된다
    follow_candidates_used: int        # 후보 검색 도구를 이 질문에서 부른 횟수(시도를 넘어 센다, agent._capped_follow_up_tool)

    # ── 되묻기와 결과 ──
    awaiting: bool                     # 되묻기 대기 중 (ask_prepare가 켜고 ask_wait가 재개 때 내린다)
    resumed: bool                      # 멈췄다 이어 간 실행 (ask_wait가 올린다). decorate가 처리 이력을 다시 받는 조건 (EC-15)
    response: AgentResponse | None     # 그래프의 결과

    # 지나간 노드 이름. 유일한 누적 리듀서. 새로 돌 때 스레드를 지우므로 지난 실행이 따라오지 않는다 (2절)
    path: Annotated[list[str], operator.add]


# 평문 컬럼(checkpoints.checkpoint JSONB)에 그대로 들어가도 되는 최상위 값의 이름 (2절 State 표의 "평문 허용: 예").
# 식별 값(B/L, 컨테이너 번호, 메모, LLM 문장)은 여기 넣지 않는다. 새 최상위 str/int/float/bool 필드는
# 식별 값이 아님을 확인한 뒤에만 더한다 (test_graph_state.py가 어긋남을 잡는다).
PLAINTEXT_FIELDS: frozenset[str] = frozenset({
    "saved_due", "user_id", "bl_masked", "previous_eta", "trusted_vessel", "db_failed", "resumable",
    "bl_type_used", "vessel_match", "mmsi", "call_sign", "rejected",
    "trusted_eta", "days_margin", "has_position", "from_cache",
    "table_verdict", "gray",
    "judge_attempt", "judge_spent_sec", "judge_last_sec", "judge_route",
    "follow_attempt", "follow_route", "follow_candidates_used", "awaiting", "resumed",
    "path",
})
