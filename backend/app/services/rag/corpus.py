"""코퍼스 A 입력 목록(docs/rag/corpus.yaml)을 읽고 파일마다 청킹한다 (docs/specs/v2-rag.md 1절, 작업 R1).

chunker.py는 글만 받는 순수 함수라, 파일 읽기와 목록 검사는 이 모듈이 맡는다. 색인 빌드(R7)와 테스트가 같이 쓴다.
목록에 없는 파일은 색인하지 않는다. excluded는 "왜 뺐는지"를 남기는 칸이고, 목록과 겹치면 실패시킨다.
"""
from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from backend.app.services.rag.chunker import Chunk, ChunkParams, chunk_markdown

REPO_ROOT = Path(__file__).resolve().parents[4]
CORPUS_FILE = REPO_ROOT / "docs" / "rag" / "corpus.yaml"
KINDS = ("skill", "public_doc")  # corpus.yaml kind에 올 수 있는 값 (Spec 1절)


@dataclass(frozen=True)
class CorpusEntry:
    """corpus.yaml 항목 하나. last_verified는 사람이 채우는 검수일이라 비어 있을 수 있다 (지어내지 않는다)."""

    corpus_id: str
    path: str
    kind: str
    last_verified: str | None
    license: str


@dataclass(frozen=True)
class Corpus:
    """입력 목록과 제외 목록. excluded는 (경로 패턴, 이유) 쌍이다."""

    entries: tuple[CorpusEntry, ...]
    excluded: tuple[tuple[str, str], ...]

    def is_excluded(self, path: str) -> bool:
        """경로(레포 기준 슬래시)가 제외 패턴에 걸리는가. fnmatch라 `*`가 `/`도 넘는다."""
        return any(fnmatch.fnmatchcase(path, pattern) for pattern, _ in self.excluded)


def load_corpus(path: Path = CORPUS_FILE) -> Corpus:
    """corpus.yaml을 읽어 검사한다. 칸이 빠졌거나 kind가 틀렸거나, 같은 id·경로가 둘이거나, 항목이 제외 패턴에 걸리면 ValueError."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[CorpusEntry] = []
    for raw in data.get("corpora") or []:
        missing = [k for k in ("corpus_id", "path", "kind", "license") if not raw.get(k)]
        if missing:
            raise ValueError(f"{path}: 항목에 {missing} 칸이 없습니다: {raw}")
        if raw["kind"] not in KINDS:
            raise ValueError(f"{path}: kind는 {KINDS} 중 하나여야 합니다: {raw['corpus_id']}")
        verified = raw.get("last_verified")
        if isinstance(verified, date):  # YAML이 2026-10-01을 날짜로 읽는다
            verified = verified.isoformat()
        entries.append(CorpusEntry(str(raw["corpus_id"]), str(raw["path"]), str(raw["kind"]),
                                   str(verified) if verified else None, str(raw["license"])))
    excluded = []
    for raw in data.get("excluded") or []:
        if not raw.get("path") or not raw.get("reason"):
            raise ValueError(f"{path}: excluded 항목에는 path와 reason이 모두 있어야 합니다: {raw}")
        excluded.append((str(raw["path"]), str(raw["reason"])))
    corpus = Corpus(tuple(entries), tuple(excluded))
    for name, values in (("corpus_id", [e.corpus_id for e in entries]), ("path", [e.path for e in entries])):
        if len(set(values)) != len(values):
            raise ValueError(f"{path}: {name}가 겹칩니다")
    for e in entries:
        if corpus.is_excluded(e.path):
            raise ValueError(f"{path}: {e.path}가 corpora와 excluded에 모두 있습니다")
    return corpus


def file_sha256(path: Path) -> str:
    """파일 바이트의 sha256. index_manifest.json corpus[].sha256의 재료다 (Spec 1절)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def chunk_corpus(corpus: Corpus | None = None, *, root: Path = REPO_ROOT, params: ChunkParams | None = None) -> list[Chunk]:
    """목록의 파일을 모두 청킹해 한 목록으로 돌려준다. 순서는 corpus.yaml 순서와 파일 안 순서다 (같은 입력이면 같은 결과).
    ordinal은 파일 안 순서다. params를 안 주면 rules.md 값을 읽는다."""
    corpus = corpus or load_corpus()
    params = params or ChunkParams.from_rules()
    chunks: list[Chunk] = []
    for entry in corpus.entries:
        text = (root / entry.path).read_text(encoding="utf-8")
        chunks.extend(chunk_markdown(text, corpus_id=entry.corpus_id, source_path=entry.path,
                                     last_verified=entry.last_verified, params=params))
    return chunks
