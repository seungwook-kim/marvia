"""RAG 검색 질의 가리기 (services/rag/query.py), 작업 R6 (docs/specs/v2-rag.md 5절 1번, EC-06, AC-05).

완료 기준: 원문 B/L, MRN, 컨테이너 번호, 10자 이상 영숫자가 든 입력이 질의에 남지 않거나 거부된다. 가린 글이 아닌 str을 넘기면 TypeError.
값은 모두 가짜다 (TESTBL99 등). 실제 B/L, 컨테이너 번호를 쓰지 않는다 (CLAUDE.md).
"""
import pytest

from backend.app.services import chat
from backend.app.services.rag.query import ALNUM_RUN_MIN, MaskedText, build_query
from backend.app.services.rules import rule

FAKE_BL = "TESTBL99"
FAKE_MRN = "26TESTMR01I"
_FAKE_CNTR_BASE = "ABCU123456"
FAKE_CNTR = _FAKE_CNTR_BASE + str(chat.container_check_digit(_FAKE_CNTR_BASE))  # 검산 숫자까지 맞는 가짜 컨테이너 번호


def _masked(*texts):
    return [MaskedText.of(t) for t in texts]


# ── 타입 경계 ──

def test_plain_str_is_rejected_by_build_query():
    with pytest.raises(TypeError):
        build_query(["도착통지서 못 받았어요"])
    with pytest.raises(TypeError):
        build_query("도착통지서 못 받았어요")  # 문자열 하나를 통째로 넘겨도 글자 하나하나가 str이라 거부된다
    with pytest.raises(TypeError):
        build_query(_masked("가린 글") + ["가리지 않은 글"])  # 하나라도 str이면 거부


def test_masked_text_cannot_be_built_directly():
    with pytest.raises(TypeError):
        MaskedText("아무 글")
    with pytest.raises(TypeError):
        MaskedText.of(123)
    assert isinstance(MaskedText.of("체화료가 뭐예요"), str)
    assert isinstance(MaskedText.of("체화료가 뭐예요"), MaskedText)


def test_str_methods_drop_the_masked_mark():
    """strip 같은 str 메서드의 결과는 일반 str이다. 가림 표시가 조용히 이어지지 않는다."""
    assert type(MaskedText.of(" 글 ").strip()) is str
    with pytest.raises(TypeError):
        build_query([MaskedText.of(" 글 ").strip()])


# ── 원문 번호는 MaskedText가 되지 못한다 ──

@pytest.mark.parametrize("raw", [
    f"{FAKE_BL} 화물 어디야",
    f"MRN {FAKE_MRN} 확인",
    f"내 컨테이너 {FAKE_CNTR} 반출 언제",
    f"앞 {FAKE_BL.lower()} 뒤",
])
def test_raw_identifiers_are_refused(raw):
    with pytest.raises(ValueError):
        MaskedText.of(raw)


def test_text_masked_by_chat_helpers_is_accepted_and_leaves_no_value():
    """chat.py의 가리기 함수로 가린 글은 받아 주고, 질의에는 원문 번호도 자리표시자도 남지 않는다."""
    raw = f"{FAKE_BL} 도착통지서 못 받았어요"
    masked_text, refs = chat.mask_bls(raw)
    assert refs == {"[BL1]": FAKE_BL} and "[BL1]" in masked_text
    query = build_query(_masked(masked_text))
    assert query == "도착통지서 못 받았어요"
    assert FAKE_BL not in query and "BL1" not in query

    masked_text, _ = chat.mask_bls(chat._MRN.sub("[MRN]", chat.mask_containers(f"{FAKE_CNTR} {FAKE_MRN} 체화료")[0]))
    query = build_query(_masked(masked_text))
    assert query == "체화료"
    assert not any(v in query for v in (FAKE_CNTR, FAKE_MRN, "CNTR", "MRN"))


# ── 질의 정리 ──

def test_placeholders_are_removed():
    text = "[BL1] [BL2] [MRN] [CNTR1] [PHONE] [EMAIL] [ID] [BL] 반출 서류가 뭐예요"
    assert build_query(_masked(text)) == "반출 서류가 뭐예요"


def test_long_alphanumeric_runs_are_removed_but_shorter_words_stay():
    run = "A" * ALNUM_RUN_MIN
    shorter = "A" * (ALNUM_RUN_MIN - 1)
    assert build_query(_masked(f"체화료 {run} 얼마")) == "체화료 얼마"
    assert build_query(_masked(f"체화료 {shorter} 얼마")) == f"체화료 {shorter} 얼마"
    assert build_query(_masked(f"체화료 {run}가나다 얼마")) == "체화료 가나다 얼마"


def test_digit_bearing_long_runs_are_refused_before_they_reach_the_query():
    """숫자가 든 영숫자 덩어리는 B/L 모양이라 MaskedText.of가 거부한다. 거부가 곧 질의에 남지 않는다는 뜻이다."""
    with pytest.raises(ValueError):
        MaskedText.of("송장 INVOICE2026123 확인")


def test_phone_and_email_shapes_are_removed():
    text = "연락처 051-123-4567 또는 010-1234-5678 또는 tester@example.test 로 알려 주세요"
    query = build_query(_masked(text))
    assert query == "연락처 또는 또는 로 알려 주세요"
    assert not any(ch.isdigit() for ch in query) and "@" not in query


def test_dates_and_plain_numbers_survive():
    assert build_query(_masked("2026-10-31 입항 3일 뒤")) == "2026-10-31 입항 3일 뒤"


def test_only_recent_turns_are_used_and_newest_is_last():
    keep = int(rule("rag_query_turns")) + 1
    turns = _masked(*[f"말{i}번" for i in range(keep + 3)])
    assert build_query(turns) == " ".join(f"말{i}번" for i in range(keep + 3 - keep, keep + 3))


def test_query_is_cut_to_max_chars_keeping_the_newest_text():
    limit = int(rule("rag_query_max_chars"))
    old, new = "오래된말 " * limit, "이번질문끝"
    query = build_query(_masked(old, new))
    assert len(query) <= limit and query.endswith(new)


def test_empty_after_cleaning_gives_empty_query():
    """지우고 남은 글이 없으면 빈 글자다. 호출한 쪽은 검색하지 않는다 (EC-06)."""
    assert build_query(_masked("[BL1] [MRN]", "   ", "[CNTR1]")) == ""
    assert build_query([]) == ""


def test_whitespace_is_collapsed():
    assert build_query(_masked("체화료가\n\n  뭐예요\t?")) == "체화료가 뭐예요 ?"
