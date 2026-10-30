"""033_rag_chunks.sql 테스트 (docs/specs/v2-rag.md R3 완료 기준 b).

pgvector가 없는 DB(로컬 postgres:16)에서 이 파일이 실패하면 뒤 번호 마이그레이션이 모두 막힌다 (db.apply_migrations).
그래서 두 가지로 지킨다: (1) DB 없이 SQL 글을 정적으로 검사, (2) DATABASE_URL_TEST가 있으면 실제로 적용.
"""
from __future__ import annotations

import os
import re

import psycopg
import pytest

from backend.app.db import MIGRATIONS_DIR

PATH = MIGRATIONS_DIR / "033_rag_chunks.sql"
_URL = os.environ.get("DATABASE_URL_TEST", "")
_REAL_CONNECT = psycopg.connect  # conftest가 테스트마다 막기 전의 함수 (test_rag_store.py와 같은 이유)


def _sql() -> str:
    return PATH.read_text(encoding="utf-8")


def _code_without_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


# ---- 정적 검사: DB 없이 ----

def test_migration_is_numbered_after_existing_ones():
    names = [p.name for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    assert "033_rag_chunks.sql" in names
    assert names == sorted(names)
    assert names.index("033_rag_chunks.sql") > names.index("031_watch_positions_source.sql")


def test_guard_checks_for_pgvector_before_creating_anything():
    code = _code_without_comments(_sql())
    guard = code.index("pg_available_extensions")
    # 표와 확장 만들기는 전부 "확장이 있으면" 분기 안에서만 나온다
    for statement in ("CREATE EXTENSION", "CREATE TABLE", "ENABLE ROW LEVEL SECURITY"):
        for match in re.finditer(statement, code):
            assert match.start() > guard, f"{statement}가 pgvector 확인보다 앞에 있다"
    # DO 블록 바깥(맨 위 문장)에는 아무 문장도 없다: 첫 문장이 DO다
    assert code.strip().startswith("DO $rag033$")
    assert code.strip().endswith("$rag033$;")


def test_missing_pgvector_only_raises_a_notice():
    code = _code_without_comments(_sql())
    assert re.search(r"ELSE\s+RAISE NOTICE", code), "확장이 없을 때는 알림만 남긴다"
    assert "RAISE EXCEPTION" not in code, "어느 경로에서도 마이그레이션을 실패시키지 않는다"
    # "pgvector를 쓸 수 없다" 오류만 잡는다. WHEN OTHERS는 SQL 버그까지 숨기므로 쓰지 않는다
    assert "WHEN OTHERS" not in code
    assert re.search(r"EXCEPTION WHEN feature_not_supported OR undefined_file OR insufficient_privilege THEN", code)


def test_tables_follow_spec_columns_and_rls():
    code = _code_without_comments(_sql())
    assert code.count("IF NOT EXISTS") >= 4  # 스키마, 확장, 표 둘: 여러 번 적용해도 같다
    for table in ("rag_index", "rag_chunks"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in code
    for column in ("index_id", "fingerprint", "embed_model", "embed_dim", "chunking", "is_active", "chunk_id",
                   "corpus_id", "source_path", "heading_path", "anchor", "ordinal", "last_verified", "source_url",
                   "embed_text", "embedding"):
        assert re.search(rf"\b{column}\b", code), column
    assert "extensions.vector(384)" in code
    assert "ON DELETE CASCADE" in code
    # 정확 검색이라 ANN 인덱스를 만들지 않는다 (Spec 4절)
    assert "ivfflat" not in code.lower() and "hnsw" not in code.lower()


def test_migration_has_no_em_dash():
    assert "—" not in _sql()


# ---- 실제 DB ----

def _scalar(conn, sql, *params):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


@pytest.mark.skipif(not _URL, reason="DATABASE_URL_TEST 없음")
def test_applies_cleanly_twice_and_matches_available_extensions():
    """pgvector가 있는 DB면 표 두 개가 생기고, 없는 DB(postgres:16)면 오류 없이 표가 하나도 안 생긴다. 두 번 적용해도 같다."""
    with _REAL_CONNECT(_URL, autocommit=True) as conn:
        has_vector = _scalar(conn, "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'") is not None
        conn.execute(_sql())
        conn.execute(_sql())
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename LIKE 'rag\\_%'").fetchall()}
        if has_vector:
            assert {"rag_index", "rag_chunks"} <= tables
            rls = dict(conn.execute(
                "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('rag_index', 'rag_chunks')").fetchall())
            assert rls == {"rag_index": True, "rag_chunks": True}
        else:
            assert tables == set()  # 표를 만들지 않았다 (파일 색인으로 동작, EC-03)


@pytest.mark.skipif(not _URL, reason="DATABASE_URL_TEST 없음")
def test_runs_inside_apply_migrations_style_transaction():
    """db.apply_migrations는 파일 하나를 한 트랜잭션으로 적용한다. 그 안에서도 오류가 없고 되돌려도 깨끗하다."""
    with _REAL_CONNECT(_URL, autocommit=True) as conn:
        with conn.transaction():
            conn.execute(_sql())
