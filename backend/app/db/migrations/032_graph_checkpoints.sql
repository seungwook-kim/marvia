-- 판정 그래프(LangGraph)의 체크포인트 표 (docs/specs/v2-langgraph.md 4절, SDD 3.9). 되묻기에서 멈춘 그래프의 State를 담는다.
--
-- 표 4개는 langgraph-checkpoint-postgres 3.2.x의 PostgresSaver.MIGRATIONS(v0~v9)가 만드는 것과 같은 모양이다.
-- 라이브러리의 setup()에 맡기지 않고 이 파일이 만드는 이유:
--   - setup()은 CREATE INDEX CONCURRENTLY를 쓰는데, db.apply_migrations는 파일 하나를 트랜잭션으로 묶어 실행해 거기서 못 돈다
--     (이 파일의 색인은 CONCURRENTLY 없이 만든다. 표가 비어 있어 문제없다)
--   - 새 표에 RLS를 켜는 레포 규칙(005 이후)을 setup()이 모른다
-- 라이브러리를 올려 MIGRATIONS가 늘면 새 마이그레이션(033 이후 번호)이 필요하다. 아래 checkpoint_migrations 기록 수(10)와
-- len(PostgresSaver.MIGRATIONS)가 같은지 backend/tests/test_graph_migration.py가 본다.
--
-- 저장되는 것: 멈춰 있는 동안의 State와 중단 값뿐이다. 끝난 실행은 코드가 바로 지운다 (graph/facade.py).
-- blob·writes 값은 서버가 B/L 열쇠로 잠근 바이트만 든다(graph/checkpoint.py). thread_id에는 B/L이 없다(해시 두 개, graph/threads.py).

-- v0
CREATE TABLE IF NOT EXISTS checkpoint_migrations (
  v INTEGER PRIMARY KEY
);

-- v1
CREATE TABLE IF NOT EXISTS checkpoints (
  thread_id TEXT NOT NULL,
  checkpoint_ns TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL,
  parent_checkpoint_id TEXT,
  type TEXT,
  checkpoint JSONB NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- v2, v4 (blob은 비어 있을 수 있다)
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
  thread_id TEXT NOT NULL,
  checkpoint_ns TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL,
  version TEXT NOT NULL,
  type TEXT NOT NULL,
  blob BYTEA,
  PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

-- v3, v9 (task_path)
CREATE TABLE IF NOT EXISTS checkpoint_writes (
  thread_id TEXT NOT NULL,
  checkpoint_ns TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  channel TEXT NOT NULL,
  type TEXT,
  blob BYTEA NOT NULL,
  task_path TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- v6, v7, v8
CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints (thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs (thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes (thread_id);

-- 라이브러리가 "이 버전까지 적용됨"으로 읽는 기록. v5는 라이브러리에서 빈 문장(SELECT 1)이지만 번호는 채운다.
-- 라이브러리의 MIGRATIONS 길이가 이 기록 수와 같아야 PostgresSaver가 setup 없이 쓸 수 있다
INSERT INTO checkpoint_migrations (v) VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)
ON CONFLICT (v) DO NOTHING;

-- 005와 같다. 공개 키(Data API)로는 읽지 못하게 RLS를 켜고 규칙은 두지 않는다
ALTER TABLE checkpoint_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_writes ENABLE ROW LEVEL SECURITY;

-- 오래 남은 스레드를 지운다. 스레드의 나이는 그 스레드의 가장 최근 체크포인트 시각(checkpoint->>'ts')이다.
-- 어느 요청도 오지 않아 코드가 못 지운 멈춘 스레드의 안전망이다(코드는 요청이 오면 TTL 지난 스레드를 지운다).
-- 기간은 SQL에 박지 않고 인자로 받는다: 규칙값은 skills/cargo-verdict/rules.md graph_checkpoint_retention_days
-- (worker/run.py가 읽어 넘긴다). 지운 스레드 수를 돌려준다. 여러 번 불러도 결과는 같다.
CREATE OR REPLACE FUNCTION purge_old_graph_checkpoints(keep_days integer)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  old_threads text[];
BEGIN
  SELECT coalesce(array_agg(thread_id), ARRAY[]::text[]) INTO old_threads
  FROM (
    SELECT thread_id
    FROM checkpoints
    GROUP BY thread_id
    HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => keep_days)
  ) AS stale;
  DELETE FROM checkpoint_writes WHERE thread_id = ANY (old_threads);
  DELETE FROM checkpoint_blobs WHERE thread_id = ANY (old_threads);
  DELETE FROM checkpoints WHERE thread_id = ANY (old_threads);
  RETURN coalesce(array_length(old_threads, 1), 0);
END;
$$;
