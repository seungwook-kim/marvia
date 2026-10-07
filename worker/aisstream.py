"""aisstream.io 실시간 AIS 시험 수집(파일럿). 지켜보는 배의 위치를 watch_positions에 쌓는다.

왜: ShipFinder는 하루 50회 한도라 지켜보는 배 위치가 6시간에 한 번뿐이다. aisstream.io는 무료 웹소켓 AIS라
부산 근처에서 받히면 더 촘촘한 경로를 그릴 수 있다. 다만 이용 약관과 부산 수신 범위(지상국 커버리지)를
확인하지 않아서 기본은 꺼져 있고, AISSTREAM_KEY가 있을 때만 돈다 (.env.example).

    python -m worker.aisstream          # 키가 있으면 계속 돈다. Ctrl+C로 끈다
    AISSTREAM_ENABLED=true + AISSTREAM_KEY 가 있으면 worker/run.py 주기 실행이 이 수집을 뒤 스레드로 같이 띄운다

하는 일:
1. 지켜보는 배 MMSI를 읽는다. worker/collector.SELECT_WATCHED를 그대로 쓴다 (읽기만, 같은 대상)
   상한 rules.md aisstream_max_mmsi(200)
2. wss://stream.aisstream.io/v0/stream에 붙고 3초 안에 구독 메시지를 보낸다 (aisstream 규칙: 늦으면 끊는다)
   범위는 rules.md aisstream_bbox_busan(기본) 또는 aisstream_bbox_wide(AISSTREAM_BBOX=wide)
3. PositionReport가 오면 순수 함수 parse_position으로 한 줄을 만들고
   worker/collector.INSERT_WATCH_POSITION과 같은 SQL로 넣는다. 같은 (mmsi, ts)는 한 줄 (UNIQUE 제약)
4. rules.md aisstream_refresh_minutes마다 지켜보는 배 목록을 다시 읽고, 바뀌었으면 다시 붙어 구독을 갈아 낀다
끊기면 지수 백오프로 다시 붙는다. 어떤 예외에도 프로세스를 죽이지 않는다. 로그에는 오류 종류만 남긴다(키·메시지 원문 금지).

메시지 안의 글자(선명, 목적지 등)는 데이터일 뿐이고 저장하지도 않는다 (CLAUDE.md "AIS 목적지 문자열은 데이터").
판정은 하지 않는다. 판정 쪽이 쓰는 위치는 여전히 ShipFinder다.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from backend.app.services.rules import rule
from worker.collector import INSERT_WATCH_POSITION, SELECT_WATCHED

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
SOURCE = "aisstream"
# 아래 값은 판정 임계값이 아니라 연결 운영값이라 코드에 둔다 (mof.py CIRCUIT_OPEN_SEC와 같은 성격)
OPEN_TIMEOUT_SEC = 10.0  # 연결 수립 대기. 구독은 연결 직후 바로 보내 aisstream의 3초 제한을 지킨다
BACKOFF_BASE_SEC = 2.0  # 첫 재접속 대기. 실패할 때마다 두 배
BACKOFF_MAX_SEC = 300.0  # 재접속 대기 상한 5분
IDLE_WAIT_SEC = 60.0  # 지켜보는 배가 없을 때 다시 읽기까지 대기 (빈 구독은 범위 안 모든 배를 받게 되어 붙지 않는다)

# AIS 규격의 "값 없음" 표시 (ITU-R M.1371). 이 값이면 저장하지 않거나 None으로 둔다
_LAT_NA, _LNG_NA, _SOG_NA, _COG_NA = 91.0, 181.0, 102.3, 360.0


def _log(event: str, **fields) -> None:
    """한 줄 JSON 로그 (worker/run.py와 같은 모양). 오류는 종류(kind)만 넣는다."""
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
                     ensure_ascii=False, default=str), flush=True)


def enabled() -> bool:
    """worker/run.py가 뒤 스레드를 띄울지. AISSTREAM_KEY가 있고 AISSTREAM_ENABLED=true일 때만 (기본 꺼짐, 파일럿)."""
    return bool(os.environ.get("AISSTREAM_KEY", "").strip()) and \
        os.environ.get("AISSTREAM_ENABLED", "false").strip().lower() in ("1", "true", "on", "yes")


def bounding_box(mode: str | None = None) -> list[list[float]]:
    """구독 범위 [[남서 위도, 경도], [북동 위도, 경도]]. rules.md aisstream_bbox_busan(기본) 또는 _wide.

    mode는 AISSTREAM_BBOX 환경변수(busan|wide). 지켜보는 배가 중국·일본에서 출발했으면 wide가 더 많이 받는다."""
    mode = (mode or os.environ.get("AISSTREAM_BBOX", "busan")).strip().lower()
    box = rule("aisstream_bbox_wide" if mode == "wide" else "aisstream_bbox_busan")
    return [[float(box[0][0]), float(box[0][1])], [float(box[1][0]), float(box[1][1])]]


def subscription(key: str, mmsis: list[int], box: list[list[float]]) -> dict:
    """aisstream 구독 메시지. 지켜보는 배만(FiltersShipMMSI), 위치 보고만(PositionReport) 받는다.
    MMSI는 aisstream 규칙상 문자열 9자리로 보낸다."""
    return {
        "APIKey": key,
        "BoundingBoxes": [box],
        "FiltersShipMMSI": [str(m) for m in mmsis],
        "FilterMessageTypes": ["PositionReport"],
    }


def parse_time_utc(value: Any) -> datetime | None:
    """MetaData.time_utc를 datetime으로. 예: '2026-10-07 01:23:45.123456789 +0000 UTC'.

    Go 형식이라 소수점이 9자리까지 온다. 파이썬은 6자리까지라 잘라 읽는다. 못 읽으면 None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[:-4]
    parts = text.split(" ")
    if len(parts) < 2:
        return None
    day, clock = parts[0], parts[1]
    if "." in clock:
        whole, frac = clock.split(".", 1)
        clock = f"{whole}.{(frac + '000000')[:6]}"
    offset = parts[2] if len(parts) > 2 else "+0000"
    try:
        return datetime.strptime(f"{day} {clock} {offset}", "%Y-%m-%d %H:%M:%S.%f %z" if "." in clock
                                 else "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_position(raw: Any, watched: set[int], fetched_at: datetime) -> tuple | None:
    """aisstream 메시지 하나를 watch_positions 한 줄(INSERT_WATCH_POSITION 순서)로 바꾼다. 순수 함수.

    돌려주는 값: (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat) 또는 None.
    None인 경우: JSON이 아님, PositionReport가 아님, MMSI가 지켜보는 목록에 없음, 시각·좌표를 못 읽음.
    문자열 칸(ShipName, Destination 등)은 읽지도 저장하지도 않는다. 그 안의 문장은 지시가 아니라 데이터다 (SDD 7절).
    sog 102.3, cog 360은 AIS의 '값 없음'이라 None으로 둔다."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or raw.get("MessageType") != "PositionReport":
        return None
    body = raw.get("Message")
    report = body.get("PositionReport") if isinstance(body, dict) else None
    meta = raw.get("MetaData")
    if not isinstance(report, dict) or not isinstance(meta, dict):
        return None

    mmsi = report.get("UserID", meta.get("MMSI"))
    if isinstance(mmsi, bool) or not isinstance(mmsi, int) or mmsi not in watched:
        return None
    if report.get("Valid") is False:
        return None
    ts = parse_time_utc(meta.get("time_utc"))
    lat, lng = _number(report.get("Latitude")), _number(report.get("Longitude"))
    if ts is None or lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or lat == _LAT_NA or lng == _LNG_NA:
        return None
    sog, cog = _number(report.get("Sog")), _number(report.get("Cog"))
    if sog is not None and (sog < 0 or sog >= _SOG_NA):
        sog = None
    if cog is not None and (cog < 0 or cog >= _COG_NA):
        cog = None
    nav = report.get("NavigationalStatus")
    navistat = nav if isinstance(nav, int) and not isinstance(nav, bool) and 0 <= nav <= 15 else None
    return (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat)


def load_watched(conn) -> list[int]:
    """지켜보는 배 MMSI 목록. collector.SELECT_WATCHED와 같은 대상이고 오래 못 받은 배가 앞이다. 상한 rules.md aisstream_max_mmsi."""
    limit = int(rule("aisstream_max_mmsi"))
    out: list[int] = []
    for row in conn.execute(SELECT_WATCHED).fetchall():
        if row[0] is not None and row[0] not in out:
            out.append(int(row[0]))
        if len(out) >= limit:
            break
    return out


def handle_message(conn, raw: Any, watched: set[int], fetched_at: datetime | None = None) -> bool:
    """메시지 하나를 읽어 저장한다. 새로 들어간 줄이면 True. 저장 대상이 아니거나 이미 있던 보고면 False."""
    row = parse_position(raw, watched, fetched_at or datetime.now(timezone.utc))
    if row is None:
        return False
    cur = conn.execute(INSERT_WATCH_POSITION, row)
    return (getattr(cur, "rowcount", 0) or 0) > 0


def _default_ws_connect(url: str):
    """websockets 라이브러리로 연결한다. 테스트가 websockets 없이도 돌도록 함수 안에서 import한다."""
    import websockets

    return websockets.connect(url, open_timeout=OPEN_TIMEOUT_SEC, ping_interval=20, ping_timeout=20,
                              max_size=2 ** 20)


async def run_session(key: str, conn, mmsis: list[int], box: list[list[float]], refresh_sec: float,
                      ws_connect: Callable = _default_ws_connect, stats: dict | None = None) -> dict:
    """한 번 붙어서 refresh_sec 동안 받는다. 시간이 다 되면 정상으로 돌아간다(목록을 다시 읽으러).

    연결이 끊기거나 예외가 나면 그대로 올린다. 다시 붙는 것은 run_forever가 한다."""
    stats = stats if stats is not None else {"received": 0, "saved": 0}
    watched = set(mmsis)
    deadline = time.monotonic() + refresh_sec
    async with ws_connect(STREAM_URL) as ws:
        # aisstream은 연결 뒤 3초 안에 구독을 받지 못하면 끊는다. 다른 일 없이 바로 보낸다
        await ws.send(json.dumps(subscription(key, mmsis, box)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return stats
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return stats
            stats["received"] += 1
            try:
                if handle_message(conn, raw, watched):
                    stats["saved"] += 1
            except Exception as e:  # noqa: BLE001 - 한 줄 저장 실패로 연결을 끊지 않는다
                _log("aisstream_save_failed", kind=type(e).__name__)


def _backoff(failures: int) -> float:
    """재접속 대기. 2초에서 두 배씩, 상한 5분, 몰림을 피하려고 0~1초 흔들기."""
    return min(BACKOFF_BASE_SEC * (2 ** max(failures - 1, 0)), BACKOFF_MAX_SEC) + random.random()


async def run_forever(key: str, connect_db: Callable | None = None, stop: threading.Event | None = None,
                      ws_connect: Callable = _default_ws_connect) -> None:
    """멈추라는 신호가 올 때까지 붙고, 받고, 끊기면 다시 붙는다. 어떤 예외도 밖으로 내보내지 않는다."""
    if connect_db is None:
        from backend.app.db import connect as connect_db  # DATABASE_URL로 붙는다 (autocommit)
    stop = stop or threading.Event()
    refresh_sec = float(rule("aisstream_refresh_minutes")) * 60
    box = bounding_box()
    failures = 0
    conn = None
    while not stop.is_set():
        try:
            if conn is None:
                conn = connect_db()
            mmsis = load_watched(conn)
            if not mmsis:
                await asyncio.sleep(IDLE_WAIT_SEC)
                continue
            stats = await run_session(key, conn, mmsis, box, refresh_sec, ws_connect=ws_connect)
            _log("aisstream_session", watched=len(mmsis), received=stats["received"], saved=stats["saved"])
            failures = 0
        except Exception as e:  # noqa: BLE001 - 파일럿 수집이 워커를 죽이면 안 된다
            failures += 1
            _log("aisstream_error", kind=type(e).__name__, failures=failures)
            if conn is not None and getattr(conn, "closed", False):
                conn = None  # DB가 끊겼으면 다음 차례에 다시 붙는다
            await asyncio.sleep(_backoff(failures))
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def start_in_thread() -> threading.Thread | None:
    """worker/run.py 주기 실행이 부른다. 꺼져 있으면(enabled() False) 아무것도 안 하고 None."""
    if not enabled():
        return None
    key = os.environ["AISSTREAM_KEY"].strip()
    thread = threading.Thread(target=lambda: asyncio.run(run_forever(key)), daemon=True, name="aisstream")
    thread.start()
    _log("aisstream_started", bbox=os.environ.get("AISSTREAM_BBOX", "busan"))
    return thread


def main() -> int:
    """python -m worker.aisstream. 키가 없으면 안내만 하고 끝낸다 (파일럿, 기본 꺼짐)."""
    key = os.environ.get("AISSTREAM_KEY", "").strip()
    if not key:
        _log("aisstream_off", message="AISSTREAM_KEY가 없어 끝낸다. 약관·부산 수신 범위 미확인 파일럿이다")
        return 0
    _log("aisstream_started", bbox=os.environ.get("AISSTREAM_BBOX", "busan"))
    try:
        asyncio.run(run_forever(key))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
