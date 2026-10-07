"""B/L 없이 배(MMSI)만 지켜보기의 업무 규칙 (10-08 정운 실사용 57번).

하는 일:
  add_vessel()     채팅에서 본 배를 지켜보기에 올린다 (같은 배를 다시 올리면 보인 값만 갱신)
  remove_vessel()  지켜보기를 푼다
  list_vessels()   내가 지켜보는 배와 마지막 위치·지나온 길 (GET /api/watch/vessels)
HTTP는 routers/watch.py가, 위치 수집은 worker/collector.py(SELECT_WATCHED)가 맡는다. 여기는 DB와 규칙만 안다.

왜 이렇게 했나:
- 판정·통관·카카오톡 알림이 없다: B/L이 없어 판정 재료(통관, 판매 예정일)가 없고, "도착 예정이 바뀌었다" 같은 사건을
  기록하는 곳도 아직 없다. 알림은 판정 변화(services/watch._notify)만 보낸다. 배만 지켜보는 줄은 화면에서 마지막 위치를 보여 줄 뿐이다
- 위치는 새로 받는 코드를 만들지 않는다: 수집 워커가 화물 배와 같은 길(watch_positions, 017)로 받는다. 켜는 순간에는
  이미 받아 둔 위치(ShipFinder 캐시)를 첫 점으로 넣는다 (services/watch.seed_position, 10-08). 캐시가 비었으면
  켜기 전에 위치를 한 번만 받아 둔다 (fetch_position, 상세를 안 열고 지켜본 배도 지도에 나오게, 10-08 사용자 결정)
- 최대 MAX_WATCH척(화물 지켜보기와 같은 값, 따로 세는 상한): 배 한 척마다 워커가 ShipFinder를 한 번 부른다 (하루 한도 예산)
- name·flag·dest·eta는 사용자가 채팅에서 본 글이다. 검증이 아니라 길이만 자르고 그대로 저장한다. 판정 값이 아니라 표시용이고,
  LLM이 정하는 값이 아니다. 목적지 글 안의 문장은 데이터일 뿐 지시로 읽지 않는다 (CLAUDE.md "하지 말 것")
B/L은 이 경로에 아예 없다. DB 연결은 부르는 쪽이 연다.
"""
from __future__ import annotations

from backend.app.clients.shipfinder import ShipFinderClient, ShipFinderError
from backend.app.observability.logging import log_failure
from backend.app.services.watch import MAX_WATCH, _track, seed_position

make_client = ShipFinderClient  # 테스트가 가짜 클라이언트로 바꿔 끼우는 자리 (mcp_server/tools/vessel_candidates.py와 같은 방식)

TEXT_MAX = 120  # 표시용 글(이름·국적·목적지·도착 예정) 길이 상한. 화면 말풍선이 깨지지 않게 자르는 값이다


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


def _same_sign(hits: list, sign: str) -> set[int]:
    """검색 결과 중 호출부호가 정확히 같은 배의 MMSI들."""
    return {h.mmsi for h in hits if (h.call_sign or "").strip().upper() == sign}


def resolve_mmsi(call_sign: str, name: str | None = None) -> int:
    """입항 예정 목록(해수부 신고)의 호출부호로 MMSI를 찾는다 (10-08 정운 실사용 57번).

    왜 호출부호인가: 해수부 신고에는 MMSI가 없고, 선명은 같은 이름의 다른 배가 많다. 호출부호가 정확히 같은 배만 받아들인다.
    ShipFinder 검색은 예전에 같은 호출부호를 쓴 배도 돌려주므로 MMSI가 정확히 하나일 때만 정한다.
    선명으로 한 번 더 (10-08): ShipFinder 검색은 선명 위주라 호출부호 검색이 0건인 일이 잦다. 그래서 목록에서 "중국에서 오는 배"를
    지켜보려 하면 422가 났다(선박 자세히 보기는 선명과 호출부호를 함께 써서 배가 나오는데). 호출부호 검색에 같은 호출부호가
    없을 때만 선명(name)으로 다시 찾는다. 이때도 받는 조건은 같다: 호출부호가 정확히 같은 MMSI가 하나일 때만.
    선명이 같은 다른 배를 고르지 않는다. 다시 찾는 것은 그 한 번뿐이다 (ShipFinder 하루 50회 한도).
    0건이나 여러 건이면 추측하지 않고 unknown_vessel (엉뚱한 배를 지켜보게 하면 안 된다).
    """
    sign = (call_sign or "").strip().upper()
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
        mmsis = _same_sign(_search(client, sign), sign)
        query = (name or "").strip()
        if not mmsis and query and query.upper() != sign:
            mmsis = _same_sign(_search(client, query), sign)
    finally:
        if client is not None:
            client.close()
    if len(mmsis) != 1:
        log_failure("vessel_watch_resolve", "no_match" if not mmsis else "many_match")
        raise VesselWatchError("unknown_vessel")
    return mmsis.pop()


def fetch_position(mmsi: int) -> bool:
    """지켜보기를 켜기 전에 그 배 위치가 캐시에 없으면 ShipFinder로 한 번 받아 캐시에 둔다 (10-08 사용자 결정).

    왜: 켜는 순간의 첫 점(services/watch.seed_position)은 캐시에 있는 위치만 쓴다. 입항 목록에서 상세를 열지 않고 바로 지켜보면
    캐시가 비어 있어 감시 지도에 점이 없었다. 사용자가 "상세를 안 열었으면 자동으로 조회해서 가져온 것처럼" 하자고 정했다.
    선박 자세히 보기(services/vessel_detail.py)는 후보 검색까지 여러 번 부르지만, 여기는 MMSI를 이미 알아 위치 조회 한 번뿐이다.
    캐시에 TTL(rules.md api_cache_ttl_hours) 안의 위치가 있으면 부르지 않는다 (하루 50회 한도).
    받은 값은 ShipFinderClient가 캐시에 넣는다(clients/shipfinder._cached). 실패해도 지켜보기는 점 없이 켜진다. 받았으면 True.
    DB 연결을 열기 전에 부른다: 외부 호출이 느려도 연결을 붙잡지 않게 (resolve_mmsi와 같은 이유)."""
    if not mmsi or mmsi <= 0:
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


def add_vessel(conn, user_id: str, mmsi: int, name: str, flag: str | None = None,
               dest: str | None = None, eta: str | None = None) -> dict:
    """배를 지켜보기에 올린다. 이미 있으면 보인 값(이름·국적·목적지·도착 예정)만 갱신하고 한도를 다시 세지 않는다."""
    name = _clean(name)
    if not mmsi or mmsi <= 0 or not name:
        raise VesselWatchError("invalid")
    exists = conn.execute("SELECT 1 FROM vessel_watch WHERE user_id = %s AND mmsi = %s", [user_id, mmsi]).fetchone()
    if not exists:
        count = conn.execute("SELECT count(*) FROM vessel_watch WHERE user_id = %s", [user_id]).fetchone()[0]
        if count >= MAX_WATCH:
            raise VesselWatchError("limit")
    conn.execute(
        """INSERT INTO vessel_watch (user_id, mmsi, name, flag, dest, eta) VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, mmsi) DO UPDATE SET name = EXCLUDED.name, flag = EXCLUDED.flag,
             dest = EXCLUDED.dest, eta = EXCLUDED.eta, updated_at = now()""",
        [user_id, mmsi, name, _clean(flag), _clean(dest), _clean(eta)],
    )
    # 이미 받아 둔 위치를 지도 첫 점으로 (10-08). 워커 스케줄러가 꺼져 있어 이게 없으면 지도에 점이 없다. 실패해도 켜기는 된다
    seed_position(conn, mmsi)
    return {"mmsi": mmsi, "watch": True}


def remove_vessel(conn, user_id: str, mmsi: int) -> dict:
    """지켜보기를 푼다. 없어도 오류가 아니다 (원하는 상태가 이미 그렇다)."""
    conn.execute("DELETE FROM vessel_watch WHERE user_id = %s AND mmsi = %s", [user_id, mmsi])
    return {"mmsi": mmsi, "watch": False}


def list_vessels(conn, user_id: str) -> dict:
    """내가 지켜보는 배 목록. 위치는 watch_positions의 최근 점(마지막 점이 현재), 없으면 null (수집 전).
    판정·통관 칸은 일부러 없다 (B/L이 없다)."""
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
        })
    return {"items": items}
