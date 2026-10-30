"""rag:search tool span과 검색 시간 상한 테스트 (services/rag/runtime.py), 작업 R10 (docs/specs/v2-rag.md 10절, AC-05).

키 없이 돈다: 가짜 Langfuse 클라이언트(test_trace_root_span.py 방식)로 확인한다.
지키는 것:
  - 키가 없으면 span 없이 그대로 돈다
  - span 이름은 rag:search, as_type은 tool, input은 비어 있고, metadata와 output은 Spec 10절 칸이다
  - 어느 칸에도 질의 글, 사용자 원문, 조각 글이 없다
  - 노드 span이 열려 있으면 그 아래, 아니면 요청 루트 span 아래에 걸린다
  - rag_search_timeout_ms가 null이면 상한 없음, 값이 있으면 넘긴 검색이 search_timeout 도구 실패가 된다
"""
import time
from types import SimpleNamespace

import pytest

from backend.app.observability import langfuse as tracing
from backend.app.observability.logging import request_id_var
from backend.app.services import llm, rules
from backend.app.services.rag import runtime
from backend.app.services.rag.search import SearchUnavailable
from backend.tests.test_rag_chat import ANSWER, GOOD, QUESTION, ask, built_index, rag_on, scripted  # noqa: F401 - 픽스처와 도우미
from backend.tests.test_trace_root_span import FakeClient

QUERY_ONLY_WORDS = ("못 받으면", "어떻게", "A/N")  # 질문에만 있고 코퍼스 글에는 없는 낱말(조각 글과 헷갈리지 않게)
SPAN_META_KEYS = {"query_chars", "mode", "top_k", "n_hits", "gated", "dense_top1", "bm25_terms", "embed_ms", "search_ms", "index_id", "store"}


class InputClient(FakeClient):
    """FakeClient에 열릴 때 받은 input도 모아 둔다 (input이 비었는지 보려고)."""

    def __init__(self):
        super().__init__()
        self.inputs: dict[str, object] = {}

    def start_observation(self, *, name, as_type, input=None, trace_context=None, metadata=None, **extra):
        span = super().start_observation(name=name, as_type=as_type, input=input, trace_context=trace_context, metadata=metadata, **extra)
        span.input = input
        return span


@pytest.fixture
def fake(monkeypatch):
    import langfuse

    client = InputClient()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(langfuse, "get_client", lambda: client)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    return client


@pytest.fixture(autouse=True)
def _fresh_runtime():
    runtime.reset()
    yield
    runtime.reset()


@pytest.fixture
def searcher(monkeypatch, built_index):  # noqa: F811
    """가짜 임베더 색인 위의 hybrid Searcher (키, DB, 모델 없음)."""
    from backend.app.services.rag import store as store_mod
    from backend.app.services.rag.store import FileChunkStore

    index_dir, manifest = built_index
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: FileChunkStore(index_dir, manifest))
    monkeypatch.setenv("RAG_MODE", "hybrid")
    found = runtime.active_searcher()
    assert found is not None
    return found


def dump(span) -> str:
    """span 하나에 실린 모든 것을 글로 펼친다 (input, output, metadata, 상태)."""
    return repr((getattr(span, "input", None), span.fields))


def rag_span(fake):
    spans = fake.named("rag:search")
    assert len(spans) == 1
    return spans[0]


# ── 키 없음 ──

def test_no_keys_search_still_works_without_span(monkeypatch, searcher):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    result, found = runtime.run_search(searcher, [QUESTION])
    assert found is not None and result["hits"]


# ── span 칸 ──

def test_span_has_spec_fields_and_empty_input(fake, searcher):
    result, found = runtime.run_search(searcher, [QUESTION])
    span = rag_span(fake)
    assert span.as_type == "tool" and span.input is None and span.ended.is_set()
    meta = span.fields["metadata"]
    assert SPAN_META_KEYS <= set(meta)
    assert meta["mode"] == "hybrid" and meta["n_hits"] == len(found.hits) > 0 and meta["gated"] is False
    assert meta["query_chars"] == found.query_chars and meta["top_k"] == 4  # top_k는 rules.md rag_top_k(시작값 4)다
    assert meta["store"] == "file" and 0 < len(meta["index_id"]) <= 8 and meta["latency_ms"] >= 0
    assert meta["search_ms"] is not None and meta["embed_ms"] is not None
    output = span.fields["output"]
    assert output["chunk_ids"] == [h.chunk.chunk_id for h in found.hits]
    assert len(output["scores"]) == len(found.hits) and {"rrf", "dense_cos", "bm25_terms"} == set(output["scores"][0])
    assert "level" not in span.fields  # 정상은 ERROR가 아니다


def test_no_query_or_chunk_text_anywhere_in_span(fake, searcher):
    result, found = runtime.run_search(searcher, [QUESTION])
    text = dump(rag_span(fake))
    for word in QUERY_ONLY_WORDS:
        assert word not in text, word
    for hit in found.hits:  # 조각 글 앞부분도 없다 (조각 id와 점수만)
        assert hit.chunk.text[:20] not in text and hit.chunk.heading_path not in text


def test_history_text_is_not_in_span_either(fake, searcher):
    runtime.run_search(searcher, ["컨테이너 체화료 알려줘 비밀문장", QUESTION])
    assert "비밀문장" not in dump(rag_span(fake))


def test_blocked_query_leaves_a_span_with_no_text(fake, searcher):
    result, found = runtime.run_search(searcher, ["TESTBL99 도착통지서 비밀문장"])
    span = rag_span(fake)
    assert found is None and result["error"]["code"] == "query_unsafe"
    assert span.fields["metadata"]["blocked"] == "query_unsafe" and "level" not in span.fields
    assert "TESTBL99" not in dump(span) and "비밀문장" not in dump(span)


def test_gated_search_span(fake, monkeypatch, searcher):
    original = rules.load_rules()
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_min_terms": 10 ** 6, "rag_min_score": {"fake-hash": 1.0}})
    runtime.reset()
    gated = runtime.active_searcher()
    result, found = runtime.run_search(gated, [QUESTION])
    meta = rag_span(fake).fields["metadata"]
    assert result["gated"] is True and meta["gated"] is True and meta["n_hits"] == 0 and meta["reason"] == "low_score"
    assert meta["gate_enabled"] is True and rag_span(fake).fields["output"]["chunk_ids"] == []


def test_search_failure_marks_span_error_without_text(fake):
    class Dead:
        store = SimpleNamespace(name="file")

        def search(self, turns):
            raise SearchUnavailable(f"질의 비밀문장 {turns}")  # 예외 메시지에 글이 섞여도 span에는 이름만 간다
    result, found = runtime.run_search(Dead(), [QUESTION])
    span = rag_span(fake)
    assert found is None and result["error"]["code"] == "search_unavailable"
    assert span.fields["level"] == "ERROR" and span.fields["status_message"] == "search_unavailable"
    assert span.fields["metadata"]["failure"] == "SearchUnavailable"
    assert "비밀문장" not in dump(span) and "도착통지서" not in dump(span)


# ── 부모 ──

def test_span_attaches_to_request_root(fake, searcher):
    token = request_id_var.set("req-rag")
    try:
        with tracing.request_span("/api/chat"):
            gen = tracing.start_generation("marvia-chat-agent-rag", "{}")  # 같은 요청의 generation과 한 트레이스
            runtime.run_search(searcher, [QUESTION])
    finally:
        request_id_var.reset(token)
    root = fake.named("request:/api/chat")[0]
    span = rag_span(fake)
    assert span.parent_id == root.id and span.trace_id == root.trace_id == gen.trace_id


def test_span_attaches_under_graph_node(fake, searcher):
    with tracing.request_span("/api/chat"):
        with tracing.node_scope("agent"):
            runtime.run_search(searcher, [QUESTION])
    node = fake.named("node:agent")[0]
    assert rag_span(fake).parent_id == node.id


def test_chat_turn_leaves_one_span_under_the_request(fake, monkeypatch, rag_on):  # noqa: F811
    """채팅 한 턴에서 span이 하나 남고 그 조상에 요청 루트가 있다 (AGENT_GRAPH가 켜져 있으면 중간에 노드 span이 낀다)."""
    local = lambda name, label="production": tracing.Prompt(  # noqa: E731 - 가짜 키로 진짜 Langfuse를 부르지 않게 로컬 사본을 읽는다
        name=name, text=(tracing.PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8"), version=tracing.LOCAL_VERSION)
    monkeypatch.setattr(llm, "get_prompt", local)
    scripted_calls = scripted(monkeypatch, GOOD)
    with tracing.request_span("/api/chat"):
        assert ask().reply == ANSWER
    root = fake.named("request:/api/chat")[0]
    by_id = {s.id: s for s in fake.spans}
    parent, chain = by_id.get(rag_span(fake).parent_id), []
    while parent is not None:
        chain.append(parent.id)
        parent = by_id.get(parent.parent_id)
    assert scripted_calls and root.id in chain
    assert all(w not in dump(s) for s in fake.spans for w in QUERY_ONLY_WORDS)


# ── 시간 상한 ──

class Slow:
    """search가 지정한 초만큼 걸리는 가짜 검색기."""
    store = SimpleNamespace(name="file")

    def __init__(self, seconds, inner=None):
        self.seconds, self.inner = seconds, inner

    def search(self, turns):
        time.sleep(self.seconds)
        return self.inner.search(turns)


def set_timeout(monkeypatch, value):
    original = rules.load_rules()
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_search_timeout_ms": value})


def test_null_timeout_means_no_limit(monkeypatch, searcher):
    assert rules.rule("rag_search_timeout_ms") is None  # 실제 rules.md 값은 R10 측정 뒤에 정한다
    result, found = runtime.run_search(Slow(0.2, searcher), [QUESTION])
    assert found is not None and result["hits"]


def test_timeout_turns_slow_search_into_tool_error(fake, monkeypatch, searcher):
    set_timeout(monkeypatch, 50)
    started = time.perf_counter()
    result, found = runtime.run_search(Slow(1.0, searcher), [QUESTION])
    assert time.perf_counter() - started < 0.9  # 검색이 끝나기를 기다리지 않는다
    assert found is None and result["error"] == {"code": "search_timeout", "message": runtime.MSG_UNAVAILABLE, "retryable": False}
    span = rag_span(fake)
    assert span.fields["level"] == "ERROR" and span.fields["status_message"] == "search_timeout"


def test_search_within_timeout_is_unchanged(monkeypatch, searcher):
    set_timeout(monkeypatch, 5000)
    result, found = runtime.run_search(searcher, [QUESTION])
    assert found is not None and result["hits"]


def test_exception_inside_timeout_thread_is_still_a_tool_error(monkeypatch):
    set_timeout(monkeypatch, 5000)

    class Dead:
        store = SimpleNamespace(name="file")

        def search(self, turns):
            raise SearchUnavailable("x")
    result, found = runtime.run_search(Dead(), [QUESTION])
    assert found is None and result["error"]["code"] == "search_unavailable"


def test_timed_out_search_in_a_chat_turn_ends_as_fixed_notice(monkeypatch, rag_on):  # noqa: F811
    from backend.app.services import chat as svc

    set_timeout(monkeypatch, 50)
    monkeypatch.setattr(runtime, "active_searcher", lambda: Slow(1.0, SimpleNamespace(search=lambda t: None)))
    calls = scripted(monkeypatch, {"search": True, "out": ("자료에 없어요.", "not_in_skills", "")})
    assert ask().reply == svc.NOT_IN_SKILLS
    assert calls[0]["result"]["error"]["code"] == "search_timeout"


# ── 지연 측정 스크립트 (scripts/measure_rag_latency.py) ──

def test_latency_queries_come_from_corpus_headings_and_are_stable():
    from scripts import measure_rag_latency as m
    from backend.app.services.rag.corpus import chunk_corpus

    first, second = m.heading_queries(), m.heading_queries()
    assert first == second and first["single"] and first["pairs"]  # 같은 코퍼스면 같은 목록
    headings = " ".join(c.heading_path for c in chunk_corpus())
    assert all(title in headings for title in first["single"])  # 제목에서만 만든다(사용자 질문이 아니다)
    assert len(set(first["single"])) == len(first["single"])
    assert len(first["pairs"]) == len(first["single"]) - 1


def test_latency_percentile_is_linear_interpolation():
    from scripts import measure_rag_latency as m

    assert m.percentile([7.0], 0.95) == 7.0
    assert m.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert m.percentile([4.0, 1.0, 3.0, 2.0], 1.0) == 4.0
    stats = m.summarize([1.0, 2.0, 3.0, 4.0])
    assert stats["n"] == 4 and stats["p50_ms"] == 2.5 and stats["max_ms"] == 4.0
