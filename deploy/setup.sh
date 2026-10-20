#!/usr/bin/env bash
# 무료 배포(SDD 6.8) 첫 설정을 한 번에 한다. 손으로 할 일은 아래 "준비" 세 가지 로그인과 Supabase 주소뿐이다.
#
# 준비
#   gh auth login            GitHub (repo 권한)
#   hf auth login            Hugging Face (Write 토큰. 이 토큰이 GitHub 시크릿 HF_TOKEN으로도 들어간다)
#   vercel login             Vercel. 레포 루트에서 `vercel link --project marvia`가 돼 있어야 한다
#
# 실행 (레포 루트에서)
#   bash deploy/setup.sh                       Supabase 세션 풀러 주소를 물어본다(화면에 안 보임)
#   ENV_FILE=../somewhere/.env bash deploy/setup.sh   그 파일의 DATABASE_URL을 쓴다
#   SPACE_NAME=marvia-api (기본값)             Space 이름
#
# 하는 일
#   1. 이 레포만 읽는 deploy key를 새로 만들어 GitHub에 등록한다 (읽기 전용). 개인키는 Space 시크릿에만 넣고 지운다
#   2. Hugging Face Space(Docker, 무료 CPU, 공개)를 만들고 시크릿·변수를 넣는다
#   3. GitHub 레포에 HF_TOKEN · SUPABASE_DB_URL 시크릿, HF_SPACE · HF_SPACE_URL 변수를 넣는다
#   4. Vercel에 NEXT_PUBLIC_API_BASE를 넣고 운영 배포한 뒤, 그 주소를 Space의 ALLOWED_ORIGINS에 넣는다
#   5. Deploy HF Space 워크플로를 돌려 Space를 처음 빌드한다
# 여러 번 돌려도 된다. deploy key만 돌릴 때마다 새로 생기므로 GitHub > Settings > Deploy keys에서 옛 키를 지운다.
set -euo pipefail

REPO=seungwook-kim/marvia
SPACE_NAME="${SPACE_NAME:-marvia-api}"
cd "$(dirname "$0")/.."

say() { printf '\n== %s\n' "$*"; }

say "로그인 확인"
gh auth status >/dev/null
HF_USER=$(python -c "from huggingface_hub import whoami; print(whoami()['name'])")
vercel whoami >/dev/null 2>&1 || { echo "vercel login이 필요하다"; exit 1; }
[ -f .vercel/project.json ] || { echo "레포 루트에서 vercel link --project marvia 를 먼저 한다"; exit 1; }
SPACE="$HF_USER/$SPACE_NAME"
# Space 주소는 소문자에 _ . 을 - 로 바꾼 이름이다
SPACE_URL="https://$(printf '%s-%s' "$HF_USER" "$SPACE_NAME" | tr 'A-Z_.' 'a-z--').hf.space"
echo "Space: $SPACE ($SPACE_URL)"

say "Supabase 주소"
if [ -n "${ENV_FILE:-}" ]; then
  DATABASE_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r')
else
  read -rsp "Session pooler 주소(postgresql://...:5432/postgres): " DATABASE_URL; echo
fi
DATABASE_URL="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
case "$DATABASE_URL" in postgresql://*:5432/*) ;; *) echo "세션 풀러(5432) 주소가 아니다"; exit 1;; esac

say "deploy key 만들기 (읽기 전용)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "hf-space-$SPACE_NAME" -f "$TMP/key"
gh repo deploy-key add "$TMP/key.pub" -R "$REPO" --title "hf-space $SPACE_NAME $(date +%F)"
DEPLOY_KEY_B64=$(base64 -w0 < "$TMP/key")
MCP_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

say "Hugging Face Space 만들기와 시크릿·변수"
# 비밀값은 인자가 아니라 환경변수로 넘겨 프로세스 목록에 보이지 않게 한다
SPACE="$SPACE" DEPLOY_KEY_B64="$DEPLOY_KEY_B64" DATABASE_URL="$DATABASE_URL" MCP_TOKEN="$MCP_TOKEN" python - <<'PY'
import os
from huggingface_hub import HfApi
api, space = HfApi(), os.environ["SPACE"]
api.create_repo(space, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
for k, v in {"GITHUB_DEPLOY_KEY": os.environ["DEPLOY_KEY_B64"], "DATABASE_URL": os.environ["DATABASE_URL"],
             "MCP_TOKEN": os.environ["MCP_TOKEN"]}.items():
    api.add_space_secret(space, k, v)
for k, v in {"REPLAY_MODE": "true", "WORKER_SCHEDULE": "on", "WORKER_INTERVAL_HOURS": "6",
             "WORKER_SHIPFINDER_CALLS_PER_RUN": "0", "WATCH_ENABLED": "off"}.items():
    api.add_space_variable(space, k, v)
print("ok")
PY

say "GitHub 레포 시크릿·변수"
python -c "from huggingface_hub import get_token; print(get_token())" | gh secret set HF_TOKEN -R "$REPO"
printf '%s' "$DATABASE_URL" | gh secret set SUPABASE_DB_URL -R "$REPO"
gh variable set HF_SPACE -R "$REPO" --body "$SPACE"
gh variable set HF_SPACE_URL -R "$REPO" --body "$SPACE_URL"

say "Vercel 환경변수와 운영 배포"
vercel env rm NEXT_PUBLIC_API_BASE production --yes >/dev/null 2>&1 || true
printf '%s' "$SPACE_URL" | vercel env add NEXT_PUBLIC_API_BASE production
DEPLOY_URL=$(vercel deploy --prod --yes 2>/dev/null | tail -1)
echo "배포: $DEPLOY_URL"
# 운영 별칭(예: marvia-xxx.vercel.app)을 모아 CORS 허용 목록으로 쓴다
ORIGINS=$(vercel inspect "$DEPLOY_URL" 2>&1 | grep -oE 'https://[a-z0-9.-]+\.vercel\.app' | sort -u | paste -sd, -)
echo "ALLOWED_ORIGINS: $ORIGINS"
SPACE="$SPACE" ORIGINS="$ORIGINS" python -c "import os; from huggingface_hub import HfApi; HfApi().add_space_variable(os.environ['SPACE'], 'ALLOWED_ORIGINS', os.environ['ORIGINS'])"

say "Space 첫 빌드"
gh workflow run deploy-hf.yml -R "$REPO" --ref main
echo "빌드는 5~10분 걸린다. 진행: https://huggingface.co/spaces/$SPACE (Logs 탭)"
echo "다 되면: curl $SPACE_URL/health  /  gh workflow run keepalive.yml -R $REPO"
