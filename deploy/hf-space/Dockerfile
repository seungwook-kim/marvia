# Hugging Face Spaces(Docker) 배포용 이미지. api와 수집 워커를 한 컨테이너에서 띄운다.
# 이 파일과 README.md만 Space 저장소에 올라간다(.github/workflows/deploy-hf.yml). 코드는 빌드 때 GitHub 비공개 레포에서
# 받으므로 공개 Space의 Files 탭에는 코드가 보이지 않는다. 받을 때 쓰는 토큰은 Space의 GITHUB_TOKEN 시크릿(읽기 전용)이다.
# 빌드 단계는 Dockerfile.railway와 같다. 그쪽 base · app 단계를 고치면 이 파일도 같이 고친다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Spaces 기본 포트. README.md의 app_port와 같아야 한다
ENV PORT=7860
# API 설명서 화면을 닫는다. main.py는 Railway 환경변수로만 배포를 알아보므로 Spaces에서는 직접 꺼 둔다
ENV EXPOSE_DOCS=false

# git: 코드 받기. libgl1 · libglib2.0-0: 서류 사진 OCR(opencv)이 찾는 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Spaces는 컨테이너를 uid 1000으로 돌린다. 데모 재생 파일을 /app 아래에 쓰므로 이 사용자가 쓸 수 있어야 한다
RUN useradd -m -u 1000 user
WORKDIR /app

# 받을 커밋. 배포 워크플로가 이 줄을 push된 커밋 SHA로 바꿔 올린다. 줄이 바뀌면 Space가 다시 빌드하고 캐시도 여기서 끊긴다
ARG MARVIA_REF=main
# 토큰을 주소에 직접 넣어 받고 git 설정에는 남기지 않는다. 받은 폴더는 같은 RUN 안에서 지워 이미지에 남지 않는다
RUN --mount=type=secret,id=GITHUB_TOKEN,mode=0444,required=true \
    git init -q /tmp/src && cd /tmp/src \
    && git fetch -q --depth 1 "https://x-access-token:$(cat /run/secrets/GITHUB_TOKEN)@github.com/seungwook-kim/marvia.git" "$MARVIA_REF" \
    && git checkout -q FETCH_HEAD \
    && cp -r backend mcp_server worker skills scripts evals /app/ \
    && cd / && rm -rf /tmp/src

RUN pip install --no-cache-dir -r backend/requirements.txt -r backend/requirements-doc.txt
# OCR 모델을 빌드 때 받아 이미지에 넣는다 (첫 요청이 모델 내려받기로 느려지지 않게). root로 받아야 site-packages에 쓸 수 있다
RUN python -c "from backend.app.services.doc_ocr import _get_engine; _get_engine()"

RUN chown -R user:user /app
USER user

EXPOSE 7860
# 재생 모드면 켜질 때 데모 재생 파일을 지금 시각 기준으로 만든다(Dockerfile.railway와 같음). 실패해도 서버는 켠다.
# 워커는 뒤에서 돌린다. WORKER_SCHEDULE=on이 아니면 안내 한 줄을 남기고 끝난다(worker/run.py). 워커가 죽어도 api는 계속 돈다.
# exec로 uvicorn이 PID 1이 되어 종료 신호를 직접 받는다
CMD ["sh", "-c", "export MCP_URL=\"${MCP_URL:-http://127.0.0.1:${PORT}/mcp}\"; case \"$REPLAY_MODE\" in true|1|yes) python -m scripts.make_demo || echo 'make_demo failed';; esac; python -m worker.run & exec uvicorn backend.app.main:app --host 0.0.0.0 --port \"$PORT\""]
