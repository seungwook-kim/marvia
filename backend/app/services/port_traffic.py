"""부산항 최근 입항 추세 (IMF PortWatch).

부산항 컨테이너선 입항 수의 최근 7일 평균을 28일 평균과 비교해 busy / normal / quiet로 이름 붙인다.
이것은 약 5일 늦은 참고 맥락이다. 지금의 혼잡도도, 화물 판정(verdict, signals)의 재료도 아니다.
판정 쪽 코드는 이 모듈을 import하지 않는다. 화면 카드(GET /api/port/busan/traffic)만 쓴다.

비율 계산은 코드가 하고, 이름을 가르는 경계(busy/quiet 비율)와 기간·캐시 시간은
skills/port-busan/rules.md 프론트매터에서 읽는다 (CLAUDE.md "임계값을 코드에 적지 않는다").

호출 횟수: 결과를 프로세스 안에 port_traffic_cache_hours(12시간) 동안 들고 있어 서버 하나가 12시간에 한 번만 부른다.
실패하면 마지막 성공값을 status partial로 돌려주고(SDD 8절), port_traffic_retry_minutes 동안은 다시 부르지 않는다.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from backend.app.clients.portwatch import SOURCE, DailyPortCalls, PortWatchClient, PortWatchError
from backend.app.observability.logging import log_failure
from backend.app.services.rules import rule

KST = timezone(timedelta(hours=9))  # 기준일이 며칠 전인지는 한국 날짜로 센다 (사용자가 한국에 있다)
ATTRIBUTION = "IMF PortWatch"  # IMF 이용 조건: 출처를 밝힌다 (clients/portwatch.py 설명)

# 프로세스 안 캐시. value는 마지막 성공 결과, at은 그 시각, failed_at은 마지막 실패 시각
_lock = threading.Lock()
_cache: dict = {"value": None, "at": None, "failed_at": None}


def reset_cache() -> None:
    """캐시를 비운다. 테스트가 쓴다."""
    with _lock:
        _cache.update(value=None, at=None, failed_at=None)


def _avg(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def classify(ratio: float | None) -> str | None:
    """최근 평균 / 기준 평균 비율에 이름을 붙인다. 경계는 rules.md port_traffic_busy_ratio, port_traffic_quiet_ratio."""
    if ratio is None:
        return None
    if ratio >= float(rule("port_traffic_busy_ratio")):
        return "busy"
    if ratio <= float(rule("port_traffic_quiet_ratio")):
        return "quiet"
    return "normal"


def summarize(rows: list[DailyPortCalls], fetched_at: datetime, today: date | None = None) -> dict:
    """하루치 목록(최신이 앞)에서 최근 평균, 기준 평균, 비율, 이름을 계산한다.

    최근 port_traffic_recent_days일과 기준 port_traffic_base_days일(최근 날 포함)을 날짜 순으로 자른다.
    컨테이너선 입항 수가 비어 있는 날은 평균에서 뺀다. 둘 중 하나라도 평균을 못 내면 ratio, level은 None.
    lag_days: 마지막 자료 날짜가 오늘(한국)보다 며칠 전인가. 화면이 "약 N일 전 자료"로 보여 준다."""
    recent_days = int(rule("port_traffic_recent_days"))
    base_days = int(rule("port_traffic_base_days"))
    ordered = sorted(rows, key=lambda r: r.day, reverse=True)
    counts = [r.portcalls_container for r in ordered]
    recent = [c for c in counts[:recent_days] if c is not None]
    base = [c for c in counts[:base_days] if c is not None]
    avg7, avg28 = _avg(recent), _avg(base)
    ratio = avg7 / avg28 if avg7 is not None and avg28 else None
    latest = ordered[0].day if ordered else None
    today = today or datetime.now(KST).date()
    return {
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "port": "busan",
        "fetched_at": fetched_at.isoformat(),
        "latest_date": latest.isoformat() if latest else None,
        "lag_days": (today - latest).days if latest else None,
        "recent_days": recent_days,
        "base_days": base_days,
        "days_used": len(base),
        "avg7": round(avg7, 1) if avg7 is not None else None,
        "avg28": round(avg28, 1) if avg28 is not None else None,
        "ratio": round(ratio, 3) if ratio is not None else None,
        "level": classify(ratio),
    }


def get_busan_traffic(now: datetime | None = None,
                      client_factory: Callable[[], PortWatchClient] = PortWatchClient) -> dict:
    """화면 카드용 결과. 캐시가 살아 있으면 부르지 않는다.

    돌려주는 status:
    - ok: 이번에 받았거나 캐시가 12시간 안이다
    - partial: 이번 호출이 실패해 마지막 성공값을 그대로 준다 (error에 종류가 붙는다)
    - unavailable: 성공한 적이 없고 이번에도 실패했다. 숫자 칸은 없다
    잠금 안에서 부르므로 동시에 요청이 와도 위로 나가는 호출은 하나다."""
    now = now or datetime.now(timezone.utc)
    cache_ttl = timedelta(hours=float(rule("port_traffic_cache_hours")))
    retry_wait = timedelta(minutes=float(rule("port_traffic_retry_minutes")))
    with _lock:
        value, at, failed_at = _cache["value"], _cache["at"], _cache["failed_at"]
        if value is not None and at is not None and now - at < cache_ttl:
            return {**value, "status": "ok"}
        if failed_at is not None and now - failed_at < retry_wait:
            # 방금 실패했다. 다시 부르지 않고 마지막 성공값(없으면 unavailable)을 준다
            return _fallback(value, "recent_failure")
        client = None
        try:
            client = client_factory()
            rows, fetched_at = client.daily(str(rule("portwatch_port_id")), int(rule("port_traffic_base_days")))
            if not rows:
                raise PortWatchError("empty", "PortWatch 자료가 비어 있습니다")
            result = summarize(rows, fetched_at)
        except PortWatchError as e:
            log_failure("portwatch", e.code)  # 종류만 남긴다 (log_failure 규칙)
            _cache["failed_at"] = now
            return _fallback(value, e.code)
        except Exception as e:  # noqa: BLE001 - 참고 카드 하나 때문에 요청이 500이 되면 안 된다
            log_failure("portwatch", type(e).__name__)
            _cache["failed_at"] = now
            return _fallback(value, "error")
        finally:
            if client is not None:
                client.close()
        _cache.update(value=result, at=now, failed_at=None)
        return {**result, "status": "ok"}


def _fallback(value: dict | None, code: str) -> dict:
    """실패했을 때의 답. 마지막 성공값이 있으면 partial, 없으면 숫자 없는 unavailable (SDD 8절 partial 경로)."""
    error = {"code": code}
    if value is not None:
        return {**value, "status": "partial", "error": error}
    return {"source": SOURCE, "attribution": ATTRIBUTION, "port": "busan", "status": "unavailable", "error": error}
