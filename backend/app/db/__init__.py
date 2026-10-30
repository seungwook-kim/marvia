"""DB 연결, 마이그레이션, 조회 이력·정리 쿼리 (SDD 3.9). 앱과 워커가 같이 쓴다.

psycopg는 함수 안에서 import한다. DB가 필요 없는 단위 테스트가 psycopg 설치 없이 돌게 하려는 것이다.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def plain_url(url: str) -> str:
    """postgresql+asyncpg://... 같은 SQLAlchemy식 주소를 psycopg가 읽는 postgresql://... 로 바꾼다.

    compose의 api 서비스가 asyncpg 주소를 쓰고 있어서, 같은 DATABASE_URL을 워커도 읽을 수 있게 한다.
    """
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}" if sep else url


def _env_number(name: str, default: float) -> float:
    """환경변수 숫자 하나. 비었거나 숫자가 아니면 기본값 (잘못 적은 값 때문에 연결이 막히지 않게)."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# 연결 대기 상한(초). 10-08 QA A1: 이 값이 없으면 DB가 응답하지 않을 때 psycopg가 OS 기본(수십 초 이상)만큼 기다려
# 로그인 확인(auth)이 요청마다 25초 넘게, 선박 자세히 보기가 260초 멈췄다. DB_CONNECT_TIMEOUT_SEC로 바꾼다
DEFAULT_CONNECT_TIMEOUT_SEC = 3
# 문장 하나의 실행 상한(밀리초). 잠금 대기나 느린 쿼리가 요청 스레드를 붙잡지 않게 한다 (10-08 QA A1).
# 화면 요청의 쿼리는 모두 한 사용자 몇 줄이라 1초도 안 걸린다. 넉넉히 15초. DB_STATEMENT_TIMEOUT_MS=0이면 끈다.
# 마이그레이션은 0(제한 없음)으로 부른다 (main._migrate). 다른 프로세스의 마이그레이션 잠금을 기다려야 하기 때문이다
DEFAULT_STATEMENT_TIMEOUT_MS = 15000
# 연결이 한 번 실패하면 이 시간(초) 동안은 붙어 보지 않고 바로 실패한다 (10-08 QA A1, B7).
# 한 요청이 기록 읽기·화물 읽기·저장으로 연결을 여러 번 열면 DB 장애 때 단계마다 대기 시간을 다시 냈다(판정 12초).
# 프로세스 안에서 실패를 기억해 같은 요청의 다음 단계와 동시에 들어온 다른 요청이 기다리지 않게 한다.
# 짧게 둬서 DB가 돌아오면 곧 다시 붙는다. DB_DOWN_COOLDOWN_SEC로 바꾸고 0이면 끈다
DEFAULT_DOWN_COOLDOWN_SEC = 5

_state_lock = threading.Lock()
_down_until = 0.0          # time.monotonic() 기준. 이 시각 전에는 기본 DB에 붙어 보지 않는다
_options_unsupported = False  # 연결 풀러가 options 시작 인자(statement_timeout)를 거절한 적이 있다


def reset_connection_state() -> None:
    """실패 기억과 options 거절 기억을 지운다. 테스트가 테스트마다 부른다 (backend/tests/conftest.py)."""
    global _down_until, _options_unsupported
    with _state_lock:
        _down_until, _options_unsupported = 0.0, False


def _rejects_options(err: Exception) -> bool:
    """연결 풀러(PgBouncer 등)가 options 시작 인자를 모른다고 거절한 오류인가."""
    text = str(err).lower()
    return "options" in text and ("startup parameter" in text or "unsupported" in text)


def connect(url: str | None = None, timeout: float | None = None, statement_timeout_ms: int | None = None):
    """DATABASE_URL로 연결한다. autocommit이라 넣는 즉시 저장된다 (워커는 한 줄씩 넣는다).

    - timeout(초): 그 안에 못 붙으면 포기한다. 주지 않으면 DB_CONNECT_TIMEOUT_SEC(기본 3초).
      주소에 connect_timeout을 이미 적었으면 그 값을 따른다 (/health는 더 짧게 따로 준다)
    - statement_timeout_ms: 문장 하나의 실행 상한. 주지 않으면 DB_STATEMENT_TIMEOUT_MS(기본 15초), 0이면 제한 없음.
      서버 쪽 설정(options -c statement_timeout)이라 연결마다 따로 걸린다. 트랜잭션 풀러가 options를 거절하면
      한 번 더 options 없이 붙고, 그 뒤로는 options를 보내지 않는다 (연결 대기 상한은 그대로 걸린다)
    - 기본 DB(url 없음) 연결이 실패하면 DB_DOWN_COOLDOWN_SEC 동안 바로 OperationalError를 낸다 (위 상수 주석)
    """
    import psycopg

    global _down_until, _options_unsupported
    default_db = url is None
    raw = url or os.environ["DATABASE_URL"]
    if default_db and time.monotonic() < _down_until:
        # 대기 없이 같은 종류의 오류. 부르는 쪽은 원래 연결 실패와 똑같이 처리한다
        raise psycopg.OperationalError("database recently unreachable; skipped connect (db cooldown)")

    extra: dict = {}
    if timeout:
        extra["connect_timeout"] = timeout
    elif "connect_timeout" not in raw:
        extra["connect_timeout"] = _env_number("DB_CONNECT_TIMEOUT_SEC", DEFAULT_CONNECT_TIMEOUT_SEC)
    if extra.get("connect_timeout"):
        extra["connect_timeout"] = max(1, int(extra["connect_timeout"]))  # libpq는 정수 초, 2초 미만은 2초로 본다
    limit = statement_timeout_ms if statement_timeout_ms is not None else int(
        _env_number("DB_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS))
    with_options = limit > 0 and not _options_unsupported
    try:
        try:
            conn = psycopg.connect(plain_url(raw), autocommit=True, **extra,
                                   **({"options": f"-c statement_timeout={limit}"} if with_options else {}))
        except psycopg.OperationalError as e:
            if not (with_options and _rejects_options(e)):
                raise
            with _state_lock:
                _options_unsupported = True
            conn = psycopg.connect(plain_url(raw), autocommit=True, **extra)
    except psycopg.OperationalError:
        if default_db:
            cooldown = _env_number("DB_DOWN_COOLDOWN_SEC", DEFAULT_DOWN_COOLDOWN_SEC)
            if cooldown > 0:
                with _state_lock:
                    _down_until = time.monotonic() + cooldown
        raise
    if default_db and _down_until:
        with _state_lock:
            _down_until = 0.0  # 다시 붙었다. 기다리던 다른 요청도 곧바로 붙어 본다
    return conn


def atomic(conn):
    """여러 표를 지우는 일을 한 트랜잭션으로 묶는다 (삭제 경로, docs/specs/memory.md 개인정보).
    connect()는 autocommit이라 이 안에서만 BEGIN·COMMIT으로 묶인다(psycopg Connection.transaction).
    트랜잭션 기능이 없는 연결(테스트의 가짜 연결)은 묶지 않고 그대로 실행한다."""
    from contextlib import nullcontext

    return conn.transaction() if hasattr(conn, "transaction") else nullcontext()


# 마이그레이션 전체를 감싸는 pg advisory lock 번호. 아무 값이나 되지만 앱 안에서 이 용도로만 쓴다
MIGRATION_LOCK_ID = 7_310_620_261_006


def apply_migrations(conn) -> list[str]:
    """migrations/*.sql을 이름 순서대로 한 번씩만 적용하고, 새로 적용한 파일 이름을 돌려준다.

    - 전체를 pg_advisory_lock으로 감싼다. api와 worker가 동시에 떠서 같은 파일을 두 번 적용하려는 경쟁을 막는다.
      뒤에 온 쪽은 앞쪽이 끝날 때까지 기다린 뒤 적용된 목록을 다시 읽어 건너뛴다. 연결이 끊겨도 DB가 잠금을 풀어 준다
    - 파일 하나를 한 트랜잭션으로 묶는다. connect()가 autocommit이라 묶지 않으면 중간 문장이 실패할 때 반쯤 적용된 채 남는다.
      파일과 schema_migrations 기록이 함께 들어가거나 함께 빠진다
    """
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        applied = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with atomic(conn):
                # 파라미터 없는 execute는 여러 문장을 한 번에 보낼 수 있다 (psycopg 3)
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        return applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))


def log_query(conn, session_id: str, **fields) -> None:
    """조회 이력 한 줄을 남긴다 (SDD 3.9).

    B/L 원본을 그대로 넣지 않는다. 부르는 쪽이 bl_masked와, 로그인했으면 잠긴 bl_encrypted만 넘긴다 (crypto.py, 007).
    이력 저장이 실패해도 판정 응답은 나가야 하므로 부르는 쪽에서 예외를 삼킨다.
    """
    columns = ["session_id", *fields.keys()]
    placeholders = ", ".join(["%s"] * len(columns))
    conn.execute(
        f"INSERT INTO query_log ({', '.join(columns)}) VALUES ({placeholders})",
        [session_id, *fields.values()],
    )


def _owner(session_id: str, user_id: str | None) -> tuple[str, str]:
    """이력을 누구 기준으로 찾을지 WHERE 조건과 값으로 돌려준다 (006 마이그레이션).

    로그인했으면 사용자 ID로 찾는다. 브라우저가 바뀌어도 이어진다.
    사용자가 없으면(로그인 기능이 꺼진 환경) 세션 ID로 찾되 로그인한 채 남긴 줄은 뺀다.
    같은 DB를 로그인 켠 환경과 같이 쓰더라도 사용자 기록이 세션 조회로 새지 않게 한다.
    조건 문자열은 이 두 가지로만 정해진다. 사용자 입력은 값(%s)으로만 들어간다.
    """
    return ("user_id = %s", user_id) if user_id else ("session_id = %s AND user_id IS NULL", session_id)


def recent_queries(conn, session_id: str, limit: int = 20, user_id: str | None = None,
                   since: datetime | None = None) -> list[dict]:
    """세션(로그인했으면 사용자)의 지난 조회를 최신순으로 읽는다. 새로 계산하거나 저장하지 않는다 (SDD 4.4).
    since가 있으면 그 시각 이후(>=)만 읽는다 (설정 화면 기간 필터).
    days_margin·stage_name은 저장된 결과(response JSONB)에서 두 값만 꺼낸다. 이력 한 줄 이유(routers/history)를 코드로 만드는 재료다."""
    where, owner = _owner(session_id, user_id)
    params: list = [owner]
    if since is not None:
        where += " AND created_at >= %s"
        params.append(since)
    params.append(min(limit, 100))
    rows = conn.execute(
        f"""
        SELECT request_id, bl_masked, status, verdict, vessel_match, decided_by, trace_id, created_at,
               (response->>'days_margin')::int, response->'customs'->>'stage_name'
        FROM query_log WHERE {where} ORDER BY created_at DESC LIMIT %s
        """,
        params,
    ).fetchall()
    # model 칸은 018에서 지웠다. 이력 응답의 model은 null로 남긴다
    keys = ("request_id", "bl_masked", "status", "verdict", "vessel_match", "decided_by", "trace_id", "created_at",
            "days_margin", "stage_name")
    return [dict(zip(keys, row)) for row in rows]


def latest_response(conn, session_id: str, bl_masked: str, user_id: str | None = None) -> dict | None:
    """같은 세션(로그인했으면 사용자)·같은 B/L의 가장 최근 결과(AgentResponse JSON)를 읽는다. 후속 질문이 쓴다 (004 마이그레이션).

    결과가 저장되지 않은 옛 줄(response가 NULL)은 건너뛴다. 없으면 None.
    """
    where, owner = _owner(session_id, user_id)
    row = conn.execute(
        f"""
        SELECT response FROM query_log
        WHERE {where} AND bl_masked = %s AND response IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        [owner, bl_masked],
    ).fetchone()
    return row[0] if row else None


def recent_responses(conn, session_id: str, bl_masked: str, user_id: str | None = None,
                     limit: int = 20) -> list[tuple[dict, str | None]]:
    """같은 가린 번호의 최근 결과와 잠긴 B/L 원본을 최신순으로 (결과, bl_encrypted) 목록으로 읽는다.
    가린 번호가 같은 두 화물을 가를 때 쓴다 (docs/specs/memory.md EC-05, routers/agent._previous_response)."""
    where, owner = _owner(session_id, user_id)
    rows = conn.execute(
        f"""
        SELECT response, bl_encrypted FROM query_log
        WHERE {where} AND bl_masked = %s AND response IS NOT NULL
        ORDER BY created_at DESC LIMIT %s
        """,
        [owner, bl_masked, limit],
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def delete_user_queries(conn, user_id: str) -> int:
    """로그인 사용자의 조회 기록을 전부 지우고 지운 줄 수를 돌려준다 (삭제 2단계, 개인정보보호법 제36조).

    잠긴 B/L(bl_encrypted)도 같은 줄에 있어 함께 지워진다. 다른 사용자 줄과 사용자 없는 줄은 건드리지 않는다.
    """
    return conn.execute("DELETE FROM query_log WHERE user_id = %s", [user_id]).rowcount


def purge_old_queries(conn) -> int:
    """90일 지난 조회 기록을 지우고 지운 줄 수를 돌려준다 (개인정보보호법 제21조).

    규칙(90일)은 DB 함수 purge_old_queries()에만 있다 (migrations/008). Supabase는 pg_cron이 하루 한 번
    부르고, pg_cron이 없는 로컬·채점용 DB는 워커가 실행마다 부른다. 여러 번 불러도 결과는 같다.
    """
    return conn.execute("SELECT purge_old_queries()").fetchall()[0][0]


def purge_done_cargo(conn) -> int:
    """반출 뒤 3년 지난 끝난 화물 줄과 그 화물의 메모장 항목을 지우고 지운 화물 수를 돌려준다
    (docs/specs/memory.md "보관과 크기"). 하루 지난 삭제 표시(user_forget)도 같이 정리한다.

    규칙(3년, 무엇을 지우나)은 DB 함수 purge_done_cargo()에만 있다 (migrations/015). purge_old_queries와 같이 pg_cron이 하루 한 번,
    pg_cron이 없는 로컬·채점용 DB는 워커가 실행마다 부른다. 여러 번 불러도 결과는 같다.
    """
    return conn.execute("SELECT purge_done_cargo()").fetchall()[0][0]


def purge_old_conversations(conn) -> int:
    """마지막 대화로부터 90일 지난 대화(말풍선 포함)를 지우고 지운 대화 수를 돌려준다.

    규칙(90일)은 DB 함수 purge_old_conversations()에만 있다 (migrations/018). purge_old_queries와 같이 pg_cron이 하루 한 번,
    pg_cron이 없는 로컬·채점용 DB는 워커가 실행마다 부른다. 여러 번 불러도 결과는 같다.
    """
    return conn.execute("SELECT purge_old_conversations()").fetchall()[0][0]


def purge_graph_checkpoints(conn, keep_days: int) -> int:
    """어느 요청도 오지 않아 남은 멈춘 판정 그래프 스레드를 keep_days일 뒤 지우고 지운 스레드 수를 돌려준다 (migrations/032).
    기간은 SQL에 박지 않고 인자로 받는다: 부르는 쪽(worker/run.py)이 rules.md graph_checkpoint_retention_days를 읽어 넘긴다.
    코드는 요청이 올 때 TTL(graph_pause_ttl_hours)이 지난 스레드를 지우므로 이 함수는 그 안전망이다. 여러 번 불러도 결과는 같다."""
    return conn.execute("SELECT purge_old_graph_checkpoints(%s)", [keep_days]).fetchall()[0][0]


def purge_watch_positions(conn) -> int:
    """더 지켜보지 않는 배의 30일 지난 감시·지도 위치(watch_positions)를 지우고 지운 줄 수를 돌려준다.

    규칙(30일, 지금 지켜보는 배는 남김)은 DB 함수 purge_old_watch_positions()에만 있다 (migrations/021). purge_old_queries와
    같이 pg_cron이 하루 한 번, pg_cron이 없는 로컬·채점용 DB는 워커가 실행마다 부른다. 여러 번 불러도 결과는 같다.
    """
    return conn.execute("SELECT purge_old_watch_positions()").fetchall()[0][0]


def purge_vessel_detail_usage(conn) -> int:
    """선박 자세히 보기 하루 횟수(vessel_detail_usage)에서 7일 지난 날짜 줄을 지우고 지운 줄 수를 돌려준다.

    규칙(7일)은 DB 함수 purge_old_vessel_detail_usage()에만 있다 (migrations/025). 다른 정리와 같이 pg_cron이 하루 한 번,
    pg_cron이 없는 로컬·채점용 DB는 워커가 실행마다 부른다. 여러 번 불러도 결과는 같다.
    """
    return conn.execute("SELECT purge_old_vessel_detail_usage()").fetchall()[0][0]


def recent_positions(conn, mmsi: int, limit: int = 20) -> list[dict]:
    """워커가 모은 같은 배의 위치 이력을 오래된 순으로 읽는다. anchor_wait 정지 시간 계산용 (services/signals.py).

    limit 20줄은 워커 6시간 간격이면 약 5일치다. 정지 기준 24시간(rules.md anchor_wait_hours)과
    그 앞에 움직이던 점까지 보기에 넉넉하다. 모양은 signals.compute의 history와 같게 {"at", "lat", "lng", "sog"}.

    대상 선박 이력(positions, 001)과 사용자 화물 배 이력(watch_positions, 017)을 같이 읽는다. 사용자 화물 배는
    watch_positions에만 쌓이므로(services/cargo._keep_position, worker/collector.py) 둘 다 봐야 anchor_wait를 계산할 수 있다.
    같은 보고 시각(ts)이 두 표에 다 있으면 positions 쪽 한 점만 쓴다(DISTINCT ON).
    위도·경도가 비어 있는 줄은 정지 판단에 쓸 수 없어 뺀다 (positions의 lat·lng는 NULL을 허용한다).
    """
    rows = conn.execute(
        """
        SELECT ts, lat, lng, sog FROM (
            SELECT DISTINCT ON (ts) ts, lat, lng, sog FROM (
                SELECT ts, lat, lng, sog, 0 AS pri FROM positions WHERE mmsi = %s AND lat IS NOT NULL AND lng IS NOT NULL
                UNION ALL
                SELECT ts, lat, lng, sog, 1 AS pri FROM watch_positions WHERE mmsi = %s
            ) both_tables
            ORDER BY ts DESC, pri
        ) deduped
        ORDER BY ts DESC LIMIT %s
        """,
        [mmsi, mmsi, limit],
    ).fetchall()
    return [{"at": ts, "lat": lat, "lng": lng, "sog": sog} for ts, lat, lng, sog in reversed(rows)]
