-- 선박 자세히 보기에서 최근에 본 배 기록 (10-08 QA E2-9). 여러 번 실행해도 결과는 같다 (IF NOT EXISTS).
--
-- 자세히 보기 결과 캐시(10분)는 프로세스 메모리라 서버를 다시 띄우면 비고, 방금 본 배를 다시 눌러도 하루 횟수를 한 번 더 쓰거나
-- 횟수를 다 썼으면 429가 났다. 사용자가 오늘 줄(024 vessel_detail_usage)에 "최근에 본 배 열쇠 → 본 시각"을 남겨 두고,
-- 같은 배를 캐시 수명(services/vessel_detail.py CACHE_SEC) 안에 다시 보면 횟수를 쓰지 않는다.
-- - recent: {"<캐시 열쇠>": "<본 시각 ISO>"}. 열쇠는 호출부호(공백 없이 대문자) 또는 "name:<선명>"이다. 개인정보는 없다
-- - 쓸 때마다 수명이 지난 열쇠는 지운다(services/vessel_detail._remember). 날짜 줄과 함께 025의 정리 함수가 지운다
ALTER TABLE vessel_detail_usage ADD COLUMN IF NOT EXISTS recent JSONB NOT NULL DEFAULT '{}'::jsonb;
