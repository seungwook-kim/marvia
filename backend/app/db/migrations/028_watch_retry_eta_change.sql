-- 지켜보기 워커 보완 (10-08 QA E1). 화물 표(user_cargo)에 칸을 더하고, 선박 자세히 보기 횟수 정리 함수를 바로잡는다.
--
-- 1) 재판정 실패 기록 (E1-1). 실패하거나 판정이 비어 돌아온 화물이 last_checked_at을 못 바꿔 매번 맨 앞에 서서
--    다른 화물을 굶겼다(services/watch._due_rows는 오래된 순). 이제 시도할 때마다 watch_attempt_at을 남기고 그 순서로 고른다.
--    - watch_attempt_at: 워커가 마지막으로 다시 판정을 시도한 시각 (성공·실패 모두)
--    - watch_fail_count: 잇달아 실패한 횟수. 성공하면 0. rules.md watch_pause_after_failures 이상이면 자동 확인을 멈춘다
--      (사용자가 다시 조회해 last_checked_at이 watch_attempt_at보다 새로워지거나, 지켜보기를 다시 켜면 풀린다)
-- 2) 카카오톡 알림 보류 (E1-2). 네트워크 오류 같은 일시 실패면 보낼 글을 남겨 다음 실행에서 다시 보낸다.
--    - notify_pending: 다시 보낼 글. B/L 원문은 없다(이름표나 가린 번호만 쓴 글이다). 보내면 NULL
--    - notify_pending_at: 처음 못 보낸 시각. rules.md watch_notify_retry_hours가 지나면 더 보내지 않는다
-- 3) 도착 예정 변화 띠 (E1-7). 등급은 그대로이고 도착 예정만 바뀌면 띠에 안 남았다. 카카오 미연결 사용자도 다음 접속 때 보게 남긴다.
--    - eta_changed_from / eta_changed_to: 바뀌기 전·뒤 신뢰 ETA 날짜(한국 날짜). eta_change_hours: 늦어지면 양수, 당겨지면 음수
--    띠를 봤는지는 등급 변화와 같은 change_seen 하나로 센다 (017)
-- 4) 선박 자세히 보기 횟수 정리 (E1-11). 025 함수는 day < 오늘 - 7 을 지워 오늘을 빼고 7일, 모두 8일치를 남겼다.
--    "최근 7일"(오늘 포함)에 맞게 day <= 오늘 - keep_days 를 지운다. 025는 이미 적용된 파일이라 고치지 않고 여기서 함수를 바꾼다.
--    pg_cron 예약(025)은 함수 이름으로 부르므로 그대로 새 함수를 쓴다.
-- 여러 번 실행해도 결과는 같다 (IF NOT EXISTS, CREATE OR REPLACE).

ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS watch_attempt_at TIMESTAMPTZ;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS watch_fail_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS notify_pending TEXT;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS notify_pending_at TIMESTAMPTZ;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS eta_changed_from DATE;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS eta_changed_to DATE;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS eta_change_hours INTEGER;

CREATE OR REPLACE FUNCTION purge_old_vessel_detail_usage(keep_days integer DEFAULT 7)
RETURNS integer
LANGUAGE sql
AS $$
  WITH gone AS (
    DELETE FROM vessel_detail_usage
    WHERE day <= (now() AT TIME ZONE 'Asia/Seoul')::date - keep_days
    RETURNING 1
  )
  SELECT count(*)::integer FROM gone;
$$;
