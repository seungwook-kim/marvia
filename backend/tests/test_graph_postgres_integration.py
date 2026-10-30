"""H7: 진짜 Postgres에서 체크포인트 왕복 (docs/specs/v2-langgraph.md 10절 H7 (8), AC-10). DATABASE_URL_TEST가 있을 때만 돈다.

돌리는 법(로컬 Docker가 있을 때):
  docker run -d --name marvia-pg-test -e POSTGRES_PASSWORD=pw -p 55432:5432 pgvector/pgvector:pg16
  DATABASE_URL_TEST=postgresql://postgres:pw@localhost:55432/postgres pytest backend/tests/test_graph_postgres_integration.py
이 DB는 비어 있어도 된다(032는 앞선 마이그레이션에 기대지 않는다). pgvector 이미지면 RAG 명세의 033도 이어 적용해 032, 033 순서에서도 왕복이 되는지 본다.
테스트가 만든 스레드는 끝날 때 지운다. 표는 남긴다(032가 만든 것이고 다시 적용해도 오류가 없다).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.postgres import PostgresSaver

from backend.app import crypto
from backend.app.db import MIGRATIONS_DIR, purge_graph_checkpoints
from backend.app.graph import checkpoint, facade, threads
from backend.app.routers import agent as agent_router

URL = os.environ.get("DATABASE_URL_TEST")
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL_TEST가 없으면 건너뛴다")

from backend.tests.test_graph_resume import BL, _dump, _req, _run, _setup, _thread  # noqa: E402
from backend.tests.test_routers import fake  # noqa: E402,F401  (pytest 픽스처)


@pytest.fixture
def db():
    """032(와 가능하면 033)를 적용한 연결. 같은 파일을 두 번 적용해도 오류가 없어야 한다."""
    with psycopg.Connection.connect(URL, autocommit=True) as conn:  # psycopg.connect는 conftest가 막아 둔다
        sql32 = (MIGRATIONS_DIR / "032_graph_checkpoints.sql").read_text(encoding="utf-8")
        conn.execute(sql32)
        conn.execute(sql32)  # 두 번째 적용
        has_vector = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone()
        rag = MIGRATIONS_DIR / "033_rag_chunks.sql"
        if has_vector and rag.exists():
            conn.execute(rag.read_text(encoding="utf-8"))  # 032, 033 순서 (RAG 명세)
        yield conn
        conn.execute("DELETE FROM checkpoint_writes WHERE thread_id LIKE 'verdict:%'")
        conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id LIKE 'verdict:%'")
        conn.execute("DELETE FROM checkpoints WHERE thread_id LIKE 'verdict:%'")


@pytest.fixture
def postgres_mode(monkeypatch, db):
    monkeypatch.setenv(crypto.BL_KEY_ENV, Fernet.generate_key().decode())
    monkeypatch.setenv("GRAPH_CHECKPOINT", "postgres")
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("GRAPH_DB_POOL_MAX", "2")
    checkpoint.close_checkpointer()
    yield
    checkpoint.close_checkpointer()


def test_migration_records_all_library_versions(db):
    versions = [r[0] for r in db.execute("SELECT v FROM checkpoint_migrations ORDER BY v").fetchall()]
    assert versions == list(range(len(PostgresSaver.MIGRATIONS)))
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
        assert db.execute("SELECT relrowsecurity FROM pg_class WHERE relname = %s", [table]).fetchone()[0] is True


def test_pause_resume_and_delete_through_postgres_saver(fake, monkeypatch, postgres_mode, db):  # noqa: F811
    """멈춤 -> 재개 -> 삭제가 진짜 PostgresSaver로 된다. 멈춘 동안 스레드가 있고 끝나면 없다. 응답은 v1과 같다. blob은 암호화돼 있다."""
    c = _setup(fake, monkeypatch)
    v1 = [_run("off", _req()), _run("off", _req(resolved_mmsi=440999001))]
    c.reset()
    first = _run("on", _req())
    thread = _thread()
    assert _dump(first) == _dump(v1[0])
    rows = db.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", [thread]).fetchone()[0]
    assert rows >= 1
    # DB에 든 blob에도 writes에도 B/L 글자가 없다. 암호 이름 접미사가 붙어 있다
    types = [r[0] for r in db.execute("SELECT type FROM checkpoint_blobs WHERE thread_id = %s AND blob IS NOT NULL", [thread]).fetchall()]
    assert types and all(t.endswith("+fernet") for t in types)
    raw = b"".join(bytes(r[0]) for r in db.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id = %s AND blob IS NOT NULL", [thread]).fetchall())
    raw += b"".join(bytes(r[0]) for r in db.execute("SELECT blob FROM checkpoint_writes WHERE thread_id = %s", [thread]).fetchall())
    assert BL.encode() not in raw
    meta = db.execute("SELECT metadata::text FROM checkpoints WHERE thread_id = %s", [thread]).fetchall()
    assert all(BL not in m[0] for m in meta)

    c.reset()
    second = _run("on", _req(resolved_mmsi=440999001))
    assert _dump(second) == _dump(v1[1]) and c.fetch == ["MASTER"] and (c.saved_row, c.load_context) == (0, 0)
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert db.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s", [thread]).fetchone()[0] == 0


def test_purge_function_deletes_only_old_threads(db):
    """정리 함수는 가장 최근 체크포인트가 keep_days보다 오래된 스레드만 지운다. 세 표를 함께 지우고 지운 스레드 수를 돌려준다."""
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    import json

    for thread, ts in (("verdict:purgeold:a", old), ("verdict:purgenew:b", new)):
        db.execute("INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint) VALUES (%s, '', 'c1', %s::jsonb)",
                   [thread, json.dumps({"ts": ts, "id": "c1"})])
        db.execute("INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) VALUES (%s, '', 'x', '1', 'msgpack+fernet', %s)",
                   [thread, b"\x00"])
        db.execute("INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, blob) VALUES (%s, '', 'c1', 't', 0, 'x', %s)",
                   [thread, b"\x00"])
    assert purge_graph_checkpoints(db, 7) == 1
    left = {r[0] for r in db.execute("SELECT thread_id FROM checkpoints WHERE thread_id LIKE 'verdict:purge%'").fetchall()}
    assert left == {"verdict:purgenew:b"}
    for table in ("checkpoint_blobs", "checkpoint_writes"):
        assert {r[0] for r in db.execute(f"SELECT thread_id FROM {table} WHERE thread_id LIKE 'verdict:purge%'").fetchall()} == {"verdict:purgenew:b"}
    assert purge_graph_checkpoints(db, 7) == 0  # 여러 번 불러도 같다


def test_user_thread_delete_removes_only_that_user(db):
    mine, other = threads.user_prefix("user-a") + "x", threads.user_prefix("user-b") + "x"
    for thread in (mine, other):
        db.execute("INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint) VALUES (%s, '', 'c1', '{}'::jsonb)", [thread])
    threads.delete_user_threads(db, "user-a")
    left = {r[0] for r in db.execute("SELECT thread_id FROM checkpoints WHERE thread_id LIKE 'verdict:%'").fetchall()}
    assert left == {other}


def test_plain_blob_row_written_directly_is_refused(fake, monkeypatch, postgres_mode, db):  # noqa: F811
    """DB에 평문 blob을 직접 써 넣은 행은 읽을 때 거부되고, 퍼사드는 그 스레드를 지우고 새로 돈다 (10절 H7 (11))."""
    c = _setup(fake, monkeypatch)
    _run("on", _req())
    thread = _thread()
    db.execute("UPDATE checkpoint_blobs SET type = 'msgpack', blob = %s WHERE thread_id = %s AND blob IS NOT NULL", [b"\x90", thread])
    c.reset()
    response = _run("on", _req(resolved_mmsi=440999001))
    assert response.status == "ok" and c.fetch == ["HOUSE", "MASTER"]
