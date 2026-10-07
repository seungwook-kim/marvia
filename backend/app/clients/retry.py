"""외부 API 재시도.

타임아웃과 5xx만 백오프를 두고 다시 부른다 (SDD 8절: 재시도 2회). 끝내 실패하면 원래 예외를
그대로 올려서 호출부가 partial로 간다. 4xx와 인증 실패는 다시 불러도 같으므로 재시도하지 않고,
빈도 초과(ShipFinder 23·29)는 캐시가 받는다.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

# 값은 환경변수로 바꿀 수 있다. 운영에서 외부 API가 느려질 때 코드를 고치지 않으려는 것이다
MAX_ATTEMPTS = int(os.environ.get("HTTP_MAX_ATTEMPTS", 3))  # 최초 1회 + 재시도 2회 (SDD 8절)
BACKOFF_SEC = float(os.environ.get("HTTP_BACKOFF_SEC", 0.5))  # 시도 사이 대기. 2회차 0.5초, 3회차 1.0초
RETRYABLE_STATUS = range(500, 600)

logger = logging.getLogger(__name__)


def is_retryable(error: Exception) -> bool:
    if isinstance(error, httpx.TimeoutException):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRYABLE_STATUS
    return isinstance(error, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError))


def with_retry(call: Callable[[], T], *, name: str, attempts: int = MAX_ATTEMPTS, backoff: float | None = None,
               stats: dict | None = None) -> T:
    """call을 부르고, 되살아날 만한 실패면 백오프를 두고 다시 부른다.

    Args:
        call: 인자 없는 호출. 보통 lambda로 감싼다
        name: 로그에 남길 이름. 예: shipfinder:VesselSearch
        attempts: 최초 호출을 포함한 총 시도 횟수
        backoff: 첫 대기 시간(초). 시도마다 곱절로 늘린다. 생략하면 모듈 기본값(테스트는 0으로 바꾼다)
        stats: 주면 stats["attempts"]에 실제로 부른 횟수를 적는다. 성공·실패 모두. Langfuse span metadata용이다
    """
    wait_base = BACKOFF_SEC if backoff is None else backoff
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        if stats is not None:
            stats["attempts"] = attempt
        try:
            return call()
        except Exception as e:
            if not is_retryable(e) or attempt == attempts:
                raise
            last = e
            wait = wait_base * (2 ** (attempt - 1))
            logger.warning("%s 실패(%s), %.1f초 뒤 재시도 %d/%d", name, type(e).__name__, wait, attempt + 1, attempts)
            time.sleep(wait)
    raise last  # 여기 오지 않는다. 마지막 시도는 위에서 raise된다
