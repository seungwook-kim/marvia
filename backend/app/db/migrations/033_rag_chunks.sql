-- 033: 채팅 실무 질문 검색(RAG)의 색인 표 (docs/specs/v2-rag.md 4절, 작업 R3).
-- 코퍼스 A(skills/*/SKILL.md)의 조각과 임베딩만 든다. 사용자 데이터, B/L, 연락처는 들어가지 않는다 (Spec 확정 결정 3).
--
-- 이 파일은 pgvector 확장이 없는 DB(로컬 postgres:16 같은 일반 이미지)에서도 실패하지 않아야 한다.
-- apply_migrations는 번호 순서대로 적용하다가 한 파일이 실패하면 멈추므로(db/__init__.py), 여기서 실패하면
-- 뒤 번호 마이그레이션이 모두 막힌다. 그래서 확장이 없으면 표를 만들지 않고 알림만 남기고(Spec EC-03),
-- 확장 설치가 "쓸 수 없음" 계열 오류(파일 없음, 권한 부족)로 막혀도 이 블록 안의 변경만 되돌리고 경고로 넘어간다.
-- 그 경우 서버는 파일 색인(docs/rag/index/)으로 검색한다 (backend/app/services/rag/store.py FileChunkStore).
-- 나중에 확장을 켰다면 아래 DO 블록을 그대로 한 번 더 실행하면 된다(모두 IF NOT EXISTS라 여러 번 실행해도 같다).
--
-- 벡터 열 차원 384는 후보 모델(multilingual-e5-small) 기준이다 (Spec 4절). 모델의 차원이 바뀌면 같은 열을 다시 쓰지 않고
-- 다음 번호 마이그레이션에서 새 표 또는 새 열을 만든다 (지문 불일치로 잡힌다, Spec EC-05).
-- 인덱스: 조각이 수십 개라 ANN(IVFFlat, HNSW)을 만들지 않고 정확 검색(순차 스캔)이다 (Spec 4절).
-- 벡터 확장은 Supabase 관례대로 extensions 스키마에 둔다 (https://supabase.com/docs/guides/database/extensions/pgvector).
-- 일반 postgres는 이 스키마가 없으므로 먼저 만든다 (Supabase에는 이미 있다).
DO $rag033$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
    BEGIN
      CREATE SCHEMA IF NOT EXISTS extensions;
      CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

      CREATE TABLE IF NOT EXISTS rag_index (
        index_id    TEXT PRIMARY KEY,                 -- manifest 지문 앞 12자 (Spec 1절)
        fingerprint TEXT NOT NULL,                    -- 코퍼스, 청킹, 모델, 차원, 접두어, 풀링의 해시. 서버 기동 때 manifest와 견준다 (EC-05)
        embed_model TEXT NOT NULL,                    -- 모델 id (rules.md rag_min_score 맵의 키와 같다)
        embed_dim   INT NOT NULL,
        chunking    JSONB NOT NULL,                   -- 청킹 파라미터 (version, target_chars 등)
        built_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        is_active   BOOLEAN NOT NULL DEFAULT false    -- 읽는 쪽이 보는 색인. 교체는 한 트랜잭션에서 전환한다 (Spec 3절)
      );

      CREATE TABLE IF NOT EXISTS rag_chunks (
        index_id     TEXT NOT NULL REFERENCES rag_index (index_id) ON DELETE CASCADE,
        chunk_id     TEXT NOT NULL,                   -- 글이 바뀌면 바뀌는 증거용 id
        corpus_id    TEXT NOT NULL,
        source_path  TEXT NOT NULL,
        heading_path TEXT NOT NULL,
        anchor       TEXT NOT NULL,                   -- 글이 바뀌어도 같은 자리를 가리키는 안정 키
        ordinal      INT NOT NULL,                    -- 파일 안 순서
        last_verified DATE,
        source_url   TEXT[],
        text         TEXT NOT NULL,                   -- 사용자에게 보여 줄 글
        embed_text   TEXT NOT NULL,                   -- 임베딩과 BM25에 넣은 글 (맥락 머리말 포함)
        embedding    extensions.vector(384) NOT NULL,
        PRIMARY KEY (index_id, chunk_id)
      );

      -- 다른 표와 같이 RLS를 켠다(정책 없음 = 브라우저의 공개 키로는 못 읽는다). 서버는 DB 소유자 연결로 읽는다 (005)
      ALTER TABLE rag_index ENABLE ROW LEVEL SECURITY;
      ALTER TABLE rag_chunks ENABLE ROW LEVEL SECURITY;
    EXCEPTION WHEN feature_not_supported OR undefined_file OR insufficient_privilege THEN
      -- "pgvector를 쓸 수 없다"에 해당하는 오류만 잡는다: 확장 파일이 설치되지 않음(0A000 feature_not_supported,
      -- 일반 postgres:16에서 CREATE EXTENSION vector가 실제로 내는 코드, 58P01 undefined_file도 같은 뜻),
      -- 확장·스키마를 만들 권한 없음(42501). 그 밖의 오류(SQL 오타, 이름 충돌 등)는 숨기지 않고 마이그레이션을 실패시킨다.
      -- 잡은 경우 이 블록 안의 변경은 모두 되돌려지고 마이그레이션은 계속 진행한다 (위 설명)
      RAISE WARNING '033: pgvector 표를 만들지 못했다 (%, 코드 %). 파일 색인으로 동작한다', SQLERRM, SQLSTATE;
    END;
  ELSE
    RAISE NOTICE '033: pgvector 없음. rag_* 표를 만들지 않는다 (파일 색인으로 동작)';
  END IF;
END
$rag033$;
