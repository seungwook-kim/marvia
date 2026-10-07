-- 10-08 세 가지를 한 번에 고친다. 여러 번 실행해도 결과는 같다 (IF NOT EXISTS, DROP NOT NULL은 다시 해도 같다).
--
-- 1. user_cargo.last_eta_at: 지난 조회의 신뢰 ETA 시각.
--    last_eta(014)는 한국 날짜뿐이라 도착 예정 변화(services/port_facts.eta_change)가 하루 단위로만 잡혔다.
--    services/cargo.upsert_assessment가 last_eta와 같이 적고, 둘 다 있으면 시간으로 센다. 옛 줄은 비어 있어 날짜로 센다.
-- 2. user_cargo.bl_type NULL 허용: 사용자가 B/L 구분을 모르는 경우가 많아(10-08 결정) 판정 흐름이 하우스·마스터를 차례로 조회한다.
--    서류 사진만 넣고 아직 조회 전인 줄은 구분을 모르므로 비워 둔다. 조회가 끝나면 UNI-PASS에서 찾은 구분으로 채운다.
-- 3. vessel_detail_usage: 선박 자세히 보기의 사용자별 하루 횟수 (services/vessel_detail.take_daily_slot).
--    전에는 프로세스 메모리라 서버를 다시 띄우면 0부터였다. user_key는 로그인 사용자 ID 또는 'anonymous', day는 한국 날짜.

ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS last_eta_at TIMESTAMPTZ;

ALTER TABLE user_cargo ALTER COLUMN bl_type DROP NOT NULL;

CREATE TABLE IF NOT EXISTS vessel_detail_usage (
  user_key TEXT NOT NULL,
  day      DATE NOT NULL,
  count    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_key, day)
);

-- 005와 같다. 공개 키(Data API)로는 읽지 못하게 RLS를 켜고 규칙은 두지 않는다
ALTER TABLE vessel_detail_usage ENABLE ROW LEVEL SECURITY;
