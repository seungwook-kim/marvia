"""서류 사진 응답의 bl_status·important 계산 테스트 (10-08 사용자 결정: 채팅 첨부 사진은 확인 화면 없이 바로 조회).

화면(ChatBar)은 bl_status가 ok일 때만 그 B/L로 조회를 보낸다. 그래서 "검사에 걸리면 조회하지 않는다"를 코드에서 못 박아 둔다.
OCR·LLM 없이 칸 값만 만들어 routers/document의 순수 함수를 부른다. B/L은 가공 번호다.
"""
from backend.app.routers.document import bl_status_of, important_of
from backend.app.schemas.document import DocumentFields, FieldResult

BL = "MRVA2026090001"  # 가공 번호


def _fields(bl=BL, bl_checks=(), **others) -> DocumentFields:
    """bl_no와 원하는 칸만 채운 응답 칸. 나머지 필수 칸은 빈 값(검사 없음)."""
    empty = FieldResult(value=None, checks=[])
    base = {k: empty for k in ("vessel", "voyage", "eta", "pol", "pod", "item")}
    base.update({k: FieldResult(value=v[0], checks=list(v[1])) for k, v in others.items()})
    return DocumentFields(bl_no=FieldResult(value=bl, checks=list(bl_checks)), **base)


def test_clean_bl_is_ok():
    assert bl_status_of(_fields()) == "ok"


def test_flagged_bl_is_uncertain():
    for check in ("not_in_text", "format", "ambiguous"):
        assert bl_status_of(_fields(bl_checks=[check])) == "uncertain"


def test_bl_shape_the_chat_cannot_read_is_uncertain():
    # 숫자가 없으면 채팅(services/chat._BL)이 B/L로 알아듣지 못한다. 짧은 값·소문자도 조회 모양이 아니다
    assert bl_status_of(_fields(bl="ABCDEFGHIJ")) == "uncertain"
    assert bl_status_of(_fields(bl="MRV1")) == "uncertain"
    assert bl_status_of(_fields(bl="mrva2026090001")) == "uncertain"


def test_missing_bl_is_none():
    assert bl_status_of(_fields(bl=None, bl_checks=["missing"])) == "none"
    assert bl_status_of(_fields(bl=None)) == "none"


def test_important_lists_only_clean_field_names():
    fields = _fields(vessel=("SEA STAR", []), voyage=("0925W", ["not_in_text"]), eta=("2026-10-02", []),
                     etd=(None, []), pol=("QINGDAO", []))
    # 값은 담지 않고 이름만. 검사에 걸린 항차, 비어 있는 출항일, 중요 칸이 아닌 선적항은 빠진다
    assert important_of(fields) == ["vessel", "eta"]
