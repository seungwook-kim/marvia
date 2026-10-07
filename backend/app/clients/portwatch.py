"""IMF PortWatch 항구별 일일 입항 집계 클라이언트 (10-08 결정, 부산항 입항 추세 카드).

부르는 것: ArcGIS FeatureServer의 Daily_Ports_Data 표. 키가 필요 없는 공개 자료다.
IMF 이용 조건상 출처 "IMF PortWatch"를 밝히면 쓸 수 있고, 상업적 재사용은 허락이 필요하다.
이 프로젝트는 비상업 포트폴리오라 출처 표기로 쓴다 (화면 카드와 응답 source에 남긴다).

알아 둘 점:
- 자료는 위성 AIS를 IMF가 하루 단위로 묶은 것이고 약 5일 늦게, 주 단위로 갱신된다.
  그래서 "지금 부산이 붐빈다"가 아니라 "지난주 부산 입항이 평소보다 많았다" 수준의 맥락이다
- date 칸이 epoch 밀리초로 올 때도, 문자열로 올 때도 있어 둘 다 받는다
- ArcGIS는 잘못된 질의에도 HTTP 200에 {"error": {...}}를 돌려준다. 그 모양도 실패로 본다

판정(verdict, signals)에는 넣지 않는다. 값은 원본 그대로 돌려주고 계산은 services/port_traffic.py가 한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from backend.app.clients.replay import replay_enabled
from backend.app.clients.retry import with_retry

BASE_URL = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Ports_Data/FeatureServer/0/query"
OUT_FIELDS = "date,portcalls,portcalls_container,import_container,export_container"
TIMEOUT_SEC = float(os.environ.get("PORTWATCH_TIMEOUT_SEC", 15))  # 다른 외부 클라이언트(mof.py)와 같은 기본값
CONNECT_TIMEOUT_SEC = 3.0  # 연결이 안 되는 곳은 오래 기다려도 안 된다 (mof.py와 같은 이유)
SOURCE = "imf_portwatch"

# REPLAY_MODE와 테스트가 읽는 가공 응답. 실제 응답이 아니라 형식만 같은 값이다 (CLAUDE.md fixture 규칙)
FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "portwatch" / "busan_daily.json"


class PortWatchError(Exception):
    """PortWatch 호출 실패. MofError·ShipFinderError와 같은 모양으로 code, message, retryable을 갖는다."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = code in ("timeout", "busy")

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True)
class DailyPortCalls:
    """하루치 입항 집계 1줄. 값이 비어 있으면 None으로 둔다 (계산하는 쪽이 건너뛴다)."""

    day: date
    portcalls: int | None
    portcalls_container: int | None
    import_container: float | None
    export_container: float | None


def parse_day(value) -> date | None:
    """date 칸을 날짜로. epoch 밀리초(숫자)와 'YYYY-MM-DD...' 문자열을 둘 다 받는다. 못 읽으면 None.

    epoch는 UTC 자정 기준으로 오므로 UTC 날짜로 읽는다 (한국 시각으로 바꾸면 하루가 밀릴 수 있다)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return parse_day(int(text))
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _num(value) -> float | None:
    """숫자 칸. 숫자가 아니거나 음수면 None (집계값이 음수일 수는 없다)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value >= 0 else None


def _int(value) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def parse_features(body: dict) -> list[DailyPortCalls]:
    """ArcGIS 응답 dict를 하루치 목록으로. 날짜를 못 읽는 줄은 버리고, 최신 날짜가 앞에 오게 정렬한다.

    {"error": {...}}이거나 features가 없으면 PortWatchError("error")를 던진다 (모듈 설명 참고)."""
    if not isinstance(body, dict):
        raise PortWatchError("error", "PortWatch 응답 모양이 다릅니다")
    if "error" in body:
        err = body.get("error") or {}
        code = err.get("code") if isinstance(err, dict) else None
        raise PortWatchError("error", f"PortWatch 질의 오류 {code}")
    features = body.get("features")
    if not isinstance(features, list):
        raise PortWatchError("error", "PortWatch 응답에 features가 없습니다")
    rows: list[DailyPortCalls] = []
    for feature in features:
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            continue
        day = parse_day(attrs.get("date"))
        if day is None:
            continue
        rows.append(DailyPortCalls(
            day=day,
            portcalls=_int(attrs.get("portcalls")),
            portcalls_container=_int(attrs.get("portcalls_container")),
            import_container=_num(attrs.get("import_container")),
            export_container=_num(attrs.get("export_container")),
        ))
    rows.sort(key=lambda r: r.day, reverse=True)
    return rows


def replay_transport() -> httpx.MockTransport:
    """REPLAY_MODE일 때 쓰는 transport. 어떤 질의든 가공 fixture를 돌려준다 (clients/replay.py와 같은 방식)."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"),
                              headers={"content-type": "application/json"})

    return httpx.MockTransport(handle)


class PortWatchClient:
    """Daily_Ports_Data 한 항구의 최근 N일을 받는다. 키가 없어 만들 때 실패하지 않는다."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        # 테스트가 transport를 직접 주면 그쪽이 먼저다. 아니면 REPLAY_MODE에서 fixture를 읽는다 (SDD 6.1)
        if transport is None and replay_enabled():
            transport = replay_transport()
        self._http = httpx.Client(timeout=httpx.Timeout(TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC), transport=transport)

    def close(self) -> None:
        self._http.close()

    def daily(self, port_id: str, days: int) -> tuple[list[DailyPortCalls], datetime]:
        """최근 days일의 하루치 집계와 받은 시각(fetched_at)을 돌려준다.

        타임아웃·5xx는 2회 더 부른다 (retry.with_retry, SDD 8절). 그래도 안 되면 PortWatchError.
        port_id는 rules.md portwatch_port_id에서 온 값만 넣는다. 사용자 입력을 질의에 넣지 않는다."""
        params = {
            "where": f"portid='{port_id}'",
            "outFields": OUT_FIELDS,
            "orderByFields": "date DESC",
            "resultRecordCount": days,
            "f": "json",
        }

        def call() -> httpx.Response:
            resp = self._http.get(BASE_URL, params=params)
            resp.raise_for_status()
            return resp

        try:
            resp = with_retry(call, name="portwatch:Daily_Ports_Data")
        except httpx.TimeoutException as e:
            raise PortWatchError("timeout", "PortWatch 응답 시간 초과") from e
        except httpx.HTTPStatusError as e:
            code = "busy" if e.response.status_code == 429 else "error"
            raise PortWatchError(code, f"PortWatch HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise PortWatchError("error", f"PortWatch 호출 실패: {type(e).__name__}") from e
        fetched_at = datetime.now(timezone.utc)
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise PortWatchError("error", "PortWatch 응답이 JSON이 아닙니다") from e
        return parse_features(body), fetched_at
