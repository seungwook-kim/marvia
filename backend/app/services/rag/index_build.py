"""오프라인 색인 빌드의 로직: 계획, 코퍼스 누출 게이트, manifest와 지문, 점검 (docs/specs/v2-rag.md 1, 3절, 작업 R7).

scripts/build_rag_index.py가 이 모듈을 불러 쓴다(CLI는 얇게, 로직은 여기서 테스트한다). 서버가 켜질 때 색인을 만들지 않는다.
서버는 이 빌드가 쓴 산출물(docs/rag/index/, docs/rag/index_manifest.json, DB의 rag_* 표)만 읽는다.

지문(fingerprint) = 아래 셋의 정렬 직렬화 sha256 (Spec 1절):
  - 코퍼스: corpus_id, path, 파일 sha256, last_verified (파일이 바뀌거나 검수일이 바뀌면 색인이 낡은 것이다)
  - 청킹: version, target/max/min 글자 수, context_header
  - 임베딩: Embedder.describe() 전체 (model, revision, file_sha256, dim, pooling, 접두어, backend, 있으면 tokenizer_sha256)
검색 파라미터(top_k 등)는 지문에 넣지 않고 retrieval_keys_hash로 따로 기록한다(색인을 다시 만들 필요가 없는 값).
임베딩 벡터 값 자체는 부동소수 오차가 있을 수 있어 지문에 넣지 않고 모델 파일 해시를 넣는다.
누출 게이트: 조각에 B/L, MRN, 컨테이너 번호, 전화, 이메일 모양이 있으면 빌드를 실패시킨다 (EC-11, 예외 목록 없음).
가리는 모양은 새로 만들지 않고 services/chat.py와 pii_patterns의 것을 쓴다. 오류 메시지에는 값을 적지 않는다(종류와 위치만).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backend.app.services.pii_patterns import EMAIL, MOBILE_IN_TEXT
from backend.app.services.rag.chunker import CHUNKING_VERSION, Chunk, ChunkParams
from backend.app.services.rag.corpus import REPO_ROOT, Corpus, chunk_corpus, file_sha256, load_corpus
from backend.app.services.rag.embed import Embedder
from backend.app.services.rag.store import CHUNKS_FILE, EMBEDDINGS_FILE, ChunkRecord, FileChunkStore, IndexInfo, PgChunkStore
from backend.app.services import rules

INDEX_DIR = REPO_ROOT / "docs" / "rag" / "index"
MANIFEST_FILE = REPO_ROOT / "docs" / "rag" / "index_manifest.json"
# index_id는 지문 앞 12자 (Spec 1절 manifest 예시)
INDEX_ID_LEN = 12
# 누출 오류 메시지에 적는 최대 건수 (메시지 길이를 묶는 표시 설정)
MAX_LEAKS_SHOWN = 10
# 검색 파라미터 해시에서 뺀다: 색인 내용을 바꾸는 키(청킹, 코퍼스 폴더)는 지문 쪽이 맡는다
_INDEX_SHAPING_KEYS = ("rag_corpus_folders", "rag_chunk_target_chars", "rag_chunk_max_chars", "rag_chunk_min_chars",
                       "rag_context_header")


class LeakError(Exception):
    """코퍼스 조각에서 개인정보·번호 모양이 발견됐다. 메시지에는 종류와 파일, 조각 번호만 있고 값은 없다."""


@dataclass(frozen=True)
class Leak:
    """발견된 모양 하나. 값을 담지 않는다 (로그, 오류 메시지에 그대로 쓰기 때문)."""

    kind: str          # bl | mrn | container | phone | email
    source_path: str
    ordinal: int
    field: str         # text | embed_text | source_note


def find_leaks(chunks: Sequence[Chunk]) -> list[Leak]:
    """조각의 글(text, embed_text, source_note)에서 B/L, MRN, 컨테이너 번호, 전화, 이메일 모양을 찾는다 (Spec EC-11).
    URL(source_url)은 공개 링크라 검사하지 않는다. chat.py가 사용자 문장을 가릴 때 쓰는 함수와 모양을 그대로 쓴다.
    chat은 늦게 import한다 (chat.py가 이 패키지를 쓰게 될 때 순환을 피한다)."""
    from backend.app.services import chat

    leaks: list[Leak] = []
    for c in chunks:
        fields = [("text", c.text), ("embed_text", c.embed_text)] + [("source_note", note) for note in c.source_note]
        for field, value in fields:
            kinds = []
            if chat.find_containers(value):
                kinds.append("container")
            if chat.extract_mrn(value)[0] is not None:
                kinds.append("mrn")
            if chat.mask_bls(value)[0] != value:
                kinds.append("bl")
            if chat._PHONE_IN_HISTORY.search(value) or MOBILE_IN_TEXT.search(value):
                kinds.append("phone")
            if EMAIL.search(value):
                kinds.append("email")
            leaks.extend(Leak(kind, c.source_path, c.ordinal, field) for kind in kinds)
    return leaks


def assert_no_leaks(chunks: Sequence[Chunk]) -> None:
    """누출이 하나라도 있으면 LeakError. 예외 목록은 없다 (사람이 문서를 고친다, Spec EC-11)."""
    leaks = find_leaks(chunks)
    if leaks:
        detail = "; ".join(f"{leak.kind} in {leak.source_path} chunk#{leak.ordinal} ({leak.field})" for leak in leaks[:MAX_LEAKS_SHOWN])
        raise LeakError(f"코퍼스 누출 게이트 실패 {len(leaks)}건: {detail}")


def retrieval_keys_hash() -> str:
    """rules.md rag_* 키 중 색인 내용을 바꾸지 않는 것(검색 파라미터, 게이트)의 해시 (Spec 1절). 지문에는 넣지 않는다."""
    merged = rules.load_rules()
    keys = {k: merged[k] for k in sorted(merged) if k.startswith("rag_") and k not in _INDEX_SHAPING_KEYS}
    return hashlib.sha256(json.dumps(keys, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def compute_fingerprint(corpus_rows: Sequence[dict], chunking: dict, embedding: dict) -> str:
    """색인 지문. 입력이 같으면 항상 같고, 코퍼스 파일, 검수일, 청킹 값, 모델 설정 중 하나라도 바뀌면 달라진다.
    corpus_rows는 manifest corpus 칸과 같은 모양이고, 지문에는 그중 corpus_id, path, sha256, last_verified만 쓴다
    (chars, chunks는 파일과 청킹에서 나오는 값이라 중복이다)."""
    corpus = sorted(({k: row.get(k) for k in ("corpus_id", "path", "sha256", "last_verified")} for row in corpus_rows),
                    key=lambda row: row["corpus_id"])
    payload = {"corpus": corpus, "chunking": chunking, "embedding": embedding}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Plan:
    """빌드가 쓸 입력 전체와 그로부터 정해지는 지문. 임베딩 벡터는 아직 없다."""

    chunks: list[Chunk]
    corpus_rows: list[dict]
    chunking: dict
    embedding: dict
    fingerprint: str

    @property
    def index_id(self) -> str:
        return self.fingerprint[:INDEX_ID_LEN]


def corpus_rows(corpus: Corpus, chunks: Sequence[Chunk], root: Path) -> list[dict]:
    """manifest corpus 칸: 파일마다 sha256, 글자 수, 조각 수, 검수일 (Spec 1절). 본문은 넣지 않는다."""
    per_corpus = Counter(c.corpus_id for c in chunks)
    rows = []
    for entry in corpus.entries:
        path = root / entry.path
        rows.append({"corpus_id": entry.corpus_id, "path": entry.path, "sha256": file_sha256(path),
                     "chars": len(path.read_text(encoding="utf-8")), "chunks": per_corpus[entry.corpus_id],
                     "last_verified": entry.last_verified})
    return rows


def make_plan(embedding: dict, *, corpus: Corpus | None = None, root: Path = REPO_ROOT,
              params: ChunkParams | None = None) -> Plan:
    """코퍼스를 청킹하고 지문을 계산한다. 누출 게이트도 여기서 돈다(걸리면 LeakError, 임베딩 전에 멈춘다).
    embedding은 Embedder.describe() 결과다(--check에서 모델을 못 올릴 때는 manifest의 embedding 칸을 대신 준다)."""
    corpus = corpus or load_corpus()
    params = params or ChunkParams.from_rules()
    chunks = chunk_corpus(corpus, root=root, params=params)
    assert_no_leaks(chunks)
    rows = corpus_rows(corpus, chunks, root)
    chunking = params.as_manifest(CHUNKING_VERSION)
    return Plan(chunks, rows, chunking, embedding, compute_fingerprint(rows, chunking, embedding))


def _observed(rows: Sequence[dict]) -> dict:
    """검수일 범위. 코퍼스 항목에 검수일이 하나도 없으면 둘 다 None (사람이 채우기 전, 지어내지 않는다)."""
    dates = sorted(r["last_verified"] for r in rows if r.get("last_verified"))
    return {"last_verified_min": dates[0] if dates else None, "last_verified_max": dates[-1] if dates else None}


def build_manifest(plan: Plan, *, built_at: str | None = None) -> dict:
    """index_manifest.json 내용 (Spec 1절): 4축(corpus, chunking, embedding, retrieval_keys_hash) + 지문 + observed.
    built_at과 chunk_count는 지문에 들어가지 않는다."""
    built_at = built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "index_id": plan.index_id,
        "built_at": built_at,
        "chunk_count": len(plan.chunks),
        "corpus": plan.corpus_rows,
        "chunking": plan.chunking,
        "embedding": plan.embedding,
        "retrieval_keys_hash": retrieval_keys_hash(),
        "fingerprint": plan.fingerprint,
        "observed": _observed(plan.corpus_rows),
    }


def write_manifest(path: Path, manifest: dict) -> None:
    """키 순서와 줄바꿈을 고정해 쓴다 (같은 입력이면 built_at 말고는 같은 바이트)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def build(embedder: Embedder, *, corpus: Corpus | None = None, root: Path = REPO_ROOT, params: ChunkParams | None = None,
          index_dir: Path = INDEX_DIR, manifest_path: Path = MANIFEST_FILE, to_db: bool = False,
          built_at: str | None = None) -> dict:
    """청킹, 누출 게이트, 임베딩, 파일 색인과 manifest 쓰기, (선택) DB 반영까지 한다. manifest 내용을 돌려준다.

    순서가 곧 안전장치다: 누출 게이트(make_plan) -> 임베딩 -> 파일 -> manifest. 게이트가 걸리면 아무것도 쓰지 않는다.
    to_db면 DATABASE_URL의 rag_* 표에 index_id 단위로 반영하고 같은 트랜잭션에서 활성 색인을 바꾼다 (PgChunkStore.write_index).
    DB 표(033)에는 table_row, source_note 칸이 없어 DB 저장소는 그 둘을 버린다 (파일 색인에는 실린다).
    """
    plan = make_plan(embedder.describe(), corpus=corpus, root=root, params=params)
    vectors = embedder.embed_passages([c.embed_text for c in plan.chunks])
    if len(vectors) != len(plan.chunks):
        raise ValueError(f"임베딩 수 {len(vectors)}가 조각 수 {len(plan.chunks)}와 다릅니다")
    records = [ChunkRecord.from_chunk(c, vector=np.asarray(vectors[i], dtype=np.float32)) for i, c in enumerate(plan.chunks)]
    manifest = build_manifest(plan, built_at=built_at)
    FileChunkStore.write(index_dir, records)
    write_manifest(manifest_path, manifest)
    if to_db:
        info = IndexInfo(index_id=plan.index_id, fingerprint=plan.fingerprint, embed_model=embedder.model_id,
                         embed_dim=embedder.dim, chunking=plan.chunking)
        PgChunkStore().write_index(info, records, activate=True)
    return manifest


def _diff_manifest(plan: Plan, manifest: dict) -> list[str]:
    """현재 계획과 manifest가 어디서 다른지 사람이 읽는 문장 목록. 값이 아니라 어느 칸인지만 적는다."""
    problems: list[str] = []
    old = {row["corpus_id"]: row for row in manifest.get("corpus", [])}
    new = {row["corpus_id"]: row for row in plan.corpus_rows}
    for cid in sorted(set(old) | set(new)):
        if cid not in old:
            problems.append(f"코퍼스 항목 추가: {cid}")
        elif cid not in new:
            problems.append(f"코퍼스 항목 삭제: {cid}")
        else:
            for key in ("path", "sha256", "last_verified"):
                if old[cid].get(key) != new[cid].get(key):
                    problems.append(f"코퍼스 {cid}의 {key}가 manifest와 다름" + (" (파일 내용이 바뀜)" if key == "sha256" else ""))
    for section, current in (("chunking", plan.chunking), ("embedding", plan.embedding)):
        before = manifest.get(section) or {}
        for key in sorted(set(before) | set(current)):
            if before.get(key) != current.get(key):
                problems.append(f"{section}.{key}가 manifest와 다름")
    return problems


def check(embedder: Embedder | None, *, manifest_path: Path = MANIFEST_FILE, index_dir: Path = INDEX_DIR,
          corpus: Corpus | None = None, root: Path = REPO_ROOT, params: ChunkParams | None = None) -> tuple[list[str], list[str]]:
    """아무것도 쓰지 않고 manifest 지문과 현재 상태를 견준다 (--check, CI). (실패 목록, 알림 목록)을 돌려준다.

    실패: manifest 없음, 지문 불일치(코퍼스 파일, 검수일, 청킹, 모델 중 하나가 바뀜), 누출 게이트, 색인 파일이 현재 조각과 다름.
    알림(실패 아님): retrieval_keys_hash 불일치(색인을 다시 만들 필요가 없는 값이 바뀜), 모델 확인을 건너뜀, 색인 파일 없음.
    embedder가 None이면(모델 파일이 없는 CI) embedding 칸은 manifest의 값을 그대로 쓴다. 코퍼스와 청킹 변화는 그래도 잡힌다.
    """
    problems: list[str] = []
    notes: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"manifest를 읽지 못했다: {manifest_path.name}"], notes
    if embedder is None:
        embedding = manifest.get("embedding") or {}
        notes.append("임베더를 만들 수 없어 모델 설정은 manifest 값으로 견주었다 (모델 변경은 이 점검이 못 잡는다)")
    else:
        embedding = embedder.describe()
    try:
        plan = make_plan(embedding, corpus=corpus, root=root, params=params)
    except LeakError as exc:
        return [str(exc)], notes
    if plan.fingerprint != manifest.get("fingerprint"):
        problems.append("색인 지문이 manifest와 다름: build_rag_index를 다시 돌려야 한다")
        problems.extend(_diff_manifest(plan, manifest))
    if retrieval_keys_hash() != manifest.get("retrieval_keys_hash"):
        notes.append("rules.md 검색 파라미터(rag_*)가 manifest 이후 바뀜 (색인은 그대로 써도 된다)")
    if (index_dir / CHUNKS_FILE).is_file():
        try:
            ids = [json.loads(line)["chunk_id"] for line in (index_dir / CHUNKS_FILE).read_text(encoding="utf-8").splitlines() if line.strip()]
            rows = np.load(index_dir / EMBEDDINGS_FILE, allow_pickle=False).shape[0]
        except (OSError, ValueError, KeyError):
            ids, rows = None, None
            problems.append("색인 파일을 읽지 못했다 (chunks.jsonl 또는 embeddings.npy)")
        if ids is not None and (sorted(ids) != sorted(c.chunk_id for c in plan.chunks) or rows != len(ids)):
            problems.append("색인 파일(chunks.jsonl, embeddings.npy)이 현재 코퍼스 조각과 다름")
    else:
        notes.append("파일 색인이 없어 manifest만 견주었다")
    return problems, notes
