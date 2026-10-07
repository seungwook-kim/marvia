-- 선박 자세히 보기 하루 횟수(024 vessel_detail_usage) 보관 기한 (10-08 점검: 024에는 삭제 규칙이 없어 날짜 줄이 계속 쌓였다).
--
-- 이 표는 "오늘(한국 날짜) 몇 번 썼나"만 쓴다(services/vessel_detail.take_daily_slot). 지난 날짜 줄은 판단에 쓰이지 않는다.
-- 그래도 하루만 남기지 않고 7일을 두는 것은, 운영자가 최근 한 주 사용량을 볼 수 있게 하려는 것이다. 개인정보는 없다(user_key는 사용자 번호).
-- 규칙은 이 함수 하나에만 둔다. Supabase는 pg_cron이 하루 한 번, pg_cron이 없는 로컬·채점용 DB는 워커가 실행마다 부른다
-- (backend/app/db purge_vessel_detail_usage, worker/run.py). 여러 번 불러도 결과는 같다.

CREATE OR REPLACE FUNCTION purge_old_vessel_detail_usage(keep_days integer DEFAULT 7)
RETURNS integer
LANGUAGE sql
AS $$
  WITH gone AS (
    DELETE FROM vessel_detail_usage
    WHERE day < (now() AT TIME ZONE 'Asia/Seoul')::date - keep_days
    RETURNING 1
  )
  SELECT count(*)::integer FROM gone;
$$;

-- 021과 같은 방식으로 예약한다. 감시 위치 03:40에 이어 03:50 KST(18:50 UTC).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'cron') THEN
    PERFORM cron.schedule('marvia-purge-vessel-detail-usage', '50 18 * * *', 'SELECT purge_old_vessel_detail_usage()');
  END IF;
END
$$;
