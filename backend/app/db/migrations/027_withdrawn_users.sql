-- 탈퇴한 사용자 차단 표 (10-08 QA A3).
--
-- 탈퇴(services/account.withdraw)는 기록과 Supabase 계정을 지우지만, 이미 받은 로그인 출입증은 남은 기한(최대 1시간) 동안
-- 서명 확인을 통과한다. 그 사이 쓰기 요청이 오면 지운 표에 새 줄이 생기고, 하루 토큰 사용량(token_usage)도 지워진 뒤라
-- 한도가 처음부터 다시 찬다. 그래서 탈퇴한 user_id를 여기 남기고 auth.current_user가 403으로 막는다.
--
-- 남기는 것은 user_id(Supabase가 발급한 무작위 번호)와 시각뿐이다. 이메일·이름은 넣지 않는다.
-- 출입증 기한(1시간)보다 훨씬 길게 30일만 둔다. 규칙은 아래 함수 하나에만 있고, 탈퇴할 때마다 같은 트랜잭션에서 부르고
-- pg_cron이 있으면 하루 한 번도 부른다. 여러 번 불러도 결과는 같다 (IF NOT EXISTS, OR REPLACE).

CREATE TABLE IF NOT EXISTS withdrawn_users (
  user_id      TEXT PRIMARY KEY,
  withdrawn_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 005와 같다. 공개 키(Data API)로는 읽지 못하게 RLS를 켜고 규칙은 두지 않는다
ALTER TABLE withdrawn_users ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION purge_old_withdrawn_users(keep_days integer DEFAULT 30)
RETURNS integer
LANGUAGE sql
AS $$
  WITH gone AS (
    DELETE FROM withdrawn_users
    WHERE withdrawn_at < now() - make_interval(days => keep_days)
    RETURNING 1
  )
  SELECT count(*)::integer FROM gone;
$$;

-- 021·025와 같은 방식으로 예약한다. 04:00 KST(19:00 UTC)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'cron') THEN
    PERFORM cron.schedule('marvia-purge-withdrawn-users', '0 19 * * *', 'SELECT purge_old_withdrawn_users()');
  END IF;
END
$$;
