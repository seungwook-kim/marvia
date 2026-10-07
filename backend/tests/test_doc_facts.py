"""서류 사진 B/L 조건·화물 인도 연락처 자동 저장과 채팅 답 테스트 (services/doc_facts.py,
routers/document.py facts_saved, services/chat.py cargo_documents 도구). 키·DB·LLM 없이 돈다.

확인하는 것:
- 자동 저장은 로그인 + B/L 검사 통과(bl_status ok)일 때만, 검사에 걸린 칸은 저장하지 않음, 거래처는 서류 종류를 알 때만
- DocumentResponse.facts_saved에는 칸 이름만 (값 없음)
- 채팅 도구: 저장된 값은 코드가 답 뒤에 붙이고, LLM에 돌려주는 결과에는 값·연락처가 없음
- 저장된 것 없음 → 고정 문장, 로그인 안 함 → 로그인 문장, B/L 토큰은 다른 도구처럼 푼다
B/L·회사·전화는 모두 모의값이다.
"""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.routers import document as document_router
from backend.app.schemas.document import DocumentFields, FieldResult
from backend.app.services import chat as svc
from backend.app.services import doc_facts, llm
from backend.tests.test_chat_watch import BL, CONTEXT, USER, _llm, _run, agent_path  # noqa: F401 (fixture)
from backend.tests.test_routers import _login

DOC_BL = "MOCKBL20261008"  # 서류 사진 쪽 모의 B/L
COMPANY, PHONE = "GANADA LOGISTICS", "051-000-0000"  # 가공 회사·회사 전화


def _f(value=None, checks=()):
    return FieldResult(value=value, checks=list(checks))


def _fields(**over) -> DocumentFields:
    """검사를 모두 통과한 서류 칸. over로 칸을 바꾼다 (예: freight_term=_f("collect", ["not_in_text"]))."""
    base = {k: _f() for k in ("vessel", "voyage", "eta", "pol", "pod", "item")}
    base.update(bl_no=_f(DOC_BL), original_bl=_f("required"), to_order_bank=_f("false"), freight_term=_f("collect"),
                incoterm=_f("FOB"), contact_company=_f(COMPANY), contact_role=_f("포워더"), contact_phone=_f(PHONE))
    base.update(over)
    return DocumentFields(**base)


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def store(monkeypatch):
    """DB 대신 cargo.apply_document_facts·memory.add_contact를 가로채 받은 값을 모은다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(doc_facts, "connect", lambda *a, **k: FakeConn())
    got = {"cargo": [], "contact": []}

    def apply(conn, user_id, bl_no, bl_type, facts, source=None):
        got["cargo"].append({"user": user_id, "bl": bl_no, "bl_type": bl_type, "facts": facts, "source": source})
        return "key-1"

    def add_contact(conn, user_id, contact, key, today):
        got["contact"].append({"contact": contact, "key": key})
        return "saved"
    monkeypatch.setattr(doc_facts.cargo, "apply_document_facts", apply)
    monkeypatch.setattr(doc_facts.memory, "add_contact", add_contact)
    return got


# ── 자동 저장 (services/doc_facts.save_from_document) ──

def test_saves_passed_conditions_and_contact(store):
    saved = doc_facts.save_from_document("u1", DOC_BL, _fields(), "B/L")
    assert saved == ["original_bl", "to_order_bank", "freight_term", "incoterm", "contact"]
    assert store["cargo"] == [{"user": "u1", "bl": DOC_BL, "bl_type": None, "source": "B/L",
                               "facts": {"original_bl": "required", "to_order_bank": False, "freight_term": "collect",
                                         "incoterm": "FOB"}}]
    assert store["contact"] == [{"contact": {"company": COMPANY, "role": "포워더", "phone": PHONE, "source": "B/L"},
                                 "key": "key-1"}]


def test_failed_check_fields_are_not_saved(store):
    """검사에 걸린 칸(not_in_text, format, ambiguous)은 None으로 넘겨 기존 값을 덮지 않는다."""
    fields = _fields(freight_term=_f("collect", ["not_in_text"]), incoterm=_f("FOB", ["ambiguous"]),
                     contact_phone=_f(PHONE, ["not_in_text"]))
    saved = doc_facts.save_from_document("u1", DOC_BL, fields, "A/N")
    assert saved == ["original_bl", "to_order_bank", "contact"]
    facts = store["cargo"][0]["facts"]
    assert facts["freight_term"] is None and facts["incoterm"] is None
    assert store["contact"][0]["contact"] == {"company": COMPANY, "role": "포워더", "phone": None, "source": "A/N"}


def test_contact_needs_known_document_kind(store):
    """서류 종류를 모르면 거래처는 저장하지 않는다. 조건은 출처 'document'로 저장한다."""
    saved = doc_facts.save_from_document("u1", DOC_BL, _fields(), None)
    assert "contact" not in saved and store["contact"] == []
    assert store["cargo"][0]["source"] == "document"


def test_contact_needs_passed_company_and_role(store):
    doc_facts.save_from_document("u1", DOC_BL, _fields(contact_company=_f(COMPANY, ["not_in_text"])), "B/L")
    assert store["contact"] == []


def test_nothing_saved_without_login_or_db(store, monkeypatch):
    assert doc_facts.save_from_document(None, DOC_BL, _fields(), "B/L") == []
    monkeypatch.delenv("DATABASE_URL")
    assert doc_facts.save_from_document("u1", DOC_BL, _fields(), "B/L") == []
    assert store["cargo"] == [] and store["contact"] == []


def test_save_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

    def broken(*a, **k):
        raise ConnectionError("테스트")
    monkeypatch.setattr(doc_facts, "connect", broken)
    assert doc_facts.save_from_document("u1", DOC_BL, _fields(), "B/L") == []


def test_apply_records_provenance_only_when_values_written(monkeypatch):
    """조건을 실제로 적었을 때만 doc_facts_at·doc_facts_source를 남긴다 (026)."""
    from backend.app.services import cargo

    monkeypatch.setenv("BL_ENCRYPTION_KEY", Fernet.generate_key().decode())

    class Conn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            return SimpleNamespace(rowcount=1)
    conn = Conn()
    cargo.apply_document_facts(conn, "u1", DOC_BL, None, {"freight_term": "collect"}, source="B/L")
    assert len(conn.calls) == 2 and "doc_facts_source" in conn.calls[1][0] and conn.calls[1][1][0] == "B/L"
    conn = Conn()
    cargo.apply_document_facts(conn, "u1", DOC_BL, None, {}, source="B/L")  # 거래처만 저장하려고 줄을 만든 경우
    assert len(conn.calls) == 1


# ── POST /api/document (routers/document.py) ──

LINES = ["BILL OF LADING", f"B/L NO: {DOC_BL}", "FREIGHT COLLECT", "FOB BUSAN"]


def _document(monkeypatch, lines=LINES):
    """OCR·LLM을 가짜로 바꾸고 저장 호출을 모은다."""
    monkeypatch.setattr(document_router.doc_ocr, "read_lines",
                        lambda data: SimpleNamespace(lines=lines, rotation=0, runs=1, ocr_ms=1))
    out = llm.DocFieldsOutput(vessel=None, voyage=None, eta=None, pol=None, pod=None, item=None, doc_kind="bl",
                              freight_term="collect", freight_term_text="FREIGHT COLLECT", incoterm="FOB")
    monkeypatch.setattr(llm, "extract_doc_fields", lambda masked, **k: SimpleNamespace(output=out))
    calls = []

    def save(user_id, bl_no, fields, doc_kind):
        calls.append((user_id, bl_no, doc_kind, fields.freight_term.value))
        return ["freight_term", "incoterm"] if user_id else []
    monkeypatch.setattr(document_router.doc_facts, "save_from_document", save)
    return calls


def test_document_saves_when_logged_in_and_bl_ok(monkeypatch):
    calls = _document(monkeypatch)
    headers = _login(monkeypatch)
    with TestClient(app) as client:
        body = client.post("/api/document", files={"file": ("a.jpg", b"x", "image/jpeg")}, headers=headers).json()
    assert body["bl_status"] == "ok" and body["facts_saved"] == ["freight_term", "incoterm"]
    assert calls == [("user-123", DOC_BL, "B/L", "collect")]
    assert "collect" not in str(body["facts_saved"])  # 이름만, 값 없음


def test_document_without_login_saves_nothing(monkeypatch):
    calls = _document(monkeypatch)
    with TestClient(app) as client:
        body = client.post("/api/document", files={"file": ("a.jpg", b"x", "image/jpeg")}).json()
    assert body["facts_saved"] == [] and calls[0][0] is None  # 로그인 꺼진 환경: user_id 없이 불려 아무것도 안 한다


def test_document_with_uncertain_bl_saves_nothing(monkeypatch):
    calls = _document(monkeypatch, ["BILL OF LADING", "FREIGHT COLLECT"])  # B/L 번호 줄이 없다
    headers = _login(monkeypatch)
    with TestClient(app) as client:
        body = client.post("/api/document", files={"file": ("a.jpg", b"x", "image/jpeg")}, headers=headers).json()
    assert body["bl_status"] != "ok" and body["facts_saved"] == [] and calls == []


# ── 답 줄 (services/doc_facts.reply_lines, llm_view) ──

FACTS = {"conditions": {"freight_term": "collect", "incoterm": "FOB", "original_bl": "required", "to_order_bank": False},
         "unconfirmed": True, "contacts": [{"company": COMPANY, "role": "포워더", "phone": PHONE}]}


def test_reply_lines_are_code_written():
    lines = doc_facts.reply_lines(FACTS)
    assert lines[0] == ("서류 사진에서 읽은 값(확인 전)이에요. 운임: 착불(도착지 지급), 인코텀즈: FOB, "
                        "원본 B/L: 필요, 은행 지시식: 아니요.")
    assert lines[1] == f"화물 인도 연락처: {COMPANY}(포워더) {PHONE}"


def test_llm_view_has_no_values():
    view = doc_facts.llm_view(FACTS)
    text = str(view)
    assert view["stored"] == ["freight_term", "incoterm", "original_bl", "to_order_bank"] and view["contact"] is True
    for value in ("collect", "FOB", "required", COMPANY, PHONE):
        assert value not in text


# ── 채팅 도구 cargo_documents (services/chat.py) ──

DOCS = [("cargo_documents", {"bl_ref": None})]


def test_chat_tool_appends_code_lines_and_hides_values_from_llm(monkeypatch):
    results = _llm(monkeypatch, DOCS)
    asked = []
    monkeypatch.setattr(doc_facts, "read", lambda user_id, bl: asked.append((user_id, bl)) or FACTS)
    r = svc.handle("운임 착불이야?", "s1", CONTEXT, _run(), USER)
    assert asked == [(USER, BL)]  # 문장에 B/L이 없으면 화면 화물
    assert "운임: 착불(도착지 지급)" in r.reply and f"화물 인도 연락처: {COMPANY}(포워더) {PHONE}" in r.reply
    tool_result = str(results[0][1])
    for value in ("collect", "착불", COMPANY, PHONE, BL):
        assert value not in tool_result


def test_chat_tool_nothing_stored(monkeypatch):
    _llm(monkeypatch, DOCS)
    monkeypatch.setattr(doc_facts, "read", lambda user_id, bl: None)
    r = svc.handle("인코텀즈 뭐였지?", "s1", CONTEXT, _run(), USER)
    assert r.reply == doc_facts.NOTHING_STORED


def test_chat_tool_needs_login(monkeypatch):
    _llm(monkeypatch, DOCS)
    monkeypatch.setattr(doc_facts, "read", lambda *a: pytest.fail("로그인 없이 읽으면 안 된다"))
    r = svc.handle("원본 B/L 필요해?", "s1", CONTEXT, _run(), None)
    assert r.reply == doc_facts.NEED_LOGIN


def test_chat_tool_resolves_bl_token_without_lookup(monkeypatch):
    """[BL1] 토큰은 문장의 B/L로 푼다. 서류 정보만 물었으면 화물 조회(판정 흐름)를 따로 부르지 않는다."""
    results = _llm(monkeypatch, [("cargo_documents", {"bl_ref": "[BL1]"})])
    asked = []
    monkeypatch.setattr(doc_facts, "read", lambda user_id, bl: asked.append(bl) or FACTS)

    def run(req):
        raise AssertionError("서류 정보 질문에 화물 조회를 하면 안 된다")
    r = svc.handle("TESTBL09 운임 누가 내?", "s1", None, run, USER)
    assert asked == ["TESTBL09"] and "인코텀즈: FOB" in r.reply
    assert "TESTBL09" not in str(results[0][1])


def test_chat_tool_unknown_token_asks_for_bl(monkeypatch):
    _llm(monkeypatch, [("cargo_documents", {"bl_ref": "[BL7]"})])
    monkeypatch.setattr(doc_facts, "read", lambda *a: pytest.fail("지어낸 토큰으로 읽으면 안 된다"))
    r = svc.handle("운임 누가 내?", "s1", CONTEXT, _run(), USER)
    assert r.reply == doc_facts.NEED_CARGO


def test_classic_route_answers_by_keyword(monkeypatch):
    """에이전트를 끈 전 방식도 서류 정보 질문을 낱말로 골라 코드 답을 낸다 (LLM 의도 분류를 부르지 않는다)."""
    monkeypatch.setenv("CHAT_AGENT", "off")
    monkeypatch.setattr(llm, "route_chat", lambda *a, **k: pytest.fail("의도 분류를 부르면 안 된다"))
    monkeypatch.setattr(doc_facts, "read", lambda user_id, bl: FACTS)
    r = svc.handle("이거 운임 선불이야?", "s1", CONTEXT, _run(), USER)
    assert r.reply.startswith(doc_facts.UNCONFIRMED_HEAD)
