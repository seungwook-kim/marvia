"""RAG 검색: BM25, RRF, 하이브리드, 게이트 (services/rag/bm25.py, search.py), 작업 R5 (docs/specs/v2-rag.md 5, 6절).

완료 기준: 합성 코퍼스에서 약어/정확한 낱말 질의는 BM25가 1위, 한쪽 검색이 불가하면 다른 쪽만으로 결과를 내고 mode가 맞게 표시되며,
Kiwi 객체를 validate._kiwi와 공유한다(두 번째 인스턴스를 만들지 않는다). 키, DB, 모델 파일 없이 돈다 (FakeEmbedder + MemoryChunkStore).
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend.app.services import rules, validate
from backend.app.services.rag import bm25, config, search
from backend.app.services.rag.bm25 import Bm25Index, tokenize
from backend.app.services.rag.chunker import ChunkParams
from backend.app.services.rag.corpus import chunk_corpus
from backend.app.services.rag.embed import Embedder, FakeEmbedder
from backend.app.services.rag.query import MaskedText
from backend.app.services.rag.search import SearchUnavailable, Searcher, rrf_scores
from backend.app.services.rag.store import ChunkRecord, MemoryChunkStore, StoreError
from backend.app.services.rules import load_rules, rule

RAG_DIR = Path(__file__).resolve().parents[1] / "app" / "services" / "rag"

DOCS = [
    ("skills/a/SKILL.md", "# A > ## 도착통지서", "A/N 도착통지서는 선사나 포워더가 화주에게 보내는 화물 도착 안내다"),
    ("skills/a/SKILL.md", "# A > ## 화물인도지시서", "D/O 화물인도지시서는 선사나 대리점이 발행한다. 운임이 지급되지 않으면 발행되지 않는다"),
    ("skills/a/SKILL.md", "# A > ## 체화료", "터미널은 무료 보관 기간이 지나면 체화료를 받는다. 금액은 터미널마다 다르다"),
    ("skills/b/SKILL.md", "# B > ## 신항", "부산 신항은 서쪽에 있는 컨테이너 항만이다. 대형 컨테이너선이 대부분 여기 댄다"),
    ("skills/b/SKILL.md", "# B > ## 묘박지", "묘박지는 닻을 내리고 부두 자리를 기다리는 구역이다"),
]


def make_store(embedder=None, docs=DOCS, with_vectors=True):
    """합성 코퍼스 저장소. embed_text는 제목 경로 + 글이다."""
    embedder = embedder or FakeEmbedder()
    texts = [f"{heading}\n{text}" for _, heading, text in docs]
    vectors = embedder.embed_passages(texts) if with_vectors else [None] * len(docs)
    chunks = [ChunkRecord(corpus_id="c", source_path=path, heading_path=heading, chunk_id=f"id{i}", anchor=f"{path}#{heading}",
                          ordinal=i, text=text, embed_text=texts[i], vector=vectors[i])
              for i, (path, heading, text) in enumerate(docs)]
    return MemoryChunkStore(chunks)


def turns(*texts):
    return [MaskedText.of(t) for t in texts]


@pytest.fixture(autouse=True)
def _fresh_warnings():
    config._warned.clear()
    yield
    config._warned.clear()


def gates(monkeypatch, min_score, min_terms, model="fake-hash"):
    """rules.md 값 대신 게이트 임계값을 넣는다 (null 상태를 벗어난 경우의 동작을 보려는 테스트용)."""
    original = load_rules()
    patched = {**original, "rag_min_score": {model: min_score}, "rag_min_terms": min_terms}
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: patched)


# ── BM25 ──

def test_tokenize_keeps_nouns_and_english_abbreviations():
    tokens = tokenize("B/L과 D/O는 AIS 자료인데 도착통지서를 받았어요")
    assert {"b/l", "d/o", "ais", "통지서", "자료"} <= set(tokens)  # Kiwi는 도착통지서를 도착, 통지서로 가른다 (색인과 질의가 같은 토큰화를 쓴다)
    assert all(len(t) >= 2 for t in tokens)
    assert tokens.count("b/l") == 1


def test_tokenize_goes_through_validate_nouns_so_kiwi_is_shared(monkeypatch):
    """Kiwi를 직접 만들지 않고 validate._kiwi를 거친다: 그 자리를 바꾸면 토큰화가 바뀐다."""
    fake_kiwi = SimpleNamespace(tokenize=lambda text: [SimpleNamespace(form="가짜명사", tag="NNG", start=0, len=4)])
    monkeypatch.setattr(validate, "_kiwi", lambda: fake_kiwi)
    assert tokenize("가짜명사 글") == ["가짜명사"]  # 어절 줄기가 명사와 같으면 한 번만 나온다


def test_rag_code_never_imports_kiwi_itself():
    """두 번째 Kiwi 인스턴스를 만들 길이 없다: rag 패키지의 어떤 파일도 kiwipiepy를 import하지 않는다."""
    for path in RAG_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.split(".")[0] == "kiwipiepy" for n in names), path.name


def test_bm25_ranks_abbreviation_and_exact_word_queries_first():
    index = Bm25Index([f"{h}\n{t}" for _, h, t in DOCS])
    # 약어: D/O는 둘째 글에만 있다
    hits = index.rank(index.query_terms("D/O 누가 발행해요"), 5)
    assert hits[0].index == 1 and hits[0].matched >= 1
    # 정확한 낱말과 조사 차이: "체화료가"는 명사 체화료로 풀려 셋째 글과 맞는다
    assert index.rank(index.query_terms("체화료가 뭐예요"), 5)[0].index == 2
    assert index.rank(index.query_terms("묘박지는 어디"), 5)[0].index == 4
    # 일치가 없으면 결과가 없다
    assert index.rank(index.query_terms("날씨 어때"), 5) == []


def test_bm25_score_properties():
    index = Bm25Index(["가나 다라 다라", "가나 마바", "사아"], k1=1.5, b=0.75)
    terms = ["가나"]
    assert index.score(0, terms) > 0 and index.score(2, terms) == 0
    assert index.matched(0, ["가나", "다라", "없음"]) == 2
    # 같은 낱말을 질의에서 되풀이해도 질의 토큰은 한 번만 센다
    assert index.query_terms("체화료 체화료 체화료") == ["체화료"]
    # 더 짧은 글(같은 빈도)이 더 높다: 길이 정규화
    short = Bm25Index(["체화료", "체화료 " + "기타낱말 " * 30])
    assert short.score(0, ["체화료"]) > short.score(1, ["체화료"])
    # 결과는 점수 내림차순, 동점은 앞 글 먼저 (결정적)
    tie = Bm25Index(["체화료", "체화료"])
    assert [h.index for h in tie.rank(["체화료"], 5)] == [0, 1]
    assert len(tie.rank(["체화료"], 1)) == 1 and len(tie) == 2


def test_bm25_values_come_from_rules():
    index = Bm25Index(["가나"])
    assert (index.k1, index.b) == (float(rule("rag_bm25_k1")), float(rule("rag_bm25_b")))


# ── RRF ──

def test_rrf_scores_match_hand_calculation():
    scores = rrf_scores([["a", "b", "c"], ["b", "a", "d"]], rrf_k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63) and scores["d"] == pytest.approx(1 / 63)
    assert rrf_scores([["a", "b"]], rrf_k=60) == {"a": pytest.approx(1 / 61), "b": pytest.approx(1 / 62)}
    assert rrf_scores([], rrf_k=60) == {}


# ── 검색 ──

def test_hybrid_search_finds_exact_word_and_abbreviation_queries_first():
    searcher = Searcher(make_store(), FakeEmbedder())
    result = searcher.search(turns("D/O 발행은 누가 해요"))
    assert result.mode == "hybrid" and not result.gated and result.gate_enabled is False
    assert result.hits[0].chunk.heading_path == "# A > ## 화물인도지시서"
    assert result.hits[0].bm25_rank == 1 and result.hits[0].dense_rank is not None
    assert result.hits[0].dense_cos is not None and result.hits[0].bm25_terms_matched >= 1
    assert len(result.hits) <= int(rule("rag_top_k"))
    assert result.search_ms > 0 and result.embed_ms is not None and result.store == "memory"
    top = searcher.search(turns("체화료는 얼마예요")).hits[0]
    assert top.chunk.heading_path == "# A > ## 체화료"
    assert [h.rrf for h in result.hits] == sorted((h.rrf for h in result.hits), reverse=True)


def test_search_is_deterministic():
    searcher = Searcher(make_store(), FakeEmbedder())
    a, b = searcher.search(turns("신항 컨테이너")), searcher.search(turns("신항 컨테이너"))
    assert [(h.chunk.chunk_id, h.rrf) for h in a.hits] == [(h.chunk.chunk_id, h.rrf) for h in b.hits]


def test_without_embedder_it_is_bm25_only():
    result = Searcher(make_store(with_vectors=False), None).search(turns("체화료가 뭐예요"))
    assert result.mode == "bm25" and result.embed_ms is None
    assert result.hits[0].chunk.heading_path == "# A > ## 체화료"
    assert all(h.dense_cos is None and h.dense_rank is None for h in result.hits)


def test_bm25_unavailable_falls_back_to_dense_only(monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("kiwi down")

    monkeypatch.setattr(search, "Bm25Index", Broken)
    result = Searcher(make_store(), FakeEmbedder()).search(turns("체화료 보관 기간"))
    assert result.mode == "dense" and result.hits
    assert all(h.bm25_rank is None and h.bm25_terms_matched == 0 for h in result.hits)
    assert result.bm25_terms is None


def test_embedder_failure_falls_back_to_bm25_for_that_request(caplog):
    class BrokenEmbedder(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError("model gone")

    searcher = Searcher(make_store(), BrokenEmbedder())
    with caplog.at_level(logging.WARNING, logger="marvia.rag"):
        result = searcher.search(turns("체화료가 뭐예요"))
    assert result.mode == "bm25" and result.hits[0].chunk.heading_path == "# A > ## 체화료"
    assert result.notes == ("dense:RuntimeError",)
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert "체화료" not in caplog.text  # 로그에는 종류만 남고 질의 글은 남지 않는다


def test_store_dense_failure_falls_back_to_bm25():
    class NoVectors(MemoryChunkStore):
        def dense(self, query_vec, k):
            raise StoreError("pgvector 없음")

    store = make_store()
    broken = NoVectors(store.chunks())
    result = Searcher(broken, FakeEmbedder()).search(turns("묘박지는 어디"))
    assert result.mode == "bm25" and result.hits[0].chunk.heading_path == "# B > ## 묘박지"


def test_dimension_mismatch_falls_back_to_bm25():
    result = Searcher(make_store(), FakeEmbedder(dim=16)).search(turns("묘박지는 어디"))
    assert result.mode == "bm25" and result.notes and result.notes[0].startswith("dense:")


def test_both_backends_failing_raises_search_unavailable(monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            raise RuntimeError

    class BrokenEmbedder(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError

    monkeypatch.setattr(search, "Bm25Index", Broken)
    with pytest.raises(SearchUnavailable):
        Searcher(make_store(), None)  # 만들 때부터 쓸 수 있는 방식이 없다
    monkeypatch.undo()
    searcher = Searcher(make_store(), BrokenEmbedder())
    searcher._bm25 = None  # 만든 뒤 BM25가 사라진 경우: 요청마다 둘 다 불가하면 SearchUnavailable
    with pytest.raises(SearchUnavailable):
        searcher.search(turns("체화료"))


def test_unreadable_or_empty_store_raises_search_unavailable():
    class BadStore(MemoryChunkStore):
        def chunks(self):
            raise StoreError("파일 없음")

    with pytest.raises(SearchUnavailable):
        Searcher(BadStore([]), FakeEmbedder())
    with pytest.raises(SearchUnavailable):
        Searcher(MemoryChunkStore([]), FakeEmbedder())


def test_empty_query_does_not_search_and_plain_str_is_rejected():
    searcher = Searcher(make_store(), FakeEmbedder())
    result = searcher.search(turns("[BL1] [MRN]", "[CNTR1]"))
    assert result.hits == () and result.reason == search.REASON_EMPTY_QUERY and result.mode is None and not result.gated
    assert result.query_chars == 0 and result.embed_ms is None
    with pytest.raises(TypeError):
        searcher.search(["체화료가 뭐예요"])


def test_no_match_gives_no_hits_in_bm25_mode():
    result = Searcher(make_store(with_vectors=False), None).search(turns("날씨 어때요"))
    assert result.hits == () and result.reason == "no_match" and result.mode == "bm25"


def test_result_does_not_carry_the_query_text():
    searcher = Searcher(make_store(), FakeEmbedder())
    result = searcher.search(turns("고유질의어 체화료"))
    assert "고유질의어" not in repr(result)
    assert result.query_chars == len("고유질의어 체화료")


# ── 게이트 ──

def test_gate_is_off_while_thresholds_are_null_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="marvia.rag"):
        searcher = Searcher(make_store(), FakeEmbedder())
        result = searcher.search(turns("완전히 상관없는 글 날씨 점심"))
    assert result.gated is False and result.gate_enabled is False and result.hits  # 게이트가 꺼져 있어 그냥 준다
    assert any("rag_min_score" in r.getMessage() for r in caplog.records)
    bm25_only = Searcher(make_store(with_vectors=False), None).search(turns("날씨"))
    assert bm25_only.gate_enabled is False


def test_hybrid_gate_blocks_only_when_both_signals_are_low(monkeypatch):
    gates(monkeypatch, min_score=0.99, min_terms=1)
    searcher = Searcher(make_store(), FakeEmbedder())
    # 일치 명사가 없고 코사인도 낮다: 막는다. 조각은 하나도 주지 않는다
    blocked = searcher.search(turns("날씨 점심 메뉴"))
    assert blocked.gated and blocked.hits == () and blocked.reason == "low_score" and blocked.gate_enabled
    assert blocked.dense_top1 is not None and blocked.bm25_terms == 0
    # 코사인은 낮아도(임계값 0.99) 일치 명사가 있으면 통과한다
    passed = searcher.search(turns("체화료는 얼마예요"))
    assert not passed.gated and passed.hits and passed.bm25_terms >= 1


def test_hybrid_gate_passes_when_dense_is_high_even_without_term_match(monkeypatch):
    gates(monkeypatch, min_score=-1.0, min_terms=5)  # 코사인 최소값이 낮아 항상 통과, 일치 수 기준은 높다
    result = Searcher(make_store(), FakeEmbedder()).search(turns("날씨 점심 메뉴"))
    assert not result.gated and result.gate_enabled and result.hits


def test_bm25_mode_gate_uses_matched_terms_only(monkeypatch):
    gates(monkeypatch, min_score=None, min_terms=2)
    searcher = Searcher(make_store(with_vectors=False), None)
    assert searcher.search(turns("체화료는 얼마예요")).gated  # 일치 명사 1개 < 2
    passed = searcher.search(turns("체화료는 터미널마다 다르다"))
    assert not passed.gated and passed.gate_enabled and passed.hits and passed.bm25_terms >= 2


def test_hybrid_with_one_threshold_null_has_no_gate(monkeypatch):
    gates(monkeypatch, min_score=None, min_terms=1)
    result = Searcher(make_store(), FakeEmbedder()).search(turns("날씨 점심 메뉴"))
    assert not result.gated and not result.gate_enabled and result.hits


# ── 실제 코퍼스 A (FakeEmbedder) ──

@pytest.fixture(scope="module")
def real_searchers():
    params = ChunkParams.from_rules()
    chunks = chunk_corpus(params=params)
    embedder = FakeEmbedder()
    vectors = embedder.embed_passages([c.embed_text for c in chunks])
    records = [ChunkRecord(corpus_id=c.corpus_id, source_path=c.source_path, heading_path=c.heading_path, chunk_id=c.chunk_id,
                           anchor=c.anchor, ordinal=c.ordinal, text=c.text, embed_text=c.embed_text,
                           last_verified=c.last_verified, source_url=c.source_url, vector=vectors[i])
               for i, c in enumerate(chunks)]
    store = MemoryChunkStore(records)
    return Searcher(store, embedder), Searcher(store, None)


@pytest.mark.parametrize("question, section", [
    ("도착통지서를 못 받았어요", "## 2. A/N(도착통지서)"),
    ("체화료가 뭐예요", "## 4. 체화료·보관료"),
    ("D/O는 누가 발행해요", "## 3. 통관 끝난 뒤 반출"),
    ("묘박지가 뭐예요", "## 부두·정박지 개요"),
])
def test_real_corpus_questions_find_their_section_in_top_k(real_searchers, question, section):
    for searcher in real_searchers:  # 하이브리드와 BM25 단독 둘 다
        result = searcher.search(turns(question))
        assert any(h.chunk.heading_path.endswith(section) for h in result.hits), (searcher.embedder, question)


def test_real_corpus_bm25_puts_exact_keyword_chunk_first(real_searchers):
    _, bm25_searcher = real_searchers
    assert bm25_searcher.search(turns("체화료가 뭐예요")).hits[0].chunk.heading_path.endswith("## 4. 체화료·보관료")
    assert bm25_searcher.search(turns("묘박지가 뭐예요")).hits[0].chunk.text.endswith("| 박지, 묘박지, 정박지 | 바다에 닻을 내리고 부두 자리가 날 때까지 기다리는 구역. 화물을 내리는 곳이 아니다 |")


def test_domain_words_get_the_same_stem_whatever_particle_follows():
    """Kiwi는 사전에 없는 낱말을 문맥마다 다르게 가른다 (체화료가 / 체화료는). 어절 줄기 토큰이 두 쪽을 같은 토큰으로 만난다."""
    assert "체화료" in tokenize("체화료가 뭐예요") and "체화료" in tokenize("체화료는 얼마예요")
    assert "묘박지" in tokenize("묘박지는 어디") and "묘박지" in tokenize("묘박지는 닻을 내리고 기다리는 구역이다")
    assert "도착통지서" in tokenize("도착통지서를 못 받았어요")
    assert tokenize("체화료는").count("체화료") == 1
