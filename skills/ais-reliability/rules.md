---
# 계약 C6. AIS 1층 신호 기준값의 원본. 코드는 이 프론트매터를 읽는다 (SDD 3.6, CLAUDE.md "임계값을 코드에 적지 않는다").
# 키 하나는 skills/*/rules.md 중 한 파일에만 둔다. backend/app/services/rules.py가 모두 합쳐 읽고, 같은 키가 두 파일에 있으면 실패한다 (10-01).
position_stale_hours: 6
eta_conflict_hours: 12
anchor_speed_kn: 0.5
anchor_wait_hours: 24
priority: [mof_actual, mof_planned, ais_eta, ais_dest]
# 항해상태 불일치 속도 기준 (services/signals.py가 읽는다)
navstat_moored_max_kn: 2.0
navstat_underway_min_kn: 0.3
# ShipFinder 응답 캐시 TTL(시간). 빈도 초과(23·29)일 때 이 시간 안의 마지막 성공 값을 쓴다 (SDD 5.2, clients/cache.py가 읽는다)
api_cache_ttl_hours: 6
# ShipFinder 하루 50회 한도 아끼기 (10-08 결정). clients/shipfinder.py, services/vessel_pick.py, services/vessel_detail.py가 읽는다
# 같은 MMSI 위치를 이 시간(분) 안에 성공으로 받았으면 다시 부르지 않고 그 값을 쓴다. 후보 상세 조회 직후 같은 배 위치 조회가 겹친다
position_reuse_minutes: 10
# 선박 등록 국가(VesselFlagInfo)는 거의 바뀌지 않는다. 이 날수 안에 받은 값이 있으면 부르지 않는다
flag_cache_ttl_days: 30
# 호출부호로 배를 고를 때 최근 이 날수 안에 신호를 보낸 배만 지금 그 호출부호를 쓰는 배로 본다 (09-28 실측: 한 호출부호에 IMO 3척)
vessel_recent_days: 7
# 선박 자세히 보기(/api/vessels/detail)를 캐시 밖에서 새로 조회할 수 있는 사용자당 하루(한국 날짜) 횟수
vessel_detail_daily_limit: 10
# 저장된 지난 신뢰 ETA(user_cargo.last_eta)와 이번 신뢰 ETA가 이 시간 이상 달라지면 "도착 예정이 바뀜"으로 알린다 (services/port_facts.py eta_change, 10-08).
# 판정 신호가 아니다. 등급에는 쓰지 않고 응답 eta_change 칸·지켜보기 알림 글에만 쓴다
eta_change_alert_hours: 12
---

# 1층 신호 7종

코드(services/signals.py)가 아래 키를 읽어 신호마다 triggered·clear·unknown을 정한다. 키가 다른 Skill 폴더에 있으면 괄호에 적었다.

| id | 켜지는 조건 | 임계값 키 |
|---|---|---|
| position_stale | 마지막 수신이 임계 시간보다 오래됨 | position_stale_hours |
| eta_conflict | AIS ETA와 해수부 입항예정 차이가 임계 시간 초과 | eta_conflict_hours |
| anchor_wait | 부산 반경 안, 임계 속도 이하로 임계 시간 이상 정지 | anchor_speed_kn, anchor_wait_hours, busan_radius_nm·busan_ref (port-busan/rules.md) |
| dest_changed | 목적지 코드가 비었거나 부산 항구 코드가 아님 | busan_port_code (port-busan/rules.md) |
| navstat_mismatch | 항해상태와 속도가 안 맞음: 정박·계류인데 기준보다 빠름, 또는 항해 중인데 기준보다 느림 | navstat_moored_max_kn, navstat_underway_min_kn |
| customs_hold | 통관 단계 이름에 기준 낱말이 들어감 (검사 지정·보류 등) | customs_hold_words (customs-stages/rules.md) |
| sales_date_exceeded | days_margin < 0. 배가 정상이어도 판매 예정일을 넘김 | 없음 (0이 기준) |

등급(fine·watch·act)은 이 신호들을 재료로 cargo-verdict/rules.md 키를 읽어 코드가 정한다.

# 캐시 TTL (api_cache_ttl_hours)

ShipFinder가 빈도 초과(status 23·29)로 답하지 않으면 이 시간 안에 받아 둔 마지막 성공 값을 대신 쓴다 (SDD 5.2, clients/cache.py).
시간 초과와 하루 한도 소진(status 38)은 TTL이 지난 값도 쓴다. 오래된 위치는 position_stale이 드러낸다 (clients/shipfinder.py).
판정 신호가 아니라 원천 값을 언제까지 믿고 다시 쓸지의 기준이라 position_stale_hours와 같은 6시간이지만 키를 따로 둔다.

# 도착 예정 변화 알림 (eta_change_alert_hours)

같은 사용자가 같은 화물을 다시 조회하거나 지켜보기가 다시 판정할 때, 지난번 신뢰 ETA(user_cargo.last_eta, 한국 날짜)와 이번 신뢰 ETA의
한국 날짜 차이가 이 시간 이상이면 응답 eta_change 칸과 지켜보기 알림 글에 "도착 예정이 바뀜"을 적는다 (services/port_facts.py).
저장 값이 날짜뿐이라 실제로는 하루(24시간) 단위로 잡힌다. 판정 신호가 아니라 등급을 바꾸지 않는다.

# 상충 시 우선순위 (priority)

priority 순서대로 첫 유효값을 신뢰 ETA(trusted_eta)로 쓴다: 해수부 실적 > 해수부 입항 예정 > AIS ETA > AIS 목적지 문자열.
이 순서를 고르는 이유는 SKILL.md "출처 우선순위" 절에 있다. 수집 데이터로 검증해 틀리면 이 순서를 고친다 (PRD 6.4).

# ShipFinder 호출 아끼기 (position_reuse_minutes, flag_cache_ttl_days, vessel_recent_days, vessel_detail_daily_limit)

ShipFinder 무료 키는 하루 50회이고, 한도를 넘기면 그날 프로세스 전체가 ShipFinder를 못 쓴다 (clients/shipfinder.py quota_blocked).
- position_reuse_minutes: 한 요청 안에서 후보 상세 조회가 받은 위치를 바로 뒤 위치 조회가 다시 받지 않게 한다. 10분 안의 값은 실시간 값으로 본다
- flag_cache_ttl_days: 등록 국가는 배가 팔리거나 국적을 바꿀 때만 바뀐다. 실패 대비 캐시(api_cache_ttl_hours)와 달리 먼저 캐시를 본다
- vessel_recent_days: 호출부호는 다른 배에 다시 배정된다. 최근 신호를 보낸 배가 지금 그 호출부호를 쓰는 배다. 그래도 둘 이상 남으면 고르지 않는다
- vessel_detail_daily_limit: 선박 자세히 보기 한 번이 ShipFinder를 2~3회 쓴다. 캐시에서 답하는 조회는 세지 않는다
