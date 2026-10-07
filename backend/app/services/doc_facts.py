"""서류 사진에서 읽은 B/L 조건·화물 인도 연락처의 자동 저장과, 채팅에서 물을 때 코드가 쓰는 답.

  save_from_document()  POST /api/document 끝에서: 로그인 + B/L 검사 통과(bl_status ok)일 때 검사를 통과한 칸만 저장한다
  read()                채팅 도구 cargo_documents: 그 화물의 저장된 조건(user_cargo)과 서류 출처 거래처(메모장)를 읽는다
  reply_lines()         읽은 값을 사람 말 줄로. 값은 코드가 쓰고, LLM에는 칸 이름과 있음/없음만 돌려준다 (services/chat.py)
  llm_view()            LLM에 돌려줄 도구 결과. 값·연락처는 넣지 않는다

지키는 것:
- 검사(doc_ocr.check_conditions)에 걸린 칸은 저장하지 않는다. 거래처는 서류 종류(B/L·A/N)를 알 때만 저장한다
- 저장한 값은 사람이 확인하지 않았다. 답은 "서류 사진에서 읽은 값(확인 전)"이라고 밝힌다 (026 doc_facts_at)
- 연락처(회사 전화)는 사용자 화면에만 간다. LLM에는 넘기지 않는다 (CLAUDE.md 개인정보)
- 저장·읽기 실패는 서류 응답·채팅 답을 막지 않는다. 로그에는 종류만 남긴다
- B/L 원본은 LLM에 가지 않는다. 여기서는 화물 지문(cargo.cargo_key)으로만 찾는다
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from backend.app.db import connect
from backend.app.observability.logging import log_failure
from backend.app.services import cargo, memory
from backend.app.services.evidence import KST

# 저장 대상 조건 칸. 마이그레이션 015 칸 이름과 같다 (DocumentFields와 이름이 같다)
CONDITIONS = ("original_bl", "to_order_bank", "freight_term", "incoterm")
CONTACT = "contact"  # facts_saved에 넣는 거래처 이름. 값이 아니라 이름이다
DOC_SOURCES = ("B/L", "A/N")  # 거래처 출처로 받는 서류 종류 (services/memory.SOURCES 중 서류 쪽)

# 답 문장 (코드가 쓴다. LLM은 쓰지 않는다)
UNCONFIRMED_HEAD = "서류 사진에서 읽은 값(확인 전)이에요."
CONFIRMED_HEAD = "저장해 둔 서류 정보예요."  # 026 전 확인 화면에서 사람이 확인한 값 (doc_facts_at 없음)
NOTHING_STORED = "저장된 서류 정보가 없어요. B/L이나 A/N(도착 통지서) 사진을 올려 주시면 읽어 둘게요."
NEED_LOGIN = "서류 정보는 로그인한 뒤 올린 B/L·A/N 사진에서만 읽어 둬요. 로그인하고 사진을 올려 주세요."
NEED_CARGO = "어느 화물의 서류 정보인지 B/L 번호를 함께 알려 주세요."
READ_FAILED = "저장된 서류 정보를 지금 읽지 못했어요. 잠시 뒤 다시 물어봐 주세요."

LABELS = {"freight_term": "운임", "incoterm": "인코텀즈", "original_bl": "원본 B/L", "to_order_bank": "은행 지시식"}
VALUE_TEXT = {
    "freight_term": {"prepaid": "선불(선적지 지급)", "collect": "착불(도착지 지급)"},
    "original_bl": {"required": "필요", "surrendered": "서렌더(원본 불필요)"},
    "to_order_bank": {True: "예", False: "아니요"},
}
ORDER = ("freight_term", "incoterm", "original_bl", "to_order_bank")  # 답 줄 순서: 자주 묻는 것부터


def _db_ready() -> bool:
    """DATABASE_URL이 있나. 없으면(평가 실행기·채점 환경) 읽지도 쓰지도 않는다 (memory._db_ready와 같은 기준)."""
    return bool(os.environ.get("DATABASE_URL"))


def passed(fields, key: str):
    """검사를 통과한 칸 값. 값이 없거나 검사에 하나라도 걸렸으면 None (걸린 칸은 저장하지 않는다).
    fields는 schemas.document.DocumentFields다."""
    f = getattr(fields, key, None)
    if f is None or not f.value or f.checks:
        return None
    return f.value


def save_from_document(user_id: str | None, bl_no: str | None, fields, doc_kind: str | None) -> list[str]:
    """POST /api/document가 부른다. 저장한 칸 이름 목록(값 없음)을 돌려준다. 아무것도 안 했으면 [].

    부르는 쪽(routers/document.py)이 bl_status == "ok"를 이미 확인했다. 여기서는 로그인·DB·열쇠를 본다.
    - 조건 네 칸: 검사를 통과한 칸만. to_order_bank는 "true"/"false" 글자를 bool로 바꾼다
    - 거래처: 서류 종류가 B/L·A/N일 때만, 회사·역할이 검사를 통과했을 때만. 전화는 통과했을 때만 붙인다
    - B/L 구분은 서류에서 알 수 없어 비워 둔다 (024: 다음 조회가 UNI-PASS에서 찾은 구분으로 채운다)
    실패는 로그에 종류만 남기고 [] (서류 응답을 막지 않는다)."""
    if not user_id or not bl_no or not _db_ready():
        return []
    facts = {k: passed(fields, k) for k in CONDITIONS}
    if facts["to_order_bank"] is not None:
        facts["to_order_bank"] = {"true": True, "false": False}.get(facts["to_order_bank"])
    contact = None
    company, role = passed(fields, "contact_company"), passed(fields, "contact_role")
    if doc_kind in DOC_SOURCES and company and role:
        contact = {"company": company, "role": role, "phone": passed(fields, "contact_phone"), "source": doc_kind}
    if not any(v is not None for v in facts.values()) and contact is None:
        return []
    saved: list[str] = []
    today = datetime.now(timezone.utc).astimezone(KST).date()
    try:
        with connect() as conn:
            # 거래처만 있어도 화물 줄을 만들어 그 화물 칸에 붙인다. 채팅 도구가 화물 지문으로 찾게 하려는 것이다
            key = cargo.apply_document_facts(conn, user_id, bl_no, None, facts, source=doc_kind or "document")
            if key:
                saved += [k for k in CONDITIONS if facts[k] is not None]
            if contact is not None:
                status = memory.add_contact(conn, user_id, contact, key, today)
                if status in ("saved", "unchanged"):
                    saved.append(CONTACT)
                else:
                    log_failure("doc_facts_save", f"contact {status}")
    except Exception as e:  # noqa: BLE001 - 저장은 부가 기능이다. 종류만 남긴다
        log_failure("doc_facts_save", type(e).__name__)
        return []
    return saved


def read(user_id: str, bl_no: str) -> dict | None:
    """그 화물의 저장된 서류 정보 {"conditions": {칸: 값}, "unconfirmed": bool, "contacts": [{company, role, phone}]}.
    저장된 것이 하나도 없으면 None. DB·열쇠가 없으면 None. 읽기 실패는 예외를 그대로 올린다 (부르는 쪽이 문장을 고른다)."""
    key = cargo.cargo_key(bl_no)
    if not key or not _db_ready():
        return None
    with connect() as conn:
        row = cargo.get_row(conn, user_id, key)
        items, _, _, _ = memory.load(conn, user_id)
    conditions = {k: row[k] for k in CONDITIONS if row and row.get(k) is not None}
    contacts = [{k: (i.get("fields") or {}).get(k) for k in ("company", "role", "phone")} for i in items
                if i.get("kind") == "contact" and i.get("cargo_key") == key
                and (i.get("fields") or {}).get("source") in DOC_SOURCES]
    if not conditions and not contacts:
        return None
    return {"conditions": conditions, "unconfirmed": bool(row and row.get("doc_facts_at")), "contacts": contacts}


def reply_lines(facts: dict) -> list[str]:
    """저장된 서류 정보를 답 줄로. 예:
    "서류 사진에서 읽은 값(확인 전)이에요. 운임: 착불(도착지 지급), 인코텀즈: FOB, 원본 B/L: 필요, 은행 지시식: 아니요."
    "화물 인도 연락처: 가나다물류(포워더) 051-000-0000"
    모르는 칸은 말하지 않는다. 거래처는 서류에서 읽은 것이라 조건이 없어도 확인 전이라고 밝힌다."""
    cond = facts.get("conditions") or {}
    parts = [f"{LABELS[k]}: {VALUE_TEXT.get(k, {}).get(cond[k], cond[k])}" for k in ORDER if k in cond]
    head = UNCONFIRMED_HEAD if facts.get("unconfirmed") or not cond else CONFIRMED_HEAD
    lines = [f"{head} {', '.join(parts)}." if parts else head]
    for c in facts.get("contacts") or []:
        who = f"{c['company']}({c['role']})" if c.get("role") else str(c.get("company") or "")
        lines.append(f"화물 인도 연락처: {who}{' ' + c['phone'] if c.get('phone') else ''}")
    return lines


def llm_view(facts: dict | None) -> dict:
    """LLM에 돌려줄 도구 결과. 칸 이름과 있음/없음만 준다. 값·회사 이름·전화는 넣지 않는다 (개인정보).
    답 줄은 코드가 마지막 답 뒤에 붙인다(services/chat._agent). LLM이 따로 값을 말하지 않게 shown_by_code를 준다."""
    if not facts:
        return {"ok": True, "stored": [], "contact": False, "shown_by_code": True}
    return {"ok": True, "stored": [k for k in ORDER if k in (facts.get("conditions") or {})],
            "contact": bool(facts.get("contacts")), "unconfirmed": bool(facts.get("unconfirmed")), "shown_by_code": True}
