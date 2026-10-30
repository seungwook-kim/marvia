"""조각 저장소 테스트 (docs/specs/v2-rag.md R3). 메모리, 파일 구현은 키도 DB도 없이 돈다.

pgvector 저장소의 실제 DB 왕복은 DATABASE_URL_TEST가 있고 그 DB에 vector 확장이 있을 때만 돈다 (없으면 건너뛴다).
DATABASE_URL_TEST는 버려도 되는 DB여야 한다: 테스트가 rag_* 표에 줄을 넣고 지운다.
"""
from __future__ import annotations

import os

import numpy as np
import psycopg
import pytest

from backend.app.services.rag.embed import FakeEmbedder
from backend.app.services.rag.store import (
    CHUNKS_FILE,
    EMBEDDINGS_FILE,
    ChunkRecord,
    FileChunkStore,
    IndexInfo,
    MemoryChunkStore,
    PgChunkStore,
    StoreError,
)

# conftest의 자동 픽스처가 테스트마다 psycopg.connect를 막는다. 통합 테스트는 막기 전의 진짜 함수가 필요해서
# 모듈을 불러오는 시점(수집 단계, 픽스처보다 앞)에 잡아 둔다
_REAL_CONNECT = psycopg.connect

TEXTS = [
    ("skills/a/SKILL.md", "# A > ## 도착통지서", "도착통지서는 선사가 보낸다"),
    ("skills/a/SKILL.md", "# A > ## 체화료", "체화료는 보관 기간이 지나면 붙는다"),
    ("skills/b/SKILL.md", "# B > ## 신항", "부산 신항은 서쪽 항만이다"),
]


def make_chunks(embedder=None, texts=TEXTS) -> list[ChunkRecord]:
    """합성 코퍼스. 글만 바꿔도 chunk_id, 벡터가 달라진다."""
    embedder = embedder or FakeEmbedder()
    vectors = embedder.embed_passages([text for _, _, text in texts])
    return [
        ChunkRecord(
            corpus_id=f"corp-{path.split('/')[1]}", source_path=path, heading_path=heading,
            chunk_id=f"c{i:02d}", anchor=f"{path}#{heading}", ordinal=i, text=text,
            embed_text=f"{heading}\n{text}", last_verified="2026-10-01",
            source_url=("https://example.test/doc",) if i == 0 else (), vector=vectors[i],
        )
        for i, (path, heading, text) in enumerate(texts)
    ]


# ---- MemoryChunkStore ----

def test_memory_dense_returns_best_match_first():
    embedder = FakeEmbedder()
    store = MemoryChunkStore(make_chunks(embedder))
    hits = store.dense(embedder.embed_query("체화료는 언제부터 붙나요"), k=3)
    assert [h.chunk.chunk_id for h in hits][0] == "c01"
    assert [h.cosine for h in hits] == sorted((h.cosine for h in hits), reverse=True)
    assert len(store.dense(embedder.embed_query("체화료"), k=2)) == 2
    assert store.dense(embedder.embed_query("체화료"), k=0) == []


def test_memory_cosine_is_true_cosine_even_if_vectors_are_not_normalized():
    base = make_chunks()
    scaled = [ChunkRecord(**{**c.__dict__, "vector": c.vector * 5.0}) for c in base]
    embedder = FakeEmbedder()
    query = embedder.embed_query("체화료는 보관 기간")
    a = MemoryChunkStore(base).dense(query, 3)
    b = MemoryChunkStore(scaled).dense(query, 3)
    assert [h.chunk.chunk_id for h in a] == [h.chunk.chunk_id for h in b]
    assert [round(h.cosine, 5) for h in a] == [round(h.cosine, 5) for h in b]


def test_memory_chunks_are_in_file_order_and_without_requirement_of_vectors():
    chunks = make_chunks()
    shuffled = [chunks[2], chunks[0], chunks[1]]
    store = MemoryChunkStore(shuffled)
    assert [c.chunk_id for c in store.chunks()] == ["c00", "c01", "c02"]
    bm25_only = MemoryChunkStore([ChunkRecord(**{**c.__dict__, "vector": None}) for c in chunks])
    assert len(bm25_only.chunks()) == 3  # BM25 통계용으로는 벡터 없이 쓴다
    with pytest.raises(StoreError, match="벡터가 없다"):
        bm25_only.dense(FakeEmbedder().embed_query("x"), 1)


def test_memory_rejects_dimension_mismatch_and_bad_vectors():
    store = MemoryChunkStore(make_chunks())
    with pytest.raises(StoreError, match="차원"):
        store.dense(np.zeros(8, dtype=np.float32), 1)
    mixed = make_chunks()
    mixed[1] = ChunkRecord(**{**mixed[1].__dict__, "vector": np.zeros(8, dtype=np.float32)})
    with pytest.raises(StoreError, match="차원"):
        MemoryChunkStore(mixed)
    bad = make_chunks()
    bad[0].vector[0] = np.nan
    with pytest.raises(StoreError, match="NaN"):
        MemoryChunkStore(bad)


def test_memory_tie_break_is_deterministic():
    one = make_chunks(texts=[("skills/a/SKILL.md", "# A", "같은 글")])[0]
    twin = ChunkRecord(**{**one.__dict__, "chunk_id": "z", "ordinal": 1})
    hits = MemoryChunkStore([twin, one]).dense(one.vector, 2)
    assert [h.chunk.chunk_id for h in hits] == ["c00", "z"]  # 점수가 같으면 파일 순서


# ---- FileChunkStore ----

def test_file_store_round_trip(tmp_path):
    chunks = make_chunks()
    FileChunkStore.write(tmp_path / "index", chunks)
    store = FileChunkStore(tmp_path / "index")
    loaded = store.chunks()
    assert [(c.chunk_id, c.text, c.source_url, c.last_verified) for c in loaded] == \
        [(c.chunk_id, c.text, c.source_url, c.last_verified) for c in chunks]
    assert np.allclose(np.stack([c.vector for c in loaded]), np.stack([c.vector for c in chunks]))
    embedder = FakeEmbedder()
    hits = store.dense(embedder.embed_query("부산 신항 서쪽 항만"), 1)
    assert hits[0].chunk.chunk_id == "c02"
    assert store.name == "file" and store.info() == IndexInfo(None, None, None, 384)  # manifest 없음


def test_file_store_write_is_byte_identical_for_same_input(tmp_path):
    FileChunkStore.write(tmp_path / "one", make_chunks())
    FileChunkStore.write(tmp_path / "two", list(reversed(make_chunks())))
    for name in (CHUNKS_FILE, EMBEDDINGS_FILE):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()


def test_file_store_reads_manifest_fingerprint(tmp_path):
    import json
    FileChunkStore.write(tmp_path / "index", make_chunks())
    (tmp_path / "index_manifest.json").write_text(json.dumps({
        "index_id": "abc123", "fingerprint": "f" * 64, "chunking": {"version": 1},
        "embedding": {"model": "fake-hash", "dim": 384}}), encoding="utf-8")
    info = FileChunkStore(tmp_path / "index").info()
    assert info == IndexInfo("abc123", "f" * 64, "fake-hash", 384, {"version": 1})


def test_file_store_errors_are_store_errors(tmp_path):
    with pytest.raises(StoreError, match="읽지 못했다"):
        FileChunkStore(tmp_path / "nothing")
    FileChunkStore.write(tmp_path / "index", make_chunks())
    lines = (tmp_path / "index" / CHUNKS_FILE).read_text(encoding="utf-8").splitlines()
    (tmp_path / "index" / CHUNKS_FILE).write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")  # 줄과 행 수가 다르다
    with pytest.raises(StoreError, match="불일치"):
        FileChunkStore(tmp_path / "index")
    with pytest.raises(StoreError, match="vector"):
        FileChunkStore.write(tmp_path / "x", [ChunkRecord(**{**make_chunks()[0].__dict__, "vector": None})])


def test_file_store_does_not_unpickle(tmp_path):
    # npy 안에 파이썬 객체(pickle)가 있으면 읽지 않는다: 데이터 파일이 코드를 실행하지 못하게 (allow_pickle=False)
    FileChunkStore.write(tmp_path / "index", make_chunks())
    np.save(tmp_path / "index" / EMBEDDINGS_FILE, np.array([{"a": 1}] * 3, dtype=object), allow_pickle=True)
    with pytest.raises(StoreError):
        FileChunkStore(tmp_path / "index")


def test_chunk_record_from_json_ignores_unknown_fields_and_fills_optionals():
    row = {"corpus_id": "c", "source_path": "p", "heading_path": "h", "chunk_id": "i", "anchor": "a",
           "ordinal": 3, "text": "t", "future_field": "ignored"}
    record = ChunkRecord.from_json(row)
    assert record.embed_text == "t" and record.source_url == () and record.effective_date is None


# ---- PgChunkStore: DB 없이 확인하는 것 ----

def test_pg_store_turns_connection_failure_into_store_error():
    def refuse():
        raise psycopg.OperationalError("down")

    with pytest.raises(StoreError, match="DB 연결 실패"):
        PgChunkStore(connect=refuse).info()


def test_pg_store_without_database_url_is_store_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(StoreError):
        PgChunkStore().chunks()


def test_pg_store_missing_table_is_store_error():
    class NoTable:
        def execute(self, *args, **kwargs):
            raise psycopg.errors.UndefinedTable("relation rag_index does not exist")

        def close(self):
            self.closed = True

    conn = NoTable()
    with pytest.raises(StoreError, match="UndefinedTable"):
        PgChunkStore(connect=lambda: conn).dense(FakeEmbedder().embed_query("x"), 1)
    assert conn.closed  # 오류가 나도 연결은 닫는다


def test_pg_write_index_validates_before_touching_db():
    def boom():
        raise AssertionError("검증이 먼저다")

    store = PgChunkStore(connect=boom)
    good = IndexInfo("idx", "fp", "fake-hash", 384, {"version": 1})
    with pytest.raises(StoreError):
        store.write_index(IndexInfo("idx", "", "fake-hash", 384), make_chunks())
    with pytest.raises(StoreError, match="차원"):
        store.write_index(IndexInfo("idx", "fp", "m", 8), make_chunks())
    assert good.index_id == "idx"


# ---- PgChunkStore: 실제 pgvector DB (DATABASE_URL_TEST 필요) ----

_URL = os.environ.get("DATABASE_URL_TEST", "")


def _has_pgvector() -> bool:
    if not _URL:
        return False
    try:
        with _REAL_CONNECT(_URL, autocommit=True, connect_timeout=3) as conn:
            return conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone() is not None
    except psycopg.Error:
        return False


@pytest.fixture
def pg_store():
    """033을 적용하고(여러 번 적용해도 같다) 테스트가 넣은 색인을 끝에 지운다. 기존 활성 색인 표시는 되돌린다."""
    from backend.app.db import MIGRATIONS_DIR

    sql = (MIGRATIONS_DIR / "033_rag_chunks.sql").read_text(encoding="utf-8")
    with _REAL_CONNECT(_URL, autocommit=True) as conn:
        conn.execute(sql)
        active_before = [r[0] for r in conn.execute("SELECT index_id FROM rag_index WHERE is_active").fetchall()]
    store = PgChunkStore(connect=lambda: _REAL_CONNECT(_URL, autocommit=True))
    yield store
    with _REAL_CONNECT(_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM rag_index WHERE index_id LIKE 'pytest-%'")
        if active_before:
            conn.execute("UPDATE rag_index SET is_active = (index_id = ANY(%s))", (active_before,))


@pytest.mark.skipif(not _has_pgvector(), reason="DATABASE_URL_TEST가 없거나 그 DB에 pgvector가 없음")
def test_pg_store_round_trip_and_atomic_swap(pg_store):
    embedder = FakeEmbedder()
    chunks = make_chunks(embedder)
    info = IndexInfo("pytest-a", "fp-a", embedder.model_id, embedder.dim, {"version": 1, "target_chars": 500})
    pg_store.write_index(info, chunks)

    assert pg_store.info() == info
    loaded = pg_store.chunks()
    assert [c.chunk_id for c in loaded] == ["c00", "c01", "c02"]
    assert loaded[0].source_url == ("https://example.test/doc",) and loaded[0].last_verified == "2026-10-01"

    hits = pg_store.dense(embedder.embed_query("체화료는 언제부터 붙나요"), 2)
    memory_hits = MemoryChunkStore(chunks).dense(embedder.embed_query("체화료는 언제부터 붙나요"), 2)
    assert [h.chunk.chunk_id for h in hits] == [h.chunk.chunk_id for h in memory_hits]  # 같은 질의에 같은 순서
    assert [round(h.cosine, 4) for h in hits] == [round(h.cosine, 4) for h in memory_hits]

    # 같은 index_id로 다시 쓰면 조각이 통째로 바뀐다
    pg_store.write_index(info, chunks[:1])
    assert [c.chunk_id for c in pg_store.chunks()] == ["c00"]

    # 새 색인을 켜면 옛 색인은 꺼진다 (읽는 쪽은 한 색인만 본다)
    other = IndexInfo("pytest-b", "fp-b", embedder.model_id, embedder.dim, {"version": 2})
    pg_store.write_index(other, chunks)
    assert pg_store.info().index_id == "pytest-b" and len(pg_store.chunks()) == 3
    with _REAL_CONNECT(_URL, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM rag_index WHERE is_active").fetchone()[0] == 1


@pytest.mark.skipif(not _has_pgvector(), reason="DATABASE_URL_TEST가 없거나 그 DB에 pgvector가 없음")
def test_pg_failed_write_leaves_active_index_untouched(pg_store):
    embedder = FakeEmbedder()
    chunks = make_chunks(embedder)
    good = IndexInfo("pytest-good", "fp", embedder.model_id, embedder.dim, {})
    pg_store.write_index(good, chunks)
    duplicate = [chunks[0], chunks[0]]  # (index_id, chunk_id) 중복: 기본키 위반으로 트랜잭션 전체가 되돌아간다
    with pytest.raises(StoreError):
        pg_store.write_index(IndexInfo("pytest-bad", "fp2", embedder.model_id, embedder.dim, {}), duplicate)
    assert pg_store.info().index_id == "pytest-good" and len(pg_store.chunks()) == 3


# ---- make_store: RAG_STORE 선택과 폴백 (Spec 4절, EC-03, EC-04) ----

def _write_file_index(tmp_path):
    FileChunkStore.write(tmp_path / "index", make_chunks())
    return tmp_path / "index"


def test_make_store_file_choice(tmp_path, monkeypatch):
    from backend.app.services.rag.store import make_store

    monkeypatch.setenv("RAG_STORE", "file")
    store = make_store(index_dir=_write_file_index(tmp_path), connect=lambda: pytest.fail("file이면 DB를 열지 않는다"))
    assert store.name == "file"


def test_make_store_falls_back_to_file_when_db_unreachable(tmp_path, monkeypatch):
    from backend.app.services.rag.store import make_store

    def refuse():
        raise psycopg.OperationalError("down")

    monkeypatch.delenv("RAG_STORE", raising=False)  # 기본 pgvector
    assert make_store(index_dir=_write_file_index(tmp_path), connect=refuse).name == "file"


def test_make_store_falls_back_when_no_active_index(tmp_path, monkeypatch):
    from backend.app.services.rag.store import make_store

    class NoRows:
        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return None

        def close(self):
            pass

    monkeypatch.setenv("RAG_STORE", "pgvector")
    assert make_store(index_dir=_write_file_index(tmp_path), connect=lambda: NoRows()).name == "file"


def test_make_store_uses_pgvector_when_active_index_exists(tmp_path, monkeypatch):
    from backend.app.services.rag.store import make_store

    class OneRow:
        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return ("idx", "fp", "fake-hash", 384, {})

        def close(self):
            pass

    monkeypatch.setenv("RAG_STORE", "pgvector")
    assert make_store(index_dir=tmp_path / "unused", connect=lambda: OneRow()).name == "pgvector"


def test_make_store_raises_when_nothing_is_usable(tmp_path, monkeypatch):
    from backend.app.services.rag.store import make_store

    monkeypatch.setenv("RAG_STORE", "file")
    with pytest.raises(StoreError):
        make_store(index_dir=tmp_path / "missing")  # EC-04: 부르는 쪽이 v1 경로로 응답한다
    monkeypatch.setenv("RAG_STORE", "elsewhere")
    with pytest.raises(StoreError, match="알 수 없는"):
        make_store(index_dir=tmp_path)


def test_make_store_warns_once_when_table_is_missing(tmp_path, monkeypatch, caplog):
    """033이 pgvector 없는 DB에서 표를 만들지 않은 경우(표 없음)에도 파일 색인으로 내려가고, 경고는 한 번만 남는다."""
    import logging

    from backend.app.services.rag import store as store_module

    class NoTable:
        def execute(self, *args, **kwargs):
            raise psycopg.errors.UndefinedTable("relation rag_index does not exist")

        def close(self):
            pass

    monkeypatch.setenv("RAG_STORE", "pgvector")
    monkeypatch.setattr(store_module, "_warned_fallback", False)
    index = _write_file_index(tmp_path)
    with caplog.at_level(logging.WARNING, logger=store_module.__name__):
        assert store_module.make_store(index_dir=index, connect=lambda: NoTable()).name == "file"
        assert store_module.make_store(index_dir=index, connect=lambda: NoTable()).name == "file"
    warnings = [r for r in caplog.records if "파일 색인으로 내려간다" in r.getMessage()]
    assert len(warnings) == 1 and "UndefinedTable" in warnings[0].getMessage()


def test_from_chunk_converts_chunker_output():
    from backend.app.services.rag.chunker import Chunk

    chunk = Chunk(corpus_id="c", source_path="skills/a/SKILL.md", heading_path="# A > ## B", chunk_id="id1",
                  anchor="skills/a/SKILL.md#A", ordinal=2, last_verified="2026-10-01",
                  source_url=("https://example.test/x",), text="본문", embed_text="# A > ## B\n본문",
                  table_row=3, source_note=("메모",))
    vector = FakeEmbedder().embed_query("본문")
    record = ChunkRecord.from_chunk(chunk, vector)
    assert (record.chunk_id, record.ordinal, record.source_url, record.embed_text) == (
        "id1", 2, ("https://example.test/x",), "# A > ## B\n본문")
    assert record.vector is vector and ChunkRecord.from_chunk(chunk).vector is None
    # 파일 색인으로 쓰고 읽어도 같은 조각이 나온다
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        FileChunkStore.write(Path(tmp), [record])
        loaded = FileChunkStore(Path(tmp)).chunks()[0]
        assert loaded.chunk_id == "id1"
        # table_row, source_note도 파일 색인 왕복에서 살아남는다
        assert loaded.table_row == 3 and loaded.source_note == ("메모",)
    assert record.table_row == 3 and record.source_note == ("메모",)
