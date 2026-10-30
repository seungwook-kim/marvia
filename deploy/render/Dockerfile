# Render 무료 웹 서비스 배포용 이미지 (SDD 6.8). api와 수집 워커를 한 컨테이너에서 띄운다.
# Render 무료 플랜은 서비스 하나에 512MB라 worker를 따로 두지 않는다. render.yaml이 이 파일을 가리킨다.
# 채팅 RAG 임베딩 모델과 backend/requirements-rag.txt는 이 이미지에 넣지 않는다. 배포 RAG_MODE는 bm25다 (docs/specs/v2-rag.md Q5).
# 512MB 안에 모델이 들어가는지는 Linux 컨테이너 실측(R14)으로 정한 뒤, 들어간다고 나오면 기본 Dockerfile의 app 단계와 같은 줄을 여기에 둔다.
# 빌드 단계는 Dockerfile.railway와 같다. 그쪽 base · app 단계를 고치면 이 파일도 같이 고친다.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# API 설명서 화면을 닫는다. main.py는 Railway 환경변수로만 배포를 알아보므로 여기서는 직접 꺼 둔다
ENV EXPOSE_DOCS=false

# curl: 깨워 두기(아래 CMD). libgl1 · libglib2.0-0: 서류 사진 OCR(opencv)이 찾는 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends curl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements-doc.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt -r backend/requirements-doc.txt

COPY backend/ ./backend/
COPY mcp_server/ ./mcp_server/
COPY worker/ ./worker/
COPY skills/ ./skills/
COPY scripts/ ./scripts/
COPY evals/ ./evals/
# 채팅 RAG: 색인 manifest와 파일 색인(docs/rag/)만 넣는다. 모델은 넣지 않는다 (배포 RAG_MODE=bm25, docs/specs/v2-rag.md Q5)
COPY docs/rag/ ./docs/rag/

# OCR 모델을 빌드 때 받아 이미지에 넣는다 (첫 요청이 모델 내려받기로 느려지지 않게).
# 메모리에는 첫 서류 요청 때 올라간다(render.yaml DOC_WARMUP=off). 512MB 제한 로컬 실측: 대기 154MB, OCR 1장 뒤 201MB
RUN python -c "from backend.app.services.doc_ocr import _get_engine; _get_engine()"

# Render는 포트를 PORT 환경변수로 준다(기본 10000). 서비스는 자기 /mcp를 MCP_URL로 부르므로 같은 포트로 맞춘다.
# 재생 모드면 켜질 때 데모 재생 파일을 지금 시각 기준으로 만든다(Dockerfile.railway와 같음). 실패해도 서버는 켠다.
# 워커는 뒤에서 돌린다. WORKER_SCHEDULE=on이 아니면 안내 한 줄을 남기고 끝난다(worker/run.py). 워커가 죽어도 api는 계속 돈다.
# 깨워 두기: Render 무료 서비스는 밖에서 15분 요청이 없으면 잠들고, 그러면 워커도 멈추고 첫 접속이 1분쯤 걸린다.
# KEEP_AWAKE가 off가 아니면 10분마다 자기 공개 주소(RENDER_EXTERNAL_URL, Render가 넣는다)의 /health를 불러 깨워 둔다.
# /health는 DB도 조회하므로 Supabase 일시정지도 같이 막는다. 한 달 750시간 안에 든다(서비스 하나 24시간 = 최대 744시간)
# exec로 uvicorn이 PID 1이 되어 종료 신호를 직접 받는다
CMD ["sh", "-c", "export MCP_URL=\"${MCP_URL:-http://127.0.0.1:${PORT:-10000}/mcp}\"; case \"$REPLAY_MODE\" in true|1|yes) python -m scripts.make_demo || echo 'make_demo failed';; esac; python -m worker.run & if [ \"$KEEP_AWAKE\" != off ] && [ -n \"$RENDER_EXTERNAL_URL\" ]; then (while sleep 600; do curl -fsS -m 30 -o /dev/null \"$RENDER_EXTERNAL_URL/health\" || echo 'keep_awake failed'; done) & fi; exec uvicorn backend.app.main:app --host 0.0.0.0 --port \"${PORT:-10000}\""]
