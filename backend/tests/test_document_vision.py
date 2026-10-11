"""POST /api/document 사진 직접 읽기 흐름 테스트 (docs/specs/document-extract.md "사진 직접 읽기"). 키 없이 돈다.

확인하는 것:
  A(OCR → UNI-PASS)가 찾으면 세관 값으로 답하고(read_by unipass) B(사진 직접 읽기)를 끊는다
  A가 못 찾으면 B의 값으로 답한다(read_by vision). OCR 글자에 없는 값이어도 not_in_text를 걸지 않는다
  B의 B/L이 OCR과 다르고 선사 부호도 목록에 없으면 ambiguous(사용자 확인)
  B가 실패하면 OCR 정리 길로 돌아간다(read_by ocr)
  DOC_VISION_MODEL이 비면 지금처럼 OCR 정리 길만 쓴다
OCR은 가짜 줄을 쓰고, LLM·UNI-PASS는 가짜로 바꾼다. 번호는 모두 가공값이다.
"""
import threading
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.main import app
from backend.app.routers import document as doc_router
from backend.app.schemas.customs import CustomsRecord
from backend.app.services import doc_ocr, llm

LINES = ["ARRIVAL NOTICE", "H.B/1.N1.", ":PCI.UTEST000001", "Vessel & Voy: SEA STAR / 0925R", "MRN: 26ABCD12341"]
OCR_TEXT_ANSWER = dict(vessel="SEA STAR", voyage="0925R", eta=None, pol=None, pod=None, item=None)


def _vision_out(**kw):
    base = dict(bl_no="PCLUTEST000001", vessel="SEA STAR", voyage="0925W", eta="2026-10-03", etd="2026-09-30",
                pol="QINGDAO, CHINA", pod="BUSAN, KOREA", item="TEST GOODS", mrn="26ABCD1234I",
                doc_kind="arrival_notice", contact_company="TEST FORWARDER CO.", contact_role="포워더")
    base.update(kw)
    return llm.DocVisionOutput(**base)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("DOC_VISION_MODEL", "claude/claude-sonnet-5-5")
    monkeypatch.setattr(doc_ocr, "read_lines", lambda data, **kw: doc_ocr.OcrResult(lines=LINES, rotation=0, runs=1, ocr_ms=1))
    calls = {"vision": [], "customs": [], "text": []}

    def fake_text(masked, **kw):
        calls["text"].append(masked)
        return SimpleNamespace(output=llm.DocFieldsOutput(**OCR_TEXT_ANSWER))

    monkeypatch.setattr(llm, "extract_doc_fields", fake_text)
    monkeypatch.setattr(doc_router, "_customs_record", lambda bl: calls["customs"].append(bl))
    return calls


def _vision(monkeypatch, calls, out=None, error=None, wait_for_cancel=False):
    def fake(image, cancel=None, model=None):
        calls["vision"].append(cancel)
        if wait_for_cancel:
            assert cancel.wait(5), "A가 이겼으면 B를 끊어야 한다"
            raise llm.LLMError("cut", kind="cancelled")
        if error:
            raise error
        return SimpleNamespace(output=out or _vision_out())

    monkeypatch.setattr(llm, "read_doc_image", fake)


def _post():
    buf = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PNG")
    res = TestClient(app).post("/api/document", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert res.status_code == 200, res.text
    return res.json()


def test_customs_found_answers_from_customs_and_cancels_vision(setup, monkeypatch):
    record = CustomsRecord(bl_masked="PCL*******0001", fetched_at=datetime(2026, 10, 1), ship_name="SEA STAR", voyage="0925W",
                           entry_date=datetime(2026, 10, 3, 9, 0), loading_port_name="Qingdao",
                           discharge_port_name="부산항", goods_name="TEST GOODS", mrn="26ABCD1234I")
    monkeypatch.setattr(doc_router, "_customs_record", lambda bl: record if bl == "PCLUTEST000001" else None)
    _vision(monkeypatch, setup, wait_for_cancel=True)
    body = _post()
    assert body["read_by"] == "unipass" and body["bl_status"] == "ok"
    f = body["fields"]
    assert (f["bl_no"]["value"], f["voyage"]["value"], f["eta"]["value"]) == ("PCLUTEST000001", "0925W", "2026-10-03")
    assert body["mrn"] == "26ABCD1234I"
    assert setup["text"] == []  # 로그인 안 한 요청은 조건 저장도 없으니 OCR 정리 LLM을 부르지 않는다


def test_customs_missing_uses_vision_values_without_text_check(setup, monkeypatch):
    _vision(monkeypatch, setup)
    body = _post()
    assert body["read_by"] == "vision" and body["status"] == "ok"
    f = body["fields"]
    # OCR은 0925R로 읽었지만 사진 직접 읽기 값(0925W)을 쓰고, OCR 글자에 없다고 걸지 않는다
    assert f["voyage"] == {"value": "0925W", "checks": []}
    assert f["bl_no"] == {"value": "PCLUTEST000001", "checks": []}
    assert body["bl_status"] == "ok" and body["mrn"] == "26ABCD1234I"
    assert setup["customs"] == ["PCLUTEST000001"]


def test_vision_bl_unknown_prefix_and_differs_from_ocr_needs_review(setup, monkeypatch):
    _vision(monkeypatch, setup, out=_vision_out(bl_no="ZZZQTEST000009"))
    body = _post()
    assert body["fields"]["bl_no"]["checks"] == ["ambiguous"] and body["bl_status"] == "uncertain"


def test_vision_failure_falls_back_to_ocr_text(setup, monkeypatch):
    _vision(monkeypatch, setup, error=llm.LLMError("busy", code=529, kind="overloaded"))
    body = _post()
    assert body["read_by"] == "ocr"
    assert body["fields"]["voyage"]["value"] == "0925R"  # OCR 정리 길의 값
    assert setup["text"] and "TEST000001" not in setup["text"][0]  # B/L은 가려서 보낸다


def test_vision_off_keeps_ocr_only(setup, monkeypatch):
    monkeypatch.delenv("DOC_VISION_MODEL")
    _vision(monkeypatch, setup)
    body = _post()
    assert body["read_by"] == "ocr" and setup["vision"] == [] and setup["customs"] == []


def test_vision_timeout_cancels_and_falls_back(setup, monkeypatch):
    monkeypatch.setattr(doc_router, "VISION_WAIT_SEC", 0.05)
    seen: list[threading.Event] = []

    def slow(image, cancel=None, model=None):
        seen.append(cancel)
        cancel.wait(5)
        raise llm.LLMError("cut", kind="cancelled")

    monkeypatch.setattr(llm, "read_doc_image", slow)
    body = _post()
    assert body["read_by"] == "ocr" and seen and seen[0].is_set()


def test_output_config_models_get_schema_in_system_not_forced_tool():
    """Sonnet은 강제 도구 호출을 받지 않아 스키마를 지시로 붙인다. Haiku는 지금처럼 강제 도구."""
    rf = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
    sonnet = llm._anthropic_request({"model": "claude/claude-sonnet-5-5", "messages": [{"role": "user", "content": "q"}],
                                     "response_format": rf})
    assert "tool_choice" not in sonnet and "JSON schema" in sonnet["system"][-1]["text"]
    haiku = llm._anthropic_request({"model": "claude/claude-haiku-5-5", "messages": [{"role": "user", "content": "q"}],
                                    "response_format": rf})
    assert haiku["tool_choice"] == {"type": "tool", "name": llm.STRUCTURED_TOOL}


def test_image_blocks_pass_through_to_anthropic():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
    _, messages = llm._to_anthropic_messages([{"role": "user", "content": [img, {"type": "text", "text": "go"}]}])
    assert messages == [{"role": "user", "content": [img, {"type": "text", "text": "go"}]}]


def test_cancel_only_for_claude_models(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test/model")
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "q"}], cancel=threading.Event())
    assert e.value.kind == "not_configured"
