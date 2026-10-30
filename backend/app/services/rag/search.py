"""하이브리드 검색: BM25 + 코사인 + RRF + 게이트 (docs/specs/v2-rag.md 5, 6절, 작업 R5).

한 요청의 흐름 (Spec 5절):
  1. 질의는 build_query(query.py)가 이미 가린 글로만 만든다. 이 모듈의 공개 입구는 MaskedText 목록을 받는 Searcher.search 하나다
  2. BM25(bm25.py)가 명사 일치로 후보를 낸다. 밀집 검색(Embedder + ChunkStore.dense)이 코사인으로 후보를 낸다
  3. 두 순위를 RRF로 합친다. 점수를 더하지 않고 순위만 합친다 (BM25 척도와 코사인 척도는 비교할 수 없다)
  4. 게이트(Spec 6절)가 막으면 조각을 주지 않는다 (모델이 환각할 근거를 없앤다). 아니면 상위 rag_top_k개를 준다
한쪽 검색이 불가하면 다른 쪽 순위만 쓰고 mode로 알린다 (hybrid, bm25, dense). 둘 다 불가하면 SearchUnavailable이고,
부르는 쪽(chat.py, R8)이 v1 경로로 응답한다 (Spec EC-04). 임계값은 모두 skills/rag-search/rules.md에서 읽는다.
이 모듈은 사용자 글, 조각 글을 로그에 남기지 않는다. 실패 로그에는 종류(예외 이름)만 쓴다 (Spec 10절).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.app.services.rag.bm25 import Bm25Index
from backend.app.services.rag.config import GateThresholds, gate_thresholds
from backend.app.services.rag.embed import Embedder
from backend.app.services.rag.query import MaskedText, build_query
from backend.app.services.rag.store import ChunkRecord, ChunkStore
from backend.app.services.rules import rule

log = logging.getLogger("marvia.rag")

# 초를 밀리초로 바꾸는 단위 환산 (임계값이 아니다)
MS_PER_SEC = 1000
# 검색 방식 이름 (응답 mode, rag:search span의 mode 칸)
MODE_HYBRID, MODE_BM25, MODE_DENSE = "hybrid", "bm25", "dense"
# 게이트가 막았을 때의 reason (Spec 6절 도구 결과 `{"hits": [], "gated": true, "reason": "low_score"}`)
REASON_LOW_SCORE = "low_score"
# 질의가 비어 검색하지 않았을 때의 reason (Spec EC-06)
REASON_EMPTY_QUERY = "empty_query"


class SearchUnavailable(Exception):
    """검색을 할 수 없다: 저장소를 못 읽거나, BM25와 밀집 검색이 모두 불가하다. 부르는 쪽은 v1 경로로 내려간다 (Spec EC-04)."""


@dataclass(frozen=True)
class SearchHit:
    """모델에게 줄 조각 하나와 점수 셋 (Spec 5절 5번: dense_cos, bm25_terms_matched, rrf).

    dense_cos는 밀집 후보에 들지 못했거나 밀집 검색을 안 했으면 None이다. 순위는 1부터이고 후보에 없으면 None이다.
    """

    chunk: ChunkRecord
    rrf: float
    dense_cos: float | None
    bm25_terms_matched: int
    dense_rank: int | None
    bm25_rank: int | None


@dataclass(frozen=True)
class SearchResult:
    """검색 한 번의 결과. hits가 비어 있으면 gated(게이트가 막음) 또는 reason(질의 비음, 일치 없음)으로 이유를 알 수 있다.

    dense_top1, bm25_terms는 게이트가 본 값이다 (로그, span, 평가가 쓴다). 둘 다 질의 글을 담지 않는다.
    """

    hits: tuple[SearchHit, ...]
    mode: str | None
    gated: bool = False
    reason: str | None = None
    gate_enabled: bool = False
    dense_top1: float | None = None
    bm25_terms: int | None = None
    query_chars: int = 0
    embed_ms: float | None = None
    search_ms: float = 0.0
    store: str | None = None
    index_id: str | None = None
    notes: tuple[str, ...] = field(default=())  # 한쪽 검색이 빠진 이유 같은 종류 메모 (예외 이름만)


def rrf_scores(rankings: Sequence[Sequence[str]], rrf_k: float) -> dict[str, float]:
    """순위 목록 여럿을 RRF로 합친다: score(d) = sum 1 / (rrf_k + rank_i(d)), 순위는 1부터 (Spec 5절 4번).
    rankings는 방식마다 id를 높은 순으로 나열한 것이다. 어느 순위에도 없는 id는 그 방식에서 점수를 받지 않는다."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank)
    return scores


class Searcher:
    """색인(ChunkStore)과 임베더로 검색하는 객체. 만들 때 BM25 통계와 게이트 임계값을 읽어 둔다 (기동 때 한 번, Spec 4절).

    embedder가 None이면 밀집 검색을 안 쓰는 모드다(RAG_MODE=bm25). 임베더가 있어도 질의 임베딩이나 저장소 dense가 실패하면
    그 요청은 BM25만으로 간다. 반대로 BM25를 못 만들면(Kiwi 실패) 밀집 검색만 쓴다.
    색인 지문 검사(Spec EC-05)는 이 클래스가 아니라 만드는 쪽이 store.info()로 한다.
    """

    def __init__(self, store: ChunkStore, embedder: Embedder | None = None):
        self.store = store
        self.embedder = embedder
        try:
            self._chunks = store.chunks()
        except Exception as exc:  # 저장소 오류 종류는 다양하다 (StoreError, DB 오류). 어느 쪽이든 검색 불가다
            raise SearchUnavailable(f"저장소를 읽지 못했다: {type(exc).__name__}") from exc
        if not self._chunks:
            raise SearchUnavailable("색인에 조각이 없다")
        self._bm25: Bm25Index | None
        try:
            self._bm25 = Bm25Index([c.embed_text for c in self._chunks])
        except Exception as exc:  # Kiwi 로드 실패 등. 밀집 검색만으로 내려간다 (로그에는 종류만)
            log.warning("rag bm25 index unavailable: %s", type(exc).__name__)
            self._bm25 = None
        if self._bm25 is None and embedder is None:
            raise SearchUnavailable("BM25도 밀집 검색도 쓸 수 없다")
        # 임계값. null이면 그 게이트는 꺼지고 경고가 한 번 남는다 (config.gate_thresholds, Spec 6절)
        self.gate: GateThresholds = gate_thresholds(embedder.model_id if embedder is not None else None)
        self._candidates = int(rule("rag_candidates"))
        self._top_k = int(rule("rag_top_k"))
        self._rrf_k = float(rule("rag_rrf_k"))
        info = store.info()
        self._index_id = info.index_id if info else None

    def search(self, masked_turns: Sequence[MaskedText]) -> SearchResult:
        """가린 사용자 말 목록(오래된 것부터, 마지막이 이번 말)으로 검색한다. 평문 str을 넘기면 TypeError (build_query가 막는다)."""
        return self._search_query(build_query(masked_turns))

    def _search_query(self, query: str) -> SearchResult:
        """build_query가 만든 질의로 검색한다. 이 함수는 build_query를 거친 글만 받는다는 약속 아래 모듈 안에서만 부른다."""
        started = time.perf_counter()
        base = dict(query_chars=len(query), store=getattr(self.store, "name", None), index_id=self._index_id)
        if not query:  # 지우고 남은 글이 없다: 검색하지 않는다 (EC-06)
            return SearchResult(hits=(), mode=None, reason=REASON_EMPTY_QUERY, **base)

        notes: list[str] = []
        embed_ms: float | None = None

        # BM25
        bm25_hits = []
        terms: list[str] = []
        bm25_ok = self._bm25 is not None
        if self._bm25 is not None:
            try:
                terms = self._bm25.query_terms(query)
                bm25_hits = self._bm25.rank(terms, self._candidates)
            except Exception as exc:
                bm25_ok = False
                notes.append(f"bm25:{type(exc).__name__}")
                log.warning("rag bm25 search failed: %s", type(exc).__name__)

        # 밀집
        dense_hits = []
        dense_ok = False
        if self.embedder is not None:
            try:
                t0 = time.perf_counter()
                qvec = self.embedder.embed_query(query)
                embed_ms = (time.perf_counter() - t0) * MS_PER_SEC
                dense_hits = self.store.dense(qvec, self._candidates)
                dense_ok = True
            except Exception as exc:  # 임베더, 저장소, 차원 불일치 모두 이 요청만 BM25 단독으로 내려간다 (EC-02, EC-03)
                notes.append(f"dense:{type(exc).__name__}")
                log.warning("rag dense search failed: %s", type(exc).__name__)

        if not bm25_ok and not dense_ok:
            raise SearchUnavailable("BM25와 밀집 검색이 모두 실패했다: " + ", ".join(notes or ["no_backend"]))
        mode = MODE_HYBRID if (bm25_ok and dense_ok) else MODE_BM25 if bm25_ok else MODE_DENSE

        # 합치기. 한쪽만 되면 그쪽 순위만으로 RRF를 낸다
        by_id = {c.chunk_id: c for c in self._chunks}
        bm25_rank = {self._chunks[h.index].chunk_id: r for r, h in enumerate(bm25_hits, start=1)} if bm25_ok else {}
        dense_rank = {h.chunk.chunk_id: r for r, h in enumerate(dense_hits, start=1)} if dense_ok else {}
        dense_cos = {h.chunk.chunk_id: h.cosine for h in dense_hits}
        rankings = []
        if bm25_ok:
            rankings.append([self._chunks[h.index].chunk_id for h in bm25_hits])
        if dense_ok:
            rankings.append([h.chunk.chunk_id for h in dense_hits])
        scores = rrf_scores(rankings, self._rrf_k)
        # 동점은 (파일, 파일 안 순서)로 가른다. 같은 입력이면 같은 순서다
        for h in dense_hits:
            by_id.setdefault(h.chunk.chunk_id, h.chunk)
        ordered = sorted(scores, key=lambda k: (-scores[k], by_id[k].source_path, by_id[k].ordinal, k))

        # 게이트가 보는 값: 밀집 1위 코사인, BM25 1위 조각에 일치한 질의 토큰 수
        dense_top1 = dense_hits[0].cosine if dense_hits else None
        bm25_terms = bm25_hits[0].matched if bm25_hits else (0 if bm25_ok else None)
        gated, gate_on = self._gate(mode, dense_top1, bm25_terms)
        search_ms = (time.perf_counter() - started) * MS_PER_SEC
        common = dict(mode=mode, gate_enabled=gate_on, dense_top1=dense_top1, bm25_terms=bm25_terms, embed_ms=embed_ms,
                      search_ms=search_ms, notes=tuple(notes), **base)
        if gated:
            return SearchResult(hits=(), gated=True, reason=REASON_LOW_SCORE, **common)

        index_of = {c.chunk_id: i for i, c in enumerate(self._chunks)}
        hits = []
        for key in ordered[: self._top_k]:
            terms_matched = self._bm25.matched(index_of[key], terms) if self._bm25 is not None and key in index_of else 0
            hits.append(SearchHit(chunk=by_id[key], rrf=scores[key], dense_cos=dense_cos.get(key),
                                  bm25_terms_matched=terms_matched, dense_rank=dense_rank.get(key),
                                  bm25_rank=bm25_rank.get(key)))
        return SearchResult(hits=tuple(hits), reason=None if hits else "no_match", **common)

    def _gate(self, mode: str, dense_top1: float | None, bm25_terms: int | None) -> tuple[bool, bool]:
        """게이트 판정 (Spec 6절). 돌려주는 값은 (막았나, 게이트가 켜져 있었나). 임계값이 null인 쪽은 꺼진 것이다.

        - hybrid: 밀집 1위 코사인이 rag_min_score[모델] 미만이고 BM25 일치 토큰 수도 rag_min_terms 미만일 때만 막는다
          (우회 표현 질문은 BM25가 0이고 밀집만 높을 수 있고, 약어 질문은 반대일 수 있어 둘 다 낮아야 막는다).
          두 값 중 하나라도 null이면 이 판정을 못 하므로 게이트는 꺼진다
        - bm25: 일치 토큰 수가 rag_min_terms 미만이면 막는다
        - dense(BM25 불가): 코사인이 rag_min_score 미만이면 막는다
        """
        score_min, terms_min = self.gate.min_score, self.gate.min_terms
        if mode == MODE_HYBRID:
            if score_min is None or terms_min is None:
                return False, False
            low_dense = dense_top1 is None or dense_top1 < score_min
            low_terms = bm25_terms is None or bm25_terms < terms_min
            return (low_dense and low_terms), True
        if mode == MODE_BM25:
            if terms_min is None:
                return False, False
            return (bm25_terms is None or bm25_terms < terms_min), True
        if score_min is None:
            return False, False
        return (dense_top1 is None or dense_top1 < score_min), True
