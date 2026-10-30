"""검색 질의를 이미 가린 글로만 만든다 (docs/specs/v2-rag.md 5절 1번, EC-06, 작업 R6).

원문 사용자 글(B/L, MRN, 컨테이너 번호, 연락처가 든 글)이 검색 질의와 span에 새지 않게, 질의 함수는 MaskedText만 받는다.
MaskedText는 str 하위 타입이고 직접 만들 수 없다. MaskedText.of()가 B/L, MRN, 컨테이너 번호 모양이 남은 글을 거부한다.
가리는 규칙은 새로 만들지 않고 services/chat.py의 것을 쓴다 (mask_bls, find_containers, _MRN, _PHONE_IN_HISTORY).
chat.py는 나중에 이 패키지를 불러오므로(R8) 순환을 피하려고 함수 안에서 늦게 import한다.
LLM은 질의를 쓰지 않는다. 질의는 코드가 가린 문장에서 만든다 (Spec Q8).
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from backend.app.services.pii_patterns import EMAIL, MOBILE_IN_TEXT
from backend.app.services.rules import rule

# 영숫자가 이 길이 이상 이어지면 번호(B/L, MRN, 컨테이너, 송장 번호 등)일 수 있어 질의에서 지운다 (Spec EC-06 "10자 이상 영숫자 덩어리").
# 임계값이 아니라 Spec이 정한 모양 규격이라 코드에 둔다
ALNUM_RUN_MIN = 10
_ALNUM_RUN = re.compile(rf"[A-Za-z0-9]{{{ALNUM_RUN_MIN},}}")
# 가림 표시: [BL1], [MRN], [CNTR2], [PHONE], [EMAIL], [ID], [BL] 등 (chat.mask_bls, mask_containers, doc_ocr 토큰 모양)
_PLACEHOLDER = re.compile(r"\[[A-Z]{2,6}\d*\]")
_SPACES = re.compile(r"\s+")


class MaskedText(str):
    """이미 가린 글임을 타입으로 표시한 문자열. 직접 만들지 않고 MaskedText.of()로만 만든다.

    str 메서드(strip, 자르기 등)의 결과는 보통 str이라 가림 표시가 이어지지 않는다. 다시 질의에 쓰려면 of()를 다시 거친다.
    """

    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        raise TypeError("MaskedText는 직접 만들지 않습니다. MaskedText.of(가린 글)을 씁니다")

    @classmethod
    def of(cls, text: str) -> "MaskedText":
        """가린 글을 MaskedText로 감싼다. B/L, MRN, 컨테이너 번호 모양이 남아 있으면 ValueError.

        검사는 chat.py의 가리기 함수를 그대로 써서 한다: 가린 뒤 결과가 입력과 다르면 아직 가리지 않은 값이 있는 것이다.
        (날짜 숫자 8자리는 mask_bls가 B/L로 보지 않으므로 여기서도 거부하지 않는다.)
        """
        if not isinstance(text, str):
            raise TypeError("MaskedText.of는 문자열만 받습니다")
        from backend.app.services import chat  # 늦게 import: chat.py가 이 패키지를 쓰게 될 때 순환을 피한다

        raw = str(text)
        if chat.find_containers(raw):
            raise ValueError("컨테이너 번호 모양이 남아 있습니다. 가린 글만 질의에 씁니다")
        if chat.extract_mrn(raw)[0] is not None:
            raise ValueError("MRN 모양이 남아 있습니다. 가린 글만 질의에 씁니다")
        if chat.mask_bls(raw)[0] != raw:
            raise ValueError("B/L 번호 모양이 남아 있습니다. 가린 글만 질의에 씁니다")
        return str.__new__(cls, raw)


def _clean(text: MaskedText) -> str:
    """글 하나에서 자리표시자, 전화·이메일 모양, 10자 이상 영숫자 덩어리를 지우고 공백을 정리한다.

    chat.py는 현재 문장의 전화·이메일은 가리지 않고(대화 기록만 [PHONE]으로 가린다) 연락처 값은 질의에 들어가면 안 되므로 여기서 지운다.
    자리표시자를 먼저 지우면 "[BL1]"의 BL1이 영숫자 덩어리 검사에 걸릴 일이 없다.
    """
    from backend.app.services import chat

    out = _PLACEHOLDER.sub(" ", str(text))
    out = chat._PHONE_IN_HISTORY.sub(" ", out)
    out = MOBILE_IN_TEXT.sub(" ", out)
    out = EMAIL.sub(" ", out)
    out = _ALNUM_RUN.sub(" ", out)
    return _SPACES.sub(" ", out).strip()


def build_query(masked_turns: Sequence[MaskedText]) -> str:
    """가린 사용자 말 목록(오래된 것부터, 마지막이 이번 말)으로 검색 질의를 만든다 (Spec 5절 1번, EC-13).

    - 모든 항목이 MaskedText여야 한다. 일반 str이면 TypeError (가리기 전 글이 질의로 가는 길을 타입으로 막는다)
    - 이번 말과 그 앞 rag_query_turns개만 쓴다
    - 항목마다 _clean으로 자리표시자·연락처 모양·10자 이상 영숫자를 지운다
    - 합친 글이 rag_query_max_chars를 넘으면 뒤쪽(이번 말 쪽)을 남기고 앞을 자른다
    - 지우고 남은 글이 비면 빈 글자를 돌려준다. 호출한 쪽은 검색하지 않고 {hits: []}를 낸다 (EC-06)
    """
    for turn in masked_turns:
        if not isinstance(turn, MaskedText):
            raise TypeError("build_query는 MaskedText만 받습니다. 가린 글을 MaskedText.of()로 감싸서 넘깁니다")
    keep = int(rule("rag_query_turns")) + 1
    parts = [c for c in (_clean(t) for t in masked_turns[-keep:]) if c]
    query = " ".join(parts)
    limit = int(rule("rag_query_max_chars"))
    if len(query) > limit:
        query = query[-limit:].lstrip()
    return query
