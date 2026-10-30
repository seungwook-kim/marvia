"""임베딩 계층 테스트 (docs/specs/v2-rag.md R4). 키도 모델 파일도 없이 FakeEmbedder로 돈다.

모델 파일이 필요한 테스트(OnnxEmbedder)만 파일이 없으면 건너뛴다 (Spec 9절 "실제 모델이 필요한 테스트").
스펙은 @pytest.mark.model을 적었지만 pytest.ini가 없어 마커 등록이 필요하므로 skipif로 같은 효과를 낸다.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from backend.app.services.rag import embed
from backend.app.services.rag.embed import (
    DEFAULT_MODEL_ID,
    FAKE_DIM,
    MODEL_SPECS,
    EmbedderUnavailable,
    FakeEmbedder,
    OnnxEmbedder,
    PinnedFile,
    default_model_dir,
    make_embedder,
    manifest_problems,
    sha256_file,
    verify_model_dir,
    write_model_meta,
)

SPEC = MODEL_SPECS[DEFAULT_MODEL_ID]


# ---- (a) FakeEmbedder: 같은 글 같은 벡터, 겹치는 글이 더 가깝다 ----

def test_fake_same_text_same_vector():
    a, b = FakeEmbedder(), FakeEmbedder()
    text = "도착통지서는 선사가 보낸다"
    assert np.array_equal(a.embed_query(text), b.embed_query(text))
    assert np.array_equal(a.embed_passages([text])[0], a.embed_passages([text])[0])


def test_fake_hash_is_fixed_across_processes():
    # 파이썬 내장 hash()는 실행마다 값이 달라진다(PYTHONHASHSEED). blake2b라 칸 위치가 항상 같아야 한다.
    # 아래 목록이 바뀌면 해시 방식이 바뀐 것이므로 FakeEmbedder.describe()의 version 문자열도 같이 올린다
    vec = FakeEmbedder().embed_query("d/o")
    assert np.nonzero(vec)[0].tolist() == [40, 251]  # "d", "o" 두 특징이 놓이는 칸


def test_fake_overlapping_text_has_higher_cosine():
    emb = FakeEmbedder()
    query = emb.embed_query("체화료는 언제부터 붙나요")
    near, far = emb.embed_passages(["체화료는 무료 보관 기간이 지나면 붙는다", "부산 신항은 서쪽 항만이다"])
    assert float(near @ query) > float(far @ query)
    assert float(near @ query) > 0.1  # 겹치는 글자 묶음이 있으면 눈에 띄게 양수


def test_fake_vectors_are_unit_length_and_dim():
    emb = FakeEmbedder()
    matrix = emb.embed_passages(["가나다", "abc def", ""])
    assert matrix.shape == (3, FAKE_DIM) == (3, emb.dim)
    assert matrix.dtype == np.float32
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-6)  # 빈 글도 영벡터가 아니다
    assert emb.embed_passages([]).shape == (0, FAKE_DIM)


def test_fake_dim_is_configurable_and_recorded():
    emb = FakeEmbedder(dim=64)
    assert emb.embed_query("가나다").shape == (64,)
    described = emb.describe()
    assert described["dim"] == 64 and described["backend"] == "fake"
    # 차원이 다르면 지문 재료(file_sha256)도 다르다
    assert described["file_sha256"] != FakeEmbedder().describe()["file_sha256"]


# ---- 모델마다 차원을 기록한다 ----

def test_every_model_spec_records_dim_and_pins():
    assert DEFAULT_MODEL_ID in MODEL_SPECS
    for model_id, spec in MODEL_SPECS.items():
        assert spec.model_id == model_id
        assert spec.dim > 0 and spec.pooling in {"mean", "cls"}
        assert len(spec.revision) == 40  # 브랜치 이름이 아니라 커밋 해시
        for pinned in (spec.model_file, spec.tokenizer_file):
            assert len(pinned.sha256) == 64 and pinned.size_bytes > 0
            assert spec.revision in spec.download_url(pinned) and spec.hf_repo in spec.download_url(pinned)
        assert spec.license  # 모델 카드 라이선스를 적어 두었다 (R4 완료 기준 c)
    assert SPEC.dim == 384 and SPEC.query_prefix == "query: " and SPEC.passage_prefix == "passage: "


# ---- (b) 모델 파일 해시가 manifest와 다르면 실패 ----

def _manifest(**overrides) -> dict:
    emb = {"model": SPEC.model_id, "revision": SPEC.revision, "file_sha256": SPEC.model_file.sha256, "dim": SPEC.dim,
           "pooling": SPEC.pooling, "query_prefix": SPEC.query_prefix, "passage_prefix": SPEC.passage_prefix,
           "backend": "onnx"}
    emb.update(overrides)
    return {"embedding": emb}


def test_manifest_matching_has_no_problems():
    assert manifest_problems(SPEC, _manifest()) == []
    assert manifest_problems(SPEC, {}) == []  # 색인을 아직 안 만들었다
    assert manifest_problems(SPEC, _manifest(model="fake-hash", backend="fake", file_sha256="x")) == []  # EC-12


@pytest.mark.parametrize("field, value", [
    ("file_sha256", "0" * 64), ("revision", "f" * 40), ("dim", 1024), ("pooling", "cls"),
    ("query_prefix", ""), ("passage_prefix", "doc: "), ("model", "other-model"),
])
def test_manifest_mismatch_is_reported(field, value):
    assert manifest_problems(SPEC, _manifest(**{field: value}))


def test_tokenizer_hash_is_compared_only_when_manifest_has_it():
    assert manifest_problems(SPEC, _manifest(tokenizer_sha256=SPEC.tokenizer_file.sha256)) == []
    assert manifest_problems(SPEC, _manifest(tokenizer_sha256="0" * 64))


def _tiny_spec(folder: Path) -> embed.ModelSpec:
    """작은 가짜 파일로 만든 고정 스펙. 실제 모델을 받지 않고 해시 검사 코드를 돌린다."""
    model, tok = b"model-bytes", b"tokenizer-bytes"
    (folder / "a.onnx").write_bytes(model)
    (folder / "t.model").write_bytes(tok)
    return dataclasses.replace(
        SPEC,
        model_file=PinnedFile("a.onnx", "a.onnx", hashlib.sha256(model).hexdigest(), len(model)),
        tokenizer_file=PinnedFile("t.model", "t.model", hashlib.sha256(tok).hexdigest(), len(tok)),
    )


def test_verify_model_dir_detects_missing_and_changed_files(tmp_path):
    spec = _tiny_spec(tmp_path)
    assert verify_model_dir(tmp_path, spec) == []
    (tmp_path / "a.onnx").write_bytes(b"tampered")
    assert any("sha256 불일치" in p for p in verify_model_dir(tmp_path, spec))
    (tmp_path / "t.model").unlink()
    assert any("t.model 없음" in p for p in verify_model_dir(tmp_path, spec))


def test_fetch_script_fails_on_manifest_mismatch_before_downloading(tmp_path, monkeypatch, capsys):
    from scripts import fetch_rag_model

    manifest = tmp_path / "index_manifest.json"
    manifest.write_text(json.dumps(_manifest(file_sha256="0" * 64)), encoding="utf-8")

    def no_network(*args, **kwargs):
        raise AssertionError("manifest가 어긋나면 받기 전에 멈춰야 한다")

    monkeypatch.setattr(fetch_rag_model.urllib.request, "urlopen", no_network)
    code = fetch_rag_model.main(["--dest", str(tmp_path / "m"), "--manifest", str(manifest)])
    assert code == 1
    assert "file_sha256" in capsys.readouterr().err


def test_fetch_script_rejects_downloaded_file_with_wrong_hash(tmp_path, monkeypatch, capsys):
    from scripts import fetch_rag_model

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(fetch_rag_model.urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"not the model"))
    dest = tmp_path / "m"
    code = fetch_rag_model.main(["--dest", str(dest), "--manifest", str(tmp_path / "none.json")])
    assert code == 1
    assert "sha256 불일치" in capsys.readouterr().err
    assert not list(dest.glob("*.part")) and not (dest / SPEC.model_file.local_name).exists()  # 잘못된 파일을 남기지 않는다


def test_fetch_script_verify_only_reports_empty_dir(tmp_path, capsys):
    from scripts import fetch_rag_model

    code = fetch_rag_model.main(["--verify-only", "--dest", str(tmp_path), "--skip-manifest-check"])
    assert code == 1 and "없음" in capsys.readouterr().err


# ---- 임베더를 못 만드는 경우: 부르는 쪽이 BM25로 내려간다 (EC-02) ----

def test_make_embedder_fake_and_unknown_backend(monkeypatch):
    monkeypatch.setenv("RAG_EMBED_BACKEND", "fake")
    assert isinstance(make_embedder(), FakeEmbedder)
    monkeypatch.setenv("RAG_EMBED_BACKEND", "nope")
    with pytest.raises(EmbedderUnavailable):
        make_embedder()


def test_onnx_embedder_without_model_dir_is_unavailable(tmp_path, monkeypatch):
    with pytest.raises(EmbedderUnavailable):
        OnnxEmbedder(tmp_path / "missing")
    monkeypatch.setenv("RAG_EMBED_BACKEND", "onnx")
    monkeypatch.setenv("RAG_EMBED_MODEL_DIR", str(tmp_path / "missing"))
    with pytest.raises(EmbedderUnavailable):
        make_embedder()


def test_onnx_embedder_rejects_wrong_revision_and_unknown_model(tmp_path):
    (tmp_path / embed.META_FILE).write_text(json.dumps({"model_id": "no-such", "revision": "x"}), encoding="utf-8")
    with pytest.raises(EmbedderUnavailable, match="알 수 없는 모델"):
        OnnxEmbedder(tmp_path)
    (tmp_path / embed.META_FILE).write_text(
        json.dumps({"model_id": DEFAULT_MODEL_ID, "revision": "old"}), encoding="utf-8")
    with pytest.raises(EmbedderUnavailable, match="리비전"):
        OnnxEmbedder(tmp_path)
    write_model_meta(tmp_path, SPEC)  # 메모는 맞지만 모델 파일이 없다
    with pytest.raises(EmbedderUnavailable, match="없음"):
        OnnxEmbedder(tmp_path)


# ---- 실제 모델이 있을 때만 도는 테스트 ----

_MODEL_DIR = default_model_dir()
_HAS_MODEL = (not verify_model_dir(_MODEL_DIR, SPEC)) if _MODEL_DIR.is_dir() else False


@pytest.mark.skipif(not _HAS_MODEL, reason="모델 파일 없음 (python -m scripts.fetch_rag_model)")
def test_onnx_embedder_real_model():
    emb = OnnxEmbedder(_MODEL_DIR, threads=1, verify=True)
    assert emb.model_id == DEFAULT_MODEL_ID and emb.dim == SPEC.dim == 384
    query = emb.embed_query("도착통지서를 못 받았어요")
    passages = emb.embed_passages(["도착통지서(A/N)는 선사나 포워더가 보내는 입항 안내다", "오늘 점심 메뉴는 김치찌개다"])
    assert query.shape == (384,) and passages.shape == (2, 384)
    assert np.allclose(np.linalg.norm(passages, axis=1), 1.0, atol=1e-5)
    assert float(passages[0] @ query) > float(passages[1] @ query)  # 관련 있는 글이 더 가깝다
    # 같은 입력은 같은 벡터 (결정적)
    assert np.allclose(emb.embed_query("도착통지서를 못 받았어요"), query, atol=1e-6)
    described = emb.describe()
    assert described["file_sha256"] == SPEC.model_file.sha256 and described["backend"] == "onnx"
    assert sha256_file(_MODEL_DIR / SPEC.model_file.local_name) == SPEC.model_file.sha256


def test_every_model_spec_has_a_min_score_key_in_rules():
    """게이트 임계값은 모델 id별이다 (Spec 6절). MODEL_SPECS의 id와 rules.md rag_min_score의 키가 같은 글자여야 한다."""
    from backend.app.services.rules import rule

    table = rule("rag_min_score")
    assert set(table) == set(MODEL_SPECS)
