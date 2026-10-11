"""계약 C1(AgentResponse)·C2(AgentRequest). 근거: SDD 3.1, 3.2.

책임: 프론트와 백엔드가 주고받는 요청·응답의 모양을 고정한다.
- 첫 조회, 되묻기 답, 후속 질문이 전부 같은 AgentRequest 하나로 들어온다 (SDD 4.3).
- 응답은 성공·실패 모두 같은 AgentResponse 틀이고 status로 갈린다 (SDD 8절).
- 여기 필드를 바꾸면 평가셋(C7)·프론트·fixture가 같이 깨진다 (CLAUDE.md).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .customs import CustomsDetailRow

# ---- 공용 열거값 (SDD 3.2) ----
Verdict = Literal["fine", "watch", "act"]
# unsupported: 지원 범위 밖 요청(부산 외 항만 등). 외부 조회 없이 끝낸다. failed(처리 실패)와 구분한다.
Status = Literal["ok", "partial", "insufficient", "failed", "unsupported"]
VesselMatch = Literal["confirmed", "uncertain", "none"]
Freshness = Literal["fresh", "stale", "unknown"]

# B/L 번호 형식: 영문 대문자+숫자 8~20자 (SDD 3.1). 공백은 검증 전에 제거한다.
_BL_PATTERN = re.compile(r"^[A-Z0-9]{8,20}$")
# 검증 전에 지우는 글자: 공백과 하이픈 모양 글자. 서류·메일에서 "ABCD-1234567"처럼 끊어 적은 번호를 그대로 붙여 넣으면
# 422가 났다 (10-08 QA B11). UNI-PASS 번호에는 하이픈이 없어 지워도 다른 번호가 되지 않는다.
# 하이픈(-), 유니코드 하이픈·대시류(U+2010~U+2015), 빼기 기호(U+2212)
_DASHES = "".join(chr(c) for c in (*range(0x2010, 0x2016), 0x2212))  # 유니코드 하이픈·대시류와 빼기 기호. 글자를 파일에 직접 쓰지 않으려고 번호로 만든다
_BL_SEPARATORS = re.compile(r"[\s\-" + re.escape(_DASHES) + "]+")
_MRN_PATTERN = re.compile(r"^[0-9]{2}[A-Z0-9]{8}[IE]$")  # 적하목록 관리번호 (docs/specs/mrn-fallback.md)
# 항차 번호. 배 고르기 LLM(llm.pick_vessel) 프롬프트에 그대로 들어가므로 자유 글을 막아 지시문 주입을 차단한다.
# 실제 항차 모양(0925W, 2634E, 108A, 045S/N, FE-12)에 맞춰 영문·숫자로 시작하고 영문·숫자·공백·하이픈·슬래시·점만, 20자까지
VOYAGE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ./-]{0,19}$"
# MMSI는 9자리 숫자다(ITU). 화면이 보내는 값은 후보 목록의 MMSI라 이 범위를 넘을 수 없다.
# 범위 밖 값(10^20 등)은 DB BIGINT 칸(user_cargo.vessel_mmsi)에 들어가지 못해 뒤 작업이 NumericValueOutOfRange로 죽었다 (10-08 QA B1).
# 형식 경계라 rules.md(판정 재료)가 아니라 여기 둔다
MMSI_MIN, MMSI_MAX = 1, 999_999_999
# 세션 ID 길이 상한. 화면은 crypto.randomUUID()(36자), 지켜보기는 watch-YYYYMMDD, 데모는 marvia-demo-session을 쓴다.
# 이력·트레이스·사용량 이름에 그대로 들어가는 값이라 끝없이 긴 값을 받지 않는다
SESSION_ID_MAX = 128


class AgentRequest(BaseModel):
    """C2. 필수 4개 + 선택 4개. 선택 칸이 채워졌는지로 첫 조회/되묻기 답/후속 질문을 구분한다."""

    bl_no: str = Field(description="B/L 번호. 영문+숫자 8~20자, 공백·하이픈 제거")
    # B/L 구분. None이면 "모름"이다. 사용자는 자기 B/L이 마스터인지 하우스인지 모르는 경우가 많다.
    # 모르면 하우스, 마스터 순서로 UNI-PASS를 조회하고, 정했으면 그 구분 먼저 그다음 반대쪽을 본다 (routers/agent._fetch_customs)
    bl_type: Literal["MASTER", "HOUSE"] | None = None
    # 선택 입력. 필수는 B/L 하나다. 품목에 따라 판매일을 정하지 않는 경우도 많아서,
    # 없으면 여유 일수 없이 배 상태만으로 판정한다 (skills/cargo-verdict/rules.md 판정 표)
    sale_due_date: date | None = Field(default=None, description="판매 예정일 (필요 입고일). 선택")
    # 빈 세션 ID는 받지 않는다. 이력·사용량 이름이 비면 다른 요청과 섞인다 (10-08 QA B5)
    session_id: str = Field(min_length=1, max_length=SESSION_ID_MAX)
    follow_up_question: str | None = Field(default=None, max_length=300, description="후속 질의. 300자 제한 (SDD 7절)")
    resolved_mmsi: int | None = Field(default=None, ge=MMSI_MIN, le=MMSI_MAX,
                                      description="되묻기 후 사용자가 고른 선박. MMSI 9자리 이하 양수")
    voyage_no: str | None = Field(default=None, max_length=20, pattern=VOYAGE_PATTERN,
                                  description="되묻기 후 입력. 영문·숫자·공백·-·/·. 20자까지")
    forwarder_eta: date | None = None
    # 적하목록 관리번호(예: 26ABCD1234I). UNI-PASS에 B/L이 아직 없을 때 해수부 입항 신고로 배를 찾는 데만 쓴다 (docs/specs/mrn-fallback.md)
    mrn: str | None = Field(default=None, description="MRN 11자. 선택")
    # 서류 사진에서 사용자가 확인한 출항일. 내 화물 현황 표 출항 칸(user_cargo.departed_on, source 'document')에 적는 데만 쓴다
    # (services/cargo.upsert_assessment). 판정·되묻기 구분·LLM 입력에는 쓰지 않는다. 사용자가 직접 적은 날짜(manual)는 덮지 않는다
    departed_on: date | None = Field(default=None, description="서류 사진에서 읽은 출항일")

    @field_validator("follow_up_question", mode="before")
    @classmethod
    def _trim_question(cls, v):
        """앞뒤 공백을 떼고, 비면 None. 공백뿐인 질문을 후속 질문으로 받아 LLM을 부르지 않게 한다 (10-08 QA B5).
        None이면 첫 조회로 처리된다."""
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("resolved_mmsi", mode="before")
    @classmethod
    def _no_bool_mmsi(cls, v):
        """true/false는 파이썬에서 1/0이라 MMSI 1로 받아진다. 숫자가 아닌 값은 받지 않는다 (10-08 QA B1)."""
        if isinstance(v, bool):
            raise ValueError("resolved_mmsi는 숫자여야 합니다")
        return v

    @field_validator("voyage_no", mode="before")
    @classmethod
    def _trim_voyage(cls, v):
        """앞뒤 공백을 떼고, 비면 None. 화면이 빈 칸을 ""로 보내도 422가 나지 않게 한다."""
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("mrn")
    @classmethod
    def _normalize_mrn(cls, v: str | None) -> str | None:
        """공백 제거·대문자. 모양이 아니면 422 (EC-01). 연도 2자리 + 영숫자 8자 + 입항 I/출항 E"""
        if v is None or not v.strip():
            return None
        v = re.sub(r"\s+", "", v).upper()
        if not _MRN_PATTERN.match(v):
            raise ValueError("mrn은 11자(예: 26ABCD1234I)여야 합니다")
        return v

    @field_validator("bl_no")
    @classmethod
    def _normalize_bl(cls, v: str) -> str:
        """공백·하이픈 제거·대문자화 뒤 형식 검사. 형식 오류는 라우터가 400으로 돌려준다 (SDD 8절).
        하이픈은 공백처럼 끊어 적는 표기라 지운다 (10-08 QA B11, _BL_SEPARATORS)."""
        v = _BL_SEPARATORS.sub("", v).upper()
        if not _BL_PATTERN.match(v):
            raise ValueError("bl_no는 영문+숫자 8~20자여야 합니다")
        return v


class Evidence(BaseModel):
    """근거 카드 1장. 모든 판정 숫자는 여기 있는 값이어야 한다 (validate V3·V6·V7)."""

    source: Literal["shipfinder", "unipass", "mof", "searoute", "rule"]
    field: str
    value: str
    fetched_at: datetime


class Reason(BaseModel):
    """켜진 1층 규칙. rule은 rules.md의 규칙 id (SDD 3.3 Signal.id와 같은 집합)."""

    rule: str
    value: str
    triggered: bool


class Conflict(BaseModel):
    """신호가 어긋났을 때 어느 출처를 왜 골랐나. chosen은 evidence[].source에 있어야 한다 (V6)."""

    # 1개 이상. eta_conflict처럼 신호 하나가 그 자체로 "AIS와 해수부가 다르다"는 상충인 경우가 있다.
    # 구조화 출력(strict)은 minItems를 LLM에 전하지 못해 더 엄격한 조건은 맞는 답도 탈락시킨다
    signals: list[str] = Field(min_length=1, description="상충한 신호 id 1개 이상")
    chosen: str
    why: str


class VesselCandidate(BaseModel):
    """되묻기 때 사용자에게 보여줄 후보 1척."""

    mmsi: int
    imo: int | None
    name: str
    flag: str | None
    dest: str | None
    last_time: datetime
    ship_type: int | None
    match_reasons: list[str]


class VesselInfo(BaseModel):
    """식별이 끝난 배. 프론트 VesselCard가 그대로 그린다 (SDD 3.2)."""

    mmsi: int
    imo: int | None
    name: str
    call_sign: str | None = Field(description="해수부 실적 매칭 키")
    flag: str | None = Field(description="국적. UNI-PASS shipNat 우선, 없으면 ShipFinder")
    ship_type: int | None
    voyage_no: str | None = Field(description="UNI-PASS vydf 또는 사용자 입력")


class Position(BaseModel):
    """위치. freshness는 코드가 계산한다 (SDD 5.4). LLM 값은 덮어쓴다."""

    lat: float
    lng: float
    sog: float
    cog: float
    navistat: int
    last_time: datetime
    freshness: Freshness


class EtaInfo(BaseModel):
    """ETA 4종. trusted_eta는 rules.md priority 순서로 코드가 고른다 (SDD 5.8)."""

    ais_eta: datetime | None
    mof_eta: datetime | None
    calc_eta: datetime | None
    trusted_eta: datetime | None
    distance_nm: float | None


class CustomsInfo(BaseModel):
    """통관 단계와 해설. plain_explanation은 LLM이 쓰고 judge가 채점한다 (SDD 5.12)."""

    stage_code: str
    stage_name: str
    plain_explanation: str
    next_stage: str | None
    needs_broker: bool
    history: list[dict] = Field(default_factory=list, description="{code, name, at}")
    # 화물이 어디서 어디로 가나. UNI-PASS 값 그대로 (코드가 채움, LLM 아님). 없으면 null
    loading_port: str | None = Field(default=None, description="적재항 코드 ldprCd (예: CNTAO)")
    loading_port_name: str | None = Field(default=None, description="적재항 이름 ldprNm (예: Qingdao)")
    loading_country: str | None = Field(default=None, description="적출국 코드 lodCntyCd (예: CN)")
    discharge_port: str | None = Field(default=None, description="양륙항 코드 dsprCd (예: KRPUS)")
    discharge_port_name: str | None = Field(default=None, description="양륙항 이름 dsprNm (예: 부산항)")
    # UNI-PASS 값 그대로 (코드가 채움), 없으면 null. container_no는 LLM에 넘기지 않고 화면·엑셀에만 쓴다
    forwarder_name: str | None = Field(default=None, description="포워더명 frwrEntsConm")
    carrier_name: str | None = Field(default=None, description="선사·항공사 shcoFlco")
    container_no: str | None = Field(default=None, description="컨테이너 번호 cntrNo. 여러 개면 ', '로 이은 한 문자열")
    goods_name: str | None = Field(default=None, description="품명 prnm. 적하목록 영어 그대로")


class BerthInfo(BaseModel):
    facility: str | None = Field(default=None, description="해수부 fcltyNm")
    shed: str | None = Field(default=None, description="UNI-PASS shedNm")


class AnchorageInfo(BaseModel):
    """해수부 관제 이벤트로 코드가 센 묘박지 대기 사실 (services/port_facts.anchorage). 판정 신호가 아니라 등급에 안 쓴다.
    지난 일만 담는다. 앞으로 얼마나 더 기다릴지는 없다."""

    anchored_at: datetime = Field(description="가장 최근 투묘(닻 내림) 시각")
    weighed_at: datetime | None = Field(default=None, description="그 뒤 양묘(닻 올림) 시각. 아직이면 null")
    berthed_at: datetime | None = Field(default=None, description="그 뒤 접안 시각. 아직이면 null")
    state: Literal["anchored", "weighed", "berthed"] = Field(description="anchored: 아직 대기 중, weighed: 닻을 올림, berthed: 접안함")
    waited_hours: int = Field(description="투묘부터 양묘·접안(없으면 지금)까지 정수 시간")


class StageElapsed(BaseModel):
    """이미 처리된 통관 단계 두 개 사이에 걸린 시간 (services/port_facts.customs_timing). 시각은 UNI-PASS 값 그대로(한국 시각)."""

    from_stage: str
    to_stage: str
    from_at: datetime
    to_at: datetime
    hours: int = Field(description="정수 시간. 같은 시각에 처리됐으면 0")


class CustomsTiming(BaseModel):
    """통관 이력의 지난 소요 시간. 앞으로 걸릴 시간은 예측하지 않는다 (PRD 4절)."""

    steps: list[StageElapsed] = Field(default_factory=list)
    arrived_at: datetime | None = Field(default=None, description="입항보고 수리 처리 시각. 없으면 null")
    hours_since_arrival: int | None = Field(default=None, description="입항부터 지금(반출됐으면 반출)까지 정수 시간")
    measured_until: Literal["now", "released"] | None = Field(default=None, description="hours_since_arrival을 어디까지 셌나")


class EtaChange(BaseModel):
    """지난 조회 때 저장한 신뢰 ETA 날짜와 이번 신뢰 ETA 날짜가 기준(rules.md eta_change_alert_hours) 이상 달라졌다.
    from·to는 파이썬 예약어라 from_eta·to_eta로 둔다. hours는 늦어지면 양수, 당겨지면 음수."""

    from_eta: date
    to_eta: date
    hours: int


class AgentResponse(BaseModel):
    """C1. 성공·실패 모두 같은 틀. status가 ok/partial/insufficient/failed/unsupported를 가른다 (SDD 8절).

    - insufficient면 verdict는 None이고 missing_inputs 또는 vessel_candidates가 채워진다 (V4·V5).
    - days_margin은 코드 계산값. LLM이 다른 값을 내도 evidence.py가 덮어쓴다 (SDD 5.8).
    """

    status: Status
    verdict: Verdict | None
    vessel_match: VesselMatch
    vessel_candidates: list[VesselCandidate] = []
    missing_inputs: list[str] = []
    reasons: list[Reason] = []
    conflicts: list[Conflict] = []
    actions: list[str] = []
    days_margin: int | None
    decided_by: Literal["rule", "llm"] | None
    vessel: VesselInfo | None
    position: Position | None
    eta: EtaInfo | None
    customs: CustomsInfo | None
    berth: BerthInfo | None
    evidence: list[Evidence]
    answer: str
    trace_id: str
    # UNI-PASS 처리 이력 원문, 화면 전용 (docs/specs/customs-detail.md). 라우터가 판정 뒤에 붙이고,
    # 이력 저장(routers/agent.py _logged)과 후속 질문 LLM 입력(llm.answer_follow_up)에서는 뺀다
    customs_detail: list[CustomsDetailRow] = Field(default_factory=list)
    # 모두 코드가 센 지난 사실이고 선택 칸이라 옛 응답·이력 JSON도 그대로 읽힌다. 등급에는 쓰지 않는다 (services/port_facts.py)
    anchorage: AnchorageInfo | None = Field(default=None, description="해수부 관제 이벤트로 센 묘박지 대기")
    customs_timing: CustomsTiming | None = Field(default=None, description="통관 단계 사이 걸린 시간·입항 뒤 지난 시간")
    eta_change: EtaChange | None = Field(default=None, description="지난 조회 대비 도착 예정 변화")
    # UNI-PASS에서 실제로 찾은 B/L 구분. 요청 구분을 몰라도(None) 하우스·마스터를 차례로 조회하므로 어느 쪽에서 찾았는지
    # 화면·채팅이 말할 수 있게 둔다. 통관 정보를 못 받았으면(없음·실패·MRN 대체) None. 선택 칸이라 옛 이력 JSON도 그대로 읽힌다
    bl_type_used: Literal["MASTER", "HOUSE"] | None = Field(default=None, description="UNI-PASS에서 찾은 B/L 구분")
