"""H7: 마이그레이션 032 (docs/specs/v2-langgraph.md 4절, AC-10, 10절 H7 (7)). 키도 DB도 필요 없는 파일 대조 테스트다.

DB가 필요한 확인(두 번 적용, 정리 함수 동작, PostgresSaver 왕복)은 test_graph_postgres_integration.py가 DATABASE_URL_TEST가 있을 때만 한다.
"""
from __future__ import annotations

import re
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver

from backend.app.db import MIGRATIONS_DIR

FILE = MIGRATIONS_DIR / "032_graph_checkpoints.sql"
TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _sql() -> str:
    return FILE.read_text(encoding="utf-8")


def _columns(ddl: str, table: str) -> dict[str, str]:
    """CREATE TABLE ... (열 정의) 에서 열 이름 -> 정의 한 줄을 뽑는다(공백 정리, PRIMARY KEY 줄 제외)."""
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", ddl, re.S).group(1)
    cols = {}
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if line and not line.upper().startswith("PRIMARY KEY"):
            name, _, rest = line.partition(" ")
            cols[name] = " ".join(rest.split())
    return cols


def test_file_is_number_032_and_comes_after_031():
    names = sorted(p.name for p in Path(MIGRATIONS_DIR).glob("*.sql"))
    assert "031_watch_positions_source.sql" in names and FILE.name == "032_graph_checkpoints.sql"
    assert names.index(FILE.name) == names.index("031_watch_positions_source.sql") + 1


def test_every_table_has_rls_enabled():
    sql = _sql()
    for table in TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in sql


def test_recorded_versions_match_the_library_migrations():
    """checkpoint_migrations에 기록하는 버전 수가 len(PostgresSaver.MIGRATIONS)와 같다. 라이브러리를 올려 MIGRATIONS가 늘면 이 테스트가
    새 마이그레이션(033 이후 번호)이 필요하다고 알린다."""
    inserted = re.search(r"INSERT INTO checkpoint_migrations \(v\) VALUES (.*?)\s*ON CONFLICT", _sql(), re.S).group(1)
    versions = [int(v) for v in re.findall(r"\((\d+)\)", inserted)]
    assert versions == list(range(len(PostgresSaver.MIGRATIONS)))


def test_tables_have_the_same_columns_as_the_library_creates():
    """표 정의가 라이브러리 v0~v9가 만드는 것과 같은 열을 가진다(v4 blob NULL 허용, v9 task_path 포함)."""
    sql = _sql()
    lib = "\n".join(PostgresSaver.MIGRATIONS)
    lib_cols = {t: _columns(lib, t) for t in ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")}
    for table in TABLES:
        mine = _columns(sql, table)
        theirs = lib_cols[table]
        if table == "checkpoint_blobs":
            theirs = {**theirs, "blob": "BYTEA"}                     # v4: DROP NOT NULL
        if table == "checkpoint_writes":
            theirs = {**theirs, "task_path": "TEXT NOT NULL DEFAULT ''"}  # v9: ADD COLUMN
        assert {k: v.upper() for k, v in mine.items()} == {k: v.upper() for k, v in theirs.items()}, table


def test_indexes_are_not_concurrent_and_match_the_library():
    """색인은 CONCURRENTLY 없이 만든다(트랜잭션 안에서 돈다). 이름과 대상은 라이브러리 v6~v8과 같다."""
    sql = _sql()
    assert "CONCURRENTLY" not in sql.upper().replace("CONCURRENTLY 없이", "").replace("CONCURRENTLY를", "")
    for name in ("checkpoints_thread_id_idx", "checkpoint_blobs_thread_id_idx", "checkpoint_writes_thread_id_idx"):
        assert f"CREATE INDEX IF NOT EXISTS {name}" in sql
        assert name in "\n".join(PostgresSaver.MIGRATIONS)


def test_purge_function_takes_days_as_argument_and_deletes_by_checkpoint_ts():
    """정리 함수는 기간을 인자로 받는다(SQL에 7 같은 숫자를 박지 않는다). 스레드 나이는 checkpoint->>'ts'로 본다."""
    sql = _sql()
    assert "FUNCTION purge_old_graph_checkpoints(keep_days integer)" in sql
    assert "checkpoint->>'ts'" in sql and "make_interval(days => keep_days)" in sql
    body = sql[sql.index("CREATE OR REPLACE FUNCTION"):]
    assert not re.search(r"interval\s+'\d|days\s*=>\s*\d", body)
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        assert f"DELETE FROM {table} WHERE thread_id = ANY (old_threads)" in body


def test_worker_passes_the_rule_value_to_the_purge():
    """워커는 기간을 rules.md graph_checkpoint_retention_days에서 읽어 정리 함수에 넘긴다."""
    from backend.app.db import purge_graph_checkpoints

    seen = []

    class Conn:
        def execute(self, sql, params=None):
            seen.append((" ".join(sql.split()), params))

            class R:
                def fetchall(self):
                    return [(2,)]

            return R()

    assert purge_graph_checkpoints(Conn(), 7) == 2
    assert seen == [("SELECT purge_old_graph_checkpoints(%s)", [7])]
    source = (Path(__file__).resolve().parents[2] / "worker" / "run.py").read_text(encoding="utf-8")
    assert 'purge_graph_checkpoints(conn, rule("graph_checkpoint_retention_days"))' in source
