-- 서류 사진에서 읽은 B/L 조건의 출처 표시 (10-08 사용자 결정).
--
-- 서류 확인 화면을 없애고, 로그인 사용자가 채팅 입력창에 B/L·A/N 사진을 붙이면 검사를 통과한 B/L 조건만
-- 확인 없이 화물 표(user_cargo)에 저장한다 (services/doc_facts.save_from_document). 사람이 확인하지 않은 값이므로
-- 답과 화면이 "서류 사진에서 읽은 값(확인 전)"이라고 밝힐 수 있게 언제·어느 서류에서 읽었는지 남긴다.
-- - doc_facts_at: 마지막으로 서류 사진에서 조건을 저장한 시각. 비어 있으면 예전 확인 화면에서 사람이 확인한 값이거나 조건이 없다
-- - doc_facts_source: 'B/L' / 'A/N' / 'document'(서류 종류를 모름). 값 자체는 015의 original_bl 등 칸에 그대로 있다
-- 여러 번 실행해도 결과는 같다 (IF NOT EXISTS).

ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS doc_facts_at TIMESTAMPTZ;
ALTER TABLE user_cargo ADD COLUMN IF NOT EXISTS doc_facts_source TEXT
  CHECK (doc_facts_source IN ('B/L', 'A/N', 'document'));
