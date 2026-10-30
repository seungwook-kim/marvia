"""화물 지켜보기: 등록·감시·알림의 업무 규칙.

  set_watch()        결과 화면의 "지켜보기" 켜기·끄기. 사용자당 최대 MAX_WATCH건 (사용자 잠금 + 조건부 UPDATE로 동시 요청에도 지킨다)
  vessel_known()     그 화물의 배(vessel_mmsi)가 정해졌나. 모르면 워커가 다시 판정하지 않아 켜기 응답에 알린다
  seed_position()    켜는 순간 이미 받아 둔 배 위치를 지도 첫 점으로 남긴다 (ShipFinder를 새로 부르지 않는다)
  watch_state()      그 화물이 저장됐는지, 지켜보는 중인지 (버튼 상태)
  pending_changes()  아직 안 본 판정·도착 예정 변화 목록 ("다음 접속 때 표시" 띠). 지켜보는 화물만
  mark_seen()        화면에 보여 줬으니 "봤음"으로 바꾸기
  run_watch()        지켜보는 화물을 다시 판정하고, 등급이 바뀌었으면 기록하고 카카오톡으로 알린다 (워커가 부른다).
                     도착 예정이 기준(rules.md eta_change_alert_hours) 이상 바뀌었으면 그것도 알림 글에 적고 띠에도 남긴다.
                     잇달아 실패한 화물은 자동 확인을 멈추고(rules.md watch_pause_after_failures), 일시 오류로 못 보낸 알림은
                     다음 실행에서 다시 보낸다(rules.md watch_notify_retry_hours)
  usable_position_time()  위치 보고 시각을 저장해도 되나 (미래 시각 버림, 초 단위로 내림). 수집 워커·aisstream·첫 점이 같이 쓴다
HTTP는 라우터(routers/watch.py)가, 주기 실행은 worker/watch.py·worker/run.py가 맡는다. 여기는 DB와 규칙만 안다.
표는 새로 만들지 않고 화물 표(user_cargo)에 칸을 더했다 (migrations/017).

설계 이유:
- 사용자가 직접 켠 화물만 본다: 조회한 화물을 전부 감시하면 ShipFinder 하루 한도와 LLM 비용이 사용자 모르게 나간다.
  그래서 최대 MAX_WATCH건으로 제한한다
- 다시 판정은 사용자가 조회할 때와 같은 길(routers/agent._assess, LLM 포함)로 한다: 감시가 따로 만든 판정 규칙이 있으면
  "조회 화면에서는 순항인데 알림은 긴급"처럼 두 답이 어긋난다. 저장된 값으로 같은 AgentRequest를 만들어 그대로 부른다
- 한 번에 5건만: ShipFinder 호출을 아끼려는 상한이다. 마지막 확인(또는 마지막 시도)이 가장 오래된 화물부터 돌려서 한 화물만 계속 도는 일이 없다.
  실패해도 시도 시각(watch_attempt_at)을 남긴다. 안 남기면 실패한 화물이 늘 맨 앞에 서서 다른 화물을 굶긴다 (10-08 QA E1-1)
- 조회 기록(query_log)은 남기지 않는다: 사용자가 한 조회가 아니라서 "내 조회 기록" 화면과 90일 보관 대상에 섞이면 안 된다.
  그 대신 화물 표의 마지막 판정(last_*)은 갱신한다. 다음 번 비교의 기준이 그 값이다
- 판정이 없는 결과(되묻기·일시 실패, verdict 없음)는 화물 표를 덮지 않는다: 덮으면 기준 등급이 사라져서 다음에 진짜로
  바뀌어도 "이전 등급이 없음"이라 변화를 못 잡는다
- 연결 안 된 사용자(not_linked)는 조용히 넘어간다: 카카오톡은 선택 기능이다. 안 쓰는 사람에게 오류를 남기면 로그만 시끄럽다.
  알림을 못 보내도 변화 기록은 이미 화물 표에 있어서 다음 접속 때 화면 띠로 보인다. 도착 예정만 바뀐 것도 띠에 남긴다 (E1-7)
- 네트워크 오류처럼 다시 하면 될 실패는 보낼 글을 notify_pending에 남기고 다음 실행에서 다시 보낸다 (E1-2).
  연결이 끊겼거나(relink) 동의가 없는(no_consent) 실패는 사용자가 고쳐야 해서 다시 보내지 않는다
- 화물 한 건이 실패해도 나머지는 계속한다: 한 건 때문에 다른 사람의 알림이 막히면 안 된다

B/L 원문은 메시지·로그·반환값 어디에도 넣지 않는다. 가린 번호나 이름표만 쓴다. 로그에는 오류 종류만 남긴다 (log_failure 규칙).
DB 연결은 부르는 쪽이 연다. 여기 함수는 conn만 받아 SQL을 보낸다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.app import crypto
from backend.app.db import atomic
from backend.app.clients.kakao import KakaoError
from backend.app.clients.shipfinder import ShipFinderClient
from backend.app.observability.langfuse import session_scope
from backend.app.observability.logging import log_failure
from backend.app.schemas.agent import AgentRequest
from backend.app.services import cargo
from backend.app.services import kakao
from backend.app.services import quota
from backend.app.services.kakao import KakaoLinkError
from backend.app.services.rules import rule
from backend.app.services.verdict import VERDICT_LABEL

MAX_WATCH = 5      # 사용자당 지켜볼 수 있는 화물 수
RUN_LIMIT = 5      # 워커 한 번에 다시 판정하는 화물 수. ShipFinder 하루 한도를 지키려는 상한이다
# 배(vessel_mmsi)를 아는 화물 한 건을 다시 판정할 때 ShipFinder를 부르는 횟수 (routers/agent.py 경로 기준).
# resolved_mmsi가 있으면 _identify가 후보 검색을 건너뛰고(확정), _after_customs가 get_live_position 한 번만 부른다.
# 규칙이 아니라 코드 경로를 센 값이라 rules.md가 아니라 여기 둔다. 판정 경로가 ShipFinder를 더 부르게 바뀌면 같이 고친다.
# 배를 모르는 화물은 후보 검색(검색 1 + 후보 5척 x 위치·국적 = 최대 11회, mcp_server/tools/vessel_candidates.py)이라
# 워커가 예산 안에서 돌릴 때는 아예 다시 판정하지 않는다 (run_watch known_vessel_only)
SHIPFINDER_CALLS_PER_KNOWN_VESSEL = 1

# 켜기 응답에 붙이는 안내 (E1-9). 배를 모르는 화물은 워커가 다시 판정하지 않는다 (run_watch known_vessel_only 설명)
NO_VESSEL_NOTICE = ("이 화물은 아직 배가 정해지지 않아 자동 확인이 돌지 않아요. "
                    "B/L로 한 번 다시 조회해 배가 정해지면 그때부터 자동으로 확인해요.")

# 자동 확인을 멈춘 화물인가 (E1-1). 잇달아 rules.md watch_pause_after_failures번 실패했고, 그 뒤로 사용자가 다시 조회하지 않았다.
# 사용자가 조회하면 cargo.upsert_assessment가 last_checked_at을 새로 써서 마지막 시도(watch_attempt_at)보다 새로워지고 풀린다.
# 파라미터 하나(멈춤 기준 횟수)를 받는다. _due_rows·known_vessel_due_count·watch_list가 같은 조건을 쓴다
PAUSED_SQL = "(watch_fail_count >= %s AND (last_checked_at IS NULL OR last_checked_at < watch_attempt_at))"


def pause_after() -> int:
    """잇달아 몇 번 실패하면 자동 확인을 멈추나 (rules.md watch_pause_after_failures)."""
    return int(rule("watch_pause_after_failures"))


def is_paused(fail_count: int | None, attempt_at: datetime | None, checked_at: datetime | None) -> bool:
    """PAUSED_SQL과 같은 판단을 파이썬으로 (목록 화면 auto_paused 칸)."""
    if (fail_count or 0) < pause_after() or attempt_at is None:
        return False
    return checked_at is None or checked_at < attempt_at


def usable_position_time(ts: datetime | None, now: datetime | None = None) -> datetime | None:
    """위치 보고 시각을 위치 이력(watch_positions·positions)에 넣어도 되면 초 단위로 내린 값을, 아니면 None (10-08 QA E1-6).

    - 지금보다 rules.md position_future_tolerance_minutes 넘게 미래면 버린다: 시계가 틀린 보고다. 넣으면 목록이 최신순이라
      그 점이 몇 날이고 "현재 위치"로 남는다
    - 초 아래는 버린다: aisstream은 나노초까지, ShipFinder는 초까지 준다. 같은 보고가 소수점 차이로 두 줄이 되지 않게 한다
      (UNIQUE (mmsi, ts))
    수집 워커(worker/collector.py), aisstream(worker/aisstream.py), 켤 때 첫 점(seed_position)이 같은 규칙을 쓴다."""
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None and now.tzinfo is not None:
        ts = ts.replace(tzinfo=timezone.utc)  # 시간대 없는 값은 UTC로 읽는다 (ShipFinder·aisstream 모두 UTC)
    if ts > now + timedelta(minutes=float(rule("position_future_tolerance_minutes"))):
        return None
    return ts.replace(microsecond=0)


@contextmanager
def owner_lock(conn, user_id: str, scope: str):
    """한 사용자의 켜기 요청을 한 줄로 세운다 (10-08 QA E1-10: 한도 세기와 켜기 사이 경쟁으로 5건을 넘었다).

    트랜잭션을 열고 pg_advisory_xact_lock(사용자·종류별 열쇠)을 잡는다. 같은 사용자의 다음 요청은 앞 요청이 끝날 때까지
    기다렸다가 새로 센다(READ COMMITTED라 문장마다 새로 읽는다). 트랜잭션이 끝나면 잠금도 풀린다.
    트랜잭션 기능이 없는 연결(테스트 가짜)은 잠그지 않고 그대로 실행한다 (db.atomic과 같은 규칙)."""
    with atomic(conn):
        if hasattr(conn, "transaction"):
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"{scope}:{user_id}"])
        yield


class WatchError(Exception):
    """우리 쪽 규칙 때문에 못 했다. kind: not_saved(화물 줄이 아직 없음) / done(반출이 끝난 화물)
    / limit(이미 MAX_WATCH건) / no_key(B/L 지문 열쇠 없음) / quota(그 사용자가 오늘 LLM 토큰 한도를 다 씀)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


# ── 켜기·끄기·상태 ──

def _find(conn, user_id: str, key: str) -> tuple[str, bool] | None:
    """화물 줄의 (status, watch). 줄이 없으면 None."""
    row = conn.execute("SELECT status, watch FROM user_cargo WHERE user_id = %s AND cargo_key = %s",
                       [user_id, key]).fetchone()
    return (row[0], bool(row[1])) if row else None


def set_watch(conn, user_id: str, bl_no: str, on: bool) -> bool:
    """지켜보기를 켜거나 끈다. 바뀐 뒤 상태(on)를 돌려준다. 못 하면 WatchError.

    끄기: 줄이 없거나 이미 꺼져 있어도 오류가 아니다 (사용자가 원하는 상태가 이미 그렇다).
    다만 B/L 지문 열쇠가 없으면 어느 줄인지 몰라 실제로 끌 수 없다. 그때 "꺼졌다"고 답하면 거짓이라 no_key로 알린다 (10-08 QA E1-4).
    켜기는 화물 줄이 있어야 한다. 줄은 조회가 끝난 뒤 뒤에서 쓰므로(services/memory.remember_later) 조회 직후에는
    아직 없을 수 있다. 그때는 not_saved로 알려 사용자가 잠시 뒤 다시 누르게 한다 (여기서 줄을 만들지 않는다).
    한도: 사용자 잠금(owner_lock) 안에서 "지금 켠 수 < MAX_WATCH"일 때만 바꾸는 조건부 UPDATE 한 문장으로 켠다 (E1-10).
    새로 켜면 실패 기록(watch_fail_count)을 0으로 돌린다: 다시 켠 것은 자동 확인을 다시 해 달라는 뜻이다 (E1-1).
    """
    key = cargo.cargo_key(bl_no)
    if not key:
        raise WatchError("no_key")
    if not on:
        conn.execute("UPDATE user_cargo SET watch = false, updated_at = now() WHERE user_id = %s AND cargo_key = %s",
                     [user_id, key])
        return False
    with owner_lock(conn, user_id, "cargo_watch"):
        found = _find(conn, user_id, key)
        if found is None:
            raise WatchError("not_saved")
        status, watching = found
        if status == "done":
            raise WatchError("done")
        if not watching:  # 이미 켜져 있으면 한도를 다시 세지 않는다
            cur = conn.execute(
                """UPDATE user_cargo SET watch = true, watch_fail_count = 0, updated_at = now()
                   WHERE user_id = %s AND cargo_key = %s AND NOT watch
                     AND (SELECT count(*) FROM user_cargo WHERE user_id = %s AND watch AND status = 'active') < %s""",
                [user_id, key, user_id, MAX_WATCH])
            if not getattr(cur, "rowcount", 0):
                again = _find(conn, user_id, key)
                if not (again and again[1]):  # 다른 요청이 먼저 켰으면 이미 원하는 상태다. 아니면 한도에 걸렸다
                    raise WatchError("limit")
    # 첫 위치 점은 잠금(트랜잭션) 밖에서 넣는다. 안에서 실패하면 켜기까지 같이 되돌려진다
    _seed_cargo_position(conn, user_id, key)
    return True


def vessel_known(conn, user_id: str, bl_no: str) -> bool:
    """그 화물의 배(vessel_mmsi)가 정해졌나. 켜기 응답의 known_vessel 칸 (10-08 QA E1-9).
    배를 모르면 워커는 ShipFinder 예산 때문에 그 화물을 다시 판정하지 않는다 (run_watch known_vessel_only)."""
    key = cargo.cargo_key(bl_no)
    if not key:
        return False
    row = conn.execute("SELECT vessel_mmsi FROM user_cargo WHERE user_id = %s AND cargo_key = %s",
                       [user_id, key]).fetchone()
    return bool(row and row[0])


# 첫 위치 점 SQL. worker/collector.py INSERT_WATCH_POSITION과 같은 문장이다 (같은 보고 시각은 한 줄만, UNIQUE (mmsi, ts)).
# backend가 worker를 import하지 않게 여기 따로 둔다. 둘 중 하나를 고치면 같이 고친다 (services/cargo._keep_position도 같은 칸)
# 마지막 칸 source는 위치 출처다 (migrations/031, 10-08 사용자 결정). 켤 때 첫 점은 SOURCE_SEED: ShipFinder 캐시라
# rules.md api_cache_ttl_hours만큼 묵은 위치일 수 있어 화면이 속이 빈 핀으로 따로 보인다 (web/components/WatchMap.tsx)
SOURCE_SEED = "seed"
INSERT_WATCH_POSITION = """
INSERT INTO watch_positions (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (mmsi, ts) DO NOTHING
"""


def seed_position(conn, mmsi: int | None) -> bool:
    """지켜보기를 켜는 순간, 이 api 프로세스가 이미 받아 둔 그 배 위치를 watch_positions에 첫 점으로 넣는다.

    왜: 위치 이력은 수집 워커가 쌓는데, 워커 스케줄은 ShipFinder 하루 한도 때문에 꺼 둘 수 있다(WORKER_SCHEDULE=off).
    화물 판정·선박 자세히 보기가 MCP(get_live_position)로 받은 위치는 ShipFinderClient 클래스 캐시(shipfinder:pos:{mmsi})에
    남아 있고, MCP가 같은 api 프로세스에 붙어 있어(/mcp) 여기서도 보인다. 그 값을 쓰고 ShipFinder는 새로 부르지 않는다.
    캐시에 없거나(TTL rules.md api_cache_ttl_hours가 지났거나) 보고 시각(last_time)이 없으면 넣지 않는다:
    언제 위치인지 모르면 지나온 길에 못 놓는다 (worker/collector.py와 같은 규칙).
    미래 시각 보고와 (0, 0) 좌표도 넣지 않는다 (usable_position_time, 10-08 QA E1-6).
    실패해도 예외를 밖으로 내지 않는다. 지켜보기 켜기 자체는 그대로 돼야 한다. 넣었으면 True."""
    if not mmsi:
        return False
    try:
        entry = ShipFinderClient.cache.entry(f"shipfinder:pos:{mmsi}")
        pos = entry.value if entry else None
        ts = usable_position_time(getattr(pos, "last_time", None)) if pos is not None else None
        if ts is None or (pos.lat == 0 and pos.lng == 0):
            return False
        cur = conn.execute(INSERT_WATCH_POSITION,
                           (mmsi, ts, pos.fetched_at, pos.lat, pos.lng, pos.sog, pos.cog, pos.navistat, SOURCE_SEED))
        return bool(getattr(cur, "rowcount", 0))
    except Exception as e:  # DB 연결은 autocommit이라(db.connect) 이 문장이 실패해도 앞의 켜기는 이미 저장돼 있다
        log_failure("watch_seed_position", type(e).__name__)
        return False


def _seed_cargo_position(conn, user_id: str, key: str) -> None:
    """B/L 화물의 배 번호(vessel_mmsi)를 읽어 seed_position에 넘긴다. 배를 못 정한 화물(null)은 아무것도 하지 않는다.
    배를 정하지 못한 화물에 후보 중 하나의 위치를 찍으면 엉뚱한 배를 내 화물로 보이게 한다."""
    try:
        row = conn.execute("SELECT vessel_mmsi FROM user_cargo WHERE user_id = %s AND cargo_key = %s",
                           [user_id, key]).fetchone()
    except Exception as e:
        log_failure("watch_seed_position", type(e).__name__)
        return
    if row and row[0]:
        seed_position(conn, row[0])


def watch_state(conn, user_id: str, bl_no: str) -> dict:
    """결과 화면 버튼용. saved: 화물 줄이 있나, watch: 지금 지켜보는 중인가.
    반출이 끝난 화물은 더 볼 게 없어 watch를 false로 말한다 (워커도 active만 본다)."""
    key = cargo.cargo_key(bl_no)
    found = _find(conn, user_id, key) if key else None
    if found is None:
        return {"saved": False, "watch": False}
    status, watching = found
    return {"saved": True, "watch": watching and status == "active"}


# ── 바뀐 판정 띠 ──

# 감시 목록 지도에 잇는 지나온 길 점 수. worker 수집 간격 6시간이면 3일치다. 화면이 그리기 무겁지 않을 만큼만
TRACK_POINTS = 12


def watch_list(conn, user_id: str) -> dict:
    """지켜보는 화물 목록과 배의 마지막 위치·지나온 길 (GET /api/watch). 화면 web/lib/api.ts WatchItem 모양이다.

    - 줄 재료는 화물 표(user_cargo)의 마지막 조회 값이다: 등급·통관 단계·도착 예정·여유 일수·품명·선명·국적
    - 위치는 watch_positions에서 그 배의 최근 TRACK_POINTS개를 오래된 순으로. 마지막 점이 현재 위치다
      (worker 수집 또는 조회할 때 쌓인 점, services/cargo._keep_position). 점이 없으면 위치 칸은 null이고 화면은 지도에 안 그린다
    - B/L은 가린 번호만, 화물을 가리키는 값은 지문(cargo_key)만 준다. 반출 끝난 화물(done)도 목록 뒤에 남는다(화면이 개수만 알린다)
    - updated_at: 줄들 중 가장 최근 조회 시각. 없으면 null
    - known_vessel: 배가 정해졌나. false면 워커가 다시 판정하지 않는다 (E1-9)
    - auto_paused: 잇달아 실패해 자동 확인을 멈췄나 (E1-1). 사용자가 B/L로 다시 조회하면 풀린다
    - position_source: 마지막 점(현재 위치)의 출처. worker·aisstream·seed·lookup, 옛 줄이면 null (migrations/031, 10-08 사용자 결정)
    - vessel_mmsi: 이 화물의 배 번호(정해지지 않았으면 null). 같은 배를 배만 지켜보기로도 켰으면 화면이 핀 하나로 접는다
      (10-08 사용자 결정, web/lib/watchMerge.ts). MMSI는 공개 선박 식별 번호라 B/L처럼 가리지 않는다"""
    rows = conn.execute(
        """SELECT cargo_key, bl_masked, label, goods_name, vessel_name, vessel_flag, vessel_mmsi, status,
                  last_verdict, last_stage, last_eta, last_days_margin, last_checked_at, watch_fail_count, watch_attempt_at
           FROM user_cargo WHERE user_id = %s AND watch
           ORDER BY status = 'done', last_checked_at DESC NULLS LAST""",
        [user_id],
    ).fetchall()
    items = []
    for (key, masked, label, goods, ship, flag, mmsi, status,
         verdict, stage, eta, margin, checked, fails, attempted) in rows:
        track = _track(conn, mmsi) if mmsi else []
        last = track[-1] if track else None
        items.append({
            "cargo_key": key, "bl_masked": masked, "label": label, "goods_name": goods,
            "ship_name": ship, "ship_flag": flag, "status": status, "verdict": verdict,
            "stage": cargo.clean_stage(stage) if stage else None,
            "eta": eta.isoformat() if eta else None, "days_margin": margin,
            "lat": last["lat"] if last else None, "lng": last["lng"] if last else None,
            "cog": last["cog"] if last else None, "sog": last["sog"] if last else None,
            "position_at": last["at"] if last else None, "track": track,
            "position_source": last["source"] if last else None, "vessel_mmsi": mmsi,
            "known_vessel": bool(mmsi), "auto_paused": status == "active" and is_paused(fails, attempted, checked),
        })
    checked_at = [r[12] for r in rows if r[12]]
    return {"items": items, "updated_at": max(checked_at).isoformat() if checked_at else None, "sample": False}


def _track(conn, mmsi: int) -> list[dict]:
    """그 배의 최근 위치 TRACK_POINTS개를 오래된 순으로. 최신부터 잘라 온 뒤 뒤집는다.
    점마다 source(위치 출처: worker·aisstream·seed·lookup, 옛 줄은 None)를 붙인다 (migrations/031, 10-08 사용자 결정).
    화면은 마지막 점의 출처로 핀 모양과 말풍선 "위치 출처" 줄을 정한다."""
    found = conn.execute(
        "SELECT ts, lat, lng, sog, cog, source FROM watch_positions WHERE mmsi = %s ORDER BY ts DESC LIMIT %s",
        [mmsi, TRACK_POINTS],
    ).fetchall()
    return [{"at": ts.isoformat(), "lat": lat, "lng": lng, "sog": sog, "cog": cog, "source": source}
            for ts, lat, lng, sog, cog, source in reversed(found)]


def pending_changes(conn, user_id: str) -> list[dict]:
    """사용자가 아직 못 본 변화. 최근 것부터. 가린 번호만 담는다.

    지켜보는 화물(watch)만 준다 (10-08 QA E1-8): 지켜보기를 끈 화물의 띠가 계속 남으면 "끈 화물을 왜 또 알려 주나"가 된다.
    끈 화물의 안 본 기록은 지우지 않는다. 다시 켜면 그대로 보인다 (모두 확인(mark_seen)은 끈 화물 것도 확인 처리한다).
    kind: verdict(등급 변화) / eta(등급은 그대로, 도착 예정만 기준 이상 바뀜, E1-7) / both.
    to_verdict는 화물 표의 마지막 판정(last_verdict)이다. 도착 예정만 바뀐 줄은 from_verdict도 그 등급이다(바뀌지 않았다).
    eta_from·eta_to·eta_change_hours: 도착 예정 변화. 없으면 null.
    reason: 도착 예정 변화 문장(있으면)과 마지막 확인 때의 여유 일수·통관 단계를 코드가 한 줄로 쓴 것 (접속하면 먼저 말 걸기용).
    판정이 왜 바뀌었는지의 원인이 아니라 그 시점의 상태다 (화물 표에는 원인을 따로 저장하지 않는다). 없으면 None."""
    rows = conn.execute(
        """SELECT bl_masked, label, changed_from, last_verdict, changed_at, last_days_margin, last_stage,
                  eta_changed_from, eta_changed_to, eta_change_hours FROM user_cargo
           WHERE user_id = %s AND watch AND change_seen = false ORDER BY changed_at DESC NULLS LAST""",
        [user_id],
    ).fetchall()
    out = []
    for masked, label, changed_from, verdict, changed_at, margin, stage, eta_from, eta_to, eta_hours in rows:
        moved = (SimpleNamespace(from_eta=eta_from, to_eta=eta_to, hours=eta_hours)
                 if eta_from and eta_to and eta_hours else None)
        kind = "both" if changed_from and moved else "eta" if moved else "verdict"
        out.append({"bl_masked": masked, "label": label, "kind": kind,
                    "from_verdict": changed_from or verdict, "to_verdict": verdict,
                    "changed_at": changed_at.isoformat() if changed_at else None,
                    "eta_from": eta_from.isoformat() if moved else None, "eta_to": eta_to.isoformat() if moved else None,
                    "eta_change_hours": eta_hours if moved else None,
                    "reason": _change_reason(margin, stage, moved)})
    return out


def _change_reason(margin: int | None, stage: str | None, moved=None) -> str | None:
    """도착 예정 변화 문장과 마지막 확인 기준 여유 일수·통관 단계를 사람 말 한 줄로. 있는 것만 쓴다 (없는 이유를 지어내지 않는다)."""
    parts = [p for p in (cargo.margin_text(margin), f"통관 {cargo.clean_stage(stage)}" if stage else None) if p]
    state = f"마지막 확인 기준 {', '.join(parts)}" if parts else None
    lines = [t for t in (eta_change_text(moved), state) if t]
    return " ".join(lines) if lines else None


def mark_seen(conn, user_id: str) -> int:
    """안 본 변화를 모두 "봤음"으로. 바꾼 줄 수."""
    return conn.execute("UPDATE user_cargo SET change_seen = true WHERE user_id = %s AND change_seen = false",
                        [user_id]).rowcount


# ── 주기 재판정 (워커) ──

def _due_rows(conn, limit: int, known_vessel_only: bool = False) -> list[tuple]:
    """지켜보는 중이고 진행 중인 화물을 마지막 확인·시도가 오래된 순으로 limit건. 한 번도 안 본 줄이 먼저다.
    known_vessel_only면 배(vessel_mmsi)를 아는 화물만 고른다 (워커 예산, SHIPFINDER_CALLS_PER_KNOWN_VESSEL 설명).

    순서는 GREATEST(last_checked_at, watch_attempt_at)다 (10-08 QA E1-1). 실패한 화물도 시도 시각이 남아 뒤로 간다.
    예전처럼 last_checked_at만 보면 실패·판정 없음 화물이 그 칸을 못 바꿔 매번 맨 앞에 서서 다른 화물을 굶겼다.
    자동 확인을 멈춘 화물(PAUSED_SQL)은 고르지 않는다."""
    vessel = " AND vessel_mmsi IS NOT NULL" if known_vessel_only else ""
    return conn.execute(
        f"""SELECT user_id, cargo_key, bl_encrypted, bl_masked, label, bl_type, sale_due_date, forwarder_eta,
                  voyage_no, vessel_mmsi, last_verdict, last_eta, last_eta_at
           FROM user_cargo WHERE watch AND status = 'active'{vessel} AND NOT {PAUSED_SQL}
           ORDER BY GREATEST(last_checked_at, watch_attempt_at) ASC NULLS FIRST LIMIT %s""",
        [pause_after(), limit],
    ).fetchall()


def known_vessel_due_count(conn) -> int:
    """배를 아는, 지켜보는 진행 중 화물 수(자동 확인을 멈춘 화물 제외).
    워커가 재판정 몫의 ShipFinder 예산을 이만큼만 떼어 두려고 쓴다 (worker/run.py)."""
    return conn.execute(
        f"SELECT count(*) FROM user_cargo WHERE watch AND status = 'active' AND vessel_mmsi IS NOT NULL AND NOT {PAUSED_SQL}",
        [pause_after()]).fetchone()[0]


def _no_vessel_count(conn) -> int:
    """배를 모르는(vessel_mmsi 없음), 지켜보는 진행 중 화물 수. known_vessel_only로 건너뛴 건수로 알린다."""
    return conn.execute(
        "SELECT count(*) FROM user_cargo WHERE watch AND status = 'active' AND vessel_mmsi IS NULL").fetchone()[0]


def _record_attempt(conn, user_id: str, key: str, now: datetime, *, ok: bool, count_failure: bool = True) -> None:
    """다시 판정을 시도했다고 남긴다 (10-08 QA E1-1). 성공이면 실패 수를 0으로, 실패면 하나 올린다.

    - 실패 수는 "잇달아" 센다: 지난 시도 뒤 사용자가 조회했으면(last_checked_at > watch_attempt_at) 1부터 다시 센다.
      사용자가 다시 조회하면 멈춤이 풀린다는 약속(PAUSED_SQL)과 같은 기준이다
    - count_failure=False: 화물 탓이 아닌 실패(그 사용자의 오늘 LLM 한도)다. 시도 시각만 남겨 뒤로 보내고 세지 않는다
    이 기록이 실패해도(DB 끊김) 재판정 결과에는 영향이 없다. 종류만 남긴다."""
    try:
        if ok:
            conn.execute("UPDATE user_cargo SET watch_attempt_at = %s, watch_fail_count = 0 "
                         "WHERE user_id = %s AND cargo_key = %s", [now, user_id, key])
        elif count_failure:
            conn.execute(
                """UPDATE user_cargo SET watch_fail_count = CASE
                     WHEN watch_attempt_at IS NOT NULL AND last_checked_at > watch_attempt_at THEN 1
                     ELSE watch_fail_count + 1 END, watch_attempt_at = %s
                   WHERE user_id = %s AND cargo_key = %s""", [now, user_id, key])
        else:
            conn.execute("UPDATE user_cargo SET watch_attempt_at = %s WHERE user_id = %s AND cargo_key = %s",
                         [now, user_id, key])
    except Exception as e:
        log_failure("watch_attempt", type(e).__name__)


def eta_change_text(change) -> str | None:
    """도착 예정 변화(services/port_facts.eta_change)를 알림 한 문장으로. 코드가 쓰는 문장이고 LLM은 관여하지 않는다.
    날짜는 "10월 5일" 모양, 시간은 부호 없이 "늦어짐/당겨짐"으로 말한다. 변화가 없으면 None."""
    if change is None:
        return None
    way = "늦어졌습니다" if change.hours > 0 else "당겨졌습니다"
    if change.from_eta == change.to_eta:
        # 시각으로 센 변화(user_cargo.last_eta_at)는 같은 날 안에서도 잡힌다. "5일에서 5일로"라고 쓰지 않는다
        return f"도착 예정({change.to_eta.month}월 {change.to_eta.day}일)이 약 {abs(change.hours)}시간 {way}."
    return (f"도착 예정이 {change.from_eta.month}월 {change.from_eta.day}일에서 {change.to_eta.month}월 {change.to_eta.day}일로 "
            f"약 {abs(change.hours)}시간 {way}.")


def _notice_text(name: str, old: str | None, new: str | None, eta_change=None) -> str:
    """카카오톡 알림 글. old·new가 다르면 등급 변화 문장을, eta_change가 있으면 도착 예정 변화 문장을 넣는다.
    둘 다 있으면 한 메시지에 같이 쓴다. name은 이름표나 가린 번호다 (B/L 원문 금지)."""
    parts = []
    if old and new and old != new:
        parts.append(f"{name} 화물 판정이 '{VERDICT_LABEL[old]}'에서 '{VERDICT_LABEL[new]}'(으)로 바뀌었습니다.")
    moved = eta_change_text(eta_change)
    if moved:
        parts.append(moved if parts else f"{name} 화물의 {moved}")
    return f"[Marvia] {' '.join(parts)} Marvia에서 이유와 할 일을 확인하세요."


def _hold(conn, user_id: str, key: str, text: str, since: datetime) -> None:
    """못 보낸 알림 글을 남긴다(notify_pending). 다음 실행의 _resend_pending이 다시 보낸다 (10-08 QA E1-2).
    since: 처음 못 보낸 시각. 다시 보내다 또 실패하면 처음 시각을 지킨다(rules.md watch_notify_retry_hours를 그 시각부터 잰다).
    새 알림이 생기면 새 글과 새 시각으로 바꾼다: 그 글이 지금 상태를 말한다."""
    try:
        conn.execute("UPDATE user_cargo SET notify_pending = %s, notify_pending_at = %s WHERE user_id = %s AND cargo_key = %s",
                     [text, since, user_id, key])
    except Exception as e:
        log_failure("watch_notify_hold", type(e).__name__)


def _clear_pending(conn, user_id: str, key: str) -> None:
    """보냈거나 더 보내지 않을 알림 글을 지운다. 남은 글이 없으면 바뀌는 줄이 없다."""
    try:
        conn.execute("UPDATE user_cargo SET notify_pending = NULL, notify_pending_at = NULL "
                     "WHERE user_id = %s AND cargo_key = %s AND notify_pending IS NOT NULL", [user_id, key])
    except Exception as e:
        log_failure("watch_notify_hold", type(e).__name__)


def _web() -> str:
    """알림 링크의 화면 주소. 주소 규칙은 routers/kakao._web 한 곳에 둔다 (순환을 피하려고 안에서 부른다)."""
    from backend.app.routers.kakao import _web as web
    return web()


def _send(conn, user_id: str, key: str, text: str, now: datetime, since: datetime | None = None) -> str:
    """카카오톡으로 보낸다. "sent" / "held"(다시 보낼 글로 남김) / "dropped"(안 보냄)를 돌려준다.

    - not_linked(연결 안 함): 조용히 dropped. 카카오톡은 선택 기능이다
    - relink·no_key·no_consent·not_configured·unauthorized: 사용자나 운영자가 고쳐야 하는 실패라 다시 보내지 않는다. 종류만 남긴다
    - KakaoError(failed): 카카오 호출 실패(네트워크 포함, clients/kakao.py). 다시 보낼 글로 남긴다
    - 그 밖의 예상 밖 예외(httpx.ConnectError 등): 예전에는 잡지 못해 변화 기록만 남고 실행이 failed가 됐다 (10-08 QA E1-2).
      종류만 남기고 다시 보낼 글로 남긴다"""
    try:
        kakao.send_to_user(conn, user_id, text, f"{_web()}/")
    except KakaoLinkError as e:
        if e.kind != "not_linked":
            log_failure("watch_notify", e.kind)
        _clear_pending(conn, user_id, key)
        return "dropped"
    except KakaoError as e:
        log_failure("watch_notify", e.kind)
        if e.kind == "failed":
            _hold(conn, user_id, key, text, since or now)
            return "held"
        _clear_pending(conn, user_id, key)
        return "dropped"
    except Exception as e:
        log_failure("watch_notify", type(e).__name__)
        _hold(conn, user_id, key, text, since or now)
        return "held"
    _clear_pending(conn, user_id, key)
    return "sent"


def _notify(conn, user_id: str, key: str, name: str, old: str | None, new: str | None, now: datetime,
            eta_change=None) -> str:
    """판정·도착 예정 변화를 알린다. _send의 결과("sent"/"held"/"dropped")를 돌려준다."""
    return _send(conn, user_id, key, _notice_text(name, old, new, eta_change), now)


def _resend_pending(conn, now: datetime) -> dict:
    """지난 실행에서 일시 오류로 못 보낸 알림을 다시 보낸다 (10-08 QA E1-2). {"renotified", "notify_dropped"}.

    지켜보는 화물만 다시 보낸다(끈 화물에 알림이 가면 안 된다). 반출이 끝난 화물(done)도 보낸다: 마지막 변화 알림일 수 있다.
    처음 못 보낸 뒤 rules.md watch_notify_retry_hours가 지난 글은 낡아서 보내지 않고 지운다 (변화는 화면 띠에 남아 있다)."""
    out = {"renotified": 0, "notify_dropped": 0}
    try:
        rows = conn.execute("SELECT user_id, cargo_key, notify_pending, notify_pending_at FROM user_cargo "
                            "WHERE notify_pending IS NOT NULL AND watch").fetchall()
    except Exception as e:  # 칸이 없는 옛 DB 등. 재판정은 계속한다
        log_failure("watch_notify_resend", type(e).__name__)
        return out
    limit = timedelta(hours=float(rule("watch_notify_retry_hours")))
    for user_id, key, text, since in rows:
        if since is not None and now - since > limit:
            _clear_pending(conn, user_id, key)
            out["notify_dropped"] += 1
            continue
        out["renotified"] += _send(conn, user_id, key, text, now, since=since) == "sent"
    return out


# 변화 띠 기록 (E1-7). 등급 변화와 도착 예정 변화를 한 문장으로 쓴다. %s 순서: 등급 변화?, 이전 등급, 도착 예정 변화?, 이전 날짜,
# 도착 예정 변화?, 바뀐 날짜, 도착 예정 변화?, 시간, 감지 시각, user_id, cargo_key.
# 이번에 바뀌지 않은 쪽은 아직 안 본 기록이면 그대로 두고, 이미 봤으면(change_seen) 지운다: 본 변화가 새 띠에 다시 붙지 않게 한다
CHANGE_SQL = """
UPDATE user_cargo SET
  changed_from = CASE WHEN %s::boolean THEN %s::text WHEN change_seen THEN NULL ELSE changed_from END,
  eta_changed_from = CASE WHEN %s::boolean THEN %s::date WHEN change_seen THEN NULL ELSE eta_changed_from END,
  eta_changed_to = CASE WHEN %s::boolean THEN %s::date WHEN change_seen THEN NULL ELSE eta_changed_to END,
  eta_change_hours = CASE WHEN %s::boolean THEN %s::integer WHEN change_seen THEN NULL ELSE eta_change_hours END,
  changed_at = %s, change_seen = false
WHERE user_id = %s AND cargo_key = %s
"""


def _check_one(conn, row: tuple, now: datetime, assess) -> tuple[bool, bool, str | None]:
    """화물 한 건을 다시 판정한다. (판정이 나왔나, 등급이 바뀌었나, 알림 결과)를 돌려준다.
    알림 결과는 _send의 "sent"/"held"/"dropped", 알릴 것이 없으면 None. 실패는 예외로 올려 부르는 쪽이 센다."""
    user_id, key, bl_encrypted, bl_masked, label, bl_type, sale_due, forwarder_eta, voyage_no, mmsi, old, last_eta, *rest = row
    last_eta_at = rest[0] if rest else None  # 신뢰 ETA 시각 칸. 있으면 시간 단위로 센다
    bl_no = crypto.decrypt(bl_encrypted) if bl_encrypted else None
    if bl_no is None:  # 잠근 값이 비었거나 열쇠가 바뀌어 못 연다. 원문 없이는 조회할 수 없다
        raise WatchError("no_bl")
    from backend.app.routers.agent import is_released, previous_eta, sale_due_out_of_range, trusted_vessel  # 맨 위에서 부르면 라우터와 서비스가 서로 불러오는 순환이 된다

    # 범위 검사(10-08 QA B2)가 생기기 전에 저장된 터무니없는 판매 예정일(예: 1900-01-01)은 재판정에 쓰지 않는다.
    # 사용자 조회는 그런 날짜를 되묻지만, 워커는 _assess를 바로 불러 그 검사를 거치지 않는다. 날짜 없이 배 상태로만 판정한다
    if sale_due_out_of_range(sale_due):
        sale_due = None
    request = AgentRequest(bl_no=bl_no, bl_type=bl_type, sale_due_date=sale_due, forwarder_eta=forwarder_eta,
                           voyage_no=voyage_no, resolved_mmsi=mmsi, session_id=f"watch-{now:%Y%m%d}")

    # 지켜보기 재판정도 그 사용자의 LLM 사용량이다 (docs/specs/token-quota.md).
    # user_id를 넘겨야 토큰이 그 사람 몫으로 쌓이고 Langfuse 트레이스에도 user_id가 붙는다 (SDD 5.11).
    # 오늘 한도를 다 쓴 사람의 화물은 재판정하지 않는다. 화면 조회를 막는 것과 같은 한도다
    try:
        quota.ensure_allowed(None, user_id)
    except quota.QuotaExceeded as e:
        raise WatchError("quota") from e
    # 저장된 확정 배(vessel_mmsi)는 후보 확인 검색 없이 쓴다. 워커 예산은 화물당 ShipFinder 1회라서다 (routers/agent.py trusted_vessel)
    trust = trusted_vessel.set(mmsi)
    # 지난 확인 때 저장한 신뢰 ETA 날짜를 판정 흐름에 넘긴다. 판정 흐름이 바뀐 만큼을 eta_change로 센다 (routers/agent.previous_eta)
    before = previous_eta.set(last_eta_at or last_eta)
    try:
        with session_scope(request.session_id, user_id):
            response = assess(request, None)
    finally:
        previous_eta.reset(before)
        trusted_vessel.reset(trust)
    new = response.verdict
    if new is None:  # 판정이 없는 결과로 기준 등급을 지우지 않는다 (모듈 설명 "설계 이유")
        return False, False, None
    cargo.upsert_assessment(conn, user_id, request, response, released=is_released(response), now=now)
    moved = getattr(response, "eta_change", None)  # 판정 흐름이 센 도착 예정 변화. 없으면 None
    changed = old is not None and old != new
    if not changed and not moved:
        return True, False, None
    # 띠에 남긴다. 등급은 그대로여도 도착 예정이 기준 이상 바뀌었으면 남긴다: 카카오를 안 쓰는 사용자도 다음 접속 때 본다 (E1-7)
    conn.execute(CHANGE_SQL, [changed, old if changed else None,
                              bool(moved), moved.from_eta if moved else None,
                              bool(moved), moved.to_eta if moved else None,
                              bool(moved), moved.hours if moved else None,
                              now, user_id, key])
    return True, changed, _notify(conn, user_id, key, label or bl_masked, old if changed else None,
                                  new if changed else None, now, moved)


def run_watch(conn, *, limit: int = RUN_LIMIT, now: datetime | None = None, assess=None,
              known_vessel_only: bool = False) -> dict:
    """지켜보는 화물을 다시 판정하고 결과 개수를 돌려준다:
    checked / changed / notified / failed / unjudged / notify_held / renotified / notify_dropped.

    known_vessel_only: 배(vessel_mmsi)를 아는 화물만 다시 판정한다. 수집 워커(worker/run.py)가 ShipFinder 예산을
    화물당 SHIPFINDER_CALLS_PER_KNOWN_VESSEL회로 떼어 두고 부를 때 쓴다. 배를 모르는 화물은 후보 검색으로
    최대 11회를 써서 예산을 깬다. 그 화물은 skipped_no_vessel로 센다 (이 값일 때만 결과에 들어간다).
    사용자가 화면에서 다시 조회하면 배가 정해져 vessel_mmsi가 채워지고, 그다음부터 워커가 다시 판정한다.

    assess: 판정 함수 (request, memory) -> AgentResponse. 기본은 조회 화면과 같은 routers.agent._assess다(AGENT_GRAPH=on이면 같은 판정을 그래프로 도는 graph.facade.assess_graph).
    테스트가 가짜를 끼운다. 한 건이 어디서 실패하든 나머지는 계속하고, 로그에는 오류 종류만 남긴다.
    unjudged: 판정이 비어 돌아와 화물 표를 건드리지 않은 건수 (checked에도 센다). 실패처럼 연속 실패 수에 센다 (E1-1).
    notify_held: 일시 오류로 못 보내 다음 실행에 다시 보낼 알림 수. renotified: 지난 실행에서 못 보낸 알림을 이번에 보낸 수.
    notify_dropped: 너무 오래돼 다시 보내지 않고 지운 알림 수 (rules.md watch_notify_retry_hours).
    """
    if assess is None:
        from backend.app.routers import agent  # 맨 위에서 부르면 라우터와 서비스가 서로 불러오는 순환이 된다
        if agent.graph_enabled():
            # AGENT_GRAPH=on: 같은 판정을 그래프로 돈다. 저장한 확정 배와 지난 ETA가 State로 들어간다 (v2-langgraph.md 10절 H3)
            from backend.app.graph.facade import assess_graph as assess
        else:
            from backend.app.routers.agent import _assess as assess
    now = now or datetime.now(timezone.utc)
    summary = {"checked": 0, "changed": 0, "notified": 0, "failed": 0, "unjudged": 0, "notify_held": 0}
    summary.update(_resend_pending(conn, now))  # 먼저 보낸다: 이번 재판정이 같은 줄에 새 알림을 남기면 그 글로 바뀐다
    if known_vessel_only:
        summary["skipped_no_vessel"] = _no_vessel_count(conn)
    for row in _due_rows(conn, limit, known_vessel_only):
        user_id, key = row[0], row[1]
        try:
            judged, changed, notice = _check_one(conn, row, now, assess)
        except Exception as e:  # 한 건 실패가 다른 사람의 감시를 막지 않는다. 종류만 남긴다
            kind = e.kind if isinstance(e, WatchError) else type(e).__name__
            log_failure("watch_cargo", kind)
            summary["failed"] += 1
            # 시도 시각을 남겨 다음 실행에서 뒤로 보낸다. 한도(quota)는 화물 탓이 아니라 연속 실패로 세지 않는다
            _record_attempt(conn, user_id, key, now, ok=False, count_failure=kind != "quota")
            continue
        _record_attempt(conn, user_id, key, now, ok=judged)
        summary["checked"] += 1
        summary["unjudged"] += not judged
        summary["changed"] += changed
        summary["notified"] += notice == "sent"
        summary["notify_held"] += notice == "held"
    return summary
