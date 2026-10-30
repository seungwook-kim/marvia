"""RAG 색인 빌드 (services/rag/index_build.py, scripts/build_rag_index.py), 작업 R7 (docs/specs/v2-rag.md 1, 3절, EC-10, EC-11).

완료 기준: (a) 같은 입력이면 같은 fingerprint (b) 코퍼스 파일 한 글자를 바꾸면 --check 실패
(c) 조각에 B/L, MRN, 컨테이너, 전화, 이메일 모양을 일부러 넣은 임시 코퍼스에서 빌드 실패 (d) index_manifest.json이 4축 칸을 모두 가짐.
FakeEmbedder와 임시 폴더만 쓴다. 키, DB, 모델 파일이 없어도 돈다. 가짜 값만 쓴다 (TESTBL99 등).
"""
import json
import shutil

import numpy as np
import pytest

from backend.app.services import chat, rules
from backend.app.services.rag import index_build
from backend.app.services.rag.chunker import ChunkParams
from backend.app.services.rag.corpus import REPO_ROOT, chunk_corpus, load_corpus
from backend.app.services.rag.embed import MODEL_SPECS, FakeEmbedder, manifest_problems
from backend.app.services.rag.index_build import LeakError, build, check, find_leaks
from backend.app.services.rag.store import CHUNKS_FILE, EMBEDDINGS_FILE, FileChunkStore
from backend.app.services.rules import load_rules
from scripts import build_rag_index

FAKE_BL = "TESTBL99"
FAKE_MRN = "26TESTMR01I"
_BASE = "ABCU123456"
FAKE_CNTR = _BASE + str(chat.container_check_digit(_BASE))


class Workspace:
    """임시 레포 모양: skills/ 복사본, docs/rag/corpus.yaml 복사본, 색인과 manifest 위치."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        shutil.copytree(REPO_ROOT / "skills", self.root / "skills")
        (self.root / "docs" / "rag").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "docs" / "rag" / "corpus.yaml", self.root / "docs" / "rag" / "corpus.yaml")
        self.corpus_file = self.root / "docs" / "rag" / "corpus.yaml"
        self.index_dir = self.root / "docs" / "rag" / "index"
        self.manifest = self.root / "docs" / "rag" / "index_manifest.json"

    def corpus(self):
        return load_corpus(self.corpus_file)

    def build(self, embedder=None, **kw):
        return build(embedder or FakeEmbedder(), corpus=self.corpus(), root=self.root, index_dir=self.index_dir,
                     manifest_path=self.manifest, **kw)

    def check(self, embedder=None, **kw):
        return check(embedder or FakeEmbedder(), manifest_path=self.manifest, index_dir=self.index_dir,
                     corpus=self.corpus(), root=self.root, **kw)

    def skill(self, folder="import-basics"):
        return self.root / "skills" / folder / "SKILL.md"

    def append_bullet(self, text, folder="import-basics"):
        path = self.skill(folder)
        path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + f"\n- {text}\n", encoding="utf-8")


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


# ── (a) 재현 ──

def test_same_input_gives_same_fingerprint_and_same_files(ws, tmp_path):
    first = ws.build(built_at="2026-01-01T00:00:00Z")
    chunks_a = (ws.index_dir / CHUNKS_FILE).read_bytes()
    vectors_a = np.load(ws.index_dir / EMBEDDINGS_FILE)
    other = Workspace(tmp_path / "second")
    second = other.build(built_at="2030-05-05T00:00:00Z")  # 만든 시각만 다르다
    assert first["fingerprint"] == second["fingerprint"] and first["index_id"] == second["index_id"]
    assert chunks_a == (other.index_dir / CHUNKS_FILE).read_bytes()
    assert np.array_equal(vectors_a, np.load(other.index_dir / EMBEDDINGS_FILE))
    assert first["built_at"] != second["built_at"]


def test_fingerprint_changes_with_corpus_chunking_embedding_and_verified_date(ws):
    base = ws.build()["fingerprint"]
    assert ws.build(FakeEmbedder(dim=16))["fingerprint"] != base  # 모델 설정(차원)
    # 청킹 파라미터
    params = ChunkParams(target_chars=300, max_chars=600, min_chars=60, context_header=True)
    assert ws.build(params=params)["fingerprint"] != base
    off = ChunkParams(target_chars=500, max_chars=800, min_chars=80, context_header=False)
    assert ws.build(params=off)["fingerprint"] != base
    # 검수일(코퍼스 항목)
    text = ws.corpus_file.read_text(encoding="utf-8")
    ws.corpus_file.write_text(text.replace("last_verified: null         # TODO 사람이 검수일을 채운다 (YYYY-MM-DD). 지어내지 않는다",
                                           "last_verified: 2026-10-01", 1), encoding="utf-8")
    assert ws.build()["fingerprint"] != base
    # 파일 내용
    ws.append_bullet("추가 문장이다.")
    assert ws.build()["fingerprint"] not in {base}


def test_retrieval_params_are_not_part_of_the_fingerprint(ws, monkeypatch):
    manifest = ws.build()
    original = load_rules()
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_top_k": 9, "rag_min_terms": 3})
    again = ws.build()
    assert again["fingerprint"] == manifest["fingerprint"]
    assert again["retrieval_keys_hash"] != manifest["retrieval_keys_hash"]  # 따로 기록된다


def test_fingerprint_ignores_crlf_checkout(ws):
    base = ws.build()["fingerprint"]
    path = ws.skill("port-busan")
    path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    assert ws.build()["fingerprint"] == base


# ── (d) manifest 칸 ──

def test_manifest_has_the_four_axes_fingerprint_and_observed(ws):
    manifest = ws.build(built_at="2026-01-01T00:00:00Z")
    assert json.loads(ws.manifest.read_text(encoding="utf-8")) == manifest
    assert set(manifest) >= {"index_id", "built_at", "corpus", "chunking", "embedding", "retrieval_keys_hash", "fingerprint", "observed"}
    assert manifest["index_id"] == manifest["fingerprint"][:12]
    assert [row["path"] for row in manifest["corpus"]] == [e.path for e in ws.corpus().entries]
    assert all(set(row) >= {"corpus_id", "path", "sha256", "chars", "chunks", "last_verified"} for row in manifest["corpus"])
    assert sum(row["chunks"] for row in manifest["corpus"]) == manifest["chunk_count"] == len(ws.index_dir.joinpath(CHUNKS_FILE).read_text(encoding="utf-8").splitlines())
    assert manifest["chunking"] == {"version": 1, "target_chars": rules.rule("rag_chunk_target_chars"),
                                    "max_chars": rules.rule("rag_chunk_max_chars"), "min_chars": rules.rule("rag_chunk_min_chars"),
                                    "context_header": True}
    assert set(manifest["embedding"]) >= {"model", "revision", "file_sha256", "dim", "pooling", "query_prefix", "passage_prefix", "backend"}
    assert manifest["observed"] == {"last_verified_min": None, "last_verified_max": None}  # 검수일을 사람이 채우기 전
    assert "본문" not in ws.manifest.read_text(encoding="utf-8")  # 문서 본문은 manifest에 없다 (해시와 글자 수만)


def test_built_file_index_loads_through_the_file_store(ws):
    manifest = ws.build()
    store = FileChunkStore(ws.index_dir, manifest_path=ws.manifest)
    assert len(store.chunks()) == manifest["chunk_count"]
    info = store.info()
    assert (info.index_id, info.fingerprint, info.embed_model, info.embed_dim) == (
        manifest["index_id"], manifest["fingerprint"], "fake-hash", FakeEmbedder().dim)
    rows = [c for c in store.chunks() if c.table_row is not None]
    assert rows and all(c.vector is not None for c in store.chunks())  # 표 행 메타가 파일 색인에는 실린다


# ── (b) --check ──

def test_check_passes_on_fresh_build_and_fails_after_one_char_change(ws):
    ws.build()
    assert ws.check() == ([], [])
    path = ws.skill("customs-stages")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("바로 관세사에게", "바로 관세사에게.", 1), encoding="utf-8")  # 한 글자
    problems, _ = ws.check()
    assert any("지문" in p for p in problems) and any("customs-stages" in p and "sha256" in p for p in problems)


def test_check_fails_on_changed_chunking_model_and_added_corpus_item(ws):
    ws.build()
    params = ChunkParams(target_chars=400, max_chars=800, min_chars=80, context_header=True)
    problems, _ = ws.check(params=params)
    assert any("chunking.target_chars" in p for p in problems)
    problems, _ = ws.check(FakeEmbedder(dim=16))
    assert any("embedding.dim" in p for p in problems)
    ws.corpus_file.write_text(ws.corpus_file.read_text(encoding="utf-8").replace(
        "excluded:", "  - {corpus_id: skill-referrals, path: skills/referrals/SKILL.md, kind: skill, license: internal}\nexcluded:", 1)
        .replace('  - {path: "skills/referrals/**", reason: "주소·전화는 코드가 붙인다. LLM에 안 넣는다"}\n', ""), encoding="utf-8")
    problems, _ = ws.check()
    assert any("코퍼스 항목 추가: skill-referrals" in p for p in problems)


def test_check_without_embedder_still_catches_corpus_changes_and_says_so(ws):
    ws.build()
    problems, notes = ws.check(None) if False else check(None, manifest_path=ws.manifest, index_dir=ws.index_dir,
                                                            corpus=ws.corpus(), root=ws.root)
    assert problems == [] and any("임베더를 만들 수 없어" in n for n in notes)
    ws.append_bullet("바뀐 문장이다.")
    problems, _ = check(None, manifest_path=ws.manifest, index_dir=ws.index_dir, corpus=ws.corpus(), root=ws.root)
    assert any("import-basics" in p for p in problems)


def test_check_fails_without_manifest_and_on_tampered_index_files(ws):
    problems, _ = ws.check()
    assert problems and "manifest" in problems[0]
    ws.build()
    lines = (ws.index_dir / CHUNKS_FILE).read_text(encoding="utf-8").splitlines()
    (ws.index_dir / CHUNKS_FILE).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    problems, _ = ws.check()
    assert any("색인 파일" in p for p in problems)
    shutil.rmtree(ws.index_dir)
    problems, notes = ws.check()
    assert problems == [] and any("파일 색인이 없어" in n for n in notes)  # 파일 색인이 커밋되지 않은 환경에서도 manifest는 견준다


def test_changed_retrieval_params_are_only_a_note(ws, monkeypatch):
    ws.build()
    original = load_rules()
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_top_k": 9})
    problems, notes = ws.check()
    assert problems == [] and any("검색 파라미터" in n for n in notes)


# ── (c) 누출 게이트 ──

@pytest.mark.parametrize("kind, value", [
    ("bl", f"문의 번호 {FAKE_BL} 확인"),
    ("mrn", f"MRN {FAKE_MRN} 확인"),
    ("container", f"컨테이너 {FAKE_CNTR} 반출"),
    ("phone", "연락처 051-123-4567 로 전화"),
    ("phone", "휴대폰 010-1234-5678 로 전화"),
    ("email", "메일 someone@example.test 로 보냄"),
])
def test_planted_leak_fails_the_build_and_writes_nothing(ws, kind, value):
    ws.append_bullet(value)
    with pytest.raises(LeakError) as exc:
        ws.build()
    message = str(exc.value)
    assert kind in message and "import-basics" in message
    assert not any(secret in message for secret in (FAKE_BL, FAKE_MRN, FAKE_CNTR, "051-123", "010-1234", "someone@"))  # 값은 적지 않는다
    assert not ws.index_dir.exists() and not ws.manifest.exists()  # 아무것도 쓰지 않았다


def test_leak_in_source_note_is_found_but_urls_are_not_scanned(ws):
    path = ws.skill("import-basics")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.rstrip("\n") + f"\n출처: 내부 문서 {FAKE_BL}\n", encoding="utf-8")
    leaks = find_leaks(chunk_corpus(ws.corpus(), root=ws.root, params=ChunkParams.from_rules()))
    assert {(leak.kind, leak.field) for leak in leaks} == {("bl", "source_note")}
    # 실제 코퍼스는 출처 URL에 숫자가 든 긴 주소가 있어도 누출로 세지 않는다
    assert find_leaks(chunk_corpus()) == []


def test_check_runs_the_leak_gate_too(ws):
    ws.build()
    ws.append_bullet(f"번호 {FAKE_BL}")
    problems, _ = ws.check()
    assert problems and "누출" in problems[0] and FAKE_BL not in problems[0]


# ── DB 반영 ──

def test_db_option_hands_the_records_to_pg_store(ws, monkeypatch):
    calls = []

    class FakePg:
        def write_index(self, info, records, *, activate=True):
            calls.append((info, list(records), activate))

    monkeypatch.setattr(index_build, "PgChunkStore", FakePg)
    manifest = ws.build(to_db=True)
    [(info, records, activate)] = calls
    assert (info.index_id, info.fingerprint, info.embed_model, info.embed_dim) == (
        manifest["index_id"], manifest["fingerprint"], "fake-hash", FakeEmbedder().dim)
    assert activate and len(records) == manifest["chunk_count"] and all(len(r.vector) == info.embed_dim for r in records)
    calls.clear()
    ws.build()
    assert calls == []  # --db 없이는 DB를 건드리지 않는다


# ── CLI ──

def test_cli_build_then_check_and_exit_codes(ws, capsys, monkeypatch):
    monkeypatch.setenv("RAG_EMBED_BACKEND", "fake")
    args = ["--corpus", str(ws.corpus_file), "--index-dir", str(ws.index_dir), "--manifest", str(ws.manifest)]
    # 이 CLI는 기본 root(REPO_ROOT)의 파일을 읽으므로, 임시 corpus.yaml이 가리키는 상대 경로가 레포 안 실제 파일과 같다
    assert build_rag_index.main(args) == 0
    assert "색인 만듦" in capsys.readouterr().out
    assert build_rag_index.main(args + ["--check"]) == 0
    manifest = json.loads(ws.manifest.read_text(encoding="utf-8"))
    manifest["fingerprint"] = "0" * 64
    ws.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    assert build_rag_index.main(args + ["--check"]) == 1
    assert "실패" in capsys.readouterr().err


def test_cli_build_without_model_exits_2_but_check_falls_back(ws, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_EMBED_BACKEND", "onnx")
    monkeypatch.setenv("RAG_EMBED_MODEL_DIR", str(tmp_path / "no-model"))
    args = ["--corpus", str(ws.corpus_file), "--index-dir", str(ws.index_dir), "--manifest", str(ws.manifest)]
    assert build_rag_index.main(args) == 2
    assert not ws.manifest.exists()
    monkeypatch.setenv("RAG_EMBED_BACKEND", "fake")
    assert build_rag_index.main(args) == 0
    monkeypatch.setenv("RAG_EMBED_BACKEND", "onnx")
    assert build_rag_index.main(args + ["--check"]) == 0  # 모델이 없어도 manifest의 모델 설정으로 코퍼스, 청킹을 견준다
    assert "임베더를 만들 수 없어" in capsys.readouterr().out


# ── 레포에 커밋된 manifest (EC-10) ──

def test_committed_manifest_matches_current_corpus_and_chunking():
    """CI가 코퍼스 문서만 고치고 색인을 안 다시 만든 커밋을 잡는다 (Spec EC-10). 모델 파일 없이 돈다.
    이 테스트가 실패하면 `python -m scripts.build_rag_index`로 색인을 다시 만들어 manifest와 docs/rag/index/를 같이 커밋한다."""
    problems, _ = check(None)
    assert problems == [], problems


def test_committed_manifest_embedding_matches_the_pinned_model():
    manifest = json.loads(index_build.MANIFEST_FILE.read_text(encoding="utf-8"))
    emb = manifest["embedding"]
    if emb["backend"] == "fake":
        pytest.skip("fake 임베더로 만든 색인")
    assert manifest_problems(MODEL_SPECS[emb["model"]], manifest) == []
    assert "tokenizer_sha256" in emb  # 토크나이저가 바뀌면 벡터가 달라지므로 지문에 들어간다
