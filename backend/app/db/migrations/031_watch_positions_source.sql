-- 감시·지도 위치(017 watch_positions)가 어디서 왔는지 (10-08 사용자 결정: /watch 지도에서 위치 출처를 가린다).
--
-- source: 이 점을 처음 넣은 길.
--   worker    정기 수집 (worker/collector.py collect_watch_positions)
--   aisstream 실시간 AIS 시험 수집 (worker/aisstream.py)
--   seed      지켜보기를 켤 때 이미 받아 둔 위치 (services/watch.seed_position). ShipFinder 캐시라 몇 시간 전 위치일 수 있다
--   lookup    화물 조회 때 받은 위치 (services/cargo._keep_position)
-- NULL은 이 칸이 생기기 전의 줄이다 (출처 모름). 화면은 출처 줄을 숨긴다.
-- UNIQUE (mmsi, ts)와 ON CONFLICT DO NOTHING은 그대로다: 같은 보고를 두 길이 받으면 먼저 넣은 쪽 출처가 남는다.
-- 여러 번 적용해도 결과가 같게 IF NOT EXISTS로 쓴다.
ALTER TABLE watch_positions ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE watch_positions DROP CONSTRAINT IF EXISTS watch_positions_source_check;
ALTER TABLE watch_positions ADD CONSTRAINT watch_positions_source_check
  CHECK (source IN ('worker', 'aisstream', 'seed', 'lookup'));
