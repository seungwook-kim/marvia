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

사진 직접 읽기를 켜면(DOC_VISION_MODEL, 사진만. PDF는 위 흐름) 빠른 길과 정확한 길을 동시에 돌린다 (Spec "사진 직접 읽기"):
  A. OCR → B/L → UNI-PASS 조회. 찾으면 세관 값으로 바로 답하고(read_by unipass) B를 끊는다.
     B/L 조건·거래처는 응답 뒤에 위 4·7단계를 돌려 저장만 한다 (_save_conditions_later)
  B. 같은 사진을 모델이 직접 읽는다 (llm.read_doc_image, read_by vision). A가 못 찾으면(적하목록 전 서류, OCR 오독) 이 값을 쓴다
  B가 실패하거나 VISION_WAIT_SEC를 넘기면 위 1~7단계(read_by ocr)로 돌아간다
여기서 판정하지 않고 LLM SDK를 import하지 않는다 (CLAUDE.md). 뽑은 값으로 /api/agent·/api/chat을 부르지도 않는다.
채팅 조회는 화면이 bl_status가 ok일 때만 그 B/L로 보낸다 (web/components/ChatBar.tsx, AppProvider.askWithAttachment).
검사에 걸린 칸은 needs_review=true로 사용자에게 맡긴다.
"""
import contextvars
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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
from backend.app.services.customs import CustomsError, UnipassClient  # A: 읽은 B/L이 세관에 있나 (사진 직접 읽기와 동시)
from backend.app.services.rules import rule  # 선사 부호 목록 (skills/customs-stages/rules.md bl_carrier_prefixes)

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


# 사진 직접 읽기(B)를 기다리는 상한. Sonnet은 서류 한 장에 약 4.5초였다(로컬 실측). 넘기면 끊고 OCR 정리 길로 간다
VISION_WAIT_SEC = 15.0
# UNI-PASS 확인(A)을 기다리는 상한. 보통 1초 안이다. 넘기면 B를 쓴다 (UNI-PASS는 판정 흐름에서 다시 조회한다)
CUSTOMS_WAIT_SEC = 4.0
# 응답 뒤에 B/L 조건·거래처를 저장하는 일꾼 (_save_conditions_later). 요청 스레드를 막지 않는다
_later = ThreadPoolExecutor(max_workers=2, thread_name_prefix="doc-later")


def _in_context(pool: ThreadPoolExecutor, fn, *args) -> Future:
    """지금 요청의 contextvars(세션·하루 한도·트레이스)를 그대로 가진 채 다른 스레드에서 fn을 돌린다."""
    return pool.submit(contextvars.copy_context().run, fn, *args)


def _read_document(file: UploadFile, session_id: str | None, user_id: str | None) -> DocumentResponse:
    """read_document의 실제 처리. 루트 span 안에서 불린다."""
    # 서류 판독도 LLM을 부르므로 하루 토큰 한도를 확인한다 (docs/specs/token-quota.md)
    quota_guard(session_id, user_id)
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="too_large")
    # 로그인 사용자면 session 없이 와도 범위를 연다. 안 열면 서류 판독 토큰이 하루 한도에 안 쌓인다 (services/quota.py)
    with session_scope(session_id, user_id) if (session_id or user_id) else nullcontext():  # 둘 다 없으면 트레이스만 남긴다
        if doc_ocr.is_pdf(data):  # 앞 4바이트(%PDF)로 가른다. 파일 이름·content-type은 믿지 않는다
            try:
                ocr = doc_ocr.read_pdf(data)
            except doc_ocr.InvalidPdf:
                raise HTTPException(status_code=400, detail="invalid_pdf")
            return _from_ocr(ocr, user_id)
        return _read_photo(data, user_id)


def _read_photo(data: bytes, user_id: str | None) -> DocumentResponse:
    """사진 한 장. 사진 직접 읽기가 꺼져 있으면 OCR 정리 길(_from_ocr)만, 켜져 있으면 A·B를 동시에 돌린다 (모듈 설명)."""
    cancel = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="doc")
    try:
        vision: Future | None = None
        if llm.doc_vision_model():
            try:
                image = doc_ocr.vision_jpeg(data)
            except doc_ocr.ImageTooLarge:
                # 10-08 QA D3: 픽셀이 너무 많은 그림(압축 폭탄)은 500이 아니라 400. 파일 크기 상한과 같은 이름으로 알린다
                raise HTTPException(status_code=400, detail="too_large")
            except (UnidentifiedImageError, OSError):
                raise HTTPException(status_code=400, detail="invalid_image")
            vision = _in_context(pool, _vision_call, image, cancel)
        try:
            # 사진 직접 읽기가 함께 돌면 OCR은 B/L 확인에만 쓰이므로 0°만 한 번 읽는다 (doc_ocr.read_lines quick)
            ocr = doc_ocr.read_lines(data, quick=vision is not None)
        except doc_ocr.ImageTooLarge:
            raise HTTPException(status_code=400, detail="too_large")
        except (UnidentifiedImageError, OSError):
            raise HTTPException(status_code=400, detail="invalid_image")

        candidates = doc_ocr.take_bl_candidates(ocr.lines)
        fixed = doc_ocr.fix_carrier_prefix(candidates[0]) if candidates else None
        # A: B/L이 하나이고 모양이 맞을 때만 세관에 묻는다. 둘 이상(정정본, MASTER+HOUSE)이면 어느 쪽인지 몰라 묻지 않는다
        if vision is not None and fixed and len(candidates) == 1 and _BL_OK.match(fixed):
            try:
                record = _in_context(pool, _customs_record, fixed).result(timeout=CUSTOMS_WAIT_SEC)
            except FutureTimeout:
                record = None
            if record is not None:
                cancel.set()  # B를 끊는다. 세관 값이 서류를 읽은 값보다 정확하다
                return _from_customs(ocr, candidates[0], fixed, record, user_id)
        if vision is not None:
            try:
                out = vision.result(timeout=VISION_WAIT_SEC)
            except FutureTimeout:
                cancel.set()
                log_failure("doc_vision", "timeout")
                out = None
            if out is not None:
                return _from_vision(ocr, candidates, fixed, out, user_id)
            ocr = doc_ocr.read_lines(data)  # 사진 직접 읽기 실패: 누운 사진도 읽게 방향을 바꿔 가며 다시 읽는다
        return _from_ocr(ocr, user_id)
    finally:
        cancel.set()  # 어느 길로 끝났든 남은 B는 끊는다
        pool.shutdown(wait=False)


def _vision_call(image: bytes, cancel: threading.Event):
    """B: 사진 직접 읽기. 실패는 None으로 바꿔 OCR 정리 길로 넘긴다 (끊김은 실패로 세지 않는다)."""
    try:
        return llm.read_doc_image(image, cancel=cancel).output
    except llm.LLMError as e:
        if e.kind != "cancelled":
            log_failure("doc_vision", f"{e.kind} {e.code}" if e.code else e.kind)
        return None


def _customs_record(bl: str):
    """A: UNI-PASS에 이 B/L이 있으면 그 기록, 없거나 확인 못 하면 None. 하우스를 먼저, 없으면 마스터로 본다 (bl_parse와 같은 순서).
    키 없음·오류는 "확인 못 함"(None)이다. B/L 원문은 로그에 쓰지 않는다."""
    try:
        client = UnipassClient()
    except CustomsError as e:
        log_failure("doc_customs", e.code)
        return None
    try:
        for bl_type in ("HOUSE", "MASTER"):
            record = client.fetch(bl, bl_type)
            if record is not None:
                return record
        return None
    except CustomsError as e:
        log_failure("doc_customs", e.code)
        return None
    finally:
        client.close()


def _from_customs(ocr, raw_bl: str, bl: str, record, user_id: str | None) -> DocumentResponse:
    """A가 이겼을 때: 세관 값으로 칸을 채운다. 서류를 읽은 값이 아니므로 글자 검사는 걸지 않는다.
    출항일은 UNI-PASS에 없어 비운다. B/L 조건·거래처는 응답 뒤에 따로 읽어 저장한다 (_save_conditions_later)."""
    eta = record.entry_date.date().isoformat() if record.entry_date else None
    values = {"bl_no": bl, "vessel": record.ship_name, "voyage": record.voyage, "eta": eta, "etd": None,
              "pol": record.loading_port_name or record.loading_port,
              "pod": record.discharge_port_name or record.discharge_port, "item": record.goods_name}
    checks = {k: ([] if values[k] or k in doc_ocr.OPTIONAL_FIELDS else ["missing"]) for k in doc_ocr.FIELDS}
    fields = DocumentFields(**{k: FieldResult(value=values[k], checks=checks[k]) for k in doc_ocr.FIELDS})
    if user_id:
        _in_context(_later, _save_conditions_later, ocr, raw_bl, bl, values, user_id)
    return DocumentResponse(
        status="ok",
        needs_review=any(checks.values()),
        fields=fields,
        ocr=OcrInfo(lines=len(ocr.lines), rotation=ocr.rotation, runs=ocr.runs, ocr_ms=ocr.ocr_ms),
        trace_id=current_request_id(),
        mrn=record.mrn or doc_ocr.take_mrn(ocr.lines),
        bl_status=bl_status_of(fields),
        important=important_of(fields),
        flagged=flagged_of(fields),
        read_by="unipass",
    )


def _save_conditions_later(ocr, raw_bl: str, bl: str, values: dict, user_id: str) -> None:
    """A가 이긴 뒤(응답은 이미 나감): OCR 글자에서 B/L 조건·거래처를 읽어 검사를 통과한 칸만 저장한다 (위 4·5·7단계).
    실패는 로그만 남긴다. 사용자가 기다리는 응답과 상관없다."""
    try:
        masked = doc_ocr.mask_bl(ocr.lines, raw_bl)
        out = llm.extract_doc_fields(masked).output
        conditions, evidence, doc_kind = _conditions_of(out, ocr.lines)
        shown, extra_checks = doc_ocr.check_conditions(conditions, evidence, ocr.lines)
        fields = DocumentFields(**{k: FieldResult(value=values[k], checks=[]) for k in doc_ocr.FIELDS},
                                **{k: FieldResult(value=shown[k], checks=extra_checks[k]) for k in doc_ocr.CONDITION_FIELDS})
        doc_facts.save_from_document(user_id, bl, fields, doc_kind)
    except (ValueError, llm.LLMError) as e:
        log_failure("doc_conditions_later", getattr(e, "kind", type(e).__name__))


def _conditions_of(out, lines: list[str]) -> tuple[dict, dict, str | None]:
    """LLM 출력(DocFieldsOutput·DocVisionOutput)에서 B/L 조건·연락처, 근거 글, 서류 종류를 꺼낸다.
    전화는 LLM 값을 버리고 회사 이름 줄 근처에서 코드가 뽑는다 (10-08 QA D9). 서류 종류는 서류 글자와 맞을 때만 (D5)."""
    conditions = {k: getattr(out, k) for k in doc_ocr.CONDITION_FIELDS}
    if out.to_order_bank is not None:  # 응답 칸은 글자다 (FieldResult.value)
        conditions["to_order_bank"] = "true" if out.to_order_bank else "false"
    evidence = {k: getattr(out, k) for k in doc_ocr.EVIDENCE_OF.values()}
    conditions["contact_phone"] = doc_ocr.take_contact_phone(lines, conditions.get("contact_company"))
    doc_kind = DOC_KIND.get(out.doc_kind or "") if doc_ocr.doc_kind_matches(out.doc_kind, lines) else None
    return conditions, evidence, doc_kind


def _without_text_check(checks: dict[str, list[str]]) -> dict[str, list[str]]:
    """사진 직접 읽기 값에서 not_in_text를 뺀다. 모델이 OCR보다 바르게 읽은 값(PCLU, 2637E)은 OCR 글자에 없어서 늘 걸리기 때문이다.
    형식(format)·이름표 중복(ambiguous)·빈 칸(missing) 검사는 그대로 둔다."""
    return {k: [c for c in v if c != "not_in_text"] for k, v in checks.items()}


def _from_vision(ocr, candidates: list[str], ocr_bl: str | None, out, user_id: str | None) -> DocumentResponse:
    """B가 이겼을 때: 모델이 사진에서 읽은 값을 쓴다. B/L은 OCR 값과 같거나 선사 부호가 목록에 있을 때만 바로 조회(ok).
    둘 다 아니면 ambiguous를 걸어 사용자가 확인한다. OCR이 서로 다른 B/L 이름표를 둘 이상 봤으면 지금처럼 ambiguous다."""
    seen = doc_ocr._clean(out.bl_no or "")
    bl = doc_ocr.fix_carrier_prefix(seen) if seen else ocr_bl
    values = {"bl_no": bl, **{k: getattr(out, k) for k in LLM_FIELDS}}
    values["vessel"] = doc_ocr.strip_ship_suffix(values["vessel"])
    checks = _without_text_check(doc_ocr.check_fields(values, ocr.lines))
    trusted = bl is not None and (bl == ocr_bl or bl[:4] in set(rule("bl_carrier_prefixes")))
    if bl and not trusted:
        checks["bl_no"].append("ambiguous")
    if len(candidates) > 1 and "ambiguous" not in checks["bl_no"]:
        checks["bl_no"].append("ambiguous")
    conditions, evidence, doc_kind = _conditions_of(out, ocr.lines)
    shown, extra_checks = doc_ocr.check_conditions(conditions, evidence, ocr.lines)
    extra_checks = _without_text_check(extra_checks)
    fields = DocumentFields(**{k: FieldResult(value=values[k], checks=checks[k]) for k in doc_ocr.FIELDS},
                            **{k: FieldResult(value=shown[k], checks=extra_checks[k]) for k in doc_ocr.CONDITION_FIELDS})
    bl_status = bl_status_of(fields)
    saved = doc_facts.save_from_document(user_id, bl, fields, doc_kind) if bl_status == "ok" else []
    vision_mrn = doc_ocr.take_mrn(["MRN: " + out.mrn]) if out.mrn else None
    return DocumentResponse(
        status="ok",
        needs_review=any(checks.values()) or any(extra_checks.values()),
        fields=fields,
        ocr=OcrInfo(lines=len(ocr.lines), rotation=ocr.rotation, runs=ocr.runs, ocr_ms=ocr.ocr_ms),
        trace_id=current_request_id(),
        mrn=vision_mrn or doc_ocr.take_mrn(ocr.lines),
        doc_kind=doc_kind,
        bl_status=bl_status,
        important=important_of(fields),
        facts_saved=saved,
        flagged=flagged_of(fields),
        bl_candidates=[mask_bl(c) for c in candidates] if len(candidates) > 1 else [],
        read_by="vision",
    )


def _from_ocr(ocr, user_id: str | None) -> DocumentResponse:
    """OCR 정리 길 (모듈 설명 1~7단계). 사진 직접 읽기가 꺼졌거나 실패했을 때, PDF일 때."""
    # 이름표마다 찾은 서로 다른 B/L. 첫 번째를 B/L 칸 값으로 쓰고, 둘 이상이면 아래에서 ambiguous를 건다 (10-08 QA D4)
    candidates = doc_ocr.take_bl_candidates(ocr.lines)
    bl = candidates[0] if candidates else None
    # 선사 부호에서 OCR이 헷갈린 글자를 고친다 (doc_ocr.fix_carrier_prefix). 서류 글자 가리기(mask_bl)와
    # 글자에 있는지 검사(check_fields)는 OCR이 읽은 그대로의 번호(bl)로 하고, 응답 값만 고친 번호로 바꾼다
    fixed_bl = doc_ocr.fix_carrier_prefix(bl) if bl else None
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
        try:
            out = llm.extract_doc_fields(masked).output
            values.update({k: getattr(out, k) for k in LLM_FIELDS})
            values["vessel"] = doc_ocr.strip_ship_suffix(values["vessel"])
            # 전화는 LLM 입력에서 가렸다([PHONE]). 회사 이름 줄 근처에서 코드가 뽑는다. 서류 종류는 서류 글자와 대조한다 (_conditions_of)
            conditions, evidence, doc_kind = _conditions_of(out, ocr.lines)
        except llm.LLMError as e:  # 키 없음·과부하·형식 깨짐 모두 (Spec EC-05)
            log_failure("doc_extract", f"{e.kind} {e.code}" if e.code else e.kind)
            status = "partial"
            # 빈 칸만 보이면 서류 탓인지 AI 탓인지 모르므로 실패 종류를 응답에 실어 다시 시도하게 안내한다
            llm_error = e.kind

    checks = doc_ocr.check_fields(values, ocr.lines)
    values["bl_no"] = fixed_bl
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

