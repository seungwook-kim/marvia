# Spec: 세관에 아직 없는 화물은 MRN으로 배 상태까지 (MRN 대체 조회)

SDD에 없는 기능이라 Spec을 먼저 둔다.

## Why

- 실제 도착 통지서 한 건(입항 이틀 전)에서 UNI-PASS 화물통관진행은 H·M B/L, 2025·2026년, 화물관리번호 모두 0건이었다
- 같은 통지서의 MRN은 해수부 입항 신고에 있었다(입항 예정 시각과 부두·선석까지). 배는 오는데 적하목록이 아직 세관에 없는 시점이다
- 대체 조회가 없으면 UNI-PASS에서 못 찾는 순간 멈춘다. 사용자는 배가 오고 있는지조차 못 본다

## Goal

- UNI-PASS에 B/L이 없고(마스터·하우스 둘 다 조회해도 없음) 요청에 MRN이 있으면, 해수부 입항 신고(MRN)로 배를 찾고 AIS 위치·남은 거리·입항 예정을 붙여 status=partial로 돌려준다
- 판정(verdict)은 하지 않는다. 통관 정보가 없어서다. 답에 "통관 정보는 아직 세관에 없음"을 밝힌다
- **Out of Scope**: MRN만으로 첫 조회(B/L 없이), 화물관리번호 조회, 적하목록 API, 판정

## What

1. 입력: AgentRequest에 선택 칸 mrn (11자, 가공 예 26TEST0001I). 자연어 입력은 코드가 문장에서 MRN 모양을 뽑는다(LLM에 안 보냄). 서류 사진은 "MRN" 이름표 옆 값을 코드가 뽑아 문장에 넣어 준다
2. UNI-PASS 결과가 없으면(두 구분 모두 없음) mrn으로 해수부 입항 신고를 찾는다(오늘 -3일 ~ +14일)
3. 찾으면 호출부호로 ShipFinder 후보 → 위치 → 남은 거리(기존 MCP 도구). 도구가 실패해도 해수부 정보만으로 partial
4. 못 찾으면 원래 응답(`failed` + `NO_CARGO` "번호를 다시 확인") 그대로

| # | 상황 | 처리 |
|---|---|---|
| EC-01 | MRN 모양이 아님 | 422 (입력 검증) / 자연어는 MRN으로 안 뽑힘 |
| EC-02 | 해수부에도 없음 | 원래 응답(failed, NO_CARGO) 그대로 |
| EC-03 | ShipFinder 실패·한도 | 해수부 입항 예정·접안 시설만으로 partial, evidence에 남김 |
| EC-04 | UNI-PASS 자체 장애(시간 초과·키 오류) | 대체하지 않는다. 세관 장애는 그대로 알린다 (`is_not_found`로 "없음"과 가른다) |

## How

- schemas/agent.py(C2): mrn 선택 칸, 형식 ^[0-9]{2}[A-Z0-9]{8}[IE]$ (계약 변경)
- schemas/document.py: DocumentResponse.mrn (선택, 코드가 뽑음)
- services/mrn_fallback.py: 대체 응답 조립. 위치 신선도는 rules.md position_stale_hours, trusted_eta는 정하지 않는다(통관·판정 없음)
- routers/agent.py: _assess에서 UNI-PASS가 "없음"(NO_CARGO)이고 mrn이 있으면 대체를 시도
- services/chat.py: MRN 정규식, ChatContext.mrn
- 테스트: backend/tests/test_mrn_fallback.py

## 구현 상태

- 위 What·How는 모두 코드에 있다: `AgentRequest.mrn`(schemas/agent.py, 형식 `^[0-9]{2}[A-Z0-9]{8}[IE]$`), `DocumentResponse.mrn`(코드가 "MRN" 이름표 옆에서 뽑음, routers/document.py), `services/chat.extract_mrn`(자연어에서 뽑고 그 자리를 [MRN]으로 바꿔 LLM에는 안 보냄), `services/mrn_fallback.assess`, `routers/agent.py`의 "없음" 분기(`is_not_found(failure) and request.mrn`)
- 조회 기간은 입항 신고를 오늘 -3일 ~ +14일로 찾는다 (`mrn_fallback.LOOKBACK_DAYS`, `LOOKAHEAD_DAYS`)
- 이 Spec 밖에서 MRN은 한 곳에 더 쓰인다: UNI-PASS에서 B/L을 찾은 정상 흐름에서도 세관 기록의 MRN으로 해수부 입항 신고를 찾아 호출부호를 얻고 배를 확정한다 (`services/identify.call_sign_from_mrn`, `routers/agent.py _identify`). 이때 확정 방법은 `user_cargo.vessel_source = 'mrn'`으로 남는다 (docs/ERD.md)
