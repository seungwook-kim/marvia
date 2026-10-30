"""검색 게이트 임계값 읽기 (docs/specs/v2-rag.md 6절, 작업 R2). 값은 skills/rag-search/rules.md가 원본이다.

값을 코드에 적지 않는다 (CLAUDE.md "임계값을 코드에 숫자로 적지 않는다"). 읽는 곳은 services/rules.py rule() 하나다.
rag_min_score는 모델 id별 맵이고, 값이 null(아직 재지 않음)이면 그 게이트는 꺼진다 (Spec 6절 "값을 지어 쓰지 않는다").
꺼진 게이트는 조용히 지나가면 안 되므로 프로세스마다 한 번 경고를 남긴다 (운영 전환 게이트는 R9).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.app.services.rules import rule

log = logging.getLogger("marvia.rag")
_warned: set[str] = set()  # 같은 경고를 요청마다 되풀이하지 않는다. 테스트는 clear()로 비운다


def _warn_once(reason: str, message: str) -> None:
    """프로세스마다 이유 하나당 한 번만 경고한다."""
    if reason not in _warned:
        _warned.add(reason)
        log.warning(message)


def _number_or_none(value) -> float | None:
    """rules.md 값이 숫자면 숫자, null이면 None. 숫자가 아닌 글자·불리언은 오타이므로 실패시킨다."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"rules.md 임계값이 숫자도 null도 아닙니다: {value!r}")
    return float(value)


@dataclass(frozen=True)
class GateThresholds:
    """검색 게이트 임계값. None인 쪽은 그 게이트가 꺼진 것이다 (Spec 6절).

    min_score: 밀집 1위 코사인 최소값(모델 id별). min_terms: 1위 조각에 일치한 질의 명사 최소 수.
    """

    model_id: str | None
    min_score: float | None
    min_terms: int | None

    @property
    def dense_enabled(self) -> bool:
        """밀집 게이트(코사인)가 켜져 있나."""
        return self.min_score is not None

    @property
    def bm25_enabled(self) -> bool:
        """BM25 게이트(일치 명사 수)가 켜져 있나."""
        return self.min_terms is not None


def gate_thresholds(model_id: str | None) -> GateThresholds:
    """모델 id에 맞는 게이트 임계값을 읽는다. model_id가 None이면 밀집 검색을 안 쓰는 모드(bm25)라 min_score는 보지 않는다.

    맵에 그 모델 id가 없거나 값이 null이면 밀집 게이트는 꺼지고 경고를 남긴다.
    rag_min_terms가 null이면 BM25 게이트가 꺼지고 경고를 남긴다.
    """
    min_score = None
    if model_id is not None:
        table = rule("rag_min_score") or {}
        min_score = _number_or_none(table.get(model_id))
        if min_score is None:
            _warn_once(f"min_score:{model_id}",
                       f"rag_min_score[{model_id}]가 null이거나 없어 밀집 검색 게이트가 꺼졌습니다 (skills/rag-search/rules.md, R9 측정 전)")
    terms = _number_or_none(rule("rag_min_terms"))
    if terms is None:
        _warn_once("min_terms", "rag_min_terms가 null이라 BM25 검색 게이트가 꺼졌습니다 (skills/rag-search/rules.md, R9 측정 전)")
    return GateThresholds(model_id=model_id, min_score=min_score, min_terms=None if terms is None else int(terms))
