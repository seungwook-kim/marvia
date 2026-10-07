"""이미 일어난 일을 코드가 세는 사실 세 가지. 판정 신호가 아니고 등급을 바꾸지 않는다.

  anchorage()       해수부 관제 이벤트(투묘·양묘·접안)로 묘박지에서 얼마나 기다렸나
  customs_timing()  UNI-PASS 통관 이력의 단계 사이 걸린 시간과 입항 뒤 지난 시간
  eta_change()      지난 조회의 신뢰 ETA(user_cargo.last_eta_at, 없으면 last_eta)와 이번 신뢰 ETA가 얼마나 달라졌나

세 값 모두 외부 호출 없이 이미 받은 값(get_port_schedule control_events, CustomsRecord.stage_history, 화물 표 한 칸)으로 센다.
지난 일만 센다. 앞으로 며칠 걸릴지는 예측하지 않는다 (PRD 4절 "통관 소요 예측"·"창고 수령일 예측"은 만들지 않는 것).
LLM은 이 값을 정하지 않는다. 판정 LLM에는 사실로만 주고(routers/agent.py facts), 근거 카드에도 같은 값을 둔다 (V7 대조).
시간은 정수 시간으로 반올림한다. 근거 카드 숫자와 응답 숫자가 같아야 LLM이 쓴 숫자를 V7이 확인할 수 있다.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from backend.app.schemas.agent import AnchorageInfo, CustomsTiming, EtaChange, StageElapsed
from backend.app.schemas.customs import CustomsRecord
from backend.app.services.evidence import KST
from backend.app.services.rules import rule, stage_key

# 입항으로 보는 통관 단계. services/cargo.py ARRIVED_STAGE와 같은 이름이다 (실제 입항 신고가 수리된 날, 예정일 etprDt가 아니다).
# cargo.py를 불러오면 llm.py까지 끌려와 순환이 생겨 이름만 같게 둔다. 둘 중 하나를 고치면 같이 고친다
ARRIVED_STAGE = "입항보고 수리"
RELEASED_STAGE = "반출신고"  # routers/agent.RELEASED_STAGE와 같다. 반출 뒤에는 "지금까지"가 아니라 반출 시각까지 센다


def _hours(later: datetime, earlier: datetime) -> int:
    """두 시각 사이 시간을 정수 시간으로 반올림한다. 근거 카드와 응답이 같은 정수를 쓴다 (모듈 설명)."""
    return int((later - earlier).total_seconds() / 3600 + 0.5)


def _aware(value, naive_tz=KST) -> datetime | None:
    """글자나 datetime을 시간대 있는 datetime으로. 시간대가 없으면 naive_tz로 본다.
    해수부 관제 시각은 +09:00이 붙어 오고(clients/mof._dt), UNI-PASS 처리 일시는 시간대 없이 한국 시각으로 온다(services/customs._dt)."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=naive_tz)


def anchorage(schedule: dict | None, now: datetime | None = None) -> AnchorageInfo | None:
    """해수부 관제 이벤트에서 가장 최근 투묘(닻 내림)와 그 뒤의 양묘·접안을 찾아 대기 사실을 만든다. 투묘 기록이 없으면 None.

    - 이벤트 이름 목록은 skills/port-busan/rules.md anchor_event_kinds, weigh_anchor_event_kinds, berth_event_kinds
    - state: 투묘 뒤 접안이 있으면 berthed, 양묘만 있으면 weighed(부두로 가는 중), 둘 다 없으면 anchored(아직 대기 중)
    - waited_hours: 투묘부터 양묘·접안 중 이른 것까지. 아직 대기 중이면 투묘부터 지금까지. 앞으로 얼마나 더 기다릴지는 세지 않는다
    anchor_wait 신호(services/signals.py, AIS 위치 이력)와는 따로다. 이 값은 신호가 아니라 사실이라 등급에 쓰지 않는다.
    도구가 실패했거나(error) 이벤트가 없으면 None이다.
    """
    if not schedule or "error" in schedule:
        return None
    now = _aware(now) if now else datetime.now(timezone.utc)
    events = []
    for event in schedule.get("control_events") or []:
        at = _aware(event.get("at")) if isinstance(event, dict) else None
        if at is not None and at <= now:  # 앞으로 일어날 이벤트는 없지만 시계 차이로 미래 시각이 오면 세지 않는다
            events.append((at, (event.get("kind") or "").strip()))
    anchored = [at for at, kind in events if kind in rule("anchor_event_kinds")]
    if not anchored:
        return None
    start = max(anchored)
    weighed = min((at for at, kind in events if kind in rule("weigh_anchor_event_kinds") and at >= start), default=None)
    berthed = min((at for at, kind in events if kind in rule("berth_event_kinds") and at >= start), default=None)
    ends = [t for t in (weighed, berthed) if t is not None]
    state = "berthed" if berthed else ("weighed" if weighed else "anchored")
    return AnchorageInfo(anchored_at=start, weighed_at=weighed, berthed_at=berthed, state=state,
                         waited_hours=_hours(min(ends) if ends else now, start))


def customs_timing(customs: CustomsRecord | None, now: datetime | None = None) -> CustomsTiming | None:
    """통관 이력에서 처리 시각이 있는 단계를 시각 순으로 놓고, 이어진 두 단계 사이 걸린 시간을 센다. 셀 것이 없으면 None.

    - steps: 이미 처리된 단계끼리만 (처리 시각이 있는 줄). 다음 단계가 언제 될지는 세지 않는다 (PRD 4절 통관 소요 예측 제외)
    - 입항 뒤 지난 시간: ARRIVED_STAGE(입항보고 수리) 처리 시각부터 지금까지. 반출신고가 끝났으면 반출 시각까지
      (measured_until="released"). 입항 단계 시각이 없으면 이 두 칸은 비운다. 세관 입항(예정)일 etprDt는 예정일일 수 있어 쓰지 않는다
    UNI-PASS 처리 일시는 시간대 없이 한국 시각으로 온다 (services/customs._dt). 응답의 시각도 그 모양 그대로 둔다(근거 카드와 같다).
    """
    if customs is None:
        return None
    now = _aware(now) if now else datetime.now(timezone.utc)
    done = sorted((s for s in customs.stage_history if s.at is not None and _aware(s.at) <= now), key=lambda s: _aware(s.at))
    steps = [StageElapsed(from_stage=a.name, to_stage=b.name, from_at=a.at, to_at=b.at, hours=_hours(_aware(b.at), _aware(a.at)))
             for a, b in zip(done, done[1:])]
    arrived = next((s for s in done if stage_key(s.name) == stage_key(ARRIVED_STAGE)), None)
    released = next((s for s in done if stage_key(s.name) == stage_key(RELEASED_STAGE)), None)
    since, until = None, None
    if arrived is not None:
        end = _aware(released.at) if released is not None else now
        if end >= _aware(arrived.at):
            since, until = _hours(end, _aware(arrived.at)), ("released" if released is not None else "now")
    if not steps and since is None:
        return None
    return CustomsTiming(steps=steps, arrived_at=arrived.at if arrived else None, hours_since_arrival=since,
                         measured_until=until)


def _signed_hours(later: datetime, earlier: datetime) -> int:
    """두 시각 차이를 부호 있는 정수 시간으로 반올림한다 (당겨지면 음수). _hours와 같은 반올림을 절댓값에 건다."""
    seconds = (later - earlier).total_seconds()
    whole = int(abs(seconds) / 3600 + 0.5)
    return whole if seconds >= 0 else -whole


def eta_change(previous: date | datetime | None, trusted: datetime | None) -> EtaChange | None:
    """지난 조회의 신뢰 ETA와 이번 신뢰 ETA가 기준 이상 달라졌으면 그 변화를 돌려준다.

    기준은 skills/ais-reliability/rules.md eta_change_alert_hours.
    - previous가 시각(datetime, user_cargo.last_eta_at)이면 실제 시간 차이로 센다. 같은 날 안에서 14시간 밀린 것도 잡고,
      자정을 넘겨 2시간 밀린 것은 잡지 않는다
    - previous가 날짜뿐(date, 옛 줄의 user_cargo.last_eta)이면 한국 날짜 차이 x 24시간으로 센다 (하루 단위)
    from_eta·to_eta는 화면·알림 글이 쓰는 한국 날짜다. hours는 늦어지면 양수, 당겨지면 음수다.
    둘 중 하나라도 없으면(처음 조회, 이번에 ETA 없음) None. 판정 신호가 아니다. 등급은 지금처럼 판정 표가 정한다.
    """
    if previous is None or trusted is None:
        return None
    now_at = _aware(trusted, timezone.utc)
    current = now_at.astimezone(KST).date()
    if isinstance(previous, datetime):
        before_at = _aware(previous, timezone.utc)
        hours = _signed_hours(now_at, before_at)
        previous = before_at.astimezone(KST).date()
    else:
        hours = (current - previous).days * 24
    if abs(hours) < rule("eta_change_alert_hours"):
        return None
    return EtaChange(from_eta=previous, to_eta=current, hours=hours)

