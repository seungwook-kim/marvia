"""입항 예정 선박 한 척 자세히 보기 (docs/specs/chat-search.md "선박 자세히 보기" 절).

선박 표(직전 기항지·입출항 예정)에서 선명을 누르면 부른다. 새 데이터 소스는 없고 기존 MCP 도구 넷을 묶는다.
- 해수부: 입항 예정·실적, 접안 시설, 관제 이벤트 (get_port_schedule, 호출부호 우선)
- ShipFinder: 호출부호로 MMSI를 찾고(get_vessel_candidates) 실시간 AIS (get_live_position)
- searoute: 부산까지 남은 바닷길과 지금 속력으로 걸리는 시간 (estimate_distance)
판정은 하지 않는다. 도구 하나가 실패해도 나머지는 보여 주고, 실패는 errors에 출처별로 남긴다 (SDD 8절 partial 원칙).
위치가 오래됐는지는 rules.md position_stale_hours로 코드가 정한다 (화면이 정하지 않는다).

호출 아끼기 (ShipFinder 무료 키는 하루 50회, 다 쓰면 판정까지 그 프로세스 전체가 못 쓴다: clients/shipfinder.quota_blocked)
- 같은 배는 10분(CACHE_SEC) 동안 다시 부르지 않는다. 도구가 실패한 결과는 캐시하지 않는다
  (다시 눌렀을 때 실패를 10분 동안 되풀이하지 않게, 10-08 QA E2-2)
- 캐시 밖 새 조회는 사용자당 하루 rules.md vessel_detail_daily_limit번까지다 (take_daily_slot).
  횟수는 DB(vessel_detail_usage)에 세고, 없거나 실패하면 메모리로 센다.
  해수부와 후보 검색이 둘 다 실패해 아무것도 못 받았으면 쓴 횟수를 돌려준다 (give_back, E2-2)
- 같은 배를 동시에 여러 번 누르면 첫 요청만 외부를 부르고 나머지는 그 결과를 같이 받는다. 횟수도 안 쓴다 (E2-3)
- 캐시는 프로세스 메모리라 서버를 다시 띄우면 빈다. 그래서 사용자가 최근에 본 배를 DB(vessel_detail_usage.recent, 029)에도
  남겨, 캐시 수명 안에 같은 배를 다시 보면 횟수를 쓰지 않는다 (E2-9). 응답 remaining은 오늘 남은 횟수다(모르면 null)
- 선명만 있으면 가장 비슷한 후보 1척만 상세 조회한다 (rules.md vessel_detail_lookups, E2-1).
  국적은 clients/shipfinder가 rules.md flag_cache_ttl_days(30일) 캐시를 먼저 본다

배 고르기: 호출부호가 있으면 services/vessel_pick.py 규칙대로 MMSI가 하나로 정해질 때만 보여 준다.
선명만 있으면 가장 비슷한 후보를 보여 주되 vessel.match = "name_similar"로 "이름이 비슷한 배"임을 알린다.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.app.db import connect
from backend.app.observability.logging import log_failure
from backend.app.services.rules import rule
# MCP 도구 4종은 /mcp를 거쳐 부른다 (services/mcp_client.py).
# route_points는 지도에 그릴 항로 좌표를 만드는 도우미라 MCP 도구가 아니다. 그래서 직접 부른다
from backend.app.services.mcp_client import (
    estimate_distance, get_live_position, get_port_schedule, get_vessel_candidates,
)
from backend.app.services.vessel_pick import norm_sign, single
from mcp_server.tools.distance import route_points

CACHE_SEC = 600  # services/chat.py CACHE_SEC와 같다 (입항 예정 캐시와 같은 수명)
# 캐시에 두는 배 수 상한. 선명을 조금씩 바꿔 불러도 메모리가 끝없이 늘지 않게 가장 오래 안 쓴 것부터 버린다(LRU)
CACHE_MAX = 256
_cache: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
_cache_lock = threading.Lock()

# 진행 중인 조회 (E2-3). 열쇠마다 첫 요청이 Future를 걸고, 같은 열쇠의 다음 요청은 그 결과를 기다려 같이 받는다
_inflight: dict[tuple[str, str], Future] = {}
_inflight_lock = threading.Lock()
WAIT_SEC = 120  # 앞 요청을 기다리는 상한. 도구마다 타임아웃·재시도가 있어 보통 이보다 훨씬 빨리 끝난다

# 배를 고르다 못 정한 결과. 도구 실패가 아니라 사실이라 캐시해도 된다 (_choose)
PICK_CODES = {"not_found", "ambiguous", "stale_only"}


def _clean_name(value: str | None) -> str | None:
    """선명 맞춤: 앞뒤 공백을 떼고 안쪽 공백을 하나로. 선명은 낱말 사이 공백이 뜻이 있어 지우지 않는다. 비면 None."""
    return re.sub(r"\s+", " ", (value or "").strip()) or None


def clean(call_sign: str | None, ship_name: str | None) -> tuple[str | None, str | None]:
    """요청 값 맞춤. 호출부호는 vessel_pick.norm_sign(공백 모두 제거, 대문자)과 같게, 선명은 _clean_name.
    공백뿐인 값은 None이 된다. 라우터가 둘 다 None이면 400을 낸다 (10-08 QA E2-4: 공백뿐인 값이 200으로 외부까지 갔다).
    도구에도 이 값을 넘겨 캐시 열쇠와 ShipFinder 검색어가 같은 값을 쓰게 한다."""
    return norm_sign(call_sign) or None, _clean_name(ship_name)


def _key(call_sign: str | None, ship_name: str | None) -> tuple[str, str]:
    """캐시 열쇠. 호출부호는 norm_sign(공백 모두 제거, 대문자), 선명은 안쪽 공백 하나로 맞춘 대문자.
    호출부호 맞춤을 배 고르기(vessel_pick.norm_sign)와 같게 둔다 (10-08 QA E2-4: " qa 001 "이 캐시를 못 찾아 429가 났다).
    호출부호가 있으면 그것으로만 찾으므로(detail) 선명은 열쇠에서 뺀다."""
    cs = norm_sign(call_sign)
    return (cs, "") if cs else ("", (_clean_name(ship_name) or "").upper())


def _db_key(key: tuple[str, str]) -> str:
    """DB(vessel_detail_usage.recent)에 남길 열쇠 글자. 호출부호면 그대로, 선명이면 "name:" 앞붙임 (029)."""
    return key[0] or f"name:{key[1]}"


def _cache_get(key: tuple[str, str]) -> dict | None:
    """수명 안의 캐시 값. 쓰면 맨 뒤(최근)로 옮긴다. 수명이 지난 값은 지운다."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if time.monotonic() - hit[0] >= CACHE_SEC:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return hit[1]


def _cache_put(key: tuple[str, str], value: dict) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def cached(call_sign: str | None, ship_name: str | None) -> bool:
    """캐시에 있어 외부 호출 없이 답할 수 있나 (이 프로세스 메모리 기준)."""
    return _cache_get(_key(call_sign, ship_name)) is not None


# 하루 횟수 상한. 하루 한도를 지키는 마지막 문은 ShipFinder의 38 응답(quota_blocked)이고,
# 이 상한은 한 사용자가 그 한도를 혼자 다 쓰지 못하게 하는 몫이다.
# DB가 있으면 vessel_detail_usage에 세고, 없거나 DB가 실패하면 아래 프로세스 메모리로 센다 (서버를 다시 띄우면 0부터)
_KST = ZoneInfo("Asia/Seoul")
ANON = "anonymous"  # 로그인 없이 쓰는 경우(인증 꺼짐)는 모두 한 칸을 같이 쓴다
_daily: dict[str, int] = {}
_daily_on: date | None = None
_daily_lock = threading.Lock()


@dataclass(frozen=True)
class Slot:
    """하루 횟수에서 쓴 한 번. 돌려줄 때(give_back) 어디에 셌는지 알아야 해서 남긴다.
    store: "db"(vessel_detail_usage) / "memory"(DB 없음·실패) / "free"(세지 않는 조회).
    used: 이번까지 오늘 쓴 횟수. 모르면 None."""
    owner: str
    day: date
    store: str
    used: int | None = None


class DailyLimitReached(Exception):
    """오늘 몫을 다 썼다. 라우터가 429로 바꾼다."""


# 오늘 몫이 남았을 때만 1을 더하고 새 값을 돌려준다. 다 썼으면 WHERE가 막아 줄이 안 나온다.
# 읽고 더해 쓰지 않고 한 문장이라 두 요청이 같이 와도 상한을 넘지 않는다
USAGE_SQL = """INSERT INTO vessel_detail_usage (user_key, day, count) VALUES (%s, %s, 1)
               ON CONFLICT (user_key, day) DO UPDATE SET count = vessel_detail_usage.count + 1
               WHERE vessel_detail_usage.count < %s
               RETURNING count"""
# 외부 호출이 모두 실패한 조회의 한 번을 돌려준다 (E2-2). 0 밑으로는 내리지 않는다
GIVE_BACK_SQL = """UPDATE vessel_detail_usage SET count = count - 1
                   WHERE user_key = %s AND day = %s AND count > 0"""
# 오늘 쓴 횟수와 최근에 본 배 기록(029). 남은 횟수(remaining)와 "캐시 수명 안에 본 배" 판단에 쓴다
READ_SQL = "SELECT count, recent FROM vessel_detail_usage WHERE user_key = %s AND day = %s"
# 본 배를 남긴다. 캐시 수명이 지난 열쇠는 이때 지워 칸이 끝없이 커지지 않게 한다. 오늘 줄이 없으면 횟수 0으로 만든다
REMEMBER_SQL = """INSERT INTO vessel_detail_usage (user_key, day, count, recent) VALUES (%s, %s, 0, %s::jsonb)
                  ON CONFLICT (user_key, day) DO UPDATE SET recent = (
                    SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb)
                    FROM jsonb_each_text(vessel_detail_usage.recent) AS e(k, v)
                    WHERE v::timestamptz > now() - make_interval(secs => %s)
                  ) || EXCLUDED.recent"""


def _db_on() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _today() -> date:
    return datetime.now(_KST).date()


def _take_in_db(owner: str, today: date, limit: int) -> int | bool | None:
    """DB 표로 한 번을 쓴다. 남았으면 이번까지 쓴 횟수(1 이상), 다 썼으면 False, DB가 없거나 실패하면 None
    (부르는 쪽이 메모리로 센다). 실패를 막다른 길로 두지 않는 이유: 이 상한은 부가 장치이고, 진짜 한도는 ShipFinder가 지킨다 (위 설명)."""
    if not _db_on():
        return None
    try:
        with connect() as conn:
            row = conn.execute(USAGE_SQL, [owner, today, limit]).fetchone()
            return int(row[0]) if row is not None else False
    except Exception as e:  # noqa: BLE001 - 종류만 남기고 메모리로 센다
        log_failure("vessel_detail_usage", type(e).__name__)
        return None


def take_daily_slot(user_id: str | None) -> Slot | None:
    """캐시 밖 새 조회 한 번을 이 사용자 몫에서 쓴다. 오늘(한국 날짜) 몫이 남았으면 Slot, 다 썼으면 None.

    lookup이 캐시·진행 중 요청·최근에 본 배가 아닐 때만 부른다: 그 경우는 ShipFinder를 부르지 않으므로 세지 않는다.
    날짜가 바뀌면 모두 0부터다. 상한은 rules.md vessel_detail_daily_limit.
    DB가 있으면 표(vessel_detail_usage)에 세서 서버를 다시 띄워도 이어 센다. 없거나 실패하면 메모리로 센다.
    """
    global _daily_on
    today = _today()
    owner = user_id or ANON
    limit = rule("vessel_detail_daily_limit")
    if limit <= 0:
        return None
    stored = _take_in_db(owner, today, limit)
    if stored is False:
        return None
    if stored is not None:
        return Slot(owner, today, "db", stored)
    with _daily_lock:
        if _daily_on != today:
            _daily.clear()
            _daily_on = today
        if _daily.get(owner, 0) >= limit:
            return None
        _daily[owner] = _daily.get(owner, 0) + 1
        return Slot(owner, today, "memory", _daily[owner])


def give_back(slot: Slot | None) -> Slot | None:
    """쓴 한 번을 돌려준다 (10-08 QA E2-2: 외부 호출이 다 실패해도 하루 횟수가 줄었다). 센 곳(slot.store)에서 뺀다.
    DB가 실패하면 돌려주지 못한 채 둔다(종류만 남긴다). 사용자에게 한 번 불리할 뿐 한도를 넘기지는 않는다.
    돌려준 뒤의 Slot(used 1 감소)을 돌려준다."""
    if slot is None or slot.store == "free":
        return slot
    if slot.store == "db":
        try:
            with connect() as conn:
                conn.execute(GIVE_BACK_SQL, [slot.owner, slot.day])
        except Exception as e:  # noqa: BLE001
            log_failure("vessel_detail_usage", f"give_back {type(e).__name__}")
            return slot
    else:
        with _daily_lock:
            if _daily_on == slot.day and _daily.get(slot.owner, 0) > 0:
                _daily[slot.owner] -= 1
    return replace(slot, used=None if slot.used is None else max(slot.used - 1, 0))


def _read_db(owner: str) -> tuple[int, dict] | None:
    """DB의 오늘 줄(쓴 횟수, 최근에 본 배). 줄이 없으면 (0, {}), DB가 없거나 실패하면 None."""
    if not _db_on():
        return None
    try:
        with connect() as conn:
            row = conn.execute(READ_SQL, [owner, _today()]).fetchone()
    except Exception as e:  # noqa: BLE001 - 남은 횟수 표시와 무료 판단은 부가 정보라 실패해도 조회는 계속한다
        log_failure("vessel_detail_usage", f"read {type(e).__name__}")
        return None
    if row is None:
        return 0, {}
    recent = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
    return int(row[0] or 0), recent


def _seen_recently(recent: dict, key: tuple[str, str]) -> bool:
    """DB 기록상 이 배를 캐시 수명(CACHE_SEC) 안에 봤나. 서버를 다시 띄워 메모리 캐시가 빈 경우를 메운다 (E2-9)."""
    try:
        then = datetime.fromisoformat(str(recent.get(_db_key(key))))
    except (TypeError, ValueError):
        return False
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() < CACHE_SEC


def _remember(owner: str, key: tuple[str, str]) -> None:
    """이 사용자가 지금 이 배를 봤다고 DB에 남긴다 (E2-9). DB가 없거나 실패하면 넘어간다(메모리 캐시가 같은 일을 한다)."""
    if not _db_on():
        return
    entry = json.dumps({_db_key(key): datetime.now(timezone.utc).isoformat()})
    try:
        with connect() as conn:
            conn.execute(REMEMBER_SQL, [owner, _today(), entry, CACHE_SEC])
    except Exception as e:  # noqa: BLE001
        log_failure("vessel_detail_usage", f"remember {type(e).__name__}")


def _used_today(owner: str) -> int | None:
    """오늘 쓴 횟수. DB가 있으면 DB, 없거나 실패하면 메모리 값."""
    stored = _read_db(owner)
    if stored is not None:
        return stored[0]
    with _daily_lock:
        return _daily.get(owner, 0) if _daily_on == _today() else 0


def _remaining(used: int | None) -> int | None:
    """오늘 남은 횟수 (응답 remaining, 화면이 보여 준다). 쓴 횟수를 모르면 None."""
    if used is None:
        return None
    return max(rule("vessel_detail_daily_limit") - used, 0)


def reset_daily() -> None:
    """하루 횟수를 지운다. 테스트가 서로 영향을 주지 않게 쓴다."""
    global _daily_on
    with _daily_lock:
        _daily.clear()
        _daily_on = None


def _choose(found: dict, call_sign: str | None) -> tuple[dict | None, dict | None]:
    """후보 중 보여 줄 배 하나와, 못 골랐을 때 errors에 넣을 줄.

    호출부호로 찾았으면 후보 도구가 이미 같은 호출부호·IMO마다 최신·최근 신호로 걸렀다 (services/vessel_pick.py).
    거기서 MMSI가 하나일 때만 그 배다. 여러 척이면 예전에 그 호출부호를 쓴 배와 섞인 것이라 맨 앞을 고르지 않는다.
    선명만 있으면 가장 비슷한 후보를 보여 주되 이름이 비슷할 뿐인 배라고 표시한다 (화면이 "같은 배인지 확인" 문구를 단다).
    후보가 0척인데 검색은 됐고 최근 신호가 없어 빠진 배가 있으면(stale_count) not_found가 아니라 stale_only다
    (10-08 QA E2-5: "찾지 못함"이라고 하면 배가 없는 것처럼 읽힌다). 기간은 rules.md vessel_recent_days.
    """
    cands = found.get("candidates") or []
    if not cands:
        if found.get("stale_count"):
            days = rule("vessel_recent_days")
            return None, {"source": "shipfinder", "code": "stale_only",
                          "message": f"이 배는 최근 {days}일 안에 AIS 신호가 없습니다"}
        return None, {"source": "shipfinder", "code": "not_found", "message": "AIS에서 이 배를 찾지 못했습니다"}
    if call_sign:
        one = single(cands)
        if one is None:
            return None, {"source": "shipfinder", "code": "ambiguous",
                          "message": "같은 호출부호를 쓰는 배가 여러 척이라 한 척으로 정하지 못했습니다"}
        return {**one, "match": "call_sign"}, None
    return {**cands[0], "match": "name_similar"}, None


def _err(source: str, result: dict) -> dict | None:
    """MCP 도구는 예외 대신 {error: {code, message}}를 돌려준다 (SDD 3.4). 그 모양이면 화면용 한 줄로."""
    e = result.get("error") if isinstance(result, dict) else None
    return {"source": source, "code": e.get("code"), "message": e.get("message")} if e else None


def _freshness(last_time: str | None) -> str:
    """fresh / stale / unknown. 기준은 rules.md position_stale_hours (CLAUDE.md: 임계값을 코드에 숫자로 두지 않는다)."""
    if not last_time:
        return "unknown"
    try:
        then = datetime.fromisoformat(str(last_time).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    return "stale" if hours > rule("position_stale_hours") else "fresh"


def _fetch(call_sign: str | None, ship_name: str | None) -> tuple[dict, bool]:
    """도구를 불러 결과를 만든다. (결과, 외부 호출이 모두 실패했나).

    ShipFinder 호출: 검색 1 + 보여 줄 1척의 위치 1·국적 1(국적은 rules.md flag_cache_ttl_days 캐시).
    후보 상세 조회는 rules.md vessel_detail_lookups척까지만 한다 (선명만 있으면 가장 비슷한 1척만 보여 주므로, E2-1).
    호출부호로 여러 척이 나오면 도구가 상세 조회 없이 끝낸다. 뒤의 실시간 위치 조회는 방금 받은 위치를 다시 쓴다
    (rules.md position_reuse_minutes, clients/shipfinder.position)."""
    errors: list[dict] = []
    schedule = get_port_schedule(call_sign=call_sign, ship_name=None if call_sign else ship_name)
    mof_failed = bool(e := _err("mof", schedule))
    if mof_failed:
        errors.append(e)
        schedule = None

    vessel, position, distance = None, None, None
    found = get_vessel_candidates(ship_name=ship_name or call_sign or "", call_sign=call_sign,
                                  detail_limit=rule("vessel_detail_lookups"))
    search_failed = bool(e := _err("shipfinder", found))
    if search_failed:
        errors.append(e)
    else:
        vessel, miss = _choose(found, call_sign)
        if miss:
            errors.append(miss)
    if vessel:
        pos = get_live_position(vessel["mmsi"])
        if (e := _err("shipfinder", pos)):
            errors.append(e)
        else:
            position = {**pos, "freshness": _freshness(pos.get("last_time"))}
            dist = estimate_distance(pos["lat"], pos["lng"], pos.get("sog"))
            # 지도에 그릴 남은 예상 바닷길. 실제 항적이 아니다 (화면에도 그렇게 적는다)
            position["route"] = route_points(pos["lat"], pos["lng"])
            if (e := _err("searoute", dist)):
                errors.append(e)
            else:
                distance = dist
                hours = dist.get("calc_hours_at_sog")
                if hours is not None:
                    # 지금 속력을 끝까지 유지한다는 가정의 계산값이다. 판정에 쓰는 trusted_eta와는 다르다
                    distance["calc_eta"] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    result = {"call_sign": call_sign, "ship_name": ship_name, "vessel": vessel, "position": position,
              "distance": distance, "schedule": schedule, "errors": errors,
              # 이 결과를 만든 시각. 10분 캐시에서 다시 나가도 그대로라 화면이 "몇 시 기준"인지 보여 줄 수 있다
              "cached_at": datetime.now(timezone.utc).isoformat()}
    # 해수부와 ShipFinder 검색이 둘 다 실패했으면 외부에서 받은 것이 없다 (위치 조회는 검색이 돼야 부른다)
    return result, mof_failed and search_failed


def _tool_failed(result: dict) -> bool:
    """도구 호출 실패가 하나라도 있나. 배를 못 고른 결과(PICK_CODES)는 사실이라 실패로 보지 않는다."""
    return any(e.get("code") not in PICK_CODES for e in result.get("errors") or [])


def _view(result: dict, call_sign: str | None, ship_name: str | None, remaining: int | None) -> dict:
    """응답 모양. 캐시·같이 받은 결과라도 call_sign·ship_name은 이번 요청 값으로 둔다
    (10-08 QA E2-4: 다른 선명으로 부른 앞 요청 값이 나갔다). remaining은 오늘 남은 하루 횟수 (E2-9, 모르면 None)."""
    return {**result, "call_sign": call_sign, "ship_name": ship_name, "remaining": remaining}


def lookup(call_sign: str | None, ship_name: str | None, user_id: str | None = None, *, charge: bool = True) -> dict:
    """자세히 보기 한 번. 캐시 → 진행 중인 같은 조회 → (하루 횟수를 쓰고) 새 조회 순으로 답한다.

    charge=False면 하루 횟수를 세지 않는다 (detail(), 테스트·스크립트용). 횟수를 다 썼으면 DailyLimitReached.
    횟수를 세지 않는 경우: 캐시에 있을 때, 같은 배를 누가 이미 조회 중일 때(그 결과를 같이 받는다, E2-3),
    이 사용자가 캐시 수명 안에 본 배라고 DB에 남아 있을 때(서버 재시작 뒤, E2-9).
    외부 호출이 모두 실패하면 쓴 횟수를 돌려주고, 도구가 실패한 결과는 캐시하지 않는다 (E2-2)."""
    call_sign, ship_name = clean(call_sign, ship_name)
    key = _key(call_sign, ship_name)
    owner = user_id or ANON

    hit = _cache_get(key)
    if hit is not None:
        return _view(hit, call_sign, ship_name, _remaining(_used_today(owner)) if charge else None)
    with _inflight_lock:
        running = _inflight.get(key)
    if running is not None:
        return _view(running.result(timeout=WAIT_SEC), call_sign, ship_name,
                     _remaining(_used_today(owner)) if charge else None)

    slot: Slot | None = Slot(owner, _today(), "free")
    if charge:
        stored = _read_db(owner)
        if stored is not None and _seen_recently(stored[1], key):
            slot = Slot(owner, _today(), "free", stored[0])
        else:
            slot = take_daily_slot(user_id)
            if slot is None:
                raise DailyLimitReached

    # 진행 중 자리 잡기. 횟수를 쓰는 사이 다른 요청이 먼저 잡았으면 쓴 횟수를 돌려주고 그 결과를 같이 받는다
    with _inflight_lock:
        running = _inflight.get(key)
        if running is None:
            mine: Future = Future()
            _inflight[key] = mine
    if running is not None:
        slot = give_back(slot)
        return _view(running.result(timeout=WAIT_SEC), call_sign, ship_name,
                     _remaining(slot.used) if charge else None)

    try:
        # 자리를 잡기 직전에 앞 요청이 끝나 캐시에 넣었을 수 있다. 그러면 외부를 부르지 않는다
        hit = _cache_get(key)
        if hit is not None:
            slot = give_back(slot)
            result = hit
        else:
            result, nothing = _fetch(call_sign, ship_name)
            if not _tool_failed(result):
                _cache_put(key, result)
                if charge:
                    _remember(owner, key)
            if nothing:
                slot = give_back(slot)
        mine.set_result(result)
    except BaseException as e:
        give_back(slot)
        mine.set_exception(e)
        raise
    finally:
        with _inflight_lock:
            if _inflight.get(key) is mine:
                del _inflight[key]
    return _view(result, call_sign, ship_name, _remaining(slot.used) if charge else None)


def detail(call_sign: str | None, ship_name: str | None) -> dict:
    """한 척의 자세한 정보 (하루 횟수를 세지 않는다). 호출부호가 있으면 그것으로 찾는다(선명은 한글·영문이 달라 매칭이 약하다).
    라우터는 횟수를 세는 lookup(charge=True)을 쓴다."""
    return lookup(call_sign, ship_name, charge=False)
