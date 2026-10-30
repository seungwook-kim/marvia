"""임베딩 계층: Embedder 인터페이스, 로컬 ONNX 구현, 키 없는 테스트용 가짜 구현 (docs/specs/v2-rag.md 9절, R4).

임베딩 API는 쓰지 않는다 (확정 결정 2). 모델은 로컬 ONNX 파일이고 torch 없이 onnxruntime + sentencepiece로 돈다.
후보 모델은 아직 확정이 아니다 (Spec Q6: 최종은 평가셋 Recall@k로 정한다). 그래서 모델마다 차원, 풀링, 접두어,
고정 리비전, 파일 해시를 MODEL_SPECS 한 곳에 적고, 코드는 모델 id로 이 표를 찾아 쓴다. 차원은 모델마다 다르다
(후보 1은 384, bge-m3는 1024). 모델을 바꾸면 색인 지문이 달라져 서버가 RAG를 끈다 (Spec EC-05, SDD 3.x 지문).

이 파일의 책임은 "글 -> 정규화된 벡터"까지다. 청킹, 색인 만들기, 검색은 다른 파일(chunker, store, search)이 맡는다.
onnxruntime과 sentencepiece는 OnnxEmbedder를 만들 때만 import한다. 두 패키지는 backend/requirements-rag.txt에
있고(api 이미지 전용), 없는 곳(worker, Render 무료 이미지의 bm25 모드, 테스트)에서도 이 파일은 import된다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 레포 루트. 컨테이너에서는 /app (Dockerfile WORKDIR). 상대 경로로 준 모델 폴더는 여기를 기준으로 읽는다
REPO_ROOT = Path(__file__).resolve().parents[4]

# 후보 1: multilingual-e5-small int8 + sentencepiece (Spec 9절 표, Q6). 평가 뒤에 바뀔 수 있다
DEFAULT_MODEL_ID = "multilingual-e5-small-int8"

# 모델 폴더에 fetch_rag_model.py가 쓰는 메모 파일. 어떤 모델이 받아져 있는지 코드가 알아보는 용도다
META_FILE = "marvia_model.json"

# FakeEmbedder의 차원. 운영 색인 열의 차원(033 마이그레이션 vector(384), Spec 4절)과 같게 둬서
# 가짜 벡터도 pgvector 표에 그대로 들어간다. 모델 차원은 ModelSpec.dim이 따로 가진다
FAKE_DIM = 384

# XLM-R 계열 sentencepiece 어휘의 특수 id (모델 카드의 tokenizer 규칙). fairseq가 <s>=0, <pad>=1, </s>=2, <unk>=3을
# 앞에 놓으므로 sentencepiece 원래 id에는 +1을 한다. sentencepiece id 0(<unk>)은 3으로 간다.
# 이 규칙으로 만든 id가 HF tokenizers가 만든 id와 20/20 같았다 (Spec 9절 측정)
_XLMR_CLS, _XLMR_PAD, _XLMR_EOS, _XLMR_UNK, _XLMR_SP_OFFSET = 0, 1, 2, 3, 1

# ONNX를 한 번에 돌리는 문장 수. 메모리 피크를 묶는 실행 설정이지 판정 임계값이 아니다
_BATCH_SIZE = 16


class EmbedderUnavailable(Exception):
    """임베더를 만들 수 없다: 모델 파일 없음, 패키지 없음, 해시 불일치, 알 수 없는 설정.
    부르는 쪽은 BM25 단독으로 내려간다 (Spec EC-02). 사용자에게는 보이지 않는다."""


@dataclass(frozen=True)
class PinnedFile:
    """고정한 모델 파일 하나. 리비전이 같아도 파일이 바뀌지 않았음을 sha256으로 확인한다 (Spec 9절 "고정 리비전·파일 해시")."""
    repo_path: str   # HF 저장소 안 경로 (예: onnx/model_qint8_avx512_vnni.onnx)
    local_name: str  # 모델 폴더에 저장할 파일 이름 (폴더 구조 없이 평평하게 둔다)
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ModelSpec:
    """모델 하나의 성질. 차원은 모델마다 다르므로 여기에 적는다 (코드에 384를 흩어 두지 않는다)."""
    model_id: str            # rag_min_score 맵의 키이기도 하다 (Spec 6절, 모델마다 분포가 달라 임계값이 다르다)
    hf_repo: str
    revision: str            # HF 커밋 해시. 브랜치 이름이 아니라 고정값
    dim: int
    pooling: str             # mean | cls
    query_prefix: str        # E5 계열은 질의 "query: ", 문서 "passage: "를 붙여야 학습한 대로 동작한다
    passage_prefix: str
    max_tokens: int          # 모델이 받는 최대 토큰 수 (특수 토큰 포함). 넘으면 자른다
    license: str             # 모델 카드 표기. web/THIRD_PARTY.md에도 적는다 (R4 완료 기준 c)
    model_file: PinnedFile
    tokenizer_file: PinnedFile

    def download_url(self, pinned: PinnedFile) -> str:
        """고정 리비전 기준 내려받기 주소. 브랜치(main)가 아니라 커밋 해시라 파일이 몰래 바뀌지 않는다."""
        return f"https://huggingface.co/{self.hf_repo}/resolve/{self.revision}/{pinned.repo_path}"


# 후보 모델 표. 새 후보는 여기에 한 항목을 더하면 fetch_rag_model.py와 OnnxEmbedder가 같이 쓴다.
# 값(리비전, 해시, 크기)은 2026-10-31에 HF API(?blobs=true)와 내려받은 파일의 sha256으로 확인했다.
# 라이선스: 모델 카드 메타데이터 license: mit (https://huggingface.co/intfloat/multilingual-e5-small)
MODEL_SPECS: dict[str, ModelSpec] = {
    "multilingual-e5-small-int8": ModelSpec(
        model_id="multilingual-e5-small-int8",
        hf_repo="intfloat/multilingual-e5-small",
        revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        dim=384,
        pooling="mean",
        query_prefix="query: ",
        passage_prefix="passage: ",
        max_tokens=512,
        license="MIT",
        model_file=PinnedFile(
            repo_path="onnx/model_qint8_avx512_vnni.onnx",
            local_name="model_qint8_avx512_vnni.onnx",
            sha256="dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
            size_bytes=118346824,
        ),
        tokenizer_file=PinnedFile(
            repo_path="sentencepiece.bpe.model",
            local_name="sentencepiece.bpe.model",
            sha256="cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            size_bytes=5069051,
        ),
    ),
}


def default_model_dir(model_id: str = DEFAULT_MODEL_ID) -> Path:
    """RAG_EMBED_MODEL_DIR을 안 줬을 때 모델 폴더. models/는 .gitignore라 모델 파일이 커밋되지 않는다."""
    return REPO_ROOT / "models" / "rag" / model_id


def sha256_file(path: Path) -> str:
    """파일의 sha256 (16진수). 1MB씩 읽어 큰 모델 파일도 메모리를 쓰지 않는다."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_model_meta(model_dir: Path) -> dict | None:
    """모델 폴더의 메모 파일(marvia_model.json)을 읽는다. 없거나 깨졌으면 None (어떤 모델인지 모른다)."""
    try:
        data = json.loads((model_dir / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_model_meta(model_dir: Path, spec: ModelSpec) -> None:
    """받은 모델이 무엇인지 폴더에 적는다. OnnxEmbedder가 model_id를 이 파일에서 알아 MODEL_SPECS를 찾는다.
    그래서 모델을 바꿀 때 env 키를 따로 두지 않고 RAG_EMBED_MODEL_DIR만 바꾸면 된다 (Spec 9절 env 5개 유지)."""
    meta = {"model_id": spec.model_id, "hf_repo": spec.hf_repo, "revision": spec.revision}
    (model_dir / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_model_dir(model_dir: Path, spec: ModelSpec) -> list[str]:
    """모델 폴더가 고정한 파일 그대로인지 검사하고, 어긋난 곳을 사람이 읽는 문장 목록으로 돌려준다 (빈 목록 = 정상).
    fetch_rag_model.py와 `OnnxEmbedder(verify=True)`가 쓴다. 해시가 다르면 같은 이름의 다른 모델일 수 있어서
    (같은 차원의 다른 모델이 가장 위험하다, Spec EC-05) 크기가 아니라 sha256으로 본다."""
    problems: list[str] = []
    for pinned in (spec.model_file, spec.tokenizer_file):
        path = model_dir / pinned.local_name
        if not path.is_file():
            problems.append(f"{pinned.local_name} 없음")
            continue
        actual = sha256_file(path)
        if actual != pinned.sha256:
            problems.append(f"{pinned.local_name} sha256 불일치 (고정값 {pinned.sha256[:12]}, 실제 {actual[:12]})")
    return problems


def manifest_problems(spec: ModelSpec, manifest: dict) -> list[str]:
    """색인 manifest(docs/rag/index_manifest.json)의 embedding 칸이 이 모델의 고정값과 같은지 본다 (R4 완료 기준 b).
    다르면 이 모델로 만든 질의 벡터가 색인 벡터와 같은 공간이 아니다. 목록이 비면 일치.
    manifest에 embedding 칸이 없으면(아직 색인을 안 만듦) 비교할 것이 없으니 일치로 본다.
    fake 임베더로 만든 색인(EC-12)은 모델이 아니라 해시 벡터라서 이 비교의 대상이 아니다."""
    emb = manifest.get("embedding")
    if not isinstance(emb, dict) or emb.get("backend") == "fake":
        return []
    if emb.get("model") != spec.model_id:
        return [f"manifest의 모델은 {emb.get('model')!r}, 받으려는 모델은 {spec.model_id!r}"]
    expected = {
        "revision": spec.revision,
        "file_sha256": spec.model_file.sha256,
        "dim": spec.dim,
        "pooling": spec.pooling,
        "query_prefix": spec.query_prefix,
        "passage_prefix": spec.passage_prefix,
    }
    # 토크나이저 해시는 manifest에 있을 때만 견준다 (Spec 1절 manifest 예시에는 아직 없는 칸)
    if "tokenizer_sha256" in emb:
        expected["tokenizer_sha256"] = spec.tokenizer_file.sha256
    return [f"manifest embedding.{key}={emb.get(key)!r}, 고정값 {want!r}"
            for key, want in expected.items() if emb.get(key) != want]


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """행마다 길이를 1로 맞춘다. 길이가 1이면 내적이 곧 코사인이라 검색이 단순해진다. 영벡터는 그대로 둔다(0으로 나누지 않음)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe = np.where(norms == 0, 1.0, norms)
    return (matrix / safe).astype(np.float32)


class Embedder(ABC):
    """글을 정규화된 float32 벡터로 바꾸는 계층의 인터페이스 (Spec 9절).

    구현은 둘: OnnxEmbedder(실제 모델), FakeEmbedder(결정적 해시, 키·모델 없는 테스트).
    질의와 문서는 접두어가 다를 수 있어 메서드를 나눈다. 반환은 항상 길이 1로 정규화된 float32다.
    """

    backend: str  # manifest의 embedding.backend (onnx | fake)

    @property
    @abstractmethod
    def model_id(self) -> str:
        """rag_min_score 맵과 manifest embedding.model의 키."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """벡터 차원. 저장소 열 차원과 같아야 한다 (PgChunkStore가 검사)."""

    @abstractmethod
    def embed_query(self, text: str) -> np.ndarray:
        """질의 한 문장 -> (dim,) 벡터."""

    @abstractmethod
    def embed_passages(self, texts: list[str]) -> np.ndarray:
        """조각 글 여러 개 -> (n, dim) 행렬. 빈 목록이면 (0, dim)."""

    @abstractmethod
    def describe(self) -> dict:
        """manifest embedding 칸 (Spec 1절): model, revision, file_sha256, dim, pooling, query_prefix, passage_prefix, backend.
        지문 계산에 들어간다(R7). 값 자체는 지문에 넣지 않고 모델 파일 해시를 넣는다 (부동소수 오차 때문)."""


class FakeEmbedder(Embedder):
    """토큰 해시 기반 결정적 임베더 (Spec 9절 "키 없는 테스트(필수)").

    같은 글이면 항상 같은 벡터, 글자 겹침이 많으면 코사인이 높다. 의미를 이해하지는 못한다.
    파이썬 내장 hash()는 실행마다 값이 달라서(PYTHONHASHSEED) 쓰지 않고 blake2b로 해시한다.
    특징 = 낱말(영숫자 덩어리, 한글 덩어리) + 한글 낱말의 글자 2개씩 묶음. 한글은 조사가 붙어 낱말이 달라져도
    글자 묶음이 겹쳐 점수가 이어지도록 한 것이다. 특징을 dim칸 중 한 곳에 +1/-1로 더한 뒤 정규화한다.
    """

    backend = "fake"
    _TOKEN = re.compile(r"[0-9a-z]+|[가-힣]+")

    def __init__(self, dim: int = FAKE_DIM):
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "fake-hash"

    @property
    def dim(self) -> int:
        return self._dim

    def _features(self, text: str) -> list[str]:
        """글에서 해시할 특징 목록. 영문은 소문자로, 전각 문자는 NFKC로 맞춘다. 비면 빈 글 표시 하나를 둔다(영벡터 방지)."""
        words = self._TOKEN.findall(unicodedata.normalize("NFKC", text).lower())
        features = list(words)
        for word in words:
            if "가" <= word[0] <= "힣":
                features.extend(word[i:i + 2] for i in range(len(word) - 1))
        return features or ["\x00empty"]

    def _vector(self, text: str) -> np.ndarray:
        """글 하나의 (정규화 전) 벡터. 특징마다 위치 = 해시 % dim, 부호 = 해시의 다른 비트."""
        vec = np.zeros(self._dim, dtype=np.float64)
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            vec[value % self._dim] += 1.0 if (value >> 40) & 1 else -1.0
        return vec

    def embed_query(self, text: str) -> np.ndarray:
        return _l2_normalize(self._vector(text)[None, :])[0]

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        return _l2_normalize(np.stack([self._vector(t) for t in texts]))

    def describe(self) -> dict:
        # 가짜는 모델 파일이 없으므로 file_sha256에 "이 구현의 버전 문자열"의 해시를 둔다. 알고리즘을 바꾸면 이 문자열을 올린다
        version = f"fake-hash-v1-dim{self._dim}"
        return {
            "model": self.model_id,
            "revision": "1",
            "file_sha256": hashlib.sha256(version.encode("utf-8")).hexdigest(),
            "dim": self._dim,
            "pooling": "hash",
            "query_prefix": "",
            "passage_prefix": "",
            "backend": self.backend,
        }


class OnnxEmbedder(Embedder):
    """로컬 ONNX 임베더: onnxruntime(CPU) + sentencepiece, torch 없음 (Spec 9절).

    토크나이저를 HF `tokenizers` 대신 sentencepiece로 쓰는 이유: 250k 단어 토크나이저 로드가 RSS를 +268 MB 늘렸고
    sentencepiece로 바꾸면 114.6 MB가 되었다. 토큰 id는 20문장 모두 같았다 (Spec 9절 측정).
    모델 폴더에 fetch_rag_model.py가 받은 파일이 없으면 EmbedderUnavailable (Spec EC-02, 부르는 쪽이 BM25 단독으로 내려감).
    """

    backend = "onnx"

    def __init__(self, model_dir: Path, threads: int | None = None, *, verify: bool = False):
        """model_dir: fetch_rag_model.py가 채운 폴더. threads: onnxruntime 스레드 수(None 또는 0이면 라이브러리 기본).
        verify=True면 시작 때 파일 sha256을 고정값과 견준다(수 초 걸리는 일이라 기본은 끔, 빌드·점검에서 켠다)."""
        self._dir = Path(model_dir)
        self._spec = self._resolve_spec(self._dir)
        problems = [p for p in (
            f"{pinned.local_name} 없음" for pinned in (self._spec.model_file, self._spec.tokenizer_file)
            if not (self._dir / pinned.local_name).is_file()
        )]
        if not problems and verify:
            problems = verify_model_dir(self._dir, self._spec)
        if problems:
            raise EmbedderUnavailable(f"모델 폴더 {self._dir}: " + "; ".join(problems))
        try:
            import onnxruntime as ort  # noqa: PLC0415 - api 이미지에만 있다
            import sentencepiece as spm  # noqa: PLC0415 - api 이미지에만 있다
        except ImportError as exc:
            raise EmbedderUnavailable(f"임베딩 패키지가 없다 (backend/requirements-rag.txt): {exc}") from exc
        options = ort.SessionOptions()
        if threads and threads > 0:
            options.intra_op_num_threads = threads
        try:
            self._session = ort.InferenceSession(
                str(self._dir / self._spec.model_file.local_name), options, providers=["CPUExecutionProvider"])
            # 파일 경로가 아니라 바이트로 넘긴다: sentencepiece는 Windows에서 한글이 든 경로를 못 연다 (5 MB라 부담 없음)
            self._sp = spm.SentencePieceProcessor(
                model_proto=(self._dir / self._spec.tokenizer_file.local_name).read_bytes())
        except Exception as exc:  # 파일이 깨졌거나 onnx 형식이 아님. 종류만 남기고 BM25로 내려간다
            raise EmbedderUnavailable(f"모델 로드 실패: {type(exc).__name__}") from exc
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._file_hashes: tuple[str, str] | None = None

    @staticmethod
    def _resolve_spec(model_dir: Path) -> ModelSpec:
        """폴더 메모(marvia_model.json)의 model_id로 MODEL_SPECS를 찾는다. 메모가 없거나 리비전이 다르면 쓰지 않는다
        (파일은 있는데 어떤 모델인지 모르면 차원·접두어를 짐작해야 하므로 짐작하지 않고 거절한다)."""
        meta = read_model_meta(model_dir)
        if meta is None:
            raise EmbedderUnavailable(
                f"{model_dir}에 {META_FILE}가 없다. python -m scripts.fetch_rag_model 로 받는다")
        spec = MODEL_SPECS.get(str(meta.get("model_id")))
        if spec is None:
            raise EmbedderUnavailable(f"알 수 없는 모델 id: {meta.get('model_id')!r} (MODEL_SPECS에 없음)")
        if meta.get("revision") != spec.revision:
            raise EmbedderUnavailable(
                f"모델 리비전이 고정값과 다르다 (폴더 {meta.get('revision')}, 고정 {spec.revision}). 다시 받는다")
        return spec

    @property
    def model_id(self) -> str:
        return self._spec.model_id

    @property
    def dim(self) -> int:
        return self._spec.dim

    def _token_ids(self, text: str) -> list[int]:
        """글 -> XLM-R 규칙의 토큰 id: <s> + (sentencepiece id + 1, unk는 3) + </s>. max_tokens를 넘으면 뒤를 자른다."""
        pieces = [(i + _XLMR_SP_OFFSET) if i != 0 else _XLMR_UNK for i in self._sp.encode(text)]
        return [_XLMR_CLS, *pieces[: self._spec.max_tokens - 2], _XLMR_EOS]

    def _embed(self, texts: list[str], prefix: str) -> np.ndarray:
        """접두어를 붙여 _BATCH_SIZE개씩 모델에 넣고 풀링한 뒤 정규화한다. 빈 목록이면 (0, dim)."""
        if not texts:
            return np.zeros((0, self._spec.dim), dtype=np.float32)
        out: list[np.ndarray] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            rows = [self._token_ids(prefix + t) for t in texts[start:start + _BATCH_SIZE]]
            width = max(len(r) for r in rows)
            ids = np.full((len(rows), width), _XLMR_PAD, dtype=np.int64)
            mask = np.zeros((len(rows), width), dtype=np.int64)
            for i, row in enumerate(rows):
                ids[i, : len(row)] = row
                mask[i, : len(row)] = 1
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feed)[0]  # (배치, 길이, dim)
            if self._spec.pooling == "cls":
                pooled = hidden[:, 0]
            else:  # mean: 패딩 자리를 빼고 평균
                pooled = (hidden * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
            out.append(_l2_normalize(pooled))
        return np.concatenate(out)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], self._spec.query_prefix)[0]

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._embed(list(texts), self._spec.passage_prefix)

    def describe(self) -> dict:
        """manifest embedding 칸. 파일 해시는 처음 부를 때 한 번 계산해 둔다(빌드·기동 점검용, 요청마다 부르지 않는다).
        tokenizer_sha256은 Spec 1절 예시에 없는 칸이다: 토크나이저가 바뀌면 같은 모델 파일이어도 벡터가 달라지므로 지문에 넣는다."""
        if self._file_hashes is None:
            self._file_hashes = (sha256_file(self._dir / self._spec.model_file.local_name),
                                 sha256_file(self._dir / self._spec.tokenizer_file.local_name))
        model_hash, tokenizer_hash = self._file_hashes
        return {
            "model": self._spec.model_id,
            "revision": self._spec.revision,
            "file_sha256": model_hash,
            "tokenizer_sha256": tokenizer_hash,
            "dim": self._spec.dim,
            "pooling": self._spec.pooling,
            "query_prefix": self._spec.query_prefix,
            "passage_prefix": self._spec.passage_prefix,
            "backend": self.backend,
        }


def _env_int(name: str) -> int | None:
    """정수 환경변수. 비었거나 숫자가 아니면 None (잘못 적은 값 때문에 임베더가 죽지 않게)."""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def make_embedder() -> Embedder:
    """환경변수로 임베더를 만든다 (Spec 9절 env): RAG_EMBED_BACKEND(onnx 기본 | fake), RAG_EMBED_MODEL_DIR, RAG_EMBED_THREADS.
    만들 수 없으면 EmbedderUnavailable. 부르는 쪽(search, R5/R8)이 잡아 BM25 단독으로 내려간다 (Spec EC-02)."""
    backend = (os.environ.get("RAG_EMBED_BACKEND") or "onnx").strip().lower()
    if backend == "fake":
        return FakeEmbedder()
    if backend != "onnx":
        raise EmbedderUnavailable(f"알 수 없는 RAG_EMBED_BACKEND: {backend!r} (onnx 또는 fake)")
    raw_dir = os.environ.get("RAG_EMBED_MODEL_DIR", "").strip()
    model_dir = (REPO_ROOT / raw_dir) if raw_dir else default_model_dir()  # 절대 경로면 REPO_ROOT / 절대경로 = 절대경로
    return OnnxEmbedder(model_dir, threads=_env_int("RAG_EMBED_THREADS"))
