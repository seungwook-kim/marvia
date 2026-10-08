# Marvia 개발 작업 리스트

PRD를 작업 단위로 나눈 개발 계획과 그 결과 기록이다. 작업 ID(A1~G5)는 코드 주석과 SDD가 가리키므로 바꾸지 않는다. 묶음 사이는 **계약(인터페이스)**으로만 이어지고, 계약을 먼저 고정해 서로 기다리지 않고 개발했다.

상태: ✅ 완료 · 🔁 바뀜(계획과 다른 모습으로 구현, 비고에 무엇이 바뀌었는지) · 🚫 안 함 · 🔶 일부 (남은 것은 비고) · ⬜ 남음.

상태는 코드·폴더·마이그레이션·`docs/release_manifest.json`을 읽어 적었다. 완료는 완료 기준을 테스트나 실행으로 확인한 것만 적고, 코드는 있으나 실행 확인이 안 된 것은 비고에 "확인하지 못했다"로 적는다.

## 진행률

| 묶음 | 완료 · 바뀜 | 일부 | 남음 · 안 함 | 진행률 |
| --- | --- | --- | --- | --- |
| A. 백엔드 골격과 배포 | 12 | 1 | 1 | 89% |
| B. MCP 서버와 데이터 소스 | 9 | 1 | 0 | 95% |
| C. 판정 로직, LLM 호출, Skill | 12 | 0 | 0 | 100% |
| D. 관측, 수집 워커, 평가 | 10 | 3 | 1 | 82% |
| E. 프론트엔드 | 10 | 1 | 0 | 95% |
| F. 마감 작업 | 1 | 1 | 4 | 25% |
| G. 확장 (최소 범위) | 4 | 0 | 0 | 100% |

> 진행률 = (완료 + 바뀜 + 일부 × 0.5) ÷ 항목 수. 아래 각 표의 상태 열을 센 값이다 (H, I는 제외).

## 0. 먼저 고정할 계약

병행 개발의 전제다. 바꾸면 평가셋, fixture, 프론트가 같이 깨지므로 SDD 3절과 함께 고친다.

| 계약 | 내용 | 파일 |
| --- | --- | --- |
| C1 응답 스키마 | status(ok · partial · insufficient · failed · unsupported), verdict, decided_by, vessel_match, reasons, conflicts, actions, days_margin, evidence, bl_type_used, anchorage, eta_change, customs_timing 등 | `backend/app/schemas/agent.py` |
| C2 요청 스키마 | bl_no 필수. bl_type(MASTER/HOUSE, 비우면 서버가 저장된 구분 → 하우스 → 마스터 순으로 조회) · sale_due_date · forwarder_eta · voyage_no · resolved_mmsi · mrn · follow_up_question · session_id 선택 | `backend/app/schemas/agent.py` |
| C3 1층 신호 출력 | 신호 7종 각각 triggered / clear / unknown + 값 + 출처 | `backend/app/schemas/signals.py` |
| C4 MCP 도구 4종 입출력 | 모든 출력에 source, fetched_at 포함. 실패는 {error: {code, message, retryable}} | `mcp_server/schemas.py` |
| C5 UNI-PASS 정제 결과 | ship_name, ship_nat, voyage, entry_date, stage_history, shed_name, goods_name. LLM에 안 넘기는 필드(중량·세관명·화주) 제외. 품명은 저장하고 채팅 에이전트에만 준다 | `backend/app/schemas/customs.py` |
| C6 Skill 임계값 | 오래된 위치 6시간 · ETA 상충 12시간 · 정지 속도 0.5노트 · 정박 24시간(`ais-reliability/rules.md`) · 부산 반경 30해리(`port-busan/rules.md busan_radius_nm`) · 여유 부족 2일(`cargo-verdict/rules.md margin_act_days`). 키 하나는 한 파일에만 있고 `services/rules.py`가 합쳐 읽는다 | `skills/*/rules.md` 프론트매터 |
| C7 평가 레코드 | 입력(C2) + 가공 fixtures + 정답 status · verdict · vessel_match · 물을 것 + 라벨러 + 사유 | `evals/schema.py` |
| C8 환경변수 | .env.example 키 목록 | `.env.example` |

레포 구조 (실제):

```
marvia/
  backend/app/    FastAPI (라우터, 서비스, 스키마, 클라이언트, 관측, 마이그레이션 001~026, 프롬프트 사본)
  mcp_server/     FastMCP 도구 4종 (/mcp는 API 서버 안의 경로)
  skills/         Skill 문서 5종 (ais-reliability, customs-stages, port-busan, cargo-verdict, referrals) + rules.md
  worker/         수집 워커(collector.py), 지켜보는 화물 재판정(watch.py), aisstream 시험 수집(aisstream.py), run.py
  relay/          해수부 API 릴레이 (Cloud Run 서울)
  evals/          평가셋 30건 + 서류 12건, 베이스라인, 실행기, 채점, 재생 fixture
  scripts/        손으로 돌리는 확인 스크립트 (MCP 확인, 데모 파일 생성, 실패 훈련 등)
  web/            Next.js 16. 경로 /, /today, /watch, /cargo, /history, /settings, /admin, /metrics, /about
  docker-compose.yml  Dockerfile  Dockerfile.railway  .env.example  README.md  EVAL_REPORT.md
```

## A. 백엔드 골격과 배포

| # | 작업 | 완료 기준 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| A1 | FastAPI 프로젝트 생성, `GET /health`, `/docs` | 로컬에서 200 | ✅ | |
| A2 | Pydantic 스키마 C1·C2·C5 코드화 | import 되고 예시 JSON이 검증 통과 | ✅ | |
| A3 | Dockerfile, docker-compose.yml (api + worker + postgres) | `docker compose up` 한 줄로 전체 기동 | ✅ | MCP는 api 안의 `/mcp`. 서류 OCR 패키지는 api 이미지에만. 배포용 `Dockerfile.railway` 별도 |
| A4 | 배포 (서비스 1개, `/mcp` 포함), URL 확보 | 외부에서 /health 200, `/mcp` 토큰 없이 401 | 🔁 바뀜 | 계획은 Cloud Run, 실제는 Railway(API `marvia-api`, 화면 `marvia-web`). 배포 서버는 실제 모드(`REPLAY_MODE=false`). 주소는 `docs/release_manifest.json`. Cloud Run은 해수부 릴레이에만 쓴다. 외부 /health 200과 /mcp 401은 이 문서를 쓰며 직접 확인하지는 않았다 |
| A5 | 구조화 로그 미들웨어 (요청 ID, 모델, 토큰, 지연) | 로그 1건에 4필드 | ✅ | 실패는 `failure stage=… error=…` 한 줄 |
| A6 | `POST /api/agent` 라우터. 서비스 계층 호출만, LLM SDK import 금지 | 라우터에 openai/anthropic import 없음 | ✅ | |
| A7 | B/L 형식 검증, 범위 밖 입력 거절 | 잘못된 형식 400, 정상 통과. 부산 외 항만은 unsupported | ✅ | |
| A8 | UNI-PASS 클라이언트, 응답 → C5 정제, 필드 마스킹 | 실제 B/L 1건 조회 성공 또는 모의 응답으로 파싱 통과 | ✅ | 실제 B/L 1건 MRN 식별까지 확인 |
| A9 | 실패 경로: 외부 API 타임아웃 · 5xx 재시도 2회 + 백오프, partial 응답 | 모의 지연으로 재시도 로그 확인 | ✅ | 실패 훈련 스크립트 `scripts/failure_drill.py` |
| A10 | CORS, /mcp 토큰 인증, 비밀값 서버 환경변수만 | 브라우저에서 키 노출 없음, 토큰 없으면 401 | ✅ | 배포 때 `MCP_TOKEN`을 새로 만들어 넣는다 |
| A11 | README 실행 절차 (clone → .env → compose up, 10분) | 다른 사람이 처음 클론해서 재현 | ✅ | README에 키 없는 데모 13건(라이브 10 + 평가셋 3) 절차. 로컬과 Docker로 확인. 다른 사람 재현은 F5 |
| A12 | `GET /api/history` 라우터. query_log를 session_id로 읽기만 | agent 2회 호출 뒤 같은 세션 조회 시 2건, 다른 세션 0건 | ✅ | 로그인했으면 사용자 기준 |
| A13 | 배포 환경 trace: 배포 주소로 조회 1건이 Langfuse에 남는지 | 배포 URL 요청의 trace 1건 확인 | ⬜ 남음 | 배포 주소 trace 기록은 레포에서 확인하지 못했다 (EVAL_REPORT 8절) |
| A14 | release manifest(배포한 이미지 · 커밋 · 프롬프트 버전 · 모델 기록)와 rollback | 한 번 되돌려 보고 /health 200 | 🔶 일부 | `docs/release_manifest.json`(r4)에 커밋·프롬프트 버전·모델·rollback 절차가 있다. 프롬프트 라벨을 되돌린 사례는 EVAL_REPORT 2절. 코드 배포를 되돌려 본 기록은 없다 |

## B. MCP 서버와 데이터 소스

| # | 작업 | 완료 기준 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| B1 | FastMCP 서버, Streamable HTTP `/mcp` | MCP 클라이언트로 tool 목록 조회 | ✅ | API 서버 안의 경로. 서비스 자신도 `/mcp`를 MCP 프로토콜로 부른다(`services/mcp_client.py`). `MCP_URL`이 없으면 같은 프로세스에 메모리로 붙는다 |
| B2 | ShipFinder 클라이언트: VesselSearch, VesselPositionSingle. status 코드 매핑(0, 3, 9, 13, 21, 23, 29) | 실제 선박 1척 MMSI 조회 성공(MMSI는 로컬에만) | ✅ | VesselFlagInfo(국적)와 한도 소진 status 38도 처리 |
| B3 | 도구 `get_vessel_candidates`: 후보 목록 + 유사도 + last_time. 같은 IMO면 최신 MMSI만 | 동명 선박 검색 시 후보 복수 반환 | ✅ | |
| B4 | 도구 `get_live_position`: 원본 값과 last_time 반환 (freshness 판정은 C-4가 함) | 실제 선박 1척 조회 | ✅ | |
| B5 | 해수부 클라이언트: 관제정보(`CntlVssl2`), 선박운항정보(`VsslEtrynd5`). 호출부호 · 선명으로 조회 | 부산항 입항 예정 목록 1회 수신 | ✅ | `MOF_BASE_URL`이 있으면 서울 릴레이 경유. 연결 실패는 한 번 바로 재시도, 두 번 실패하면 15초 차단 |
| B6 | 도구 `get_port_schedule`: 입항 예정 · 실적, 접안 시설 | 실제 선박 1척 매칭 | ✅ | 직전 기항지(`previous_port`) 칸 포함 |
| B7 | searoute 도구 `estimate_distance` | 부산까지 해리 반환 | ✅ | |
| B8 | ShipFinder 캐시 (최근 값 + 캐시 시각) | 빈도 초과 모의 시 캐시 응답 | ✅ | 23 · 29는 6시간 안 캐시, 타임아웃은 오래된 캐시도 쓰고 partial (평가 F02) |
| B9 | tool_description: 언제 쓰는 도구인지 한 문장 | 리뷰 통과 | ✅ | 설명에 판단 · 지시 표현 금지 테스트 |
| B10 | /mcp 연결 스크린샷, tool 목록 · 호출 결과 캡처 | 캡처 확보 | 🔶 일부 | `docs/evidence/B10-mcp-연결-도구목록-호출.png`(langchain-mcp-adapters 클라이언트, 로컬). 배포 주소로 찍은 캡처는 없다 |

## C. 판정 로직, LLM 호출, Skill

| # | 작업 | 완료 기준 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| C-1 | `skills/ais-reliability/SKILL.md` + `rules.md`. 신호 7종, 상충 우선순위, 되묻기 조건 | 도메인 검수 | ✅ | 함정 경험이 있는 팀원이 검수. 판정 표는 C-12에서 `cargo-verdict`로 옮김 |
| C-2 | `skills/customs-stages/SKILL.md`: 실제 처리구분 이름, 의미, 다음 단계, 할 일, 관세사 확인 조건 | 단계 전체 확보 | ✅ | 실제 처리구분 10단계 |
| C-3 | `skills/port-busan/SKILL.md`: 부두 · 정박지 개요, 하역 장소 문장 규칙 | 도메인 검수 | ✅ | |
| C-4 | 1층 신호 판정 코드 (`services/signals.py`): rules.md 읽어 7종 판정 → C3 | 단위 테스트 통과 | ✅ | 정박 시간은 위치 이력으로 잰다 |
| C-5 | 선박 식별: 후보 1척이면 코드 확정, 복수면 판단, uncertain이면 판정 차단 | 동명 선박(B01) 케이스가 uncertain | ✅ | MRN 호출부호 · 세관 국적과 다르면 1척이어도 되묻기. 여럿이면 서류 대조(`identify.match_by_papers`) 먼저, LLM은 항차·포워더 ETA가 있을 때만 |
| C-6 | `services/llm.py`: LLM 호출 단일 지점, Skill 주입, 모델 스위치 | 라우터 밖에서 LLM SDK import 없음 | ✅ | OpenRouter와 Vertex(`vertex/`) 두 길. 판정 흐름은 코드가 도구를 순서대로 부르고, 후속 질문과 채팅 에이전트는 LLM이 읽기 전용 MCP 도구를 골라 쓴다. 키 없는 데모는 녹화 답 재생 |
| C-7 | 출력 검증 + 3단계 재시도 + 폴백 | 위반 응답 주입 시 재시도 트레이스 | ✅ | 검증 규칙 V1~V15(V8 등급 검사, V9·V10 주입 방어, V11 통관 진행 평가 금지 등). 폴백은 판정 표 등급 + 고정 문구로 partial, failed는 통관 조회 실패만 |
| C-8 | days_margin 코드 계산, LLM 덮어쓰기 금지 울타리 | LLM이 다른 값 내도 코드 값 유지 | ✅ | 판매일 없으면 null |
| C-9 | 후속 질의: 근거 범위 밖 질문 거절 | "비트코인 시세" 거절 | ✅ | 후속 답도 V1 · V7 · V9 · V10 · V11 · V15 검사 |
| C-10 | 신뢰 경계: 도구 응답 · dest 문자열 속 지시문 무시 | dest에 지시문 넣은 케이스 통과 | ✅ | 평가 주입 3건 통과. 사용자 입력은 `services/guard.py`가 LLM 앞에서 막는다 |
| C-11 | 프롬프트 작성, Langfuse에 등록 (연동 코드는 D4) | Langfuse에 prompt 존재 | 🔁 바뀜 | 계획은 5종, 실제는 10종: chat-agent · chat-route · chat-title · doc-extract · follow-up · judge · judge-eval · pick-vessel · port-match는 Langfuse, memory-write는 레포 파일(`backend/app/prompts/`). 버전은 `docs/prompt_versions.md`, `docs/release_manifest.json` |
| C-12 | 등급은 판정 표 코드로 (`services/verdict.py`), 회색 지대만 LLM이 올리기 | 판정 표와 다른 등급이면 V8, 회색 지대는 표 이상만 | ✅ | 재료는 `skills/cargo-verdict/rules.md`. 평가 30건 판정 정확도 1.0 |

## D. 관측, 수집 워커, 평가

| # | 작업 | 완료 기준 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| D1 | 수집 워커: 부산 입항 예정 컨테이너선 최대 12척 선정 → 6시간마다 위치 저장 (ShipFinder 하루 50회 안) | 기록 쌓임 | 🔶 | 코드 완료. 스케줄러 기본 꺼짐(`WORKER_SCHEDULE=off`)이라 주기 가동은 안 함. 지켜보는 화물 배와 배만 지켜보는 배 위치는 `watch_positions`에 쌓는다 |
| D2 | 입항 실적 수집: 관제정보 · 선박운항정보 저장 | 입항 완료 선박에 실제 시각 기록 | 🔶 | 코드 완료. D1과 같이 주기 가동 안 함 |
| D3 | Langfuse 트레이스: llm.py 호출마다 generation, session_id, 재시도 · 모델 · 토큰 · 비용 | 대시보드에 trace 1건 | ✅ | 서버 종료 때 남은 트레이스 전송. 도착 집계는 `docs/evidence/langfuse_summary.md` |
| D4 | Langfuse Prompt fetch 코드(`get_prompt`), 버전 → 트레이스 링크 | trace에 prompt 버전 표시 | ✅ | |
| D5 | 평가셋 30건: 정상 8, 경계 14(판정 9 · 되묻기 5), 실패 8. 가상 B/L · 통관 · 판매 예정일 | Langfuse Dataset 업로드 | ✅ | `marvia-golden-30`. 평가 실행이 문항별로 연결된다 |
| D6 | 라벨링: 문항마다 판정자 · 사유. 사후 입항 시각으로 예측 적중 별도 | 30건 라벨 완료 | 🔶 | 30건 라벨 완료(판정자 · 사유). 라벨 날짜 칸은 비어 있고 사후 입항 적중은 재지 않았다 |
| D7 | 베이스라인 (a) 규칙만 | 30건 실행 결과 | ✅ | `evals/results/a_20261001_1631.json` exact 27/30, 판정 정확도 1.0, 선박 오확정 2(B01·B12, 동명 선박 수정 전). 수정 뒤 (a)는 다시 재지 않았다 |
| D8 | 평가 실행기: (a)(b)(c) × 30건, 축 7개 채점 | 결과 표 자동 생성 | ✅ | 결과는 실행마다 새 파일 (`evals/results/<구성>_<시각>.json`) |
| D9 | LLM-as-judge: 통관 해설 채점 모델 ≠ 생성 모델 | 표본 5건 사람 대조 | ✅ | 사람 대조 5건: 채점 모델과 4/5 일치(`evals/results/grader_check_20261001.md`). 채점 모델은 실행마다 `EVAL_GRADER_MODEL`(gpt-4o-mini 생성 실행은 gemini-2.5-flash, 현재 설정은 claude-haiku-4.5). `judge.py`는 생성 모델과 같으면 채점을 건너뛴다 |
| D10 | 개선 실험: 한 가지만 바꿔 재측정 | KEEP/DISCARD 기록 | ✅ | EVAL_REPORT 4절에 KEEP/DISCARD 6건 |
| D11 | EVAL_REPORT.md: 평가셋 구성, 베이스라인 비교, 전후 지표, 실패 사례, 판정 방식 · 한계 | 게이트 판정 표시 | ✅ | 1~9절. 요청당 비용 상한은 "미정"(7절) |
| D12 | 운영 지표: p50/p95, 요청당 비용, 계약 준수율 | README 지표 표 | ✅ | README 7절, EVAL_REPORT 6절 |
| D13 | 계약 준수율 100건 이상 측정 | 100건 이상 실행 결과 파일 | ✅ | EVAL_REPORT 9-2: (b) 5회 LLM 출력 112개 중 위반 2개(98.2%). 같은 30종류를 5번 반복한 출력 단위 값 |
| D14 | 재조정 가설 한 줄 (배포 환경 trace를 보고 무엇을 바꾸면 나아질지) | EVAL_REPORT에 한 줄 | ⬜ 남음 | 배포 환경 trace(A13)가 없어 쓰지 못했다. EVAL_REPORT 8절 약점 목록이 가까운 내용 |

## E. 프론트엔드

처음 계획은 입력 폼 + 결과 화면이었고 실제는 채팅 중심 화면이다(`web/`, Next.js 16).

| # | 작업 | 완료 기준 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| E1 | Next.js 프로젝트, 배포 | URL 확보 | 🔁 바뀜 | 계획은 Vercel, 실제는 Railway `marvia-web` |
| E2 | 입력: B/L(필수), M/H 구분, 판매 예정일(선택), 서류 사진 | 형식 오류 인라인 표시 | 🔁 바뀜 | 입력 폼이 아니라 채팅 입력창(`ChatBar`, `lib/composer.ts`). M/H 구분은 묻지 않고 서버가 둘 다 조회. 서류 사진·PDF는 입력창에 첨부(클립·붙여넣기·끌어놓기, `lib/docAttach.ts`)하고, B/L을 확실히 읽으면 확인 화면 없이 바로 조회 |
| E3 | 조회 진행 표시 (식별 → 위치 → 판정 단계) | 로딩 상태 3단계 | ✅ | `lib/progress.ts`, `docs/specs/chat-progress.md` |
| E4 | 결과 화면: 판정 배지, 할 일, 지도(stale은 흐리게), 화물 카드, 통관 타임라인, 하역 장소 | C1 응답 전 필드 렌더 | ✅ | `VerdictCard` · `MapPanel`/`VesselMap` · `CustomsTimeline` · `VesselCard` · `EtaCompare`. 등급 화면 이름은 순항 · 주의 · 긴급 |
| E5 | 근거 카드: evidence 목록 (출처, 값, 조회 시각) | 클릭 시 원본 값 | ✅ | `EvidenceCards`, 채팅 답 속 `[E#]` 근거 팝업(`EvidencePopover`) |
| E6 | 되묻기 화면: 후보 + 근거, missing_inputs 입력 → 재요청 | 동명 선박(B01) 케이스 흐름 | ✅ | `CandidatePicker`, 채팅 후보 칩(`ChatCandidateChips`) |
| E7 | 실패 화면: partial · failed 원인, 마지막 확인 시각, 다음 행동 | 타임아웃 모의 시 표시 | ✅ | `FailureBanner` |
| E8 | 채팅 후속 질의 UI | 근거 범위 답변 표시 | 🔁 바뀜 | 별도 UI가 아니라 채팅 화면 자체가 질의 창구(`ChatBar`, `/api/chat/stream`) |
| E9 | 재생 모드 (데모 B/L, "재생" 배지) | API 키 없이도 시연 가능 | 🔶 일부 | 백엔드 재생(키 없는 데모 13건)과 화면 체험 모드("체험 · 예시 데이터" 칩), 로그인 없는 "데모 둘러보기"는 있다. 백엔드 재생 모드를 알리는 "재생" 배지는 코드에서 찾지 못했다 |
| E10 | 소개 페이지 (한 줄 정의, 지표 표, 아키텍처 그림) | 공개 | ✅ | `web/app/about`, 로그인 전 첫 화면(`IntroContent`) |
| E11 | 조회 이력 화면 (A12 응답 렌더) | 이력 목록에서 클릭 시 해당 판정으로 | ✅ | `/history` · `HistoryTable` |

## F. 마감 작업

| # | 작업 | 상태 | 비고 |
| --- | --- | --- | --- |
| F1 | 데모 영상: 정상 · 실패 · 되묻기 | ⬜ | 레포에 없다. 대신 로그인 없는 "데모 둘러보기"가 있다 |
| F2 | Langfuse 스크린샷 3장 이상 (트레이스 상세 1장 포함) | 🔶 | 레포에는 집계 파일(`docs/evidence/langfuse_summary.md`)만 있다. 찍을 화면 목록은 EVAL_REPORT 1절 |
| F3 | README 최종: 한 줄 정의, 지표 표, compose up, 아키텍처 | ✅ | |
| F4 | .env.example 점검, 민감값 커밋 검사 | ✅ | `secret-scan.yml`(gitleaks)이 push마다 검사한다. `.env.example`은 코드가 읽는 키 이름과 맞췄다 |
| F5 | 다른 컴퓨터에서 10분 재현 테스트 | ⬜ | |
| F6 | 제출 시점 태그 | 🚫 | 개인 포트폴리오 레포로 옮기며 해당 없음 |

## G. 확장 (최소 범위)

| # | 작업 | 최소 범위 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| G1 | 서류 사진 1차 | 사진 → OCR(B/L 가림) → LLM이 칸 채움 | ✅ | 가공 서류 확인, 옆으로 누운 사진도 읽음. PDF도 받는다(앞 2쪽, 글자층 먼저, 열 수 없으면 400 `invalid_pdf`). 출항일(etd)과 B/L 조건·화물 인도 연락처 칸도 읽고, 로그인 사용자는 검사 통과한 조건을 화물 표에 저장(026) |
| G3 | 로그인 · 사용자 ID | 간단 로그인, 조회를 사용자에 묶기 | ✅ | Supabase 구글 로그인 토큰 확인 · 로그인 필수 · B/L 암호화 · 삭제 3종 · 트레이스 user_id. 화면은 `LoginScreen`, `lib/auth.ts` |
| G4 | 메모리 | 지난 조회로 "이 B/L 다시 볼까요?" 제안 | 🔁 바뀜 | 화물 표와 메모장(`/api/memory`, 마이그레이션 014·015), 채팅의 "지켜볼까요?" 제안, 답에 붙는 "지난 조회에서는 …" 한 줄로 구현 |
| G5 | 등록 · 감시 · 알림 | 등록한 B/L 목록 + 주기 재판정 · 변화 감지 + 다음 접속 때 바뀐 판정 표시 | 🔁 바뀜 | `/api/watch`와 `worker/watch.py`(재판정은 `WATCH_ENABLED` 기본 꺼짐), 알림은 카카오톡 "나에게 보내기"(등급 변화, ETA 12시간 이상 변화). 바뀐 판정은 `/today` "판정 변동" 카드와 대화 첫 말풍선으로 보여 준다 |

하지 않는 것과 확장 가능성은 PRD 4절을 따른다: 어려운 판정을 대형 모델로 넘기기 · 판정 파인튜닝 · 서류 사진 2차(OCR이 못 읽은 사진을 모델이 직접 읽기) · 이메일 · 푸시 알림 · 부산 외 · 해외 항만 · 대형 화주.

## H. 처음 목록에 없던 것

작업 ID 없이 만들어진 기능이다. 근거는 `backend/app/routers/`, `db/migrations/`, `web/`, `docs/specs/`. 완료 기준을 따로 세우지 않았다.

| 기능 | 코드 | 비고 |
| --- | --- | --- |
| 대화 목록 (새 대화 · 지난 대화 열기 · 삭제, 첨부 턴 저장) | `routers/conversations.py`, 마이그레이션 018_conversations | 로그인 + `PII_ENCRYPTION_KEY`일 때만 저장 |
| 채팅 에이전트 · 스트림 | `routers/chat.py`, `services/chat.py` | 범위 밖 주제·주입 모양은 LLM 앞에서 고정 답 |
| 답 속 근거 번호 `[E#]` | `services/citations.py`, `EvidencePopover`, `ConversationEvidence` | 번호는 코드가 검사 |
| 카카오톡 알림 연결 | `routers/kakao.py`, `clients/kakao.py`, 마이그레이션 016_kakao_links, 022 | 연결 · 시험 발송 · 채팅에서 "지금 보내 줘" |
| 내 화물 현황표 · 엑셀 받기 | `routers/cargo_sheet.py`, 마이그레이션 019 | 신용장별 묶음 |
| 품명(goods_name) · 선명 저장 | 마이그레이션 020, 021_cargo_vessel_name | 품명은 채팅 에이전트에만 준다 |
| 컨테이너 번호 가리기와 번호로 화물 찾기 | `services/chat.py` 등 | |
| B/L 구분 자동 두 번 조회 | `routers/agent.py` `_fetch_customs`, 마이그레이션 024(`bl_type` NULL 허용) | 찾은 구분을 `bl_type_used`로 알림 |
| 지난 사실: 묘박 대기 · ETA 변화 · 통관 소요 | `services/port_facts.py`, `routers/agent.py` | 판정 신호가 아니라 코드가 붙이는 사실 |
| 오늘의 화물 `/today` | `web/components/TodayBoard.tsx` | 기존 API만 읽는다 |
| 데모 둘러보기 | `web/lib/tour.ts`, `TourGate`, `TourBanner` | 로그인·서버 호출 없는 가상 시뮬레이터 |
| 화면 색 설정 | `web/lib/theme.ts`, `ThemeSync` | system / light / dark / comfort / custom |
| 배만 지켜보기 | `services/vessel_watch.py`, `/api/watch/vessels`, 마이그레이션 023 | 판정 · 알림 없음 |
| 선박 자세히 보기 하루 횟수 상한 | `routers/chat.py`, 마이그레이션 024 · 025 | `vessel_detail_daily_limit` |
| 부산항 입항 추세 카드 | `routers/port.py`, `clients/portwatch.py`, `PortTrafficCard` | IMF PortWatch, 판정과 무관 |
| aisstream.io 시험 수집 | `worker/aisstream.py` | 기본 꺼짐, 판정에 안 씀 |
| 운영자 · 고객 관리 · 지표 | `routers/admin.py`, 마이그레이션 009 ~ 013 | 운영자 이메일 암호화 |
| 하루 토큰 한도 | `services/quota.py`, 마이그레이션 016_token_usage | 1명당 `DAILY_TOKENS_PER_USER` |
| 안내처(referrals) | `skills/referrals/`, `services/referrals.py` | |
| 해수부 릴레이 | `relay/` | 코드 있음, 실서버 연결은 레포로 확인 불가 |
| 서버 마이그레이션 자동 적용 | `backend/app/main.py` → `db.apply_migrations` | 001 ~ 026 |

## I. 실사용 점검 (항목 32~58)

작업 ID 없이 실사용 점검 번호로 받은 일이다. 프롬프트는 Langfuse chat-agent 13 · follow-up 9(`docs/release_manifest.json` r4, `docs/prompt_versions.md`). 완료 기준 열의 확인은 그때 실행한 값이다: pytest backend/tests mcp_server/tests 2145 passed, web tsc 오류 0, verify-contract PASS, e2e 데모 56 PASS (라이브는 SKIP). 실제 모델에서 `[E#]` 근거 번호가 만들어지는지는 확인하지 못했다.

| 항목 | 작업 | 완료 기준 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| 34·35·38·52·54 | 채팅 답: 도구만 돈 뒤 답이 비거나 검사(V7·V9·V10·V13)에 걸리면 한 번 고쳐 묻고, 그래도 안 되면 도구 결과를 코드로 문장화(lookup_fallback). 입항 검색 답에 가까운 순 5척 이름과 도착 예정일. 해요체. 같은 B/L 결과 없음이 반복되면 포워더·선사 확인 안내 | pytest 통과 | ✅ | 실제 모델 답은 확인 전 |
| 33 | 해수부 연결 실패는 한 번 다시 시도, 차단 15초, "잠시 후 다시 시도" 안내 | pytest 통과 | 🔶 일부 | 릴레이 쪽 배포 환경변수와 실서버 호출은 확인하지 못했다 |
| 32·39~47·49·53·55·56 | 화면 정리: 서류 첨부를 입력칸 클립 버튼과 끌어다 놓기로(파일 칩, 바로 조회), 미니 지도 캡션, 말풍선 · 대화 열 여백, 대화 제목 표시, 입력칸 안내 문구, 대화 목록 누르면 대화 화면으로, 바뀐 게 없으면 저장 비활성, 결과 없음 제목 "조회 결과를 찾지 못했어요" | tsc 통과, e2e 데모 PASS | ✅ | |
| 48 | 설정 탭 4개(회원 정보 / 기억한 화물 / 설정 / 계정과 이력), 조회 이력은 사이드바로 | verify.mjs 설정 탭 4개, web tsc 오류 0 | ✅ | "설정" 탭에 화면 색 · 카카오 연결 · 판단 기준(읽기 전용) |
| 36·37 | 채팅에 화면의 배(on_screen)를 함께 보내기, 되묻기 후보를 채팅 칩으로 고르기 | pytest · tsc · e2e 통과 | ✅ | 실제 모델 확인 전 |
| 57 | B/L 없이 배만 지켜보기: 마이그레이션 023_vessel_watch, `/api/watch/vessels` (GET/POST/DELETE), 워커 위치 수집 포함. 채팅 후보 칩과 여러 화물 비교표의 배 옆 지켜보기 버튼. 입항 목록의 배는 호출부호로 선박 번호를 찾아 정확히 1척일 때만 | pytest · tsc · e2e 통과 | ✅ | 배만 지켜보는 배의 판정과 알림은 범위 밖(PRD 4절) |
| 58 | 답 문장 끝 근거 번호 [E#]: 번호 검사(`citations.py`), 응답의 evidence_items · citations, 대화 저장 포함, 답 속 근거 팝업과 대화별 근거 모음, 체험용 모의 근거 | pytest · tsc · verify-contract · e2e 통과. 채팅 계약은 더하기만 | 🔶 일부 | 실제 모델이 [E#]를 만드는지 확인 전 |
| 50 | (보류) | | 보류 | |
| 51 | 서류 확인 뒤 입력칸에 붙여 넣던 것을 바로 조회(채팅 전송)로 바꿈 | | 🔶 일부 | 서류에서 읽은 항차·포워더 입항일을 채팅 요청에 싣는 것은 하지 않았다. 되묻기 화면 미리 채움으로 충분하다고 봤다 |

## 책임 경계 (겹치기 쉬운 곳)

| 경계 | 어느 작업 | 아닌 작업 |
| --- | --- | --- |
| freshness 판정 | C-4 signals.py | B4는 last_time만 반환 |
| 등급 | C-12 verdict.py 판정 표 | LLM은 설명 · 회색 지대 올리기만 |
| 프롬프트 | C-11 내용 · 등록 | D4 fetch 코드 · 버전 링크 |
| Langfuse 트레이스 | D3 | C-6은 llm.py 본체만 |
| UNI-PASS | A8 클라이언트 · 정제 | C는 정제 결과(C5)만 소비 |
| days_margin | C-8 코드 계산 | LLM 출력값은 덮어씀 |

## 의존 관계 (무엇이 무엇을 기다리나)

```
계약 C1~C8 ─┬─> A6 라우터 ──> E4 결과 화면
            ├─> B3~B7 도구 ──> C-5, C-6 판정 ──> D8 평가 실행
            ├─> C-4 신호 판정 ──> C-12 판정 표 · D7 베이스라인 (a)
            └─> D1 워커 ──> D2 실적 ──> G5 등록 · 감시
G3 로그인 ──> G4 메모리 · G5 등록 · 감시
A4 배포(Railway) ──> A13 배포 trace · B10 MCP 캡처 · E1 화면 배포 연결
```

## AI 코딩 도구에 넘길 때

각 항목마다 이렇게 넘기면 SDD 흐름에 맞는다.

1. 계약 파일(C1~C8) 경로와 PRD 해당 장 번호를 함께 준다
2. "완료 기준" 열을 그대로 테스트 조건으로 쓴다
3. 금지 사항을 명시한다: langgraph 설치 금지, 벡터 검색 · 청킹 금지, 라우터에서 LLM SDK import 금지, 프롬프트 문자열 코드 하드코딩 금지(Langfuse Prompt)
4. 외부 API 응답은 실제와 형식만 같은 가공 JSON을 fixture로 준다. 실제 응답 원본은 약관상 레포에 올리지 않는다 (SDD 7절)
