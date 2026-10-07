"""화물 비서 기억 API 계약 (docs/specs/memory.md "API").

책임: GET·DELETE /api/memory, DELETE /api/memory/items/{id}의 모양을 고정한다.
서류에서 읽은 B/L 조건은 services/doc_facts.py가 자동 저장한다.
화면(web/lib/types.ts)이 이 모양을 그대로 쓴다. 필드를 바꾸면 프론트가 같이 깨진다 (CLAUDE.md 계약 파일 규칙).
원본 B/L은 응답에 넣지 않는다. 화면에는 가린 번호만 간다 (SDD 3.5).
화물 지문(cargo_key, HMAC)은 확인 대기 적용·무시 주소용 cargo_ref 칸으로만 준다 (10-08 QA F5, 화물 현황 표 API와 같은 값).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.agent import Verdict

# 값 목록은 마이그레이션 015 CHECK, services/memory.py ROLES·SOURCES와 같다
Role = Literal["포워더", "관세사", "수출자", "판매처"]
Kind = Literal["preference", "schedule", "contact", "worry", "todo"]
OriginalBl = Literal["required", "surrendered"]
FreightTerm = Literal["prepaid", "collect"]
Incoterm = Literal["CIF", "FOB", "CFR"]


class MemoryItem(BaseModel):
    """메모장 항목 하나. id는 지우기 버튼용이고 화면 글에는 쓰지 않는다 (Spec "번호 없는 마크다운")."""

    id: str
    section: Literal["about", "cargo"]
    kind: Kind
    fields: dict[str, str | bool | None]
    noted_at: str | None
    cargo_name: str | None = Field(description="화물별 항목이면 화물 이름(이름표 또는 가린 번호). 이 사람에 대해 항목은 null")


class PendingChange(BaseModel):
    """대화에서 나온 화물 값 변경. 바로 적용하지 않고 확인 대상으로만 둔다 (Spec 쓰기 흐름 4)."""

    field: Literal["sale_due_date", "forwarder_eta", "label"]
    value: str
    noted_at: str | None = None


class CargoSummary(BaseModel):
    """화물 표 한 줄 요약. 원본 B/L·지문·배 MMSI는 넣지 않는다."""

    bl_masked: str
    bl_type: Literal["MASTER", "HOUSE"] | None  # 서류만 넣고 아직 조회 전인 줄은 구분을 모른다
    label: str | None
    status: Literal["active", "done"]
    sale_due_date: date | None
    forwarder_eta: date | None
    original_bl: OriginalBl | None
    to_order_bank: bool | None
    freight_term: FreightTerm | None
    incoterm: Incoterm | None
    last_verdict: Verdict | None
    last_checked_at: datetime | None
    last_days_margin: int | None
    last_eta: date | None
    last_stage: str | None
    pending: list[PendingChange]
    # 선택: B/L 조건을 서류 사진에서 확인 없이 읽어 저장했으면 그 시각과 서류 종류.
    # 화면이 조건 옆에 "서류 사진에서 읽음(확인 전)"을 붙인다. 비어 있으면 사용자가 확인한 값이거나 조건이 없다
    doc_facts_at: datetime | None = None
    doc_facts_source: Literal["B/L", "A/N", "document"] | None = None
    summary: str | None = Field(description="끝난 화물의 한 줄 요약 (코드가 만든다). 진행 중이면 null")
    # 10-08 QA F5: 확인 대기 값을 적용·무시하는 API 주소에 쓰는 화물 지문. 화물 현황 표 API(/api/cargo/sheet/{cargo_key})가
    # 이미 쓰는 값과 같다. B/L의 HMAC이라 되돌릴 수 없고 원본 B/L은 여전히 응답에 없다
    cargo_ref: str | None = None


class MemoryResponse(BaseModel):
    markdown: str = Field(description="번호 없는 메모장 (Spec 템플릿)")
    items: list[MemoryItem]
    cargo: list[CargoSummary]
    full: bool = Field(description="상한을 넘어 새 기억을 저장하지 못했다 (EC-06). 정리하면 다시 false")
    available: bool = Field(default=True, description="DB를 못 읽었으면 false. 빈 기억과 구분한다")
    used_chars: int = Field(default=0, description="메모장 글자 수 (markdown 길이, 상한 판단과 같은 기준)")
    max_chars: int = Field(default=3000, description="메모장 글자 상한 (services/memory.MAX_CHARS)")
    # 10-08 QA F11·F12. 기본값이 있어 옛 화면은 그대로 돈다
    pruned_count: int = Field(default=0, description="상한을 넘어 코드가 마지막으로 자동 정리한 항목 수 (완료된 할 일·끝난 화물 걱정)")
    pruned_at: datetime | None = Field(default=None, description="그 자동 정리를 한 시각. 정리한 적이 없으면 null")
    can_undo: bool = Field(default=False, description="직전 버전으로 되돌릴 수 있나 (POST /api/memory/undo)")


class MemoryDeleteResponse(BaseModel):
    deleted: int  # 지운 줄 수 (제36조 ②, 결과를 알린다)


class MemoryUndoResponse(BaseModel):
    """POST /api/memory/undo. restored가 true면 메모장이 직전 버전으로 돌아갔다. 한 번 더 부르면 되돌리기를 취소한다."""

    restored: bool


class PendingResolveResponse(BaseModel):
    """확인 대기 값 하나를 적용(applied)하거나 무시(dismissed)한 결과 (10-08 QA F5)."""

    result: Literal["applied", "dismissed"]
