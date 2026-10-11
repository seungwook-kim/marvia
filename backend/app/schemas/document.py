"""서류 사진 응답 계약 (docs/specs/document-extract.md "How").

책임: POST /api/document가 돌려주는 모양을 고정한다. 화면이 이 모양을 그대로 그린다.
판정 요청(AgentRequest)으로 바로 넘기지 않는다. 필드를 바꾸면 프론트가 같이 깨진다 (CLAUDE.md 계약 파일 규칙).
채팅 입력창 사진 첨부는 bl_status가 ok일 때만 확인 화면 없이 바로 조회한다.
B/L 조건(원본 필요·은행 지시식·운임 조건), 인코텀즈, 화물 인도 연락처 칸은 서류에 없는 경우가 흔해서
비어도 missing을 걸지 않고, 값이 있을 때만 검사(not_in_text, format, ambiguous)를 건다.
로그인 사용자의 사진이고 B/L이 검사를 통과하면 검사를 통과한 칸만 서버가 바로 저장하고
저장한 칸 이름을 facts_saved로 알린다 (services/doc_facts.py).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 검사 이름은 services/doc_ocr.check_fields와 같다. 빈 목록이면 통과
Check = Literal["missing", "not_in_text", "format", "ambiguous"]  # ambiguous: 같은 칸 이름표가 두 번 이상
# 확인이 필요한 칸으로 알릴 수 있는 칸 이름 (bl_no 뺀 DocumentFields 칸, routers/document.FLAGGABLE_FIELDS와 같다)
FlaggedField = Literal["vessel", "voyage", "eta", "etd", "pol", "pod", "item", "original_bl", "to_order_bank",
                       "freight_term", "incoterm", "contact_company", "contact_role", "contact_phone"]


class FieldResult(BaseModel):
    value: str | None = Field(description="서류에서 읽은 값. 못 읽었으면 null")
    checks: list[Check] = Field(description="걸린 검사. 비어 있으면 통과")


class DocumentFields(BaseModel):
    bl_no: FieldResult    # 코드가 이름표 근처에서 뽑는다. LLM은 보지 않는다
    vessel: FieldResult
    voyage: FieldResult
    eta: FieldResult      # YYYY-MM-DD
    pol: FieldResult      # 선적항
    pod: FieldResult      # 양하항
    item: FieldResult     # 품명. 사용자 서류에서 읽은 값이며 저장하지 않는다 (Spec "규칙과의 관계")
    # 출항일 YYYY-MM-DD. 도착 통지서의 E.T.D. 또는 B/L의 Shipped/Laden on board 날짜를 서류에서 옮긴 값이다
    # (계산하지 않는다). 화물 시트 출항 칸에 채운다. 서류에 없는 경우가 흔해서 비어도 missing을 걸지 않고, 값이 있을 때만
    # 날짜 검사를 건다. 입항일(eta)보다 늦으면 format (services/doc_ocr.check_fields). 기본값을 둬서 etd 이전 모양의 응답도 읽힌다
    etd: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                             description="출항일 YYYY-MM-DD. 서류에 적힌 날짜만, 없으면 null")
    # B/L 조건. 판정 등급에는 쓰지 않는다. 비서가 짚을 거리(원본 B/L·은행 L/G, 착불 운임)로만 쓴다
    original_bl: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                                     description="required(원본 필요) / surrendered(서렌더, 원본 불필요)")
    to_order_bank: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                                       description='"true" / "false": 수하인이 TO ORDER OF ... BANK(은행 지시식)인가')
    freight_term: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                                      description="prepaid(선불) / collect(착불)")
    incoterm: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                                  description="CIF / FOB / CFR")
    # 화물 인도 연락처 (B/L "Party to contact for cargo release", A/N). 회사 단위만. 개인 이름·휴대폰·이메일은 받지 않는다
    contact_company: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]))
    contact_role: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                                      description="포워더 / 관세사 / 수출자 / 판매처")
    # 전화는 LLM이 아니라 코드가 OCR 줄에서 뽑는다 (doc_ocr.take_contact_phone, 10-08 QA). LLM 입력에서는 [PHONE]으로 가린다
    contact_phone: FieldResult = Field(default_factory=lambda: FieldResult(value=None, checks=[]),
                                       description="회사·부서 전화, 숫자와 하이픈. 개인 휴대폰이면 비우고 format")


class OcrInfo(BaseModel):
    lines: int            # 읽은 글자 줄 수. 0에 가까우면 사진이 글자를 담지 못한 것
    rotation: Literal[0, 180, 270]
    runs: int             # OCR을 몇 번 돌렸나 (보통 1, 누운·뒤집힌 사진 3)
    ocr_ms: int


class DocumentResponse(BaseModel):
    # partial: LLM이 실패해서 B/L만 채웠다 (Spec EC-05). 사진 자체가 문제면 400이다
    status: Literal["ok", "partial"]
    needs_review: bool = Field(description="검사에 걸린 칸이 하나라도 있으면 true. 확인 화면에서 강조한다")
    fields: DocumentFields
    ocr: OcrInfo
    trace_id: str
    # 적하목록 관리번호. "MRN" 이름표 옆 값을 코드가 뽑는다(LLM 아님). 세관에 아직 없는 화물의 대체 조회용 (docs/specs/mrn-fallback.md)
    mrn: str | None = None
    # 서류 종류 (LLM이 고르고 코드가 서류 글자와 대조, doc_ocr.doc_kind_matches). 거래처를 메모장에 저장할 때 출처(B/L·A/N)로 쓴다.
    # 모르거나 서류 글자와 맞지 않으면 null이고 거래처는 저장하지 않는다
    doc_kind: Literal["B/L", "A/N"] | None = None
    # 선택: LLM 단계가 실패했을 때 실패 종류(LLMError.kind: bad_format, timeout, overloaded 등). 성공이면 null.
    # status는 그대로 partial이고 코드가 뽑은 B/L도 그대로 준다. 화면이 "AI가 서류 글자를 정리하지 못했어요"를 보여 주고,
    # 평가 스크립트가 제공사 실패와 틀린 답을 가르는 데 쓴다. 옛 모양의 응답도 읽히도록 기본값을 null로 둔다
    llm_error: str | None = None
    # 선택: 채팅 입력창에 사진을 붙여 보내면 확인 화면 없이 바로 조회한다.
    # 그래서 "B/L로 바로 조회해도 되나"를 코드가 정해 화면에 준다 (routers/document.bl_status_of). LLM이 정하지 않는다.
    #   ok: B/L을 읽었고 검사를 모두 통과 → 화면이 그 B/L로 바로 조회한다
    #   uncertain: B/L 모양 값은 있지만 검사에 걸렸다 → 조회하지 않고 사용자에게 직접 입력을 부탁한다
    #   none: B/L을 못 읽었다
    # 옛 모양의 응답도 읽히도록 기본값을 둔다 (화면은 없으면 fields로 같은 규칙을 다시 계산한다)
    bl_status: Literal["ok", "uncertain", "none"] = "none"
    # 검사를 통과한 "중요한 칸"의 이름만 (vessel, voyage, eta, etd). 값은 넣지 않는다.
    # 사진을 다시 보여 주지 않으므로 화면은 "선박 이름과 입항 예정일은 읽었어요"처럼 이름만 말한다
    important: list[Literal["vessel", "voyage", "eta", "etd"]] = Field(default_factory=list)
    # 선택: 이번 사진에서 저장한 B/L 조건·거래처의 칸 이름만 (값 없음). 확인 화면 없이 저장했으므로
    # 화면이 "운임 조건·인코텀즈를 저장해 두었어요"라고 알린다 (web/lib/docAttach.ts). 로그인 안 함·DB 없음·B/L 검사 걸림이면 빈 목록
    facts_saved: list[Literal["original_bl", "to_order_bank", "freight_term", "incoterm", "contact"]] = Field(default_factory=list)
    # 선택: 값을 읽었지만 검사에 걸린 칸의 이름만 (값 없음, routers/document.flagged_of). bl_status가 ok여도 다른 칸이
    # 걸렸으면 화면이 "확인이 필요한 칸: 입항 예정일"처럼 알린다 (10-08 QA). 값이 없는 칸(missing)은 넣지 않는다
    flagged: list[FlaggedField] = Field(default_factory=list)
    # 선택: 서류에 서로 다른 B/L 번호가 둘 이상 있을 때(정정본, MASTER+HOUSE) 그 번호들의 가린 값 (SDD 3.5, 예 QA****02).
    # 이때 bl_no 칸에는 ambiguous가 걸리고 bl_status는 uncertain이다. 화면은 자동 조회하지 않고 직접 입력을 부탁한다 (10-08 QA)
    bl_candidates: list[str] = Field(default_factory=list)
    # 선택: 칸 값을 어디서 가져왔나 (routers/document.py 흐름).
    #   unipass: OCR로 읽은 B/L이 UNI-PASS에 있어 선명·항차·입항일·항구·품명을 세관 값으로 채웠다 (서류를 읽은 값이 아니다)
    #   vision: 서류 사진을 모델이 직접 읽었다 (llm.read_doc_image)
    #   ocr: OCR 글자를 모델이 정리했다 (llm.extract_doc_fields, 사진 직접 읽기가 꺼졌거나 실패했을 때)
    # 옛 모양의 응답도 읽히도록 기본값을 둔다
    read_by: Literal["unipass", "vision", "ocr"] = "ocr"
