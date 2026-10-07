"""POST /api/document: 서류 사진에서 조회 값 7칸과 출항일(etd)을 뽑고, 바로 조회해도 되는지(bl_status)를 코드로 정한다
(docs/specs/document-extract.md).

처리 순서 (Spec "흐름"):
  1~2. 전처리 + OCR               services/doc_ocr.read_lines (로컬, 외부 호출 없음). PDF는 read_pdf: 글자층 먼저, 없으면 그림으로 OCR
  3.   B/L 번호를 코드로 뽑고 가림   doc_ocr.take_bl_candidates, mask_bl. 가려지지 않으면 LLM을 부르지 않는다.
                                   B/L 모양·컨테이너 번호·전화·이메일은 B/L을 못 찾았어도 늘 가린다 (10-08 QA)
  4.   나머지 6칸                   services/llm.extract_doc_fields (소형 모델, LLM_MODEL).
                                   연락처 전화는 코드가 OCR 줄에서 뽑고(take_contact_phone), 서류 종류는 서류 글자와 대조한다(doc_kind_matches)
  5.   칸 검사                      doc_ocr.check_fields, B/L 조건·연락처는 doc_ocr.check_conditions.
                                   서로 다른 B/L 번호가 둘 이상이면 bl_no에 ambiguous
  6.   바로 조회해도 되나            bl_status_of, important_of, flagged_of (코드가 정한다)
  7.   B/L 조건·거래처 자동 저장      services/doc_facts.save_from_document (로그인 + bl_status ok일 때 검사를 통과한 칸만).
                                   저장한 칸 이름만 facts_saved로 돌려준다
여기서 판정하지 않고 LLM SDK를 import하지 않는다 (CLAUDE.md). 뽑은 값으로 /api/agent·/api/chat을 부르지도 않는다.
채팅 조회는 화면이 bl_status가 ok일 때만 그 B/L로 보낸다 (web/components/ChatBar.tsx, AppProvider.askWithAttachment).
검사에 걸린 칸은 needs_review=true로 사용자에게 맡긴다.
"""
import re
from contextlib import nullcontext
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import UnidentifiedImageError

from backend.app.auth import current_user_id
from backend.app.observability.langfuse import request_span, session_scope
from backend.app.observability.logging import current_request_id, log_failure
from backend.app.routers.agent import quota_guard
from backend.app.schemas.customs import mask_bl  # 가린 B/L 표기 (SDD 3.5, 가운데 절반을 *로)
from backend.app.schemas.document import DocumentFields, DocumentResponse, FieldResult, OcrInfo
from backend.app.services import doc_facts, doc_ocr, llm

router = APIRouter(prefix="/api", tags=["document"])

# 사진 한 장 상한. 폰 사진(보통 2~5MB)보다 넉넉히 잡되 큰 파일로 서버 메모리를 쓰게 두지 않는다 (Spec EC-02)
MAX_BYTES = 10 * 1024 * 1024
LLM_FIELDS = ("vessel", "voyage", "eta", "pol", "pod", "item", "etd")  # etd는 없으면 null (프롬프트가 etd를 안 돌려줘도 안전)
# 바로 조회 판단에 쓰는 B/L 모양: 영문·숫자 8~20자 + 숫자 하나 이상.
# schemas/agent.py _BL_PATTERN과 services/chat.py _BL(숫자 하나 이상)을 둘 다 만족해야 채팅이 B/L로 알아듣는다
_BL_OK = re.compile(r"^(?=[A-Z0-9]*\d)[A-Z0-9]{8,20}$")
# "중요한 칸": 내 배를 확인하는 데 쓰는 값. 이름만 화면에 알린다
IMPORTANT_FIELDS = ("vessel", "voyage", "eta", "etd")
# 확인이 필요한 칸으로 알릴 수 있는 칸 (bl_no 뺀 전부, schemas/document.FlaggedField와 같은 순서)
FLAGGABLE_FIELDS = ("vessel", "voyage", "eta", "etd", "pol", "pod", "item", "original_bl", "to_order_bank",
                    "freight_term", "incoterm", "contact_company", "contact_role", "contact_phone")
# 서류 종류 → 거래처 출처 이름 (메모장 거래처 출처 목록, docs/specs/memory.md)
DOC_KIND = {"bl": "B/L", "arrival_notice": "A/N"}


@router.post("/document", response_model=DocumentResponse)
def read_document(file: UploadFile = File(...), session_id: str | None = Form(None),
                  user_id: Annotated[str | None, Depends(current_user_id)] = None) -> DocumentResponse:
    """사진을 받아 7칸을 채운다. OCR이 CPU를 1~2초 쓰므로 def(동기)로 두어 FastAPI가 스레드풀에서 돌리게 한다.
    로그인 필수다 (auth.py). 로그인 기능이 꺼진 환경에서만 user_id 없이 돈다."""
    # 요청 전체를 루트 span 하나로 감싼다. OCR과 서류 칸 추출 LLM이 그 자식이 된다. Langfuse 키가 없으면 아무것도 안 한다
    with request_span("/api/document"):
        return _read_document(file, session_id, user_id)


def _read_document(file: UploadFile, session_id: str | None, user_id: str | None) -> DocumentResponse:
    """read_document의 실제 처리. 루트 span 안에서 불린다."""
    # 서류 판독도 LLM을 부르므로 하루 토큰 한도를 확인한다 (docs/specs/token-quota.md)
    quota_guard(session_id, user_id)
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="too_large")
    if doc_ocr.is_pdf(data):  # 앞 4바이트(%PDF)로 가른다. 파일 이름·content-type은 믿지 않는다
        try:
            ocr = doc_ocr.read_pdf(data)
        except doc_ocr.InvalidPdf:
            raise HTTPException(status_code=400, detail="invalid_pdf")
    else:
        try:
            ocr = doc_ocr.read_lines(data)
        except doc_ocr.ImageTooLarge:
            # 10-08 QA D3: 픽셀이 너무 많은 그림(압축 폭탄)은 500이 아니라 400. 파일 크기 상한과 같은 이름으로 알린다
            raise HTTPException(status_code=400, detail="too_large")
        except (UnidentifiedImageError, OSError):
            raise HTTPException(status_code=400, detail="invalid_image")

    # 이름표마다 찾은 서로 다른 B/L. 첫 번째를 B/L 칸 값으로 쓰고, 둘 이상이면 아래에서 ambiguous를 건다 (10-08 QA D4)
    candidates = doc_ocr.take_bl_candidates(ocr.lines)
    bl = candidates[0] if candidates else None
    values: dict[str, str | None] = {"bl_no": bl, **{k: None for k in LLM_FIELDS}}
    conditions: dict[str, str | None] = {}
    evidence: dict[str, str | None] = {}
    doc_kind = None
    status = "ok"
    llm_error: str | None = None  # LLM 단계 실패 종류(bad_format, timeout 등). 화면이 "AI가 못 읽었다"를 알리는 데 쓴다
    try:
        masked = doc_ocr.mask_bl(ocr.lines, bl)
    except ValueError:
        # 가려지지 않은 B/L을 LLM에 보내지 않는다 (CLAUDE.md). 코드가 뽑은 B/L만 돌려준다
        log_failure("doc_mask", "bl_not_masked")
        masked, status = None, "partial"

    if masked is not None and ocr.lines:
        # 로그인 사용자면 session 없이 와도 범위를 연다. 안 열면 서류 판독 토큰이 하루 한도에 안 쌓인다 (services/quota.py)
        with session_scope(session_id, user_id) if (session_id or user_id) else nullcontext():  # 둘 다 없으면 트레이스만 남긴다
            try:
                out = llm.extract_doc_fields(masked).output
                values.update({k: getattr(out, k) for k in LLM_FIELDS})
                values["vessel"] = doc_ocr.strip_ship_suffix(values["vessel"])
                conditions = {k: getattr(out, k) for k in doc_ocr.CONDITION_FIELDS}
                if out.to_order_bank is not None:  # 응답 칸은 글자다 (FieldResult.value)
                    conditions["to_order_bank"] = "true" if out.to_order_bank else "false"
                evidence = {k: getattr(out, k) for k in doc_ocr.EVIDENCE_OF.values()}
                # 10-08 QA D9: 전화는 LLM 입력에서 가렸다([PHONE]). LLM이 낸 전화 칸은 버리고, 회사 이름 줄 근처에서 코드가 뽑는다
                conditions["contact_phone"] = doc_ocr.take_contact_phone(ocr.lines, conditions.get("contact_company"))
                # 10-08 QA D5: 서류 종류를 LLM만 정하지 않는다. 서류 글자와 맞지 않으면 모르는 것(null)으로 두어 거래처를 저장하지 않는다
                doc_kind = DOC_KIND.get(out.doc_kind or "") if doc_ocr.doc_kind_matches(out.doc_kind, ocr.lines) else None
            except llm.LLMError as e:  # 키 없음·과부하·형식 깨짐 모두 (Spec EC-05)
                log_failure("doc_extract", f"{e.kind} {e.code}" if e.code else e.kind)
                status = "partial"
                # 빈 칸만 보이면 서류 탓인지 AI 탓인지 모르므로 실패 종류를 응답에 실어 다시 시도하게 안내한다
                llm_error = e.kind

    checks = doc_ocr.check_fields(values, ocr.lines)
    if len(candidates) > 1:
        # 10-08 QA D4: B/L 이름표가 둘 이상이고 번호가 다르다(정정본, MASTER+HOUSE). 어느 번호가 사용자 화물인지 서류만으로 모르므로
        # ambiguous를 걸어 bl_status를 uncertain으로 만든다. 그러면 화면이 자동 조회하지 않고 조건·거래처도 저장하지 않는다
        checks["bl_no"].append("ambiguous")
    shown, extra_checks = doc_ocr.check_conditions(conditions, evidence, ocr.lines)
    fields = DocumentFields(**{k: FieldResult(value=values[k], checks=checks[k]) for k in doc_ocr.FIELDS},
                            **{k: FieldResult(value=shown[k], checks=extra_checks[k]) for k in doc_ocr.CONDITION_FIELDS})
    bl_status = bl_status_of(fields)
    # 틀린 번호의 화물 행에 남의 조건을 적지 않게 B/L이 검사를 통과했을 때만 저장한다.
    # 저장 실패는 서류 응답을 막지 않는다 (doc_facts가 종류만 로그로 남기고 빈 목록을 돌려준다)
    saved = doc_facts.save_from_document(user_id, fields.bl_no.value, fields, doc_kind) if bl_status == "ok" else []
    return DocumentResponse(
        status=status,
        needs_review=any(checks.values()) or any(extra_checks.values()),
        fields=fields,
        ocr=OcrInfo(lines=len(ocr.lines), rotation=ocr.rotation, runs=ocr.runs, ocr_ms=ocr.ocr_ms),
        trace_id=current_request_id(),
        mrn=doc_ocr.take_mrn(ocr.lines),  # 세관에 아직 없는 화물의 대체 조회용 (mrn-fallback)
        doc_kind=doc_kind,
        llm_error=llm_error,
        bl_status=bl_status,
        important=important_of(fields),
        facts_saved=saved,
        flagged=flagged_of(fields),
        # 가린 값만 (SDD 3.5). 원본 번호는 bl_no 칸 하나에만 있다. 화면이 "번호가 여러 개예요(가린 값 …)"라고 알린다
        bl_candidates=[mask_bl(c) for c in candidates] if len(candidates) > 1 else [],
    )


def bl_status_of(fields: DocumentFields) -> str:
    """사진의 B/L로 바로 조회해도 되는지 코드가 정한다.
    ok: 값이 있고, 검사(missing, not_in_text, format, ambiguous)에 하나도 안 걸렸고, 채팅이 B/L로 알아듣는 모양이다.
    uncertain: 값은 있지만 위 조건 중 하나라도 어긋난다. 틀린 번호로 남의 화물을 조회하지 않게 조회를 멈춘다.
    none: 값이 없다."""
    bl = fields.bl_no
    if not bl.value:
        return "none"
    if bl.checks or not _BL_OK.match(bl.value):
        return "uncertain"
    return "ok"


def important_of(fields: DocumentFields) -> list[str]:
    """검사를 통과한 중요한 칸(IMPORTANT_FIELDS)의 이름만 돌려준다. 읽은 값은 다시 보여 주지 않는다."""
    return [k for k in IMPORTANT_FIELDS if getattr(fields, k).value and not getattr(fields, k).checks]


def flagged_of(fields: DocumentFields) -> list[str]:
    """값을 읽었지만 검사(not_in_text, format, ambiguous)에 걸린 칸의 이름만 돌려준다. B/L 칸은 bl_status가 따로 알린다.

    10-08 QA D6: bl_status가 ok면 화면이 바로 조회해서, 입항 예정일 같은 다른 칸이 검사에 걸린 것을 사용자가 몰랐다.
    화면이 "확인이 필요한 칸: 입항 예정일"처럼 이름만 말한다. 값이 없는 칸(missing, 휴대폰이라 비운 전화)은 넣지 않는다:
    서류에 원래 없는 칸까지 확인하라고 하면 매번 확인 줄이 붙는다.
    """
    return [k for k in FLAGGABLE_FIELDS if getattr(fields, k).value and getattr(fields, k).checks]

