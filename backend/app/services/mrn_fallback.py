"""세관(UNI-PASS)에 아직 없는 화물은 MRN으로 배 상태까지 보여 준다 (docs/specs/mrn-fallback.md).

routers/agent.py의 조회가 UNI-PASS에서 B/L을 못 찾았고(insufficient) 요청에 MRN이 있을 때만 부른다.
- 해수부 입항 신고에서 MRN으로 배(선명·호출부호·입항 예정·접안 시설)를 찾는다
- 호출부호로 ShipFinder 후보 → AIS 위치 → 부산까지 남은 거리 (기존 MCP 도구)
- status=partial, verdict=None. 통관 정보가 없어서 판정하지 않는다. trusted_eta도 정하지 않는다
해수부에서 그 MRN을 못 찾으면 None, 해수부 조회 자체가 실패하면 MrnLookupFailed.
부르는 쪽(routers/agent._assess)은 둘을 다른 문장으로 알린다: "MRN도 찾지 못함"과 "해수부 조회 실패"는 사용자가 할 일이 다르다 (10-08 QA B11).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from backend.app.clients.mof import MofClient, MofError
from backend.app.observability.logging import current_request_id, log_failure
from backend.app.schemas.agent import (
    AgentRequest, AgentResponse, BerthInfo, EtaInfo, Evidence, Position, VesselInfo,
)
from backend.app.services.vessel_detail import _freshness
from backend.app.services.vessel_pick import single
# MCP 도구는 /mcp를 거쳐 부른다 (services/mcp_client.py)
from backend.app.services.mcp_client import estimate_distance, get_live_position, get_vessel_candidates

LOOKBACK_DAYS, LOOKAHEAD_DAYS = 3, 14  # 입항 신고를 찾는 기간. 통지서는 보통 입항 며칠 전에 온다

class MrnLookupFailed(Exception):
    """해수부 입항 신고 조회가 실패했다(시간 초과·키 오류 등). MRN이 없는 것과 구분한다. code는 MofError 코드."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


NO_CUSTOMS_YET = ("통관 정보는 아직 세관(UNI-PASS)에 없습니다. 보통 적하목록이 올라오는 입항 전후에 조회됩니다. "
                  "대신 MRN으로 해수부 입항 신고에서 배를 찾았습니다.")


def _iso(t) -> str | None:
    return t.isoformat() if isinstance(t, datetime) else (str(t) if t else None)


def _answer(ship_name: str | None, arrival: datetime | None, facility: str | None) -> str:
    """MRN 대체 답 문장. 모르는 값은 "확인되지 않음입니다"처럼 칸 이름에 끼워 넣지 않고 문장을 바꿔 말한다 (10-08 QA B11)."""
    ship = f"{ship_name}호" if ship_name else "이 배"
    parts = [NO_CUSTOMS_YET]
    if arrival:
        when = arrival.astimezone(timezone(timedelta(hours=9))).strftime("%m월 %d일 %H:%M")
        parts.append(f"{ship}가 {when} 부산 입항 예정입니다.")
    else:
        parts.append(f"{ship}의 부산 입항 예정 시각은 아직 신고되지 않았습니다.")
    parts.append(f"접안 예정지는 {facility}입니다." if facility else "접안 예정지는 아직 정해지지 않았습니다.")
    parts.append("통관 정보가 들어오면 판정까지 다시 볼 수 있습니다.")
    return " ".join(parts)


def assess(request: AgentRequest) -> AgentResponse | None:
    """MRN 대체 응답. 해수부에 그 MRN이 없으면 None, 해수부 조회가 실패하면 MrnLookupFailed."""
    if not request.mrn:
        return None
    today = date.today()
    client = MofClient()
    try:
        plans = client.sailing_plans_all(today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=LOOKAHEAD_DAYS))
    except MofError as e:
        log_failure("mrn_fallback_mof", e.code)
        raise MrnLookupFailed(e.code) from None
    finally:
        client.close()
    plan = next((p for p in plans if p.mrn and p.mrn.upper() == request.mrn), None)
    if plan is None:
        return None

    now = datetime.now(timezone.utc)
    evidence = [Evidence(source="mof", field="mrn_plan", value=f"{plan.ship_name} / {plan.call_sign or '-'}", fetched_at=now)]
    if plan.expected_arrival:
        evidence.append(Evidence(source="mof", field="planned_arrival", value=_iso(plan.expected_arrival), fetched_at=now))
    if plan.facility:
        evidence.append(Evidence(source="mof", field="facility", value=plan.facility, fetched_at=now))

    vessel = position = None
    ais_eta = calc_eta = distance_nm = None
    found = get_vessel_candidates(ship_name=plan.ship_name or "", call_sign=plan.call_sign)
    cands = [] if "error" in found else (found.get("candidates") or [])
    # 호출부호를 알면 같은 호출부호·IMO마다 최신·최근 신호로 거른 뒤 MMSI가 하나일 때만 그 배다
    # (services/vessel_pick.py). 여러 척이면 맨 앞을 고르지 않고 해수부 정보만으로 답한다.
    # 호출부호가 없을 때만 가장 비슷한 선명 후보를 쓴다
    c = (single(cands) if plan.call_sign else (cands[0] if cands else None))
    if c is not None:
        vessel = VesselInfo(mmsi=c["mmsi"], imo=c.get("imo"), name=c.get("name") or plan.ship_name,
                            call_sign=plan.call_sign, flag=c.get("flag") or plan.nationality, ship_type=c.get("ship_type"),
                            voyage_no=None)
        pos = get_live_position(c["mmsi"])
        if "error" not in pos:
            position = Position(lat=pos["lat"], lng=pos["lng"], sog=pos.get("sog") or 0.0, cog=pos.get("cog") or 0.0,
                                navistat=pos.get("navistat") or 0, last_time=pos.get("last_time") or now,
                                freshness=_freshness(pos.get("last_time")))
            ais_eta = pos.get("ais_eta")
            evidence.append(Evidence(source="shipfinder", field="position", value=f"{pos['lat']}, {pos['lng']}",
                                     fetched_at=pos.get("fetched_at") or now))
            dist = estimate_distance(pos["lat"], pos["lng"], pos.get("sog"))
            if "error" not in dist:
                distance_nm = dist["distance_nm"]
                if dist.get("calc_hours_at_sog") is not None:
                    calc_eta = now + timedelta(hours=dist["calc_hours_at_sog"])
                evidence.append(Evidence(source="searoute", field="distance_nm", value=str(distance_nm), fetched_at=now))
        else:
            log_failure("mrn_fallback_position", pos["error"].get("code"))  # 위치가 없어도 해수부 정보만으로 계속
    else:
        code = (found.get("error") or {}).get("code") or ("ambiguous" if cands else "not_found")
        log_failure("mrn_fallback_vessel", code)

    answer = _answer(plan.ship_name, plan.expected_arrival, plan.facility)
    return AgentResponse(
        status="partial", verdict=None, vessel_match="confirmed" if vessel else "none", vessel_candidates=[],
        missing_inputs=[], reasons=[], conflicts=[], actions=["입항 전후에 같은 B/L로 다시 조회하세요"],
        days_margin=None, decided_by=None, vessel=vessel, position=position,
        eta=EtaInfo(ais_eta=ais_eta, mof_eta=plan.expected_arrival, calc_eta=calc_eta, trusted_eta=None, distance_nm=distance_nm),
        customs=None, berth=BerthInfo(facility=plan.facility, shed=None), evidence=evidence, answer=answer,
        trace_id=current_request_id(),
    )
