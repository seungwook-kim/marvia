---
# 계약 C6. 2층 등급 판정 표의 재료. 코드(services/verdict.py)가 이 프론트매터를 읽는다 (SDD 3.6, 09-30 D7, 10-01).
# 키 하나는 skills/*/rules.md 중 한 파일에만 둔다. backend/app/services/rules.py가 모두 합쳐 읽고, 같은 키가 두 파일에 있으면 실패한다.
margin_act_days: 2
act_signals: [anchor_wait, customs_hold, sales_date_exceeded]
gray_signals: [anchor_wait, customs_hold]
ignore_signals: [navstat_mismatch]
ignore_dest_values: [broken, empty]
# 판매 예정일 문장 (검사 규칙 V15, services/validate.py, 10-06 점검).
# days_margin이 음수면 "판매 예정일보다 늦게 도착"이라는 뜻이지 판매 예정일이 지났다는 뜻이 아니다. 오늘 날짜를 LLM에 주지 않으므로
# "판매 예정일이 지난 상태입니다"는 근거가 없다. 한 문장에 sale_date_words와 sale_passed_words가 같이 있으면 위반
sale_date_words: [판매 예정일, 판매예정일, 판매일, 필요 입고일]
sale_passed_words: [지났, 지난 상태, 지나 버, 지나버, 넘겼, 넘긴 상태, 이미 지, 경과]
# 판매 예정일을 받지 않았을 때(근거 카드에 sale_due_date가 없을 때) 쓰면 안 되는 비교 낱말 (10-06 점검: 날짜 없이 "예정보다 빠른 도착")
plan_compare_words: [예정보다 빠, 예정보다 늦, 예정보다 일찍, 계획보다 빠, 계획보다 늦, 계획보다 일찍]
# 판매 예정일이 없을 때 sale_date_words와 한 문장에 있으면 판매 예정일과 비교한 것으로 보는 낱말.
# "판매 예정일을 알려 주시면 ..." 같은 안내 문장은 걸리지 않게 비교 표현만 둔다
sale_compare_words: [보다, 앞서]
# 받아 주는 판매 예정일 범위 (10-08 QA B2, routers/agent.sale_due_out_of_range). 오늘 기준 이보다 과거거나 먼 미래면
# 입력 실수(1900-01-01 등)로 보고 판정하지 않고 다시 묻는다(insufficient, missing_inputs [sale_due_date]). 저장된 값도 덮지 않는다.
# 조금 지난 날짜는 받는다: 판매 예정일보다 늦게 도착하는 화물(sales_date_exceeded)을 알리는 것이 이 서비스의 일이다
sale_due_past_days: 30
sale_due_future_days: 730
# 판정 재시도 상한과 시간 예산 (SDD 5.7, 3.6, docs/specs/v2-langgraph.md Q5). 코드(routers/agent.py JUDGE_ATTEMPTS, JUDGE_BUDGET_SEC)가 읽는다.
# judge_max_attempts: 판정 LLM을 부르는 최대 횟수. 처음 + 위반 내용 첨부 + "JSON만, 규칙 준수" 강화(마지막 시도 직전)다. 후속 질문 재시도도 같은 값을 쓴다
# judge_budget_sec: 다음 시도는 "지금까지 쓴 시간 + 방금 한 번에 걸린 시간"이 이 초 안에 들어올 때만 시작한다.
#   화면이 90초에 요청을 포기하므로(web/lib/api.ts AGENT_TIMEOUT_MS) 판정 앞 단계에 쓸 몫을 남긴 값이다
judge_max_attempts: 3
judge_budget_sec: 60
# 판정 그래프(AGENT_GRAPH=on)의 노드 실행 횟수 안전망 recursion_limit (v2-langgraph.md 2절, EC-12). 정상 경로는 State의 시도 횟수가 먼저 막는다.
# 센 방법: 노드 한 번 실행이 한 단계다. 가장 긴 경로는 후속 질문인데 지난 결과가 없어 판정을 다시 돌리고(세 번 위반해 폴백),
# 이어서 후속 답도 세 번 위반해 실패 문구로 끝나는 길이고 25단계다 (H5 뒤 다시 셌다)
#   guard_input, load_memory, load_saved, load_previous, fetch_customs, scope_gate, identify, collect, compute_signals, table_verdict (10)
#   + judge, validate를 judge_max_attempts번(3) = 6 + fallback_partial (1) + decorate (1) = 18
#   + guard_follow_up (1) + answer_follow_up, validate_follow_up를 judge_max_attempts번(3) = 6 = 25
# 판정만 하는 가장 긴 길(후속 질문 없음)은 17단계다. langgraph는 25단계 경로를 한도 26부터 끝까지 돌린다(한도가 단계 수와 같으면 걸린다).
# test_graph_judge_loop.py가 17을, test_graph_follow_up.py가 25를 확인한다.
# 40은 25보다 크고 H7(되묻기 멈춤 노드 ask_prepare, ask_wait 추가)에서 늘어날 단계를 받을 여유(15단계)가 있어 값을 그대로 둔다.
# 노드를 더하면 이 숫자를 다시 센다. 정상 경로가 이 값에 닿으면 안 되고, 닿으면 루프가 샌 것이다
graph_recursion_limit: 40
# 되묻기에서 멈춘 판정 그래프(체크포인트)의 보관 규칙 (v2-langgraph.md 3, 4절, EC-02, Q4). 코드(graph/facade.py, worker/run.py)가 읽는다.
# graph_pause_ttl_hours: 멈춘 지 이 시간이 지나면 이어 가지 않고 스레드를 지운 뒤 처음부터 다시 돈다. 통관 단계는 시간이 지나면 바뀌어 있을 수 있어서 짧게 둔다
# graph_checkpoint_retention_days: 어느 요청도 오지 않아 코드가 못 지운 멈춘 스레드를 워커가 이 일수 뒤 지운다(DB 함수 purge_old_graph_checkpoints, migrations/032). TTL을 넘긴 고아 스레드의 안전망
graph_pause_ttl_hours: 2
graph_checkpoint_retention_days: 7
---

# 2층 등급 판정 표

코드(services/verdict.py)가 아래 키를 읽어 등급을 정한다. 등급을 정하는 곳은 여기와 그 코드뿐이다. SKILL.md는 등급을 정하지 않고 설명하는 법만 적는다.
신호(triggered·clear·unknown)는 ais-reliability/rules.md "1층 신호 7종" 표대로 코드가 먼저 정한다.

| 등급 | 조건 | 키 |
|---|---|---|
| act | act_signals 중 하나라도 켜짐. 또는 다른 신호가 켜졌는데 days_margin이 margin_act_days 미만 | act_signals, margin_act_days |
| watch | act_signals는 꺼져 있고 다른 신호가 하나라도 켜짐. 또는 켜진 신호가 없어도 days_margin이 margin_act_days 미만 | margin_act_days |
| fine | 켜진 신호 없음. unknown은 켜진 것으로 세지 않는다 | |

- 세지 않는 신호: ignore_signals는 켜져도 등급에 넣지 않는다. dest_changed는 값이 깨진 목적지(broken)·빈 목적지(empty)면 세지 않는다(ignore_dest_values). 배의 실제 상태가 아니라 AIS 입력 오류일 수 있어서다
- 회색 지대: gray_signals 중 unknown이 있으면 표가 등급을 낮게 낼 수 있다. 이때만 LLM이 표 등급 이상으로 올릴 수 있다 (낮추기 불가)
- 판매 예정일이 없으면 days_margin이 없어 여유 조건은 보지 않는다. 배 상태만으로 정한다
