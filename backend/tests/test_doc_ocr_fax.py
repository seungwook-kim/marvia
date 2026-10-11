"""팩스로 받은 서류 사진 판독 보강 테스트 (docs/specs/document-extract.md). 키 없이 돈다.

확인하는 것:
  선사 부호(B/L 앞 4글자)에서 OCR이 헷갈린 글자(I·L·1, O·0)를 목록과 맞을 때만 고친다
  폰 사진의 방향 표시(EXIF Orientation)를 따라 세운 뒤 읽는다
  라우터는 OCR이 읽은 그대로의 번호로 서류 글자를 가리고, 응답에는 고친 번호를 준다
번호는 모두 가공값이다.
"""
from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.main import app
from backend.app.services import doc_ocr, llm


def test_prefix_i_read_for_l_is_fixed():
    assert doc_ocr.fix_carrier_prefix("PCIUTEST000001") == "PCLUTEST000001"


def test_prefix_zero_read_for_o_is_fixed():
    assert doc_ocr.fix_carrier_prefix("0OLUTEST000001") == "OOLUTEST000001"


def test_known_prefix_is_kept():
    assert doc_ocr.fix_carrier_prefix("MAEUTEST000001") == "MAEUTEST000001"


def test_forwarder_prefix_not_near_any_carrier_is_kept():
    # 포워더 하우스 B/L은 부호가 목록에 없다. 헷갈리는 글자를 바꿔도 목록과 안 맞으면 그대로
    assert doc_ocr.fix_carrier_prefix("MRVA2026090001") == "MRVA2026090001"


def test_two_possible_carriers_is_kept(monkeypatch):
    # 바꿔서 맞는 부호가 둘이면 어느 쪽인지 모르므로 고치지 않는다
    monkeypatch.setattr(doc_ocr, "rule", lambda key: ["AILU", "ALLU"])
    assert doc_ocr.fix_carrier_prefix("A1LUTEST000001") == "A1LUTEST000001"


def test_exif_orientation_is_applied():
    img = Image.new("RGB", (40, 20), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # 오른쪽으로 90° 돌려 보라는 표시
    buf = BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert doc_ocr.open_image(buf.getvalue()).size == (20, 40)


def test_router_masks_raw_read_and_returns_fixed_bl(monkeypatch):
    raw_lines = ["ARRIVAL NOTICE", "H.B/1.N1.", ":PCI.UTEST000001", "Vessel & Voy: SEA STAR / 0925W"]
    monkeypatch.setattr(doc_ocr, "read_lines",
                        lambda data: doc_ocr.OcrResult(lines=raw_lines, rotation=0, runs=1, ocr_ms=1))
    sent = []

    def fake(masked_text, **kwargs):
        sent.append(masked_text)
        return SimpleNamespace(output=llm.DocFieldsOutput(vessel="SEA STAR", voyage="0925W", eta=None, pol=None,
                                                          pod=None, item=None))

    monkeypatch.setattr(llm, "extract_doc_fields", fake)
    buf = BytesIO()
    Image.new("RGB", (10, 10), "white").save(buf, "PNG")
    res = TestClient(app).post("/api/document", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert res.status_code == 200
    body = res.json()
    assert body["fields"]["bl_no"]["value"] == "PCLUTEST000001"
    # LLM에 간 글자에는 읽은 그대로의 번호도, 고친 번호도 없다
    assert sent and "TEST000001" not in sent[0]
