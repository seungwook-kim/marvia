# 무료 배포 (Render + Vercel + Supabase)

카드 등록 없이 비용 0으로 띄우는 배포 절차다. 구조와 이유는 SDD 6.8.

| 대상 | 어디 | 무엇이 빌드하나 |
|---|---|---|
| api + worker | Render 무료 웹 서비스 (512MB, CPU 0.1, 싱가포르) | `render.yaml` → `deploy/render/Dockerfile` |
| web | Vercel Hobby (프로젝트 `marvia`, Root Directory `web`) | Vercel |
| DB | Supabase Free (기존 서울 프로젝트) | 없음 (관리형) |

재생 모드(`REPLAY_MODE=true`)로 돌아서 외부 API 키와 LLM 키가 필요 없다. 녹화 답은 `TESTBL01`에 판매 예정일을 서버가 켜질 때 로그에 찍는 날짜로 넣었을 때만 맞고, 나머지 데모는 판정 표 등급과 "설명 문장은 만들지 못했습니다" 안내로 끝난다(README 4절).

## 비용이 0인 이유 (render.com/docs/free, render.com/pricing)

- 무료 웹 서비스는 $0/month. 워크스페이스에 매달 750시간을 준다. 서비스 하나를 24시간 켜도 한 달 최대 744시간이다.
- 시간·트래픽(Hobby 5GB)·빌드 시간을 넘으면 결제수단이 있을 때만 청구된다. 결제수단이 없으면 정지(빌드는 막히고 서비스는 기존 빌드로 계속)로 끝난다. **결제수단을 넣지 않는다.**

## 1. Supabase

1. Connect > Session pooler 주소를 복사한다. `postgresql://postgres.<프로젝트>:<비밀번호>@aws-...pooler.supabase.com:5432/postgres` 모양이다.
   - Direct connection 주소는 IPv6 전용이라 GitHub 러너에서 안 붙는다.
   - Transaction pooler(6543)는 psycopg의 prepared statement와 부딪힐 수 있어 쓰지 않는다.
2. 표는 api가 켜질 때 마이그레이션으로 자동 생성된다.

## 2. Render

1. https://render.com 에 GitHub로 가입한다. 결제수단은 넣지 않는다.
2. New > Blueprint > `seungwook-kim/marvia`를 고른다(GitHub 앱에 이 레포 접근을 허용). `render.yaml`대로 `marvia-api`가 만들어진다.
3. `DATABASE_URL`을 묻으면 1번 주소를 넣는다. `MCP_TOKEN`은 Render가 만든다.
4. 첫 빌드가 끝나면 서비스 주소(`https://marvia-api.onrender.com` 같은 모양)를 확인한다.

그 뒤로는 `main` push마다 다시 빌드한다.

## 3. GitHub 레포 설정 (`seungwook-kim/marvia`)

Settings > Secrets and variables > Actions (`keepalive.yml`이 쓴다)

| 이름 | 종류 | 값 |
|---|---|---|
| `SUPABASE_DB_URL` | Secret | 1번 세션 풀러 주소 |
| `API_URL` | Variable | 2-4번 Render 주소 |

```
gh secret set SUPABASE_DB_URL -R seungwook-kim/marvia      # 붙여 넣기(화면에 안 보임)
gh variable set API_URL -R seungwook-kim/marvia --body https://marvia-api.onrender.com
```

## 4. Vercel

프로젝트 `marvia`(Root Directory `web`)는 만들어 두었다. 운영 주소는 https://marvia-alpha.vercel.app.

```
printf '%s' https://marvia-api.onrender.com | vercel env add NEXT_PUBLIC_API_BASE production
vercel deploy --prod
```

- Vercel 계정 설정 > Login Connections에서 GitHub를 연결하고 `vercel git connect`를 하면 그 뒤로는 push마다 자동 배포된다.
- Vercel 주소가 바뀌면 `render.yaml`의 `ALLOWED_ORIGINS`도 고친다.
- 로그인을 켤 때만 `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`(Vercel)와 `SUPABASE_URL`(Render)을 넣는다.

## 5. 확인

- `https://<Render 주소>/health`가 `{"status":"ok","db":"ok"}`
- Render Logs에 `schedule_on`과 `run_done` 줄이 보이면 워커가 돌고 있다
- Vercel 화면에서 데모 B/L(`TESTBL01`) 조회
- Actions > Keepalive > Run workflow가 초록 (api, db 두 작업 모두)

## 깨워 두기

- Render 무료 서비스는 밖에서 15분 요청이 없으면 잠든다. 잠들면 워커도 멈추고 첫 접속이 1분쯤 걸린다.
- 컨테이너가 10분마다 자기 공개 주소의 `/health`를 부른다(`KEEP_AWAKE=on`). `/health`는 DB도 부르므로 Supabase도 깨어 있다.
- Render가 재시작하거나 재우면 반복도 멈춘다. `keepalive.yml`이 12시간마다 밖에서 불러 다시 깨운다. DB는 따로 4일마다 직접 쿼리한다.
- 잠들어도 되면 Render 환경변수 `KEEP_AWAKE`를 `off`로 바꾼다. 그러면 워커 6시간 주기도 잠든 동안은 돌지 않는다.

## 한계

- CPU 0.1개라 서류 사진 OCR은 로컬(약 1.5초)보다 몇 배 느리다. 판정 조회는 DB 왕복이 대부분이다.
- Render 문서: "Do not use them for production applications", "Render might restart a Free web service at any time". 포트폴리오 데모용이다.
- 재생 파일은 컨테이너 안에 생겨 재시작마다 다시 만든다(약 5시간 반 뒤 위치가 오래된 위치가 된다, scripts/make_demo.py).
