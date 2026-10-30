"""FastAPI 진입점: 앱 생성, 미들웨어, 라우터 등록, /mcp 마운트, /health, 기동·종료 처리.

판정·LLM 호출은 여기 두지 않는다. LLM 호출은 services/llm.py 한 곳에서만 한다 (CLAUDE.md).

미들웨어:
- RequestLogMiddleware: 요청마다 JSON 로그 한 줄 (SDD 5.11)
- CORS: ALLOWED_ORIGINS에 적힌 출처만 (SDD 7절)
- SecurityHeadersMiddleware: 모든 응답에 X-Content-Type-Options: nosniff, Referrer-Policy: no-referrer (10-08 QA A10)

API 문서(/docs, /redoc, /openapi.json)는 로컬에서만 연다. 배포(Railway)에서는 기본으로 닫는다 (docs_enabled, 10-08 QA A10).

MCP 서버(/mcp)도 이 앱의 경로로 붙여 배포 서비스를 하나로 둔다 (SDD 5.9).
외부 MCP 클라이언트(Claude Desktop 등)용 창구이고, 서비스 자신은 도구 함수를 직접 부른다.

기동할 때 DB 마이그레이션을 적용한다. api와 worker 중 누가 먼저 떠도 표가 생기게 한다 (db.apply_migrations가 잠금으로 겹침을 막는다).
DB가 없거나 붙지 않으면 경고만 남기고 서버는 뜬다 (DB 없이도 판정은 돌아야 한다). 연결 대기는 db.connect 상한(기본 3초)까지다.
재생 모드(REPLAY_MODE=true)면 데모 재생 파일을 주기적으로 새로 만든다 (_refresh_demo_forever).
"""
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.clients.replay import replay_enabled
from backend.app.db import apply_migrations, connect
from backend.app.observability.langfuse import flush as langfuse_flush
from backend.app.observability.logging import RequestLogMiddleware, log_failure, setup_logging
from backend.app.routers import account, admin, agent, cargo_sheet, chat, conversations, document, history, kakao, memory, port, profile, watch
from backend.app.services import memory as memory_service  # 뒤 작업 일꾼 닫기 (lifespan)
from mcp_server.server import MountedMCP

setup_logging()
logger = logging.getLogger(__name__)
mcp_app = MountedMCP()


def _close_graph_checkpointer() -> None:
    """판정 그래프 체크포인터의 DB 연결 풀을 닫는다 (v2-langgraph.md 4절 연결). 그래프를 한 번도 쓰지 않았으면(AGENT_GRAPH 꺼짐) 모듈이
    적재돼 있지 않으므로 아무것도 안 한다: 닫으려고 langgraph를 새로 읽지 않는다. 닫기 실패는 종료를 막지 않는다."""
    module = sys.modules.get("backend.app.graph.checkpoint")
    if module is None:
        return
    try:
        module.close_checkpointer()
    except Exception as e:  # noqa: BLE001 - 종료 중 오류가 나머지 정리를 막으면 안 된다
        log_failure("graph_checkpoint", f"close_failed {type(e).__name__}")


def _migrate() -> None:
    """DATABASE_URL이 있을 때만 migrations/*.sql을 적용한다. 이미 적용한 파일은 건너뛴다 (db/__init__.py)."""
    if not os.environ.get("DATABASE_URL"):
        return
    try:
        # 문장 상한은 끈다(0). 다른 프로세스가 잡은 마이그레이션 잠금을 기다리거나 큰 표를 고칠 수 있다.
        # 연결 대기 상한은 그대로라 DB가 응답하지 않으면 3초 안에 포기하고 서버는 뜬다 (10-08 QA A1)
        with connect(statement_timeout_ms=0) as conn:
            applied = apply_migrations(conn)
        if applied:
            logger.info("migrations applied: %s", applied)
    except Exception as e:
        logger.warning("DB 마이그레이션을 적용하지 못했습니다(%s). DB 없이 계속합니다", type(e).__name__)


# 재생 모드 데모 파일을 새로 만드는 주기(시간). 데모 위치는 "30분 전 수신"으로 만들어서
# position_stale_hours(skills/ais-reliability/rules.md)가 지나면 결과가 바뀐다. 그보다 넉넉히 짧게 둔다
DEMO_REFRESH_HOURS = 4


def _refresh_demo_forever(stop: threading.Event) -> None:
    """재생 모드에서 DEMO_REFRESH_HOURS마다 데모 재생 파일을 새로 만든다 (scripts/make_demo.refresh).
    실패해도 서버는 계속 돌고 다음 차례에 다시 시도한다."""
    from scripts import make_demo  # 재생 모드에서만 필요해서 여기서 불러온다

    while not stop.wait(DEMO_REFRESH_HOURS * 3600):
        try:
            until = make_demo.refresh()
            logger.info("demo replay refreshed, fresh until %s", until.isoformat())
        except Exception as e:  # noqa: BLE001 - 데모 갱신 실패가 서버를 멈추면 안 된다
            log_failure("demo_refresh", type(e).__name__)


def _warm_document() -> None:
    """서류 판독의 첫 요청이 느리지 않게 OCR 엔진과 서류 추출 프롬프트를 미리 올린다 (서버가 켜질 때 한 번, 뒤 스레드).

    둘 다 처음 한 번만 드는 시간이다: OCR 모델 로드 약 1초, Langfuse 프롬프트 첫 조회 약 1초 (로컬 실측).
    rapidocr가 없는 이미지·테스트에서는 아무것도 안 한다. 실패해도 서버는 그대로 뜨고, 첫 요청이 지금처럼 그때 올린다.
    """
    try:
        from backend.app.observability.langfuse import get_prompt
        from backend.app.services import doc_ocr, llm

        doc_ocr._get_engine()
        get_prompt(llm.DOC_EXTRACT_PROMPT)
    except Exception as e:  # noqa: BLE001 - 미리 올리기 실패가 서버를 멈추면 안 된다
        log_failure("warm_document", type(e).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _migrate()
    # 테스트(pytest)에서는 끈다. 엔진을 가짜로 바꿔 끼우는 테스트와 겹치지 않게 한다
    if "PYTEST_CURRENT_TEST" not in os.environ and os.environ.get("DOC_WARMUP", "on").strip().lower() not in ("0", "off", "false", "no"):
        threading.Thread(target=_warm_document, daemon=True, name="warm-document").start()
    stop_demo = threading.Event()
    if replay_enabled() and os.environ.get("DEMO_REFRESH", "on").strip().lower() not in ("0", "off", "false", "no"):
        threading.Thread(target=_refresh_demo_forever, args=(stop_demo,), daemon=True, name="demo-refresh").start()
    # /mcp의 세션 관리자를 서버 수명 동안 돌린다. 이게 없으면 /mcp 요청이 처리되지 않는다 (MountedMCP)
    async with mcp_app.running():
        yield
    stop_demo.set()
    # 기억 뒤 작업을 먼저 끝내야 그 안의 LLM 트레이스도 아래 flush에 실린다
    memory_service.shutdown()
    _close_graph_checkpointer()
    # 꺼질 때 남은 트레이스를 보낸다. 안 하면 마지막 요청들의 트레이스가 사라진다
    langfuse_flush()


def docs_enabled() -> bool:
    """API 문서(/docs, /redoc, /openapi.json)를 열지 정한다 (10-08 QA A10).

    EXPOSE_DOCS가 있으면 그 값(1·true·on·yes면 열기, 그 밖은 닫기)을 따른다.
    없으면 Railway 배포(Railway가 넣는 RAILWAY_ENVIRONMENT·RAILWAY_ENVIRONMENT_NAME이 있음)에서는 닫고 그 밖(로컬 compose,
    채점자 실행)은 연다. 배포에서 닫는 이유: 전체 경로·입력 모양이 공개되면 직접 호출을 시도할 표면이 넓어지고,
    화면 사용자에게는 쓸모가 없다. 로컬은 채점자가 /docs로 API를 확인하므로 연다 (CLAUDE.md "실행").
    """
    raw = os.environ.get("EXPOSE_DOCS", "").strip().lower()
    if raw:
        return raw in ("1", "true", "on", "yes")
    return not (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_ENVIRONMENT_NAME"))


def _docs_kwargs() -> dict:
    """FastAPI 생성 인자. 문서를 닫으면 세 경로 모두 없앤다(404)."""
    if docs_enabled():
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


# 모든 응답에 붙이는 보안 헤더 (10-08 QA A10).
# nosniff: 브라우저가 JSON·파일 응답을 HTML·스크립트로 추측해 실행하지 않게 한다.
# no-referrer: API를 부른 화면 주소(쿼리에 든 값 포함)가 다른 곳으로 새지 않게 한다. API 응답이 다른 곳을 부르는 일은 없다
SECURITY_HEADERS = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"))


class SecurityHeadersMiddleware:
    """응답 시작 메시지에 SECURITY_HEADERS를 붙인다. 이미 같은 헤더가 있으면 그대로 둔다.
    순수 ASGI로 둔다. 응답 본문을 건드리지 않아 스트리밍(/api/chat/stream)과 /mcp도 그대로 흐른다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(title="Marvia API", lifespan=lifespan, **_docs_kwargs())
app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    # 기본값은 로컬 프론트만. 배포에서는 ALLOWED_ORIGINS에 화면 주소(Railway marvia-web)를 넣는다
    allow_origins=[o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    # DELETE: 기록 삭제·회원 탈퇴, PUT: 회원 정보 저장. 빠지면 브라우저가 사전 요청에서 막는다
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    # 엑셀 받기(GET /api/cargo/sheet.xlsx)의 파일 이름을 다른 출처의 화면이 읽으려면 열어 둬야 한다
    expose_headers=["Content-Disposition"],
)
# 마지막에 더해 가장 바깥에 둔다. CORS 사전 요청 응답과 오류 응답에도 보안 헤더가 붙는다
app.add_middleware(SecurityHeadersMiddleware)


app.include_router(agent.router)
app.include_router(chat.router)  # 자연어 입력 (docs/specs/chat-search.md)
app.include_router(history.router)
app.include_router(conversations.router)  # 대화 목록
app.include_router(document.router)  # 서류 사진 (docs/specs/document-extract.md)
app.include_router(account.router)  # 회원 탈퇴
app.include_router(admin.router)  # 운영자 관리 (docs/specs/operator-admin.md)
app.include_router(profile.router)  # 회원 정보 (docs/specs/user-profile.md)
app.include_router(memory.router)  # 화물 비서 기억 (docs/specs/memory.md)
app.include_router(kakao.router)  # 카카오톡 "나에게 보내기" 알림 연결
app.include_router(watch.router)  # 화물 지켜보기·바뀐 판정
app.include_router(cargo_sheet.router)  # 내 화물 현황 표·엑셀 받기
app.include_router(port.router)  # 부산항 입항 추세 참고 카드 (IMF PortWatch). 판정과 무관


# /mcp: MCP 클라이언트가 연결하는 프로토콜 창구. 사용자 화면용 /api/agent와 경로를 나눈다.
# MCP_TOKEN이 있으면 Bearer 토큰 없이는 401 (mcp_server/auth.py)
app.add_route("/mcp", mcp_app, include_in_schema=False)


# /health의 DB 연결 대기 시간(초). compose healthcheck timeout(5초)보다 짧아야 compose가 끊기 전에 503을 받는다
HEALTH_DB_TIMEOUT_SEC = 3


@app.get("/health")
def health():
    """채점자와 배포 환경이 서비스 상태를 확인하는 곳. 서버가 떠 있는지와 DB에 붙는지를 본다.

    - DB 연결 성공: 200 {"status": "ok", "db": "ok"}
    - DATABASE_URL 없음: 200 {"status": "ok", "db": "not_configured"}. 평가 실행기처럼 DB를 일부러 끈 환경이라 고장이 아니다
    - DB 연결 실패: 503 {"status": "error", "db": "down"}. compose·배포 환경이 비정상으로 알아채게 한다
    외부 API(ShipFinder 등)는 부르지 않는다. 10초마다 부르면 ShipFinder 하루 50회 한도가 바닥나고,
    남의 서버 장애가 우리 서버 고장으로 보이게 된다.
    """
    if not os.environ.get("DATABASE_URL"):
        return {"status": "ok", "db": "not_configured"}
    try:
        with connect(timeout=HEALTH_DB_TIMEOUT_SEC) as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        # 예외 메시지에는 DB 주소가 섞일 수 있어 종류만 남긴다 (log_failure 규칙)
        log_failure("health", type(e).__name__)
        return JSONResponse(status_code=503, content={"status": "error", "db": "down"})
    return {"status": "ok", "db": "ok"}

