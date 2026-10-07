# Marvia frontend

B/L 하나로 내 화물이 제때 오는지 판정하고, 왜 그런지 근거까지 보여주는 프론트엔드입니다.
**Next.js 16 App Router + TypeScript · 기본은 로컬 fixture 체험 모드, NEXT_PUBLIC_API_BASE를 주면 실제 백엔드에 연결.**

## 바로 실행

Node.js **20.9 이상**과 npm이 필요합니다. `web/` 폴더에서 실행하세요.

```bash
npm install
npm run dev
```

브라우저에서 <http://localhost:3000>을 엽니다. 체험 모드에는 키, `.env`, Docker, Python이 필요 없습니다. 첫 실행은 정상 예시 화물을 자동으로 보여줍니다. 글꼴은 로컬 번들이고, 지도 바탕은 OpenFreeMap 한 곳에서 받으며 못 받으면 미리 구운 타일(`public/basemap/`)과 Natural Earth 해안선으로 그립니다.

`npm run dev`와 `npm run build` 앞에 `scripts/copy-maplibre-worker.mjs`가 MapLibre 워커 파일을 `public/maplibre/`로 복사합니다.

프로덕션 확인:

```bash
npm run build
npm start
```

`npm ci`로 package-lock.json의 정확한 의존성을 재설치할 수도 있습니다.

## 화면

| 주소 | 화면 |
| --- | --- |
| `/today` | 오늘의 화물: 기억한 화물, 지켜보는 화물, 판정 변동, 카카오 연결 상태. 사이드바 첫 메뉴이자 데모 둘러보기 첫 화면 |
| `/` | 내 화물(채팅): 왼쪽 대화(첨부, 진행 단계, 미니 지도, 여러 B/L 비교표, 후보 칩, 근거 팝업), 오른쪽 결과(판정 카드, 지켜보기 토글, 배 카드, 지도, ETA 비교, 통관 타임라인, 근거, 후보 선택, 실패 배너) 또는 입항 예정 선박 목록·지도·자세히 보기 |
| `/watch` | 지켜보는 화물 지도·목록, 배만 지켜보는 줄, 부산항 입항 추세 카드(IMF PortWatch) |
| `/cargo` | 화물 현황표(신용장 묶음), 엑셀 받기 |
| `/history` | 조회 이력 필터, 판정 분포. 체험 모드에서는 저장된 fixture 판정 재생 |
| `/settings` | 탭 4개: 회원 정보 / 기억한 화물 / 설정(화면 색, 카카오톡 연결, 판단 기준 읽기 전용) / 계정과 이력 |
| `/settings/kakao` | 카카오 동의 뒤 돌아오는 주소. 처리 후 `/settings`로 보냄 |
| `/metrics` | 평가 지표(운영자 전용) |
| `/admin` | 운영자: 고객 관리, 운영자 등록·해지 |
| `/about` | 제품 소개(로그인 전 첫 화면과 같은 내용) |

## 예시와 상태 전환

- 체험 모드 개발 서버(`npm run dev`)는 URL로 상태별 화면을 엽니다: `/?fixture=ok`, `/?fixture=partial`, `/?fixture=insufficient`, `/?fixture=failed`, `/?fixture=unsupported`, `/?fixture=watch`, `/?fixture=act`.
- 프로덕션 빌드에서는 `?fixture=`가 무시됩니다. 채팅의 예시 문장과 아래 B/L은 계속 쓸 수 있습니다.
- 모든 예시는 MASTER입니다. 저장된 날짜·구분과 다르게 조회하면 조건 불일치 안내가 나옵니다. **fixture를 새로운 입력에 맞춰 임의로 재판정하지 않습니다.**

| 예시 B/L (가공) | 판매 예정일 | 응답 |
| --- | --- | --- |
| MRVDEMO1101 | 2026-10-25 | ok / fine |
| MRVDEMO1102 | 2026-10-25 | insufficient / null |
| MRVDEMO1103 | 2026-10-25 | partial / null / stale 위치 |
| MRVDEMO1104 | 2026-10-25 | failed / null |
| MRVDEMO1105 | 2026-10-25 | unsupported / null |
| MRVDEMO1106 | 2026-10-25 | ok / watch / stale 위치 |
| MRVDEMO1107 | 2026-10-20 | ok / act |

후보 선택: insufficient에서 첫 후보를 선택하거나 후보 미선택 상태로 항차·포워더 ETA를 입력하면 정상 예시로 이어집니다. 후속 질문은 질문에 관계없이 저장된 답변을 돌려주며 화면에 재생임을 표시합니다.

### 데모 둘러보기

로그인 화면의 **데모 둘러보기** 버튼은 `sessionStorage['marvia-tour']`를 켜고 `/today`로 갑니다. `TourGate`가 체험 모드로 바꾸고 로그인을 끈 뒤, `lib/tour.ts` 시뮬레이터가 `watch-map.json`의 배들을 10초마다 가상 6시간씩 움직이고 판정과 판정 변동을 가상으로 만듭니다. 서버·DB·LLM은 부르지 않습니다. 채팅은 예시 버튼만 쓰고 첨부는 막힙니다. 상단 `TourBanner`의 "데모 끝내기"로 나옵니다.

## 폴더 구조

```text
app/                   App Router 페이지(/today, /, /watch, /cargo, /history, /settings, /settings/kakao, /metrics, /admin, /about), layout, globals.css
app/styles/            화면 영역별 CSS. layout.tsx가 globals.css 뒤에 불러오고 enterprise.css가 공통 테마를 덮는다
components/            화면 컴포넌트 (SDD 5.13 + 채팅·지켜보기·현황표·설정·운영자·데모 둘러보기)
lib/types.ts           백엔드 Pydantic 직렬화 계약 그대로 옮긴 타입
lib/api.ts             요청 함수. 실제 모드와 체험 모드(fixture)를 이 파일 안에서만 가른다
lib/auth.ts            Supabase 브라우저 로그인
lib/format.ts          KST 날짜, B/L 마스킹, 표시용 라벨
lib/progress.ts        서버 처리 단계 이름·라벨과 SSE 해석 (docs/specs/chat-progress.md)
lib/composer.ts        다른 카드에서 AI 입력창으로 돌아가는 통로
lib/docAttach.ts       채팅 입력창의 서류 사진·PDF 첨부
lib/basemap.ts         지도 바탕(MapLibre 벡터 → 구운 타일 → 해안선)
lib/tour.ts            데모 둘러보기 시뮬레이터
lib/theme.ts           화면 색(system / light / dark / comfort / custom)
lib/changesTold.ts     판정 변동을 대화 첫 말풍선으로 한 번만 알리기
lib/evidenceTypes.ts, meCache.ts, watchVessel.ts   근거 표시 타입, /api/me 캐시, 배만 지켜보기
lib/fixtures/          7개 응답, 이력, 데모 입력, 설정, 지표, 지켜보기 지도, 서류, 기억, 운영자, 지형 JSON
public/basemap/        미리 구운 지도 타일 (scripts/bake-basemap.mjs)
scripts/               계약 검사, Playwright 화면·접근성 검사, 지도 타일 굽기, MapLibre 워커 복사
DESIGN.md              디자인 방향과 이유
NOTES.md               스키마·문서 차이, 알려진 한계, 미확인 값
THIRD_PARTY.md         지도·폰트·사진 출처와 라이선스
```

주요 컴포넌트: VerdictCard, VesselCard, EtaCompare, MapPanel, CustomsTimeline, CustomsDetailTable, EvidenceCards, CandidatePicker, FailureBanner, HistoryTable, MetricsPage, SettingsPage, TodayBoard, WatchMap, CargoSheet. 입력은 ChatBar(AI 입력창, POST /api/chat/stream)가 맡습니다. 서류 사진·PDF는 입력창에 첨부(클립·붙여넣기·끌어놓기)해서 보내고, 별도 확인 화면은 없습니다. 선박 자세히 보기는 선박 표의 줄 아래에 펼쳐지고, 대화별 근거 모음은 Drawer에서 엽니다. TopBar·AskPanel은 쓰이지 않는 옛 컴포넌트로 남아 있습니다.

## 백엔드 붙이기 (실제 모드)

`NEXT_PUBLIC_API_BASE`가 있으면 실제 모드, 없으면 위의 체험 모드입니다. 값은 빌드할 때 들어가므로 바꾸면 dev 서버를 다시 켭니다.

값은 `web/.env`에 둡니다. `cp .env.example .env` 뒤 채우면 아래처럼 명령에 붙이지 않고 `npm run dev`만 해도 됩니다. `web/.env`는 커밋하지 않습니다.

```bash
# 레포 루트: 키 없이 백엔드 데모 13건 (루트 README "키 없이 데모", REPLAY_MODE=true)
docker compose up -d --build
docker compose exec api python -m scripts.make_demo

# web/
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

- 예시 화물 메뉴는 백엔드 데모 B/L(라이브 시연 10건 DEMOQD2610001~DEMOQD2610010, 평가셋 3건 TESTBL01 정상·TESTBL04 되묻기·TESTBL26 판정 보류)을 채웁니다. 판매 예정일은 선택 입력이며, 예시마다 "오늘 + N일"로 채웁니다(`lib/api.ts liveExamples`).
- 백엔드 CORS `ALLOWED_ORIGINS`에 화면 주소(기본 http://localhost:3000)가 있어야 합니다.
- 세션 ID는 브라우저마다 만들어 localStorage에 둡니다. 로그인하지 않았을 때 이력이 이 값으로 묶입니다.
- 로그인은 아래 "로그인" 절을 봅니다.
- 이력 행의 상세 다시 보기는 실제 모드에서 동작하지 않습니다. GET /api/history는 요약만 주고, 새로 조회해서 과거 결과인 척하지 않습니다.

| 함수 | 실제 endpoint | 형식 |
| --- | --- | --- |
| `getAgentResponse(req)` | `POST /api/agent` | JSON AgentRequest → AgentResponse |
| 채팅 | `POST /api/chat/stream` (SSE, 404·405면 `POST /api/chat`) | ChatRequest → progress 소식 + ChatResponse |
| `getHistory()` | `GET /api/history?session_id=` | `{items: HistoryItem[], available: boolean}` |
| 서류 첨부 | `POST /api/document` | multipart → DocumentResponse |

그 밖에 `/api/me`, `/api/memory`, `/api/watch*`, `/api/cargo/*`, `/api/kakao/*`, `/api/conversations*`, `/api/port/busan/traffic`, `/api/admin/*`, `/api/metrics`를 부릅니다. 모두 `lib/api.ts`에 있습니다.

- C2에 존재하는 필드만 보냅니다. 빈 날짜·빈 문자열은 null로 바꿔 보냅니다(빈 문자열 날짜는 422).
- 날짜는 ISO 문자열, 위치는 `lng`·`navistat`, ETA는 별도 4개 필드를 사용합니다. `null`은 0이나 정상으로 바꾸지 않습니다.
- `available: false`는 이력이 빈 경우와 다릅니다. 이력을 못 읽으면 화면을 죽이지 않고 이 값으로 표시합니다.
- `getPresentationData`, `selectFixture`, `getReplaySnapshot`, `getGeography`는 로컬 UI/데모 보조 함수입니다. 백엔드 endpoint가 아닙니다.
- 서버 API 키를 브라우저 코드나 `NEXT_PUBLIC_*` 값에 넣지 마세요.

## 로그인

백엔드가 `SUPABASE_URL`로 로그인을 켜면 화면도 아래 두 값을 넣어 켭니다. 둘 다 브라우저에 보여도 되는 값입니다. secret 키는 절대 넣지 않습니다.

```bash
NEXT_PUBLIC_API_BASE=http://localhost:8000 NEXT_PUBLIC_SUPABASE_URL=https://<프로젝트>.supabase.co NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=<publishable 키> npm run dev
```

- 값이 없으면 로그인이 꺼지고 세션 ID로만 돕니다. 로그인 꺼진 compose와 같습니다.
- 켜져 있으면 로그인 전에는 소개 랜딩과 Google 시작 버튼, "데모 둘러보기" 버튼이 있는 로그인 화면이 나옵니다. 소개 페이지는 예외입니다.
- 첫 로그인 뒤 회원 정보가 비어 있으면(`GET /api/me`의 `profile_complete` false) 회원 정보 입력 화면이 먼저 나옵니다.
- 요청마다 `Authorization: Bearer <JWT>`를 붙입니다. 백엔드는 ES256 공개키로 확인합니다 (backend/app/auth.py). 정지된 계정은 403 `account_suspended`로 안내합니다.
- Supabase 대시보드에서 Google 공급자를 켜고, Redirect URL에 화면 주소(예: http://localhost:3000/**)를 넣어야 돌아올 수 있습니다.
- 설정 화면의 "계정과 이력" 탭에서 내 기록 삭제(DELETE /api/history)와 회원 탈퇴(DELETE /api/account)를 합니다.

## fixture 수정

1. `lib/fixtures/{status}.json`을 수정합니다. `watch.json`, `act.json`은 status가 `ok`인 추가 예시입니다.
2. 입력값·선택기 라벨·부산 기준 좌표는 `demo.json`에서 수정합니다.
3. 이력 요약은 `history.json`에 추가합니다. `request_id`는 재생할 응답의 `trace_id`와 일치해야 합니다.
4. 시나리오를 추가한다면 `lib/api.ts`의 레지스트리에도 등록합니다.
5. `metrics.json`과 `settings.json`은 별도 화면 fixture입니다. 미확인 값은 null·빈 배열로 유지합니다. 데모 둘러보기의 배와 항로는 `watch-map.json`과 `lib/tour.ts`에 있습니다.
6. `npm run test:contracts`와 빌드로 계약·타입을 확인합니다.

## 검증 재실행

```bash
npm run typecheck
npm run test:contracts
npm run build
```

스크린샷·상호작용·접근성 검사에는 Google Chrome이 필요합니다. 서버를 하나 띄운 뒤 다른 터미널에서 실행합니다.

```bash
npm run dev -- --hostname 127.0.0.1 --port 3100
# 다른 터미널:
npm run test:e2e
```

실제 모드(SSE 단계 표시, 일반 사용자 메뉴, 옛 서버 대체, 오류 이벤트)까지 보려면 가짜 API 주소로 빌드한 서버를 하나 더 띄웁니다. 가짜 API(:3199)는 검사 스크립트가 직접 띄웁니다.

```bash
NEXT_PUBLIC_API_BASE=http://127.0.0.1:3199 npm run build && npm run start -- --port 3101
# 다른 터미널:
MARVIA_LIVE_TEST_URL=http://127.0.0.1:3101 npm run test:e2e
```

`MARVIA_LIVE_TEST_URL`이 없으면 실제 모드 검사는 SKIP으로 표시됩니다(통과로 세지 않음). 다른 포트를 사용하면 `MARVIA_TEST_URL=http://127.0.0.1:포트 npm run test:e2e`로 지정하세요. 브라우저 채널은 `PLAYWRIGHT_CHANNEL`로 변경할 수 있습니다. 이 변수들은 검사 스크립트용이며 `.env` 파일이 필요하지 않습니다.

- 계약 검사는 백엔드 Python 스키마에서 만든 `scripts/backend-contract.json` 스냅샷(`scripts/make_backend_contract.py`)으로 수행합니다.
- 브라우저 검사는 지도 바탕 호스트(tiles.openfreemap.org) 말고 외부 HTTP를 차단하고 요청 발생 여부를 검사합니다.
- 스크린샷은 5개 status + watch/act + 기타 페이지를 각각 1440px, 375px로 촬영하고, 1024/768/390px는 가로 넘침을 검사합니다.
- 접근성 자동 검사는 axe WCAG 2/2.1 A·AA 기준입니다. 자동 검사만으로 모든 접근성이 보장되는 것은 아닙니다.

스키마와의 차이 및 체험 모드 범위는 **NOTES.md**를 먼저 확인해 주세요.
