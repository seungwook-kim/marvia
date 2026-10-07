"""호출부호로 ShipFinder 검색 결과에서 지금 그 호출부호를 쓰는 배 한 척을 고르는 규칙 (10-08 결정).

왜 한 곳에 두나: 호출부호는 배가 팔리거나 국적을 바꾸면 다른 배에 다시 배정된다. 09-28 실측: 한 호출부호에 IMO가 다른 3척,
마지막 신호 0일·123일·552일 전. 전에는 고르는 기준이 네 곳(후보 도구, 지켜보기, 수집 워커, 선박 자세히 보기)에 따로 있어
지켜보기는 여러 MMSI라서 거절하고, 워커는 최신 보고만 보고, 자세히 보기는 맨 앞 후보를 그대로 받았다.
이제 모두 같은 순서를 쓴다:
  1. 호출부호가 정확히 같은 배 (앞뒤·안쪽 공백을 떼고 대문자, identify._sign과 같은 맞춤)
  2. 같은 IMO는 가장 최근에 신호를 보낸 MMSI 하나만 (newest_per_imo)
  3. 최근 N일 안에 신호를 보낸 배만 (rules.md vessel_recent_days)
  4. MMSI가 정확히 하나 남을 때만 받는다. 0척이나 여러 척이면 None (엉뚱한 배를 고르지 않는다)
검색 결과는 VesselHit처럼 mmsi·imo·call_sign·last_time 속성을 가진 것이면 된다. ShipFinder를 부르지는 않는다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from backend.app.services.rules import rule

# 실제 응답에 7자리가 아닌 IMO가 섞여 온다. 7자리만 IMO로 믿는다 (mcp_server/tools/vessel_candidates.py에서 옮김)
IMO_RANGE = range(1000000, 10000000)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def norm_sign(call_sign: str | None) -> str:
    """호출부호 맞춤: 공백을 모두 떼고 대문자. 해수부·ShipFinder 표기가 "TST 01"·"tst01"처럼 달라도 같게 본다."""
    return re.sub(r"\s+", "", call_sign or "").upper()


def same_sign(hits: list, call_sign: str) -> list:
    """검색 결과 중 호출부호가 정확히 같은 것만. 검색어는 선명·호출부호를 두루 찾아서 비슷한 다른 배도 섞여 온다."""
    sign = norm_sign(call_sign)
    return [h for h in hits if sign and norm_sign(h.call_sign) == sign]


def newest_per_imo(hits: list) -> list:
    """같은 IMO면 last_time이 가장 최신인 1건만 남긴다 (C4).

    IMO가 없거나 7자리가 아닌 값은 서로 다른 배로 본다. 실제 응답에 9자리 같은 값이 섞여 와서,
    이런 값으로 묶으면 남의 배를 한 척으로 합쳐 버린다.
    """
    newest: dict[int, object] = {}
    kept: list = []
    for hit in hits:
        if hit.imo not in IMO_RANGE:
            kept.append(hit)
            continue
        seen = newest.get(hit.imo)
        if seen is None or (hit.last_time or _EPOCH) > (seen.last_time or _EPOCH):
            newest[hit.imo] = hit
    return kept + list(newest.values())


def recent(hits: list, days: int | None = None, now: datetime | None = None) -> list:
    """최근 days일 안에 신호를 보낸 것만. days를 안 주면 rules.md vessel_recent_days. 신호 시각이 없으면 뺀다."""
    days = rule("vessel_recent_days") if days is None else days
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return [h for h in hits if h.last_time and h.last_time >= cutoff]


def by_call_sign(hits: list, call_sign: str, days: int | None = None, now: datetime | None = None) -> list:
    """1~3단계: 같은 호출부호 → IMO마다 최신 → 최근 신호. 남은 후보 목록 (여러 척일 수 있다)."""
    return recent(newest_per_imo(same_sign(hits, call_sign)), days, now)


def single(rows: list):
    """4단계: MMSI가 정확히 하나면 그 줄, 아니면 None. 같은 MMSI가 여러 줄이면 한 척으로 센다.
    줄은 VesselHit(속성)이든 MCP 후보 dict든 된다 (services/vessel_detail.py, mrn_fallback.py)."""
    mmsis = {r["mmsi"] if isinstance(r, dict) else r.mmsi for r in rows}
    return rows[0] if len(mmsis) == 1 else None


def pick_by_call_sign(hits: list, call_sign: str, days: int | None = None, now: datetime | None = None):
    """호출부호로 지금 그 호출부호를 쓰는 배 한 척. 하나로 못 정하면 None (모듈 설명의 1~4단계)."""
    return single(by_call_sign(hits, call_sign, days, now))
