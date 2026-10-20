# 무료 배포 (Hugging Face Spaces + Vercel + Supabase)

카드 등록 없이 비용 0으로 띄우는 배포 절차다. 구조와 이유는 SDD 6.8.

| 대상 | 어디 | 무엇이 빌드하나 |
|---|---|---|
| api + worker | Hugging Face Space (Docker, CPU basic 무료) | `deploy/hf-space/Dockerfile` |
| web | Vercel Hobby | Vercel (Root Directory `web`) |
| DB | Supabase Free | 없음 (관리형) |

재생 모드(`REPLAY_MODE=true`)로 돌아서 외부 API 키와 LLM 키가 필요 없다. 녹화 답은 `TESTBL01` 입력에만 맞고, 나머지 데모는 판정 표 등급과 "설명 문장은 만들지 못했습니다" 안내로 끝난다(README 4절). `LLM_MODEL`과 키를 넣으면 진짜 LLM이 설명하지만 그 호출 비용은 따로 든다.

## 1. Supabase

1. 기존 프로젝트를 쓴다(구글 로그인용). 없으면 Free로 새로 만든다.
2. Connect > Session pooler 주소를 복사한다. `postgresql://postgres.<프로젝트>:<비밀번호>@aws-...pooler.supabase.com:5432/postgres` 모양이다.
   - Direct connection 주소는 IPv6 전용이라 Space와 GitHub 러너에서 안 붙는다.
   - Transaction pooler(6543)는 psycopg의 prepared statement와 부딪힐 수 있어 쓰지 않는다.
3. 표는 api가 켜질 때 마이그레이션으로 자동 생성된다.

## 2. GitHub 토큰 (Space가 코드를 받을 때)

GitHub > Settings > Developer settings > Fine-grained tokens에서 새 토큰을 만든다.

- Repository access: `seungwook-kim/marvia`만
- Permissions: Contents **Read-only** (나머지는 없음)
- 만료일을 두면 만료 뒤 Space 빌드가 실패한다. 그때 새 토큰으로 바꾼다.

## 3. Hugging Face Space

1. New Space > SDK **Docker** > Blank > Hardware **CPU basic (free)** > **Public**.
   - 화면(브라우저)이 api를 불러야 해서 Public이어야 한다. Files 탭에는 Dockerfile과 README만 보이고 코드는 빌드 때 받는다.
2. Settings > Variables and secrets에 아래 값을 넣는다. Secret은 빌드와 실행 모두에 쓰인다.

| 이름 | 종류 | 값 |
|---|---|---|
| `GITHUB_TOKEN` | Secret | 2번 토큰 |
| `DATABASE_URL` | Secret | 1번 세션 풀러 주소 |
| `MCP_TOKEN` | Secret | 아무 긴 임의 문자열. 비워도 재생 모드는 돌지만 `/mcp`가 밖에 열린다 |
| `REPLAY_MODE` | Variable | `true` |
| `ALLOWED_ORIGINS` | Variable | Vercel 주소 (예: `https://marvia.vercel.app`) |
| `WORKER_SCHEDULE` | Variable | `on` |
| `WORKER_INTERVAL_HOURS` | Variable | `6` |
| `WORKER_SHIPFINDER_CALLS_PER_RUN` | Variable | `0` |
| `WATCH_ENABLED` | Variable | `off` |
| `SUPABASE_URL` | Variable | 로그인을 켤 때만. `https://<프로젝트>.supabase.co` |

넣지 않는 것: `LLM_MODEL`과 LLM 키, `SHIPFINDER_KEY`, `UNIPASS_KEY`, `DATA_GO_KR_KEY`, `MOF_*`, `LANGFUSE_*`, `KAKAO_*`.

3. Hugging Face > Settings > Access Tokens에서 **Write** 토큰을 만든다(배포 워크플로가 Space에 올릴 때 쓴다).

## 4. GitHub 레포 설정 (`seungwook-kim/marvia`)

Settings > Secrets and variables > Actions

| 이름 | 종류 | 값 |
|---|---|---|
| `HF_TOKEN` | Secret | 3-3번 Write 토큰 |
| `SUPABASE_DB_URL` | Secret | 1번 세션 풀러 주소 (`keepalive.yml`) |
| `HF_SPACE` | Variable | `seungwook-kim/<Space 이름>` |
| `HF_SPACE_URL` | Variable | `https://seungwook-kim-<Space 이름>.hf.space` |

`HF_SPACE`를 넣은 뒤 Actions > Deploy HF Space > Run workflow로 첫 배포를 한다. 그 뒤에는 `main` push마다 자동으로 다시 배포된다.

## 5. Vercel

1. Add New > Project > `seungwook-kim/marvia` 가져오기.
2. Root Directory `web`, Framework Next.js (자동 인식).
3. Environment Variables

| 이름 | 값 |
|---|---|
| `NEXT_PUBLIC_API_BASE` | `HF_SPACE_URL`과 같은 값 |
| `NEXT_PUBLIC_SUPABASE_URL` | 로그인을 켤 때만 |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | 로그인을 켤 때만 |

4. 배포 주소가 정해지면 Space의 `ALLOWED_ORIGINS`에 넣는다. 로그인을 켰으면 Supabase Auth의 Redirect URLs에도 넣는다.

## 6. 확인

- `https://<Space 주소>/health`가 200
- Space Logs에 `schedule_on`과 `run_done` 줄이 보이면 워커가 돌고 있다
- Vercel 화면에서 데모 B/L(`TESTBL01`) 조회
- Actions > Keepalive > Run workflow가 초록 (space, db 두 작업 모두)

## 한계

- Space는 48시간 접속이 없으면 잠든다. `keepalive.yml`이 12시간마다 `/health`를 불러 깨워 둔다. 예약 실행이 빠지거나 Space가 재시작되면 첫 접속에 1분 안팎 걸린다.
- Supabase Free는 7일 안 쓰면 일시정지된다. `keepalive.yml`이 4일마다 DB에 직접 쿼리를 보낸다.
- Space는 미국, 기존 Supabase는 서울이다. DB 왕복마다 0.15~0.2초가 붙으므로 느리면 미국 동부에 Supabase 프로젝트를 새로 만든다.
- 워커가 죽어도 api는 계속 돈다. 워커 상태는 Space Logs로만 본다.
