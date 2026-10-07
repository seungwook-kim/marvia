"""화물 비서 기억 API 계약 (docs/specs/memory.md "API", 10-02 결정 6·7). 계약 C1~C8 밖의 새 파일이다.

책임: GET·DELETE /api/memory, DELETE /api/memory/items/{id}의 모양을 고정한다.
확인 화면용 POST /api/memory/document-facts(DocumentFactsRequest·Response)는 10-08 사용자 결정으로 없앴다 (services/doc_facts.py가 자동 저장).
화면(web/lib/types.ts)이 이 모양을 그대로 쓴다. 필드를 바꾸면 프론트가 같이 깨지므로 PR 리뷰를 거친다 (CLAUDE.md 계약 파일 규칙).
원본 B/L과 화물 지문(cargo_key)은 응답에 넣지 않는다. 화면에는 가린 번호만 간다 (SDD 3.5).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.agent import Verdict

# 값 목록은 마이그레이션 015 CHECK, services/memory.py ROLES·SOURCES와 같다 (10-02 결정 5·6)
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
    bl_type: Literal["MASTER", "HOUSE"] | None  # 서류만 넣고 아직 조회 전인 줄은 구분을 모른다 (024, 10-08)
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
    # 10-08 사용자 결정 (additive, 선택): B/L 조건을 서류 사진에서 확인 없이 읽어 저장했으면 그 시각과 서류 종류 (026).
    # 화면이 조건 옆에 "서류 사진에서 읽음(확인 전)"을 붙인다. 비어 있으면 예전 확인 화면에서 확인한 값이거나 조건이 없다
    doc_facts_at: datetime | None = None
    doc_facts_source: Literal["B/L", "A/N", "document"] | None = None
    summary: str | None = Field(description="끝난 화물의 한 줄 요약 (코드가 만든다). 진행 중이면 null")


class MemoryResponse(BaseModel):
    markdown: str = Field(description="번호 없는 메모장 (Spec 템플릿)")
    items: list[MemoryItem]
    cargo: list[CargoSummary]
    full: bool = Field(description="상한을 넘어 새 기억을 저장하지 못했다 (EC-06). 정리하면 다시 false")
    available: bool = Field(default=True, description="DB를 못 읽었으면 false. 빈 기억과 구분한다")
    used_chars: int = Field(default=0, description="메모장 글자 수 (markdown 길이, 상한 판단과 같은 기준)")
    max_chars: int = Field(default=3000, description="메모장 글자 상한 (services/memory.MAX_CHARS)")


class MemoryDeleteResponse(BaseModel):
    deleted: int  # 지운 줄 수 (제36조 ②, 결과를 알린다)
