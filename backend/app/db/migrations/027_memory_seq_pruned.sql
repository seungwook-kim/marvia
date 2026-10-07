-- 메모장(user_memory) 번호 단조 증가와 자동 정리 기록 (10-08 QA F4·F11, services/memory.py save).
--
-- next_seq: 이 사용자에게 다음에 줄 메모 번호. 지운 번호를 다시 쓰지 않으려는 것이다.
--   예전에는 "남은 번호 중 가장 큰 것 + 1"이라, 맨 끝 줄을 지우고 새로 더하면 같은 번호가 돌아왔다.
--   그러면 화면이 들고 있던 옛 번호로 지우기를 누르거나 기억 뽑기가 옛 번호를 고칠 때 엉뚱한 새 줄이 바뀐다.
--   0은 "아직 기록 없음"이라 코드가 지금 있는 번호의 최댓값 + 1과 비교해 큰 쪽을 쓴다 (옛 줄 호환).
-- pruned_count / pruned_at: 상한(3,000자)을 넘어 코드(fit)가 마지막으로 자동 정리한 항목 수와 그 시각.
--   정리는 응답 뒤 기억 쓰기에서 조용히 일어나서, 설정 "내 기억" 화면이 이 값으로 사실을 알린다.
ALTER TABLE user_memory ADD COLUMN IF NOT EXISTS next_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_memory ADD COLUMN IF NOT EXISTS pruned_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_memory ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;
