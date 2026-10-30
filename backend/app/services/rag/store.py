"""조각 저장소: ChunkStore 인터페이스와 구현 셋 (메모리, 파일, pgvector). docs/specs/v2-rag.md 4절, R3.

검색(search.py)은 저장소가 무엇인지 모르고 두 가지만 부른다: `chunks()`(BM25 통계용 조각 목록)와 `dense(query_vec, k)`(코사인 상위 k).
- MemoryChunkStore: 프로세스 메모리. 테스트와 다른 구현의 속살로 쓴다.
- FileChunkStore: 빌드가 만든 docs/rag/index/ 파일(chunks.jsonl + embeddings.npy)을 읽어 메모리 저장소로 올린다.
  DB가 없거나 pgvector 확장이 없을 때의 폴백이다 (Spec EC-03).
- PgChunkStore: Supabase pgvector (033_rag_chunks.sql). 활성 색인(`rag_index.is_active`)의 조각만 읽는다.
  표나 확장이 없거나 연결이 안 되면 StoreError로 알린다. 부르는 쪽이 파일 저장소로 내려간다 (EC-03).

조각 글에 B/L, MRN, 컨테이너 번호, 연락처가 없다는 것은 색인 빌드의 누출 게이트가 보장한다 (Spec EC-11, R7). 이 파일은 글을 검사하지 않고 옮기기만 한다.
저장소는 사용자 데이터를 다루지 않는다. 코퍼스 A(내부 문서)뿐이다 (확정 결정 3).
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# pgvector가 설치되는 스키마. Supabase는 확장을 extensions 스키마에 둔다 (033 마이그레이션이 같은 이름을 쓴다)
VECTOR_SCHEMA = "extensions"

_log = logging.getLogger(__name__)
_warned_fallback = False  # 폴백 경고는 프로세스에서 한 번만 (요청마다 같은 줄이 쌓이지 않게)

# 레포 루트와 파일 색인 위치 (Spec 3절 docs/rag/index/). 컨테이너에서는 /app
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_INDEX_DIR = REPO_ROOT / "docs" / "rag" / "index"

# 파일 색인의 파일 이름 (Spec 3절)
CHUNKS_FILE = "chunks.jsonl"
EMBEDDINGS_FILE = "embeddings.npy"


class StoreError(Exception):
    """저장소를 쓸 수 없다: 파일이 없음, 차원이 안 맞음, DB 연결·표 없음. 부르는 쪽이 다음 저장소로 내려간다 (Spec EC-03, EC-04).
    메시지에는 종류만 담는다. 접속 주소나 SQL 인자는 넣지 않는다."""


@dataclass(frozen=True)
class ChunkRecord:
    """색인에 들어가는 조각 하나. 칸은 Spec 2절 "조각 메타" 표와 같고 vector가 더해진다.

    `chunker.py`(R1)가 만드는 조각에 embed_text와 vector를 채운 것이다. 필드 이름과 뜻은 Spec 표를 따른다.
    vector는 정규화된 float32 한 줄이다. BM25만 쓰는 곳(DB에서 chunks()로 읽을 때)은 None일 수 있다.
    """
    corpus_id: str
    source_path: str              # 레포 기준 상대 경로, 슬래시
    heading_path: str
    chunk_id: str                 # 글이 바뀌면 바뀌는 증거용 id
    anchor: str                   # 글이 바뀌어도 같은 자리를 가리키는 안정 키 (평가 정답이 쓴다)
    ordinal: int                  # 파일 안 순서
    text: str                     # 사용자에게 보여 줄 글
    embed_text: str               # 임베딩과 BM25에 넣는 글 (맥락 머리말 포함)
    last_verified: str | None = None          # YYYY-MM-DD, corpus.yaml의 검수일
    source_url: tuple[str, ...] = ()
    # Spec EC-14: 공개 문서용으로 자리만 예약한다. DB 표(033)에는 아직 칸이 없고 파일 색인에만 실린다
    effective_date: str | None = None
    supersedes: str | None = None
    # chunker.Chunk에서 오는 메타. 임베딩 글과 chunk_id에는 들어가지 않는다. 파일 색인(chunks.jsonl)에만 실리고
    # DB 표(033)에는 칸이 없어 pgvector 저장소는 버린다(칸을 더하는 일은 후속 마이그레이션이 정한다)
    table_row: int | None = None                   # 표 행 조각이면 본문 행 번호(1부터)
    source_note: tuple[str, ...] = ()              # URL 없는 `출처:` 줄의 글 (출처 표시용)
    vector: np.ndarray | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_chunk(cls, chunk, vector: np.ndarray | None = None) -> ChunkRecord:
        """chunker.py의 Chunk(벡터 없음)에 임베딩 벡터를 붙여 색인용 조각으로 만든다 (build_rag_index.py가 쓴다, R7).
        Chunk를 import하지 않고 칸 이름으로 읽는다: 두 모듈이 서로 얽히지 않게 하려는 것이다.
        table_row, source_note도 그대로 옮긴다."""
        return cls(
            corpus_id=chunk.corpus_id, source_path=chunk.source_path, heading_path=chunk.heading_path,
            chunk_id=chunk.chunk_id, anchor=chunk.anchor, ordinal=chunk.ordinal, text=chunk.text,
            embed_text=chunk.embed_text, last_verified=chunk.last_verified, source_url=tuple(chunk.source_url or ()),
            effective_date=chunk.effective_date, supersedes=chunk.supersedes,
            table_row=chunk.table_row, source_note=tuple(chunk.source_note or ()), vector=vector,
        )

    def to_json(self) -> dict:
        """chunks.jsonl 한 줄용 dict. 벡터는 embeddings.npy에 따로 둔다."""
        return {
            "corpus_id": self.corpus_id, "source_path": self.source_path, "heading_path": self.heading_path,
            "chunk_id": self.chunk_id, "anchor": self.anchor, "ordinal": self.ordinal, "text": self.text,
            "embed_text": self.embed_text, "last_verified": self.last_verified,
            "source_url": list(self.source_url), "effective_date": self.effective_date, "supersedes": self.supersedes,
            "table_row": self.table_row, "source_note": list(self.source_note),
        }

    @classmethod
    def from_json(cls, row: dict, vector: np.ndarray | None = None) -> ChunkRecord:
        """chunks.jsonl 한 줄 -> 조각. 모르는 칸은 무시하고, 없는 선택 칸은 기본값이다 (칸 추가에 강하게)."""
        return cls(
            corpus_id=row["corpus_id"], source_path=row["source_path"], heading_path=row["heading_path"],
            chunk_id=row["chunk_id"], anchor=row["anchor"], ordinal=int(row["ordinal"]), text=row["text"],
            embed_text=row.get("embed_text", row["text"]), last_verified=row.get("last_verified"),
            source_url=tuple(row.get("source_url") or ()), effective_date=row.get("effective_date"),
            supersedes=row.get("supersedes"), table_row=row.get("table_row"),
            source_note=tuple(row.get("source_note") or ()), vector=vector,
        )


@dataclass(frozen=True)
class DenseHit:
    """밀집 검색 결과 하나: 조각과 질의 벡터와의 코사인 (-1~1). 게이트와 RRF가 이 값을 쓴다 (Spec 5, 6절)."""
    chunk: ChunkRecord
    cosine: float


@dataclass(frozen=True)
class IndexInfo:
    """활성 색인의 이름표 (rag_index 한 줄 또는 index_manifest.json). EC-05 지문 비교에 쓴다."""
    index_id: str | None
    fingerprint: str | None
    embed_model: str | None
    embed_dim: int | None
    chunking: dict | None = None


class ChunkStore(ABC):
    """조각 저장소 인터페이스 (Spec 4절 "런타임 저장소 선택"). 구현은 모두 같은 두 메서드를 낸다."""

    name: str  # 로그와 rag:search span의 store 칸 (memory | file | pgvector)

    @abstractmethod
    def chunks(self) -> list[ChunkRecord]:
        """활성 색인의 모든 조각. 파일 안 순서(source_path, ordinal)대로. vector는 비어 있을 수 있다 (BM25 통계용)."""

    @abstractmethod
    def dense(self, query_vec: np.ndarray, k: int) -> list[DenseHit]:
        """질의 벡터와 코사인이 높은 순으로 최대 k개. 같은 점수면 (source_path, ordinal) 순이라 결과가 결정적이다."""

    @abstractmethod
    def info(self) -> IndexInfo | None:
        """활성 색인의 이름표. 색인이 하나도 없으면 None."""


def _sorted(chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    """(source_path, ordinal) 순. 저장소마다 같은 순서를 내야 BM25 통계와 동점 처리가 같다."""
    return sorted(chunks, key=lambda c: (c.source_path, c.ordinal, c.chunk_id))


class MemoryChunkStore(ChunkStore):
    """프로세스 메모리 저장소. 조각마다 vector가 있어야 dense()를 쓸 수 있다 (없으면 dense는 StoreError)."""

    name = "memory"

    def __init__(self, chunks: Sequence[ChunkRecord], info: IndexInfo | None = None):
        self._chunks = _sorted(chunks)
        self._info = info
        vectors = [c.vector for c in self._chunks]
        if self._chunks and all(v is not None for v in vectors):
            try:
                self._matrix: np.ndarray | None = np.stack(vectors).astype(np.float32)
            except ValueError as exc:  # 조각마다 벡터 길이가 다르다
                raise StoreError("조각 벡터의 차원이 서로 다르다") from exc
        else:
            self._matrix = None  # 벡터가 없다. BM25 전용으로만 쓴다
        if self._matrix is not None and not np.isfinite(self._matrix).all():
            raise StoreError("조각 벡터에 NaN 또는 무한대가 있다")

    def chunks(self) -> list[ChunkRecord]:
        return list(self._chunks)

    def info(self) -> IndexInfo | None:
        return self._info

    def dense(self, query_vec: np.ndarray, k: int) -> list[DenseHit]:
        if self._matrix is None:
            raise StoreError("이 저장소의 조각에는 벡터가 없다 (밀집 검색 불가)")
        query = np.asarray(query_vec, dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != self._matrix.shape[1]:
            raise StoreError(f"질의 벡터 차원 {query.shape}이 색인 차원 {self._matrix.shape[1]}과 다르다")
        # 벡터가 정규화되어 있다고 믿지 않고 길이로 나눠 진짜 코사인을 낸다 (정규화가 빠진 색인이 조용히 틀리지 않게)
        norms = np.linalg.norm(self._matrix, axis=1) * np.linalg.norm(query)
        scores = (self._matrix @ query) / np.where(norms == 0, 1.0, norms)
        # 점수 내림차순, 같으면 원래 순서(이미 정렬됨). 안정 정렬이라 결정적이다
        order = np.argsort(-scores, kind="stable")[: max(k, 0)]
        return [DenseHit(self._chunks[i], float(scores[i])) for i in order]


class FileChunkStore(ChunkStore):
    """빌드가 만든 파일 색인(chunks.jsonl + embeddings.npy)을 읽어 메모리 저장소로 올린다 (Spec 3, 4절, EC-03 폴백).

    manifest(docs/rag/index_manifest.json)가 있으면 info()에 지문을 싣는다. 없으면 index_id 등은 None이다.
    파일이 없거나 줄 수와 행 수가 다르면 StoreError (색인이 반쯤 쓰인 채 남은 경우도 같다).
    """

    name = "file"

    def __init__(self, directory: Path, manifest_path: Path | None = None):
        directory = Path(directory)
        manifest_path = Path(manifest_path) if manifest_path else directory.parent / "index_manifest.json"
        try:
            lines = (directory / CHUNKS_FILE).read_text(encoding="utf-8").splitlines()
            matrix = np.load(directory / EMBEDDINGS_FILE, allow_pickle=False)  # pickle은 읽지 않는다 (데이터 파일이 코드를 못 돌리게)
        except (OSError, ValueError) as exc:
            raise StoreError(f"파일 색인을 읽지 못했다: {directory} ({type(exc).__name__})") from exc
        rows = [json.loads(line) for line in lines if line.strip()]
        if matrix.ndim != 2 or matrix.shape[0] != len(rows):
            raise StoreError(f"파일 색인 불일치: 조각 {len(rows)}개, 벡터 {matrix.shape}")
        records = [ChunkRecord.from_json(row, vector=matrix[i]) for i, row in enumerate(rows)]
        self._inner = MemoryChunkStore(records, info=self._read_info(manifest_path, matrix.shape[1]))

    @staticmethod
    def _read_info(manifest_path: Path, dim: int) -> IndexInfo:
        """manifest에서 이름표를 읽는다. 없거나 깨졌으면 차원만 아는 이름표 (지문 비교는 부르는 쪽이 못 한다고 판단한다)."""
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return IndexInfo(None, None, None, dim)
        embedding = manifest.get("embedding") or {}
        return IndexInfo(manifest.get("index_id"), manifest.get("fingerprint"), embedding.get("model"),
                         embedding.get("dim", dim), manifest.get("chunking"))

    def chunks(self) -> list[ChunkRecord]:
        return self._inner.chunks()

    def dense(self, query_vec: np.ndarray, k: int) -> list[DenseHit]:
        return self._inner.dense(query_vec, k)

    def info(self) -> IndexInfo | None:
        return self._inner.info()

    @staticmethod
    def write(directory: Path, chunks: Sequence[ChunkRecord]) -> None:
        """파일 색인을 쓴다 (build_rag_index.py, R7). 모든 조각에 vector가 있어야 한다.
        같은 입력이면 같은 바이트가 나오도록 정렬 순서, 키 순서, 줄바꿈을 고정한다 (Spec 3절 재현)."""
        ordered = _sorted(chunks)
        if any(c.vector is None for c in ordered):
            raise StoreError("vector가 없는 조각은 파일 색인에 쓸 수 없다")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(c.to_json(), ensure_ascii=False, sort_keys=True) + "\n" for c in ordered)
        (directory / CHUNKS_FILE).write_text(body, encoding="utf-8", newline="\n")
        matrix = np.stack([c.vector for c in ordered]).astype(np.float32) if ordered else np.zeros((0, 0), np.float32)
        np.save(directory / EMBEDDINGS_FILE, matrix, allow_pickle=False)


def _vector_literal(vec: np.ndarray) -> str:
    """pgvector 입력 글자열 '[0.1,0.2,...]'. 파라미터로 보내고 SQL 쪽에서 vector로 바꾼다 (SQL에 값을 이어 붙이지 않는다)."""
    return "[" + ",".join(f"{float(x):.8g}" for x in np.asarray(vec, dtype=np.float32)) + "]"


class PgChunkStore(ChunkStore):
    """Supabase pgvector 저장소 (033_rag_chunks.sql). 정확 검색(순차 스캔)이다: 조각이 수십 개라 ANN 인덱스 없이 이게 가장 빠르고 정확하다 (Spec 4절).

    - connect: 연결을 여는 함수. 기본은 backend.app.db.connect(DATABASE_URL). 테스트는 DATABASE_URL_TEST용 함수를 넘긴다.
    - 읽기는 활성 색인(rag_index.is_active) 한 개의 조각만. 표가 없거나(pgvector 없는 DB, EC-03) 연결이 안 되면 StoreError.
    - 연산자는 `OPERATOR(extensions.<=>)`로 스키마를 적는다. search_path에 extensions가 없는 연결에서도 동작하게 하려는 것이다.
    """

    name = "pgvector"

    def __init__(self, connect: Callable | None = None):
        self._connect = connect

    def _open(self):
        """DB 연결 하나. db.connect는 DATABASE_URL이 없으면 KeyError를 내므로 함께 StoreError로 바꾼다."""
        if self._connect is not None:
            return self._connect()
        from backend.app.db import connect  # noqa: PLC0415 - psycopg가 필요 없는 환경에서도 이 모듈이 import되게
        return connect()

    def _run(self, fn):
        """연결을 열어 fn(conn)을 실행하고 DB 오류를 StoreError로 바꾼다. 연결은 항상 닫는다. 메시지에는 오류 종류만 담는다."""
        import psycopg  # noqa: PLC0415 - psycopg가 필요 없는 환경에서도 이 모듈이 import되게
        try:
            conn = self._open()
        except (psycopg.Error, KeyError) as exc:
            raise StoreError(f"DB 연결 실패 ({type(exc).__name__})") from exc
        try:
            return fn(conn)
        except psycopg.Error as exc:
            # UndefinedTable: pgvector가 없어 033이 표를 만들지 않았다 (EC-03). 그 밖의 오류도 같은 폴백으로 간다
            raise StoreError(f"pgvector 저장소 오류 ({type(exc).__name__})") from exc
        finally:
            conn.close()

    @staticmethod
    def _active_id(conn) -> str | None:
        row = conn.execute("SELECT index_id FROM rag_index WHERE is_active LIMIT 1").fetchone()
        return row[0] if row else None

    def info(self) -> IndexInfo | None:
        def read(conn):
            row = conn.execute(
                "SELECT index_id, fingerprint, embed_model, embed_dim, chunking FROM rag_index WHERE is_active LIMIT 1"
            ).fetchone()
            return IndexInfo(row[0], row[1], row[2], row[3], row[4]) if row else None
        return self._run(read)

    _CHUNK_COLUMNS = ("corpus_id, source_path, heading_path, chunk_id, anchor, ordinal, last_verified, source_url, "
                      "text, embed_text")

    @staticmethod
    def _record(row, vector: np.ndarray | None = None) -> ChunkRecord:
        """SELECT 결과 한 줄(_CHUNK_COLUMNS 순서) -> 조각."""
        return ChunkRecord(
            corpus_id=row[0], source_path=row[1], heading_path=row[2], chunk_id=row[3], anchor=row[4],
            ordinal=row[5], last_verified=row[6].isoformat() if row[6] else None,
            source_url=tuple(row[7] or ()), text=row[8], embed_text=row[9], vector=vector,
        )

    def chunks(self) -> list[ChunkRecord]:
        def read(conn):
            active = self._active_id(conn)
            if active is None:
                raise StoreError("활성 색인이 없다")
            rows = conn.execute(
                f"SELECT {self._CHUNK_COLUMNS} FROM rag_chunks WHERE index_id = %s ORDER BY source_path, ordinal",
                (active,)).fetchall()
            return [self._record(r) for r in rows]
        return self._run(read)

    def dense(self, query_vec: np.ndarray, k: int) -> list[DenseHit]:
        literal = _vector_literal(query_vec)

        def read(conn):
            active = self._active_id(conn)
            if active is None:
                raise StoreError("활성 색인이 없다")
            # 코사인 거리(<=>)가 작을수록 가깝다. 코사인 = 1 - 거리. 같은 거리면 파일 순서로 결정적이게 한다
            rows = conn.execute(
                f"SELECT {self._CHUNK_COLUMNS}, "
                f"1 - (embedding OPERATOR({VECTOR_SCHEMA}.<=>) %s::{VECTOR_SCHEMA}.vector) AS cosine "
                f"FROM rag_chunks WHERE index_id = %s "
                f"ORDER BY embedding OPERATOR({VECTOR_SCHEMA}.<=>) %s::{VECTOR_SCHEMA}.vector, source_path, ordinal "
                f"LIMIT %s",
                (literal, active, literal, max(k, 0))).fetchall()
            return [DenseHit(self._record(r), float(r[10])) for r in rows]
        return self._run(read)

    def write_index(self, info: IndexInfo, chunks: Sequence[ChunkRecord], *, activate: bool = True) -> None:
        """색인 하나를 한 트랜잭션으로 넣는다 (build_rag_index.py --db, R7). index_id가 같으면 조각을 통째로 바꾼다.
        activate=True면 같은 트랜잭션에서 다른 색인을 끄고 이 색인을 켠다: 읽는 쪽은 옛 색인 아니면 새 색인만 본다 (Spec 3절).
        모든 조각에 vector가 있어야 하고 차원은 info.embed_dim과 같아야 한다 (표의 열 차원은 033이 정한다)."""
        if not info.index_id or not info.fingerprint or not info.embed_model or not info.embed_dim:
            raise StoreError("index_id, fingerprint, embed_model, embed_dim이 모두 있어야 한다")
        if any(c.vector is None or len(c.vector) != info.embed_dim for c in chunks):
            raise StoreError("모든 조각에 embed_dim 차원의 vector가 있어야 한다")

        def write(conn):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO rag_index (index_id, fingerprint, embed_model, embed_dim, chunking, is_active) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, false) "
                    "ON CONFLICT (index_id) DO UPDATE SET fingerprint = EXCLUDED.fingerprint, "
                    "embed_model = EXCLUDED.embed_model, embed_dim = EXCLUDED.embed_dim, "
                    "chunking = EXCLUDED.chunking, built_at = now()",
                    (info.index_id, info.fingerprint, info.embed_model, info.embed_dim,
                     json.dumps(info.chunking or {}, ensure_ascii=False, sort_keys=True)))
                conn.execute("DELETE FROM rag_chunks WHERE index_id = %s", (info.index_id,))
                for c in _sorted(chunks):
                    conn.execute(
                        f"INSERT INTO rag_chunks ({self._CHUNK_COLUMNS}, index_id, embedding) "
                        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::{VECTOR_SCHEMA}.vector)",
                        (c.corpus_id, c.source_path, c.heading_path, c.chunk_id, c.anchor, c.ordinal,
                         c.last_verified, list(c.source_url), c.text, c.embed_text, info.index_id,
                         _vector_literal(c.vector)))
                if activate:
                    conn.execute("UPDATE rag_index SET is_active = (index_id = %s)", (info.index_id,))
        self._run(write)


def make_store(index_dir: Path | None = None, connect: Callable | None = None) -> ChunkStore:
    """RAG_STORE로 런타임 저장소를 고른다 (Spec 4절): pgvector(기본) | file.

    pgvector는 활성 색인이 실제로 읽히는지까지 확인한 뒤 쓴다. 연결이 안 되거나, 표가 없거나(033이 pgvector 없는 DB에서
    표를 만들지 않았다), 활성 색인이 하나도 없으면 파일 색인으로 내려간다 (Spec EC-03). 파일도 없으면 StoreError를 그대로
    올리고, 부르는 쪽이 v1 경로로 응답한다 (EC-04). 지문 검사(EC-05)는 이 함수가 아니라 부르는 쪽이 info()로 한다.
    """
    index_dir = Path(index_dir) if index_dir else DEFAULT_INDEX_DIR
    choice = (os.environ.get("RAG_STORE") or "pgvector").strip().lower()
    if choice not in {"pgvector", "file"}:
        raise StoreError(f"알 수 없는 RAG_STORE: {choice!r} (pgvector 또는 file)")
    if choice == "pgvector":
        store = PgChunkStore(connect=connect)
        try:
            if store.info() is not None:
                return store
            reason = "활성 색인이 없다"
        except StoreError as exc:
            # 표가 없는 경우(UndefinedTable)가 여기로 온다: 033은 pgvector가 없던 DB에서 표를 만들지 않고도 "적용됨"으로 기록된다
            reason = str(exc)
        _warn_fallback_once(reason)
    return FileChunkStore(index_dir)


def _warn_fallback_once(reason: str) -> None:
    """pgvector 저장소를 못 써서 파일 색인으로 내려간다는 경고를 프로세스당 한 번 남긴다 (Spec EC-03). 접속 정보는 담지 않는다."""
    global _warned_fallback
    if not _warned_fallback:
        _warned_fallback = True
        _log.warning("rag: pgvector 저장소를 쓸 수 없어 파일 색인으로 내려간다 (%s)", reason)
