"""skills/rag-search/rules.md 키와 읽는 코드 (services/rag/config.py), 작업 R2 (docs/specs/v2-rag.md 6절).

완료 기준: rule("rag_top_k") 등이 읽히고, 같은 키가 두 파일에 있으면 시작 실패(load_rules),
코드에 임계값 숫자가 없고, rag_min_score가 null이면 게이트가 꺼지고 경고가 남는다.
"""
import ast
import logging
from pathlib import Path

import pytest

from backend.app.services import rules
from backend.app.services.rag import config
from backend.app.services.rag.config import gate_thresholds
from backend.app.services.rules import load_rules, rule

RAG_DIR = Path(__file__).resolve().parents[1] / "app" / "services" / "rag"
RAG_KEYS = {"rag_corpus_folders", "rag_chunk_target_chars", "rag_chunk_max_chars", "rag_chunk_min_chars", "rag_context_header",
            "rag_candidates", "rag_top_k", "rag_rrf_k", "rag_bm25_k1", "rag_bm25_b", "rag_query_turns", "rag_query_max_chars",
            "rag_min_score", "rag_min_terms", "rag_search_timeout_ms", "rag_ann_min_chunks"}
# 숫자 검사 대상 (R1, R2, R6 파일). 임베더·저장소처럼 모델 규격 숫자(차원 등)가 정당하게 있는 파일은 각 작업이 따로 본다
CODE_FILES = ("chunker.py", "corpus.py", "config.py", "query.py", "bm25.py", "search.py", "index_build.py")
# Spec 6절이 "미정"이라 한 값은 null이다. 값을 지어 쓰지 않는다
UNDECIDED = {"rag_min_terms", "rag_search_timeout_ms", "rag_ann_min_chunks"}


@pytest.fixture(autouse=True)
def _fresh_warnings():
    config._warned.clear()
    yield
    config._warned.clear()


def test_rag_search_folder_has_only_rules_md_with_the_spec_keys():
    folder = rules.SKILLS_DIR / "rag-search"
    assert [p.name for p in folder.iterdir()] == ["rules.md"]  # SKILL.md를 두지 않는다 (어떤 LLM 호출도 통째로 넣지 않는다)
    assert set(rules._front_matter(folder / "rules.md")) == RAG_KEYS
    merged = load_rules()
    assert RAG_KEYS <= set(merged)


def test_rag_keys_are_readable_with_spec_starting_values():
    assert rule("rag_top_k") == 4 and rule("rag_candidates") == 10 and rule("rag_rrf_k") == 60
    assert (rule("rag_chunk_target_chars"), rule("rag_chunk_max_chars"), rule("rag_chunk_min_chars")) == (500, 800, 80)
    assert rule("rag_context_header") is True
    assert (rule("rag_query_turns"), rule("rag_query_max_chars")) == (2, 200)
    assert rule("rag_corpus_folders") == ["ais-reliability", "customs-stages", "cargo-verdict", "import-basics", "port-busan"]
    assert "referrals" not in rule("rag_corpus_folders")
    assert rule("rag_bm25_k1") > 0 and 0 <= rule("rag_bm25_b") <= 1


def test_undecided_values_are_null_not_invented():
    for key in UNDECIDED:
        assert rule(key) is None, key
    table = rule("rag_min_score")
    assert isinstance(table, dict) and table and all(v is None for v in table.values())


def test_duplicate_rag_key_in_another_file_fails_at_load(tmp_path):
    for name, body in (("rag-search", "rag_top_k: 4"), ("other-skill", "rag_top_k: 5")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "rules.md").write_text(f"---\n{body}\n---\n\n# 설명\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rag_top_k"):
        load_rules(tmp_path)


def test_null_min_score_disables_dense_gate_and_warns_once(caplog):
    model = next(iter(rule("rag_min_score")))
    with caplog.at_level(logging.WARNING, logger="marvia.rag"):
        gate = gate_thresholds(model)
        gate_thresholds(model)
    assert gate.min_score is None and gate.dense_enabled is False
    assert gate.min_terms is None and gate.bm25_enabled is False
    messages = [r.getMessage() for r in caplog.records if r.name == "marvia.rag"]
    assert sum("rag_min_score" in m and model in m for m in messages) == 1  # 같은 경고를 되풀이하지 않는다
    assert any("rag_min_terms" in m for m in messages)


def test_unknown_model_is_treated_like_null(caplog):
    with caplog.at_level(logging.WARNING, logger="marvia.rag"):
        gate = gate_thresholds("no/such-model")
    assert gate.dense_enabled is False
    assert any("no/such-model" in r.getMessage() for r in caplog.records)


def test_bm25_only_mode_does_not_look_at_min_score(caplog):
    with caplog.at_level(logging.WARNING, logger="marvia.rag"):
        gate = gate_thresholds(None)
    assert gate.model_id is None and gate.min_score is None
    assert not any("rag_min_score" in r.getMessage() for r in caplog.records)


def test_numbers_in_rules_turn_the_gates_on(monkeypatch, caplog):
    original = load_rules()
    model = next(iter(original["rag_min_score"]))
    patched = {**original, "rag_min_score": {model: 0.5}, "rag_min_terms": 2}
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: patched)
    with caplog.at_level(logging.WARNING, logger="marvia.rag"):
        gate = gate_thresholds(model)
    assert (gate.min_score, gate.min_terms) == (0.5, 2)
    assert gate.dense_enabled and gate.bm25_enabled
    assert not [r for r in caplog.records if r.name == "marvia.rag"]


def test_non_numeric_threshold_is_a_typo_and_fails(monkeypatch):
    original = load_rules()
    model = next(iter(original["rag_min_score"]))
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_min_score": {model: "높음"}})
    with pytest.raises(ValueError):
        gate_thresholds(model)
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_min_score": {model: True}})
    with pytest.raises(ValueError):
        gate_thresholds(model)


def test_no_threshold_numbers_in_rag_code():
    """코드에 임계값 숫자를 적지 않는다 (CLAUDE.md). 모듈 맨 위 대문자 상수(Spec이 정한 규격값, 출처 주석 있음)와 0, 1, -1, 2를 뺀
    모든 숫자 리터럴이 rag 패키지 코드에 없어야 한다."""
    allowed_small = {0, 1, 2, -1}
    offenders = []
    for name in CODE_FILES:
        path = RAG_DIR / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constant_nodes = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
                constant_nodes.update(id(n) for n in ast.walk(node.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                if id(node) not in constant_nodes and node.value not in allowed_small:
                    offenders.append(f"{path.name}:{node.lineno}={node.value}")
    assert not offenders, offenders
