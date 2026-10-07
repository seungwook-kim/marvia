---
# 계약 C6. 부산항 기준값의 원본. 코드는 이 프론트매터를 읽는다 (SDD 3.6, CLAUDE.md "임계값을 코드에 적지 않는다").
# 키 하나는 skills/*/rules.md 중 한 파일에만 둔다. backend/app/services/rules.py가 모두 합쳐 읽고, 같은 키가 두 파일에 있으면 실패한다 (10-01).
busan_ref: {lat: 35.10, lng: 129.05}
busan_radius_nm: 30
busan_port_code: KRPUS
# 해수부 입항 일정 조회 기간·선명 비교 기준 (mcp_server/tools/port_schedule.py가 읽는다, 10-06 점검)
port_schedule_lookback_days: 3
port_schedule_lookahead_days: 7
port_schedule_name_match_ratio: 0.85
# 해수부 관제 이벤트 이름 중 묘박(닻 내림)·양묘(닻 올림)·접안을 가르는 목록 (services/port_facts.py anchorage가 읽는다, 10-08)
anchor_event_kinds: [투묘]
weigh_anchor_event_kinds: [양묘]
berth_event_kinds: [접안]
# 부산항 입항 추세 (IMF PortWatch, services/port_traffic.py, 10-08). 판정에는 안 쓰는 참고 맥락이다
portwatch_port_id: port1065
port_traffic_recent_days: 7
port_traffic_base_days: 28
port_traffic_busy_ratio: 1.15
port_traffic_quiet_ratio: 0.85
port_traffic_cache_hours: 12
port_traffic_retry_minutes: 30
# aisstream.io 시험 수집 (worker/aisstream.py, 10-08). 약관·부산 수신 범위 미확인이라 AISSTREAM_KEY가 있을 때만 돈다
aisstream_bbox_busan: [[34.70, 128.40], [35.35, 129.60]]
aisstream_bbox_wide: [[20.0, 110.0], [46.0, 150.0]]
aisstream_max_mmsi: 200
aisstream_refresh_minutes: 10
# 지켜보기 워커 운영값 (services/watch.py, worker/collector.py, worker/aisstream.py, 10-08 QA). 판정에는 안 쓴다
watch_pause_after_failures: 3
watch_notify_retry_hours: 24
position_future_tolerance_minutes: 60
---

# 부산항 기준값

| 키 | 뜻 | 읽는 곳 |
|---|---|---|
| busan_ref | 부산항 기준점(위도·경도). 남은 거리와 "부산 앞" 여부를 이 점에서 잰다 | services/signals.py (anchor_wait), mcp_server/tools/distance.py |
| busan_radius_nm | 기준점에서 이 해리 안이면 부산 앞으로 본다 | services/signals.py (anchor_wait) |
| busan_port_code | 부산항 UN/LOCODE. AIS 목적지 코드(dest_changed)와 UNI-PASS 양륙항(지원 범위 확인)을 이 값과 비교한다 | services/signals.py, routers/agent.py |
| port_schedule_lookback_days | 해수부 관제 실적(실제 입항·접안)을 오늘부터 며칠 전까지 볼지. get_port_schedule 기본값 | mcp_server/tools/port_schedule.py |
| port_schedule_lookahead_days | 해수부 입항 신고(입항 예정)를 오늘부터 며칠 뒤까지 볼지. 부산은 신고가 1주 앞까지 나온다 (09-23 실측). get_port_schedule 기본값 | mcp_server/tools/port_schedule.py |
| anchor_event_kinds | 해수부 관제 이벤트(cntrlNm) 중 "닻을 내렸다"(묘박지 대기 시작)로 보는 이름 | services/port_facts.py (anchorage) |
| weigh_anchor_event_kinds | 해수부 관제 이벤트 중 "닻을 올렸다"(대기 끝)로 보는 이름 | services/port_facts.py (anchorage) |
| berth_event_kinds | 해수부 관제 이벤트 중 "부두에 댔다"로 보는 이름. 투묘 뒤에 있으면 대기가 끝난 것으로 본다 | services/port_facts.py (anchorage) |
| port_schedule_name_match_ratio | 선명으로만 찾을 때 이 비율 이상 닮은 이름을 같은 배로 본다 (difflib 유사도, 정확·부분 일치가 없을 때만) | mcp_server/tools/port_schedule.py |
| portwatch_port_id | IMF PortWatch에서 부산항의 항구 ID | backend/app/clients/portwatch.py |
| port_traffic_recent_days | 입항 추세에서 "최근" 평균을 낼 날 수 | services/port_traffic.py |
| port_traffic_base_days | 입항 추세의 비교 기준 평균을 낼 날 수 (최근 날을 포함한다) | services/port_traffic.py |
| port_traffic_busy_ratio | 최근 평균 / 기준 평균이 이 값 이상이면 "평소보다 많음"(busy). 판정에는 안 쓴다 | services/port_traffic.py |
| port_traffic_quiet_ratio | 같은 비율이 이 값 이하이면 "평소보다 적음"(quiet). 판정에는 안 쓴다 | services/port_traffic.py |
| port_traffic_cache_hours | PortWatch 결과를 프로세스 안에 이 시간 동안 들고 있는다. 자료가 주 단위로 갱신돼 12시간이면 충분하다 | services/port_traffic.py |
| port_traffic_retry_minutes | PortWatch 호출이 실패하면 이 시간 동안은 다시 부르지 않고 마지막 성공값을 준다 | services/port_traffic.py |
| aisstream_bbox_busan | aisstream 구독 범위(부산 접근 수역). [[남서 위도, 경도], [북동 위도, 경도]] | worker/aisstream.py |
| aisstream_bbox_wide | aisstream 넓은 구독 범위(동아시아). AISSTREAM_BBOX=wide일 때 쓴다 | worker/aisstream.py |
| aisstream_max_mmsi | aisstream 구독에 넣는 지켜보는 배 MMSI 상한 | worker/aisstream.py |
| aisstream_refresh_minutes | 지켜보는 배 목록을 다시 읽어 구독을 갱신하는 간격(분) | worker/aisstream.py |
| watch_pause_after_failures | 지켜보는 화물의 자동 재판정이 이 횟수만큼 잇달아 실패(예외·판정 없음)하면 자동 확인을 멈춘다. 사용자가 다시 조회하거나 지켜보기를 다시 켜면 풀린다 | services/watch.py |
| watch_notify_retry_hours | 카카오톡 알림이 일시 오류(네트워크 등)로 못 나가면 다음 실행에서 다시 보낸다. 처음 실패한 뒤 이 시간이 지나면 더 보내지 않는다(낡은 알림) | services/watch.py |
| position_future_tolerance_minutes | 위치 보고 시각이 지금보다 이 분 넘게 미래면 시계가 틀린 보고로 보고 버린다. 안 버리면 그 점이 영원히 "현재 위치"가 된다 | services/watch.py (usable_position_time), worker/collector.py, worker/aisstream.py |
