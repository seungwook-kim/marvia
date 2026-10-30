"""H7: PostgresSaver가 평문 컬럼에 넣을 값에 식별 값이 없다 (docs/specs/v2-langgraph.md 4절 마스킹 2, AC-09).

진짜 PostgresSaver 클래스를 쓰되 DB 대신 SQL 인자를 기록하는 가짜 커서를 꽂아, 되묻기에서 멈추는 한 번의 실행이
checkpoints.checkpoint(JSONB), checkpoints.metadata(JSONB), checkpoint_blobs/checkpoint_writes(BYTEA)에 무엇을 넣으려 하는지 그대로 받는다.
키도 DB도 필요 없다. B/L, 컨테이너 번호, 전화번호는 가공값이다.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.postgres import PostgresSaver

from backend.app import crypto
from backend.app.graph import checkpoint, facade
from backend.app.graph.state import PLAINTEXT_FIELDS
from backend.app.routers import agent as agent_router
from backend.app.schemas.customs import mask_bl
from backend.tests.test_graph_resume import BL, SHED, _req, _setup
from backend.tests.test_routers import fake  # noqa: F401  (pytest 픽스처)

CONTAINER = "TSTU1234560"
PHONE = "010-1234-5678"
USER = "user-7f3a"          # Supabase 사용자 ID 모양의 가짜 값 (State user_id는 평문 허용 목록에 있다)
SESSION = "sess-plain-1"


class RecordingCursor:
    """SQL 인자를 모아 두는 가짜 커서. 읽기(fetch)는 빈 결과다."""

    def __init__(self, log: list):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))
        return self

    def executemany(self, sql, rows):
        for row in rows:
            self.log.append((" ".join(str(sql).split()), row))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class RecordingPostgresSaver(PostgresSaver):
    """진짜 PostgresSaver의 put/put_writes를 그대로 쓰고 DB 접근(_cursor)만 기록용으로 바꾼다. 읽기는 늘 "스레드 없음"이다."""

    def __init__(self):
        super().__init__(conn=object(), serde=checkpoint.make_serde())
        self.log: list = []

    @contextmanager
    def _cursor(self, *, pipeline: bool = False):
        yield RecordingCursor(self.log)

    def get_tuple(self, config):
        return None

    def delete_thread(self, thread_id):
        self.log.append(("DELETE", thread_id))


@pytest.fixture
def pg_saver(monkeypatch):
    monkeypatch.setenv(crypto.BL_KEY_ENV, Fernet.generate_key().decode())
    saver = RecordingPostgresSaver()
    monkeypatch.setattr(checkpoint, "get_checkpointer", lambda: saver)
    monkeypatch.setattr(facade, "_read_snapshot", lambda graph, s, thread: type("S", (), {"values": {}})())
    return saver


def _jsonb(value):
    """psycopg Jsonb 래퍼에서 파이썬 값을 꺼낸다."""
    return value.obj if hasattr(value, "obj") else value


def test_postgres_would_inline_only_allowed_fields_and_no_identifier(fake, monkeypatch, pg_saver):  # noqa: F811
    """멈춘 실행의 체크포인트: JSONB에 평문으로 들어가는 State 필드는 모두 PLAINTEXT_FIELDS에 있고, B/L·컨테이너 번호·연락처·session_id는 어디에도 없다.
    blob·writes 바이트는 암호화돼 그 글자가 보이지 않는다."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    _setup(fake, monkeypatch)
    request = _req(session_id=SESSION, follow_up_question=None)
    request = request.model_copy(update={"voyage_no": None})
    agent_router.run_agent(request, user_id=USER)

    sql = [(s, p) for s, p in pg_saver.log if s != "DELETE"]
    checkpoints = [p for s, p in sql if s.startswith("INSERT INTO checkpoints ")]
    blobs = [p for s, p in sql if s.startswith("INSERT INTO checkpoint_blobs")]
    writes = [p for s, p in sql if s.startswith("INSERT INTO checkpoint_writes")]
    assert checkpoints and blobs and writes

    # 1) 평문 JSONB: 체크포인트 본문과 metadata
    markers = ("__start__", "branch:", "start:", "join:")
    inline_keys: set[str] = set()
    plain_text = []
    for params in checkpoints:
        thread_id, _ns, _id, _parent, checkpoint_json, metadata_json = params
        body, meta = _jsonb(checkpoint_json), _jsonb(metadata_json)
        for key, value in body.get("channel_values", {}).items():
            if value is not None:  # 값이 없는(None) 칸은 평문에 들어가도 담는 정보가 없다
                inline_keys.add(key)
        plain_text.append(json.dumps(body, default=str) + json.dumps(meta, default=str) + thread_id)
    unexpected = {k for k in inline_keys if k not in PLAINTEXT_FIELDS and not k.startswith(markers)}
    assert not unexpected, f"평문 JSONB에 허용 목록 밖 State 필드가 들어간다: {sorted(unexpected)}"
    assert {"bl_masked", "user_id"} <= inline_keys  # 확인할 평문이 실제로 있다 (가린 B/L과 사용자 ID)

    # 2) 식별 값은 평문 어디에도 없다 (thread_id 포함)
    joined = "\n".join(plain_text)
    for secret in (BL, CONTAINER, SHED, SESSION, PHONE):
        assert secret not in joined, f"평문 칸에 {secret[:4]}... 가 있다"
    assert joined.count(USER) == 1 or USER in joined  # user_id는 허용된 평문 칸이다 (스펙: 이미 Langfuse·DB에 남는 값)
    assert mask_bl(BL) in joined  # 가린 값만 있다

    # 3) blob과 writes는 암호화된 바이트다 (타입에 +fernet), 글자가 보이지 않는다
    for row in blobs:
        assert row[4].endswith("+fernet") or row[4] == "empty"
        assert all(secret.encode() not in (row[5] or b"") for secret in (BL, CONTAINER, SHED, PHONE))
    for row in writes:
        assert row[7].endswith("+fernet"), row[7]
        assert all(secret.encode() not in row[8] for secret in (BL, CONTAINER, SHED, PHONE))


def test_config_metadata_carries_no_identifier(fake, monkeypatch, pg_saver):  # noqa: F811
    """checkpoints.metadata(평문 JSONB)는 config의 configurable·metadata에서 오는데, 퍼사드가 넣은 것은 thread_id와 recursion_limit뿐이라
    B/L·user_id·session_id가 없다 (4절 마스킹 2 (b))."""
    monkeypatch.setenv("AGENT_GRAPH", "on")
    _setup(fake, monkeypatch)
    agent_router.run_agent(_req(session_id=SESSION), user_id=USER)
    metas = [json.dumps(_jsonb(p[5]), default=str) for s, p in pg_saver.log
             if s != "DELETE" and s.startswith("INSERT INTO checkpoints ")]
    assert metas
    for text in metas:
        assert BL not in text and USER not in text and SESSION not in text
