"""수집 워커의 한 번 실행분 (SDD 5.10). 판정은 하지 않고 저장만 한다.

한 번 돌 때 하는 일:
1. collect_port: 해수부 두 API에서 부산 전체를 받아 저장한다. 호출 한도가 넉넉해서 매번 전체를 받는다
   - 입항 신고(선박운항정보): 어제부터 LOOKAHEAD_DAYS일 뒤까지. 예정 시각이 바뀔 때마다 새 줄이 쌓인다
   - 관제 이벤트(관제정보): 어제와 오늘. 입항·투묘·양묘·접안이 쌓인다
2. collect_positions: 추적 대상 선박만 ShipFinder 위치를 받는다
3. select_targets: 남은 예산으로 추적 대상(부산 입항 예정 풀컨테이너선)을 늘린다
ShipFinder는 하루 50회 한도라 한 번 실행에 쓸 호출 수(Budget)를 정해 두고, 위치를 먼저 받고 남으면 대상을 늘린다.

DB 연결(conn)은 인자로 받는다. psycopg 연결이든 테스트용 가짜든 execute()만 있으면 된다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from backend.app.clients.mof import MAX_PAGES, MofClient, SailingPlan
from backend.app.clients.shipfinder import ShipFinderClient, ShipFinderError
from backend.app.services.vessel_pick import pick_by_call_sign
from backend.app.services.watch import usable_position_time

# 입항 신고를 오늘부터 며칠 뒤까지 볼지. 해수부 신고가 1주 앞까지 나온다.
# mcp_server/tools/port_schedule.py의 LOOKAHEAD_DAYS와 같은 근거다
LOOKAHEAD_DAYS = 7
# 어제 것도 다시 받는 이유: 관제 이벤트가 늦게 올라오거나 신고가 최종으로 바뀌는 것을 놓치지 않으려고
LOOKBACK_DAYS = 1
# 추적 대상 척수 상한. ShipFinder가 하루 50회 한도라 12척으로 둔다.
# 24시간 정박을 위치 이력으로 잡으려면 6시간 간격으로 한 척당 하루 4번은 받아야 한다: 12척 x 4 = 48회
MAX_TARGETS = 12
# 추적 대상으로 고르는 선종. 소량 수입 화물은 컨테이너로 온다 (PRD 사용자: 소량 해상 수입자)
TARGET_SHIP_KIND = "풀컨테이너선"
# 입항 예정이 이만큼 지난 대상은 위치를 더 받지 않는다. 입항 전후 묘박·접안까지만 보면 된다 (임시값, 대기 분포가 나오면 다시 본다)
TRACK_AFTER_ARRIVAL_DAYS = 2

INSERT_REPORT = """
INSERT INTO arrival_reports (call_sign, ship_name, ship_kind, stage, arrival_at, facility, mrn, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""
INSERT_EVENT = """
INSERT INTO control_events (call_sign, ship_name, kind, at, facility, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


def _inserted(cursor) -> int:
    """ON CONFLICT DO NOTHING으로 건너뛰면 rowcount가 0이다. 새로 들어간 줄만 센다."""
    return max(getattr(cursor, "rowcount", 0) or 0, 0)


def collect_port(conn, mof: MofClient, today: date | None = None) -> dict:
    """해수부 입항 신고와 관제 이벤트를 저장하고, 받은 건수와 새로 저장한 건수를 돌려준다."""
    today = today or date.today()
    now = datetime.now(timezone.utc)

    plans = mof.sailing_plans(today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=LOOKAHEAD_DAYS), max_pages=MAX_PAGES)
    # 입항 신고가 있는 것만 남긴다. 출항만 신고한 배는 입항 예정을 재는 데 쓸 수 없다
    reports = [p for p in plans if p.planned_arrival]
    new_reports = 0
    for r in reports:
        # arrival_at에는 신고 시각을 그대로 둔다. 최종이면 실제 시각이라는 구분은 stage 칸으로 한다
        cur = conn.execute(INSERT_REPORT, (r.call_sign, r.ship_name, r.ship_kind, r.revision, r.planned_arrival, r.facility, r.mrn, now))
        new_reports += _inserted(cur)

    calls = mof.control_records(today - timedelta(days=LOOKBACK_DAYS), today, max_pages=MAX_PAGES)
    events = [(c, e) for c in calls for e in c.events if e.at]  # 시각 없는 이벤트는 쓸 데가 없다
    new_events = 0
    for c, e in events:
        cur = conn.execute(INSERT_EVENT, (c.call_sign, c.ship_name, e.kind, e.at, e.facility, now))
        new_events += _inserted(cur)

    return {
        "reports_seen": len(reports),
        "reports_new": new_reports,
        "events_seen": len(events),
        "events_new": new_events,
        "reports": reports,  # 다음 단계(추적 대상 선정)가 다시 부르지 않고 쓴다
    }


@dataclass
class Budget:
    """이번 실행에서 ShipFinder를 몇 번 부를 수 있는지. 하루 50회 한도를 넘지 않게 호출마다 하나씩 쓴다."""

    remaining: int

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _position_row_time(p, now: datetime) -> tuple[datetime | None, str | None]:
    """받은 위치를 저장해도 되면 (초 단위 보고 시각, None), 아니면 (None, 버린 이유 코드) (10-08 QA E1-6).

    - no_time: 보고 시각이 없다. 언제 위치인지 몰라 경로에 놓지 못한다
    - future_time: 지금보다 rules.md position_future_tolerance_minutes 넘게 미래다. 넣으면 그 점이 오래도록 "현재 위치"가 된다
    - no_position: (0, 0) 좌표. AIS 수신기가 위치를 못 잡았을 때 흔히 오는 값이다(기니만 한가운데). 실제 위치가 아니다
    규칙은 services/watch.usable_position_time 하나다 (aisstream·켤 때 첫 점과 같다)."""
    if p.last_time is None:
        return None, "no_time"
    ts = usable_position_time(p.last_time, now)
    if ts is None:
        return None, "future_time"
    if p.lat == 0 and p.lng == 0:
        return None, "no_position"
    return ts, None


def _spend(sf: ShipFinderClient, budget: Budget, mmsi: int) -> bool:
    """이 배 위치를 받아도 되나. 클라이언트가 받아 둔 위치를 다시 쓸 배(rules.md position_reuse_minutes)는 ShipFinder를
    부르지 않으므로 예산을 쓰지 않고 True. 그 밖에는 예산에서 하나를 쓴다. 기준은 클라이언트와 같다(position_reusable)."""
    return sf.position_reusable(mmsi) or budget.take()


INSERT_POSITION = """
INSERT INTO positions (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat, dest, destcode, ais_eta, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT DO NOTHING
"""
# 위치를 가장 오래 못 받은 대상부터. 예산이 적어도 돌아가며 골고루 받게 된다
SELECT_TRACKED = """
SELECT t.mmsi FROM targets t
LEFT JOIN (SELECT mmsi, max(fetched_at) AS last FROM positions GROUP BY mmsi) p ON p.mmsi = t.mmsi
WHERE t.mmsi IS NOT NULL AND (t.planned_at IS NULL OR t.planned_at > %s)
ORDER BY p.last NULLS FIRST
"""
INSERT_TARGET = """
INSERT INTO targets (call_sign, mmsi, ship_name, planned_at, selected_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (call_sign) DO NOTHING
"""


def collect_positions(conn, sf: ShipFinderClient, budget: Budget, now: datetime | None = None) -> dict:
    """추적 대상의 현재 위치를 받아 저장한다. 예산이 다 떨어지면 멈춘다."""
    now = now or datetime.now(timezone.utc)
    mmsis = [row[0] for row in conn.execute(SELECT_TRACKED, (now - timedelta(days=TRACK_AFTER_ARRIVAL_DAYS),)).fetchall()]
    saved, failed = 0, []
    for mmsi in mmsis:
        if not _spend(sf, budget, mmsi):
            continue  # 예산이 없어도 뒤 배 중 받아 둔 위치가 있는 배는 저장할 수 있다
        try:
            p = sf.position(mmsi)
        except ShipFinderError as e:
            failed.append({"mmsi": mmsi, "code": e.code})  # 한 척 실패로 전체를 멈추지 않는다
            continue
        ts, dropped = _position_row_time(p, now)
        if ts is None:
            failed.append({"mmsi": mmsi, "code": dropped})  # 시각 없음·미래 시각·(0, 0) 좌표는 버린다
            continue
        cur = conn.execute(INSERT_POSITION, (
            p.mmsi, ts, p.fetched_at, p.lat, p.lng, p.sog, p.cog, p.navistat,
            p.dest, p.destcode, p.ais_eta, json.dumps(p.raw, ensure_ascii=False),
        ))
        saved += _inserted(cur)
    return {"tracked": len(mmsis), "positions_new": saved, "positions_failed": failed}


# 감시·지도 (docs/specs/cargo-watch.md). 사용자가 지켜보기를 켠 화물(user_cargo.watch)의 배 중
# 위치를 가장 오래 못 받은 배부터. 진행 중 화물 전체로 잡으면 로그인 사용자의 조회마다 대상이 늘어
# 실행 예산을 다 써서 연구용 수집이 굶는다. 지켜보기는 사용자당 최대 5건이다 (services/watch.py MAX_WATCH)
# B/L 없이 배만 지켜보는 배(vessel_watch)도 같은 길로 위치를 받는다. UNION이 같은 배를 한 번만 센다.
# 판정·재판정은 하지 않는다 (run_watch는 user_cargo만 본다)
SELECT_WATCHED = """
SELECT c.mmsi FROM (SELECT vessel_mmsi AS mmsi FROM user_cargo
                    WHERE status = 'active' AND watch AND vessel_mmsi IS NOT NULL
                    UNION SELECT mmsi FROM vessel_watch) c
LEFT JOIN (SELECT mmsi, max(fetched_at) AS last FROM watch_positions GROUP BY mmsi) w ON w.mmsi = c.mmsi
ORDER BY w.last NULLS FIRST
"""
# 마지막 칸 source는 그 점을 넣은 길이다 (migrations/031, 10-08 사용자 결정): 이 정기 수집은 SOURCE_WORKER,
# worker/aisstream.py는 'aisstream'. backend/app/services/watch.py INSERT_WATCH_POSITION(켤 때 첫 점 'seed')과
# services/cargo._keep_position('lookup')도 같은 칸 순서다. 칸을 바꾸면 네 곳을 같이 고친다.
# 같은 (mmsi, ts)는 먼저 넣은 쪽 출처가 남는다 (ON CONFLICT DO NOTHING)
SOURCE_WORKER = "worker"
INSERT_WATCH_POSITION = """
INSERT INTO watch_positions (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (mmsi, ts) DO NOTHING
"""


def collect_watch_positions(conn, sf: ShipFinderClient, budget: Budget, now: datetime | None = None) -> dict:
    """지켜보는 사용자 화물(user_cargo 진행 중, watch 켬)의 배 위치를 받아 watch_positions에 쌓는다. 예산이 떨어지면 멈춘다.

    감시·지도 경로의 재료다. 연구용 대상(positions)보다 먼저 부른다. 사용자가 보는 화면이라 우선한다 (run.py).
    같은 실행의 지켜보기 재판정(run_watch)은 이 위치를 다시 쓰지 않는다. 재판정은 api의 MCP(/mcp)를 거쳐 api 프로세스의
    ShipFinderClient로 부르므로, 이 워커 프로세스의 캐시와 따로다. 그래서 worker/run.py가 재판정 몫의 예산을 따로 떼어 둔다.
    미래 시각·(0, 0) 좌표 보고는 버린다 (_position_row_time).
    """
    now = now or datetime.now(timezone.utc)
    mmsis = [row[0] for row in conn.execute(SELECT_WATCHED).fetchall()]
    saved, failed = 0, []
    for mmsi in mmsis:
        if not _spend(sf, budget, mmsi):
            continue  # 예산이 없어도 뒤 배 중 받아 둔 위치가 있는 배는 저장할 수 있다
        try:
            p = sf.position(mmsi)
        except ShipFinderError as e:
            failed.append({"mmsi": mmsi, "code": e.code})  # 한 척 실패로 전체를 멈추지 않는다
            continue
        ts, dropped = _position_row_time(p, now)
        if ts is None:
            failed.append({"mmsi": mmsi, "code": dropped})  # 언제·어디인지 믿을 수 없으면 경로에 넣지 않는다
            continue
        cur = conn.execute(INSERT_WATCH_POSITION,
                           (p.mmsi, ts, p.fetched_at, p.lat, p.lng, p.sog, p.cog, p.navistat, SOURCE_WORKER))
        saved += _inserted(cur)
    return {"watched": len(mmsis), "watch_positions_new": saved, "watch_positions_failed": failed}


def _pick_hit(hits, call_sign: str, now: datetime | None = None):
    """호출부호로 지금 그 호출부호를 쓰는 배 한 척. 하나로 못 정하면 None (services/vessel_pick.py).

    한 호출부호에 MMSI가 여러 개 나올 수 있다. 최근 신호를 보낸 배가 둘 이상이면 임의로 고르지 않는다.
    지켜보기·후보 도구와 같은 규칙이다.
    """
    return pick_by_call_sign(hits, call_sign, now=now)  # now: 최근 신호를 재는 기준 시각 (select_targets와 같은 시각)


def select_targets(conn, reports: list[SailingPlan], sf: ShipFinderClient, budget: Budget,
                   now: datetime | None = None) -> dict:
    """입항 예정 풀컨테이너선을 추적 대상으로 더한다. 호출부호로 ShipFinder를 검색해 MMSI를 찾는다.

    입항이 가까운 배부터 고른다. 예산이 떨어지거나 MAX_TARGETS를 채우면 멈춘다.
    """
    now = now or datetime.now(timezone.utc)
    known = {row[0] for row in conn.execute("SELECT call_sign FROM targets").fetchall()}
    candidates = sorted(
        (r for r in reports
         if r.ship_kind == TARGET_SHIP_KIND and r.call_sign and r.call_sign not in known
         and r.expected_arrival and r.expected_arrival > now),  # 최종 신고는 이미 들어온 배다
        key=lambda r: r.expected_arrival,
    )
    added, no_match = [], []
    for r in candidates:
        if len(known) + len(added) >= MAX_TARGETS or not budget.take():
            break
        try:
            hit = _pick_hit(sf.search(r.call_sign).hits, r.call_sign, now)
        except ShipFinderError as e:
            no_match.append({"call_sign": r.call_sign, "code": e.code})
            continue
        # 못 찾아도 대상으로 남긴다(mmsi NULL). 같은 배를 다시 검색하느라 예산을 쓰지 않으려고
        conn.execute(INSERT_TARGET, (r.call_sign, hit.mmsi if hit else None, r.ship_name, r.expected_arrival, now))
        (added if hit else no_match).append({"call_sign": r.call_sign, "mmsi": hit.mmsi if hit else None})
    return {"targets_known": len(known), "targets_added": added, "targets_unmatched": no_match}


def purge_positions(conn) -> int:
    """입항 30일 지난 배의 위치 기록을 지우고 지운 줄 수를 돌려준다.

    규칙은 DB 함수 purge_old_positions()에만 있다 (migrations/002). Supabase는 pg_cron이 하루 한 번
    부르고, pg_cron이 없는 로컬 DB는 워커가 실행마다 부른다. 여러 번 불러도 결과는 같다.
    """
    return conn.execute("SELECT purge_old_positions()").fetchall()[0][0]
