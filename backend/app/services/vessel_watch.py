"""B/L 없이 배(MMSI)만 지켜보기의 업무 규칙.

  precheck()       외부 호출(ShipFinder) 전에 이름·한도·중복을 DB로 먼저 본다 (10-08 QA E1-3)
  add_vessel()     채팅에서 본 배를 지켜보기에 올린다 (같은 배를 다시 올리면 보인 값만 갱신). 한도는 사용자 잠금 안에서 센다
  remove_vessel()  지켜보기를 푼다
  list_vessels()   내가 지켜보는 배와 마지막 위치·지나온 길 (GET /api/watch/vessels)
HTTP는 routers/watch.py가, 위치 수집은 worker/collector.py(SELECT_WATCHED)가 맡는다. 여기는 DB와 규칙만 안다.

설계 이유:
- 판정·통관·카카오톡 알림이 없다: B/L이 없어 판정 재료(통관, 판매 예정일)가 없다.
  알림은 화물 지켜보기(services/watch._notify)만 보낸다. 배만 지켜보는 줄은 화면에서 마지막 위치를 보여 줄 뿐이다
- 위치는 수집 워커가 화물 배와 같은 길(watch_positions)로 받는다. 켜는 순간에는 이미 받아 둔 위치(ShipFinder 캐시)를
  첫 점으로 넣고(services/watch.seed_position), 캐시가 비었으면 켜기 전에 위치를 한 번만 받아 둔다 (fetch_position)
- 최대 MAX_WATCH척(화물 지켜보기와 같은 값, 따로 세는 상한): 배 한 척마다 워커가 ShipFinder를 한 번 부른다 (하루 한도 예산)
- name·flag·dest·eta는 사용자가 채팅에서 본 글이다. 검증이 아니라 길이만 자르고 그대로 저장한다. 판정 값이 아니라 표시용이고,
  LLM이 정하는 값이 아니다. 목적지 글 안의 문장은 데이터일 뿐 지시로 읽지 않는다 (CLAUDE.md "하지 말 것")
B/L은 이 경로에 아예 없다. DB 연결은 부르는 쪽이 연다.
"""
from __future__ import annotations

from backend.app.clients.shipfinder import ShipFinderClient, ShipFinderError
from backend.app.observability.logging import log_failure
from backend.app.services.vessel_pick import by_call_sign, norm_sign, single
from backend.app.services.watch import MAX_WATCH, _track, owner_lock, seed_position

make_client = ShipFinderClient  # 테스트가 가짜 클라이언트로 바꿔 끼우는 자리 (mcp_server/tools/vessel_candidates.py와 같은 방식)

TEXT_MAX = 120  # 표시용 글(이름·국적·목적지·도착 예정) 길이 상한. 화면 말풍선이 깨지지 않게 자르는 값이다
# MMSI는 9자리 정수다 (ITU-R M.585). 라우터가 먼저 거르지만(routers/watch.py Mmsi) 서비스도 같은 범위만 받는다 (10-08 QA E1-5)
MMSI_MIN, MMSI_MAX = 100_000_000, 999_999_999


class VesselWatchError(Exception):
    """kind: limit(이미 MAX_WATCH척) / invalid(MMSI나 이름이 비었다) /
    unknown_vessel(호출부호로 배 번호를 하나로 못 정했다) / lookup_failed(ShipFinder 검색이 실패했다)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _clean(value: str | None) -> str | None:
    """앞뒤 공백을 떼고 TEXT_MAX로 자른다. 비면 None."""
    text = (value or "").strip()[:TEXT_MAX]
    return text or None


def _search(client, keywords: str) -> list:
    """ShipFinder 검색 한 번. 실패(시간 초과·한도 소진 등)는 lookup_failed. 클라이언트가 이미 시간 제한·재시도를 한 뒤의 결과다."""
    try:
        return client.search(keywords, max=10).hits
    except ShipFinderError as e:
        log_failure("vessel_watch_resolve", e.code)
        raise VesselWatchError("lookup_failed")
    except Exception as e:  # 클라이언트 밖 예상 못 한 실패도 사용자에게 원문을 보이지 않는다
        log_failure("vessel_watch_resolve", type(e).__name__)
        raise VesselWatchError("lookup_failed")


def resolve_mmsi(call_sign: str, name: str | None = None) -> int:
    """입항 예정 목록(해수부 신고)의 호출부호로 MMSI를 찾는다.

    왜 호출부호인가: 해수부 신고에는 MMSI가 없고, 선명은 같은 이름의 다른 배가 많다. 호출부호가 정확히 같은 배만 받아들인다.
    고르는 규칙은 services/vessel_pick.py 하나다: 같은 호출부호 → IMO마다 최신 → 최근 신호(rules.md
    vessel_recent_days) → MMSI가 하나일 때만. 예전에 그 호출부호를 쓴 배가 검색에 같이 나와도 지금 배를 고를 수 있다.
    선명으로 한 번 더: ShipFinder 검색은 선명 위주라 호출부호 검색에 같은 호출부호 배가 하나도 안 나오는 일이 잦다.
    그래서 호출부호 검색에서 쓸 만한 배(위 1~3단계를 지난 배)가 0척일 때만 선명(name)으로 다시 찾는다. 받는 조건은 같다.
    선명이 같은 다른 배를 고르지 않는다. 여러 척이 남았을 때는 다시 찾지 않는다(선명 검색도 같은 배들을 돌려줄 뿐이다).
    다시 찾는 것은 그 한 번뿐이다 (ShipFinder 하루 50회 한도).
    0척이나 여러 척이면 추측하지 않고 unknown_vessel (엉뚱한 배를 지켜보게 하면 안 된다).
    """
    sign = norm_sign(call_sign)
    if not sign:
        raise VesselWatchError("invalid")
    client = None
    try:
        try:
            client = make_client()
        except ShipFinderError as e:
            log_failure("vessel_watch_resolve", e.code)
            raise VesselWatchError("lookup_failed")
        except Exception as e:
            log_failure("vessel_watch_resolve", type(e).__name__)
            raise VesselWatchError("lookup_failed")
        rows = by_call_sign(_search(client, sign), sign)
        query = (name or "").strip()
        if not rows and query and query.upper() != sign:
            rows = by_call_sign(_search(client, query), sign)
    finally:
        if client is not None:
            client.close()
    hit = single(rows)
    if hit is None:
        log_failure("vessel_watch_resolve", "no_match" if not rows else "many_match")
        raise VesselWatchError("unknown_vessel")
    return hit.mmsi


def fetch_position(mmsi: int) -> bool:
    """지켜보기를 켜기 전에 그 배 위치가 캐시에 없으면 ShipFinder로 한 번 받아 캐시에 둔다.

    왜: 켜는 순간의 첫 점(services/watch.seed_position)은 캐시에 있는 위치만 쓴다. 입항 목록에서 상세를 열지 않고 바로 지켜보면
    캐시가 비어 있어 지도에 점이 없다. 그래서 상세를 연 것처럼 위치를 미리 받아 둔다.
    선박 자세히 보기(services/vessel_detail.py)는 후보 검색까지 여러 번 부르지만, 여기는 MMSI를 이미 알아 위치 조회 한 번뿐이다.
    캐시에 TTL(rules.md api_cache_ttl_hours) 안의 위치가 있으면 부르지 않는다 (하루 50회 한도).
    받은 값은 ShipFinderClient가 캐시에 넣는다(clients/shipfinder._cached). 실패해도 지켜보기는 점 없이 켜진다. 받았으면 True.
    DB 연결을 열기 전에 부른다: 외부 호출이 느려도 연결을 붙잡지 않게 (resolve_mmsi와 같은 이유)."""
    if not valid_mmsi(mmsi):
        return False
    if ShipFinderClient.cache.entry(f"shipfinder:pos:{mmsi}") is not None:
        return False
    client = None
    try:
        client = make_client()
        client.position(mmsi)
        return True
    except Exception as e:  # 위치를 못 받아도 켜기는 된다. 원문은 남기지 않고 종류만
        log_failure("vessel_watch_position", getattr(e, "code", None) or type(e).__name__)
        return False
    finally:
        if client is not None:
            client.close()


def valid_mmsi(mmsi) -> bool:
    """9자리 정수 MMSI인가. bool(True는 1)은 받지 않는다."""
    return isinstance(mmsi, int) and not isinstance(mmsi, bool) and MMSI_MIN <= mmsi <= MMSI_MAX


def _check_room(conn, user_id: str, mmsi: int | None) -> None:
    """올릴 자리가 있나. 이미 지켜보는 배(mmsi가 같다)는 한도를 다시 세지 않는다. 자리가 없으면 limit."""
    if mmsi is not None:
        if conn.execute("SELECT 1 FROM vessel_watch WHERE user_id = %s AND mmsi = %s", [user_id, mmsi]).fetchone():
            return
    count = conn.execute("SELECT count(*) FROM vessel_watch WHERE user_id = %s", [user_id]).fetchone()[0]
    if count >= MAX_WATCH:
        raise VesselWatchError("limit")


def precheck(conn, user_id: str, mmsi: int | None, name: str | None) -> None:
    """ShipFinder를 부르기 전에 DB만으로 거를 수 있는 것을 먼저 거른다 (10-08 QA E1-3). 못 올리면 VesselWatchError.

    - 이름이 비었거나 MMSI가 9자리가 아니면 invalid
    - 한도: 이미 MAX_WATCH척이면 limit. MMSI를 아는데 이미 지켜보는 배면 통과(보인 값만 갱신한다)
      호출부호만 온 요청(mmsi None)은 어느 배인지 아직 몰라 "이미 지켜보는 배"인지 알 수 없다. 한도가 찼으면 limit으로 끝낸다:
      그 배를 찾으려고 ShipFinder를 부르는 것이 이 확인의 목적과 어긋난다
    여기서 통과해도 저장(add_vessel)이 잠금 안에서 한도를 다시 센다. 그사이 다른 요청이 자리를 채웠을 수 있다."""
    if not _clean(name) or (mmsi is not None and not valid_mmsi(mmsi)):
        raise VesselWatchError("invalid")
    _check_room(conn, user_id, mmsi)


def add_vessel(conn, user_id: str, mmsi: int, name: str, flag: str | None = None,
               dest: str | None = None, eta: str | None = None) -> dict:
    """배를 지켜보기에 올린다. 이미 있으면 보인 값(이름·국적·목적지·도착 예정)만 갱신하고 한도를 다시 세지 않는다.

    한도 세기와 넣기를 사용자 잠금(services/watch.owner_lock) 안에서 한다. 같은 사용자의 동시 요청이 둘 다
    "4척"을 보고 6척이 되는 일을 막는다 (10-08 QA E1-10과 같은 경쟁)."""
    name = _clean(name)
    if not valid_mmsi(mmsi) or not name:
        raise VesselWatchError("invalid")
    with owner_lock(conn, user_id, "vessel_watch"):
        _check_room(conn, user_id, mmsi)
        conn.execute(
            """INSERT INTO vessel_watch (user_id, mmsi, name, flag, dest, eta) VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, mmsi) DO UPDATE SET name = EXCLUDED.name, flag = EXCLUDED.flag,
                 dest = EXCLUDED.dest, eta = EXCLUDED.eta, updated_at = now()""",
            [user_id, mmsi, name, _clean(flag), _clean(dest), _clean(eta)],
        )
    # 이미 받아 둔 위치를 지도 첫 점으로. 워커 수집 전에도 지도에 점이 나오게. 실패해도 켜기는 된다.
    # 잠금(트랜잭션) 밖에서 넣는다: 안에서 실패하면 올린 배까지 되돌려진다
    seed_position(conn, mmsi)
    return {"mmsi": mmsi, "watch": True}


def remove_vessel(conn, user_id: str, mmsi: int) -> dict:
    """지켜보기를 푼다. 없어도 오류가 아니다 (원하는 상태가 이미 그렇다)."""
    conn.execute("DELETE FROM vessel_watch WHERE user_id = %s AND mmsi = %s", [user_id, mmsi])
    return {"mmsi": mmsi, "watch": False}


def list_vessels(conn, user_id: str) -> dict:
    """내가 지켜보는 배 목록. 위치는 watch_positions의 최근 점(마지막 점이 현재), 없으면 null (수집 전).
    판정·통관 칸은 일부러 없다 (B/L이 없다).
    position_source: 마지막 점의 위치 출처(worker·aisstream·seed·lookup, 옛 줄은 null). 화물 목록(watch_list)과 같은 칸이다
    (migrations/031, 10-08 사용자 결정)."""
    rows = conn.execute(
        "SELECT mmsi, name, flag, dest, eta, created_at FROM vessel_watch WHERE user_id = %s ORDER BY created_at DESC",
        [user_id],
    ).fetchall()
    items = []
    for mmsi, name, flag, dest, eta, created in rows:
        track = _track(conn, mmsi)
        last = track[-1] if track else None
        items.append({
            "mmsi": mmsi, "name": name, "flag": flag, "dest": dest, "eta": eta,
            "created_at": created.isoformat() if created else None,
            "lat": last["lat"] if last else None, "lng": last["lng"] if last else None,
            "cog": last["cog"] if last else None, "sog": last["sog"] if last else None,
            "position_at": last["at"] if last else None, "track": track,
            "position_source": last["source"] if last else None,
        })
    return {"items": items}
