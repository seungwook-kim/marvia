"""부산항 최근 입항 추세 (IMF PortWatch).

부산항 컨테이너선 입항 수의 최근 7일 평균을 28일 평균과 비교해 busy / normal / quiet로 이름 붙인다.
이것은 약 5일 늦은 참고 맥락이다. 지금의 혼잡도도, 화물 판정(verdict, signals)의 재료도 아니다.
판정 쪽 코드는 이 모듈을 import하지 않는다. 화면 카드(GET /api/port/busan/traffic)만 쓴다.

비율 계산은 코드가 하고, 이름을 가르는 경계(busy/quiet 비율)와 기간·캐시 시간은
skills/port-busan/rules.md 프론트매터에서 읽는다 (CLAUDE.md "임계값을 코드에 적지 않는다").

호출 횟수: 결과를 프로세스 안에 port_traffic_cache_hours(12시간) 동안 들고 있어 서버 하나가 12시간에 한 번만 부른다.
실패하면 마지막 성공값을 status partial로 돌려주고(SDD 8절), port_traffic_retry_minutes 동안은 다시 부르지 않는다.
원천 호출은 잠금 밖에서 한 스레드만 한다. 그동안 온 요청은 마지막 성공값이 있으면 바로 그것을 받고,
없으면 그 호출이 끝나기를 기다려 같은 결과를 받는다 (10-08 QA E2-8: 잠금을 잡고 부르느라 동시 요청이 전부 원천을 기다렸다).
lag_days(며칠 전 자료인가)는 돌려줄 때마다 그 시각 기준으로 다시 센다. 캐시·partial 값이 받은 날 기준 숫자로 굳지 않게 한다.
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
# 앞 스레드의 원천 호출을 기다리는 상한(초). 클라이언트 타임아웃 15초 x (1 + 재시도 2회)보다 넉넉히 둔다 (clients/portwatch.py)
WAIT_SEC = 60

# 프로세스 안 캐시. value는 마지막 성공 결과, at은 그 시각, failed_at·error는 마지막 실패 시각과 종류,
# refreshing은 지금 원천을 부르는 중인지, done은 그 호출이 끝나면 켜지는 신호(기다리는 요청이 본다)
_lock = threading.Lock()
_cache: dict = {"value": None, "at": None, "failed_at": None, "error": None, "refreshing": False, "done": None}


def reset_cache() -> None:
    """캐시를 비운다. 테스트가 쓴다."""
    with _lock:
        _cache.update(value=None, at=None, failed_at=None, error=None, refreshing=False, done=None)


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


def _usable(rows: list[DailyPortCalls], today: date) -> list[DailyPortCalls]:
    """최신이 앞인 하루치 목록. 오늘(한국)보다 미래인 날짜와 같은 날짜가 두 번째로 나온 줄은 버린다 (10-08 QA E2-8).
    미래 날짜는 원천 오류라 lag_days가 음수가 되고, 같은 날이 겹치면 7일 평균이 실제로는 하루치가 된다.
    같은 날이 겹치면 컨테이너선 수가 있는 줄을 먼저 남긴다."""
    ordered = sorted((r for r in rows if r.day <= today), key=lambda r: (r.day, r.portcalls_container is not None),
                     reverse=True)
    seen: set[date] = set()
    kept = []
    for r in ordered:
        if r.day in seen:
            continue
        seen.add(r.day)
        kept.append(r)
    return kept


def _lag(latest_date: str | None, today: date) -> int | None:
    """마지막 자료 날짜가 today보다 며칠 전인가."""
    if not latest_date:
        return None
    return (today - date.fromisoformat(latest_date)).days


def summarize(rows: list[DailyPortCalls], fetched_at: datetime, today: date | None = None) -> dict:
    """하루치 목록에서 최근 평균, 기준 평균, 비율, 이름을 계산한다.

    최근 port_traffic_recent_days일과 기준 port_traffic_base_days일(최근 날 포함)을 날짜 순으로 자른다.
    미래 날짜·중복 날짜는 먼저 버린다 (_usable). 컨테이너선 입항 수가 비어 있는 날은 평균에서 뺀다.
    둘 중 하나라도 평균을 못 내면 ratio, level은 None.
    lag_days: 마지막 자료 날짜가 오늘(한국)보다 며칠 전인가. 화면이 "약 N일 전 자료"로 보여 준다."""
    recent_days = int(rule("port_traffic_recent_days"))
    base_days = int(rule("port_traffic_base_days"))
    today = today or datetime.now(KST).date()
    ordered = _usable(rows, today)
    counts = [r.portcalls_container for r in ordered]
    recent = [c for c in counts[:recent_days] if c is not None]
    base = [c for c in counts[:base_days] if c is not None]
    avg7, avg28 = _avg(recent), _avg(base)
    ratio = avg7 / avg28 if avg7 is not None and avg28 else None
    latest = ordered[0].day.isoformat() if ordered else None
    return {
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "port": "busan",
        "fetched_at": fetched_at.isoformat(),
        "latest_date": latest,
        "lag_days": _lag(latest, today),
        "recent_days": recent_days,
        "base_days": base_days,
        "days_used": len(base),
        "avg7": round(avg7, 1) if avg7 is not None else None,
        "avg28": round(avg28, 1) if avg28 is not None else None,
        "ratio": round(ratio, 3) if ratio is not None else None,
        "level": classify(ratio),
    }


def _fresh(value: dict, now: datetime) -> dict:
    """저장된 결과의 lag_days를 now(한국 날짜) 기준으로 다시 센 사본 (10-08 QA E2-8: partial 값이 받은 날 숫자로 굳었다)."""
    return {**value, "lag_days": _lag(value.get("latest_date"), now.astimezone(KST).date())}


def _answer(now: datetime, cache_ttl: timedelta) -> dict:
    """지금 캐시 상태로 낼 답 (잠금 안에서 부른다). 살아 있는 값이면 ok, 아니면 마지막 실패를 붙인 partial·unavailable."""
    value, at = _cache["value"], _cache["at"]
    if value is not None and at is not None and now - at < cache_ttl:
        return {**_fresh(value, now), "status": "ok"}
    return _fallback(value, _cache["error"] or "error", now)


def get_busan_traffic(now: datetime | None = None,
                      client_factory: Callable[[], PortWatchClient] = PortWatchClient) -> dict:
    """화면 카드용 결과. 캐시가 살아 있으면 부르지 않는다.

    돌려주는 status:
    - ok: 이번에 받았거나 캐시가 12시간 안이다. 다른 요청이 갱신 중일 때 받은 지난 값도 ok다(곧 새 값으로 바뀌고,
      실패한 것이 아니라 "최신 갱신에 실패" 문구를 달면 틀린 말이 된다)
    - partial: 이번 호출이 실패해 마지막 성공값을 그대로 준다 (error에 종류가 붙는다)
    - unavailable: 성공한 적이 없고 이번에도 실패했다. 숫자 칸은 없다
    원천 호출은 한 스레드만 하고 잠금 밖에서 한다 (모듈 설명). now는 캐시 수명·재시도 대기·lag_days 모두에 쓴다."""
    now = now or datetime.now(timezone.utc)
    cache_ttl = timedelta(hours=float(rule("port_traffic_cache_hours")))
    retry_wait = timedelta(minutes=float(rule("port_traffic_retry_minutes")))
    with _lock:
        value, at, failed_at = _cache["value"], _cache["at"], _cache["failed_at"]
        if value is not None and at is not None and now - at < cache_ttl:
            return {**_fresh(value, now), "status": "ok"}
        if failed_at is not None and now - failed_at < retry_wait:
            # 방금 실패했다. 다시 부르지 않고 마지막 성공값(없으면 unavailable)을 준다
            return _fallback(value, "recent_failure", now)
        if _cache["refreshing"]:
            if value is not None:
                return {**_fresh(value, now), "status": "ok"}  # 지난 값을 바로 준다. 갱신은 앞 스레드가 한다
            done = _cache["done"]
        else:
            done = None
            _cache["refreshing"] = True
            _cache["done"] = threading.Event()
    if done is not None:
        # 받은 적이 없는데 다른 스레드가 부르는 중: 같은 호출을 또 내지 않고 그 결과를 기다린다
        done.wait(WAIT_SEC)
        with _lock:
            return _answer(now, cache_ttl)

    result, error = None, None
    client = None
    try:
        client = client_factory()
        rows, fetched_at = client.daily(str(rule("portwatch_port_id")), int(rule("port_traffic_base_days")))
        if not rows:
            raise PortWatchError("empty", "PortWatch 자료가 비어 있습니다")
        result = summarize(rows, fetched_at, today=now.astimezone(KST).date())
    except PortWatchError as e:
        log_failure("portwatch", e.code)  # 종류만 남긴다 (log_failure 규칙)
        error = e.code
    except Exception as e:  # noqa: BLE001 - 참고 카드 하나 때문에 요청이 500이 되면 안 된다
        log_failure("portwatch", type(e).__name__)
        error = "error"
    finally:
        if client is not None:
            client.close()
        with _lock:
            if result is not None:
                _cache.update(value=result, at=now, failed_at=None, error=None)
            else:
                _cache.update(failed_at=now, error=error or "error")
            _cache["refreshing"] = False
            done = _cache["done"]
            _cache["done"] = None
            value = _cache["value"]
        if done is not None:
            done.set()
    if result is not None:
        return {**result, "status": "ok"}
    return _fallback(value, error or "error", now)


def _fallback(value: dict | None, code: str, now: datetime | None = None) -> dict:
    """실패했을 때의 답. 마지막 성공값이 있으면 partial(lag_days는 지금 기준), 없으면 숫자 없는 unavailable (SDD 8절 partial 경로)."""
    error = {"code": code}
    if value is not None:
        return {**(_fresh(value, now) if now else value), "status": "partial", "error": error}
    return {"source": SOURCE, "attribution": ATTRIBUTION, "port": "busan", "status": "unavailable", "error": error}
