"""시연용 가공 화물을 한 사용자에게 넣는다 (docs/mockups/v2/A-enterprise-navy.html의 데이터).

실제로 움직이는 B/L을 찾기 어려워서, 로그인한 계정 하나에 화물 9건(긴급 2, 주의 3, 순항 4)을 만든다.
- user_cargo: 판정·여유 일수·통관 단계·선박·포워더·지켜보기(watch=true), 그중 3건은 판정 변동(changed_from, change_seen=false)
- user_lc: 신용장 2개(DEMO-LC-0001, DEMO-LC-0002)
- watch_positions: 배마다 부산으로 오는 위치 몇 점 (지켜보는 화물 지도용)
모든 값은 가공값이다. B/L은 MRVD로 시작하는 가짜 번호이고, 배 이름은 DEMO/TEST, MMSI는 440000001~009다.
날짜는 실행한 날(한국 시각) 기준으로 계산해 여유 일수가 판정과 맞게 한다.
다시 돌리면 같은 가짜 화물만 지우고 새로 넣는다 (다른 화물은 건드리지 않는다).

    python -m scripts.seed_demo_cargo                 # auth.users에서 가장 최근 로그인한 사람
    python -m scripts.seed_demo_cargo you@example.com # 이메일로 고르기

.env의 DATABASE_URL, BL_ENCRYPTION_KEY가 필요하다.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

KST = timezone(timedelta(hours=9))

# (B/L, 품명, 판정, 선박, 국적, 도착 예정(오늘+일), 판매 예정일(오늘+일), 통관 단계, 신용장 번호 끝자리, 이전 판정(변동이면))
CARGO = [
    ("MRVD00000001", "냉동 새우", "act", "DEMO STAR", "KR", 5, 3, "입항 전", 1, "watch"),
    ("MRVD00000002", "원면", "act", "TEST OCEAN", "PA", 2, 2, "수입신고", 1, None),
    ("MRVD00000004", "커피 생두", "watch", "DEMO BRIDGE", "LR", 1, 3, "검사 대상", 1, "fine"),
    ("MRVD00000005", "올리브유", "watch", "TEST HARBOR", "MH", 4, 6, "입항 전", 2, None),
    ("MRVD00000006", "냉장 소고기", "watch", "DEMO PACIFIC", "SG", 1, 4, "검역", 2, None),
    ("MRVD00000007", "중국 LED 패널", "fine", "DEMO HARMONY", "CN", 3, 8, "적하목록 제출", 2, "watch"),
    ("MRVD00000008", "일본 문구류", "fine", "TEST MERIDIAN", "JP", 4, 12, "입항 전", None, None),
    ("MRVD00000009", "미국 견과류", "fine", "DEMO LIBRA", "US", 9, 20, "입항 전", None, None),
    ("MRVD00000010", "대만 반도체 소켓", "fine", "TEST AURORA", "TW", 2, 16, "수입신고 수리", None, None),
]
FORWARDER, CARRIER = "데모포워딩", "데모선사"
# ── 위치 점 만들기 (10-09 사용자 지적: 지도에서 배가 자로 그은 듯 부산으로 일자로 온다) ──
# 예전에는 출발점에서 부산까지 직선을 6등분한 자리에 같은 속력·방향으로 점을 찍어, 반듯한 직선이 되고 일부는 한반도를 가로질렀다.
# 이제 데모 둘러보기 시뮬레이터(web/lib/tour.ts)와 같은 바다 항로를 곡선으로 따라가고, 점마다 속력·방향을 다르게,
# 작은 좌우 흔들림을 넣는다. 도착이 하루 안인 배는 부산 앞 묘박지에서 기다리는 중(속력 0)으로 둔다.
# 모든 점은 레포 해안선(web/lib/fixtures/geography.json)으로 육지인지 확인하고, 걸리면 흔들림을 빼고 항로 위 점을 쓴다.
JEJU_S, KOREA_STRAIT, EAST_SEA = (33.0, 126.6), (34.55, 128.75), (36.6, 130.2)
KYUSHU_S = [(32.4, 133.0), (30.0, 131.3), (29.9, 130.3), (30.6, 129.5), (31.8, 128.6), (33.0, 128.2), KOREA_STRAIT]
ROUTES = {  # 출발 항구 → (경유점, 기본 속력 노트). web/lib/tour.ts ROUTES와 같은 바다 경유점
    "칭다오": ([(35.6, 121.2), (33.6, 124.8), JEJU_S, KOREA_STRAIT], 14),
    "상하이": ([(31.0, 122.6), (32.2, 125.0), JEJU_S, KOREA_STRAIT], 18),
    "닝보": ([(29.9, 122.6), (31.5, 125.2), JEJU_S, KOREA_STRAIT], 16),
    "다롄": ([(38.6, 122.0), (36.5, 123.6), (33.8, 125.0), JEJU_S, KOREA_STRAIT], 12),
    "가오슝": ([(22.55, 120.1), (23.5, 119.2), (25.2, 120.1), (26.6, 121.4), (30.5, 125.0), JEJU_S, KOREA_STRAIT], 21),
    "샤먼": ([(24.2, 118.6), (25.2, 120.1), (26.6, 121.4), (30.0, 124.5), JEJU_S, KOREA_STRAIT], 20),
    "오사카": ([(34.3, 135.0), (33.0, 134.3), *KYUSHU_S], 13),
    "블라디보스토크": ([(42.6, 132.0), (39.5, 131.4), EAST_SEA], 15),
    "도쿄": ([(34.9, 139.9), (34.3, 139.2), (33.1, 136.0), *KYUSHU_S], 11),
}
# 화물 순서(CARGO)대로 어느 항구에서 오는 배인지. 국적·품명에 맞췄다 (중국 LED는 상하이, 일본 문구류는 오사카, 대만 소켓은 가오슝 등)
ORIGIN = ["칭다오", "닝보", "블라디보스토크", "다롄", "샤먼", "상하이", "오사카", "도쿄", "가오슝"]
ANCHORAGE = (34.97, 129.02)  # 부산 앞 묘박지 (web/lib/tour.ts와 같은 자리)
BUSAN = (35.08, 129.08)


def _dist_nm(a, b):
    import math
    d_lat = (b[0] - a[0]) * 60
    d_lng = (b[1] - a[1]) * 60 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(d_lat, d_lng)


def _smooth(way, per=10):
    """경유점을 Catmull-Rom 곡선으로 촘촘히 잇는다 (web/lib/tour.ts smooth와 같은 식)."""
    out = []
    for k in range(len(way) - 1):
        p0, p1, p2, p3 = way[max(0, k - 1)], way[k], way[k + 1], way[min(len(way) - 1, k + 2)]
        for s in range(per):
            t = s / per
            f = lambda a, b, c, d: 0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t * t + (-a + 3 * b - 3 * c + d) * t ** 3)
            out.append((f(p0[0], p1[0], p2[0], p3[0]), f(p0[1], p1[1], p2[1], p3[1])))
    out.append(way[-1])
    return out


def _along(path, d):
    """경로에서 출발 후 d해리 지점."""
    left = d
    for k in range(1, len(path)):
        seg = _dist_nm(path[k - 1], path[k])
        if left <= seg:
            f = left / seg if seg else 0
            return (path[k - 1][0] + (path[k][0] - path[k - 1][0]) * f, path[k - 1][1] + (path[k][1] - path[k - 1][1]) * f)
        left -= seg
    return path[-1]


def _land_check():
    """레포 해안선으로 점이 육지 위인지 보는 함수. 파일이 없으면 늘 False(검사 생략)."""
    from pathlib import Path
    geo_path = Path(__file__).resolve().parents[1] / "web" / "lib" / "fixtures" / "geography.json"
    if not geo_path.exists():
        return lambda lat, lng: False
    polys = []
    for feat in json.loads(geo_path.read_text(encoding="utf-8")).get("features", []):
        g = feat.get("geometry") or {}
        polys += [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []

    def in_ring(x, y, ring):
        inside, j = False, len(ring) - 1
        for i in range(len(ring)):
            (xi, yi), (xj, yj) = ring[i][:2], ring[j][:2]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside
    return lambda lat, lng: any(in_ring(lng, lat, p[0]) and not any(in_ring(lng, lat, h) for h in p[1:]) for p in polys)


def track_points(i: int, eta_days: int, now: datetime, on_land) -> list[tuple]:
    """i번 화물 배의 최근 위치 4점 [(ts, lat, lng, sog, cog)]. 6시간 간격, 마지막 점이 지금 위치.
    현재 위치는 도착 예정까지 남은 날짜만큼 묘박지에서 떨어진 항로 위다. 하루 안이면 묘박지에서 대기 중이다."""
    import math
    way, kn = ROUTES[ORIGIN[i % len(ORIGIN)]]
    anchor = (ANCHORAGE[0] + 0.02 * ((i % 3) - 1), ANCHORAGE[1] + 0.03 * ((i % 4) - 1.5))
    path = _smooth([*way, anchor])
    total = sum(_dist_nm(path[k], path[k + 1]) for k in range(len(path) - 1))
    speeds = [kn * (0.85 + 0.25 * ((i * 7 + k * 3) % 10) / 10) for k in range(4)]  # 칸마다 다른 속력
    span = sum(speeds[1:]) * 6
    waiting = eta_days <= 1  # 도착이 하루 안이면 이미 묘박지에서 기다린다
    d_now = total if waiting else max(span + 5, total - min(total - span - 5, kn * 24 * eta_days))
    dists = [d_now]
    for k in (3, 2, 1):  # 지금에서 거꾸로 6시간씩
        hold = waiting and k == 3  # 대기 중인 배는 6시간 전에도 묘박지에 있었다
        dists.insert(0, dists[0] if hold else max(0.0, dists[0] - speeds[k] * 6))
    points = []
    for k, d in enumerate(dists):
        base = _along(path, d)
        f = d / total if total else 0
        fade = math.sin(math.pi * f) if d < total else 0.0  # 출발·도착 근처는 흔들지 않는다
        lat = base[0] + 0.07 * fade * math.sin(f * math.pi * 3 + i + 1)
        lng = base[1] + 0.07 * fade * math.cos(f * math.pi * 2.3 + (i + 1) * 1.7)
        if on_land(lat, lng):  # 흔들어서 해안에 걸리면 항로 위 점을 쓴다
            lat, lng = base
        points.append([lat, lng])
    out = []
    for k, (lat, lng) in enumerate(points):
        prev = points[k - 1] if k else points[0]
        moved = _dist_nm(prev, (lat, lng))
        if k == 0 and len(points) > 1:  # 첫 점은 앞 점이 없어 다음 점으로 가는 방향을 쓴다
            nxt = points[1]
            first_cog = round((math.degrees(math.atan2((nxt[1] - lng) * math.cos(math.radians(lat)), nxt[0] - lat)) + 360) % 360)
            out.append((now - timedelta(hours=18), lat, lng, round(speeds[0], 1), first_cog if _dist_nm(points[0], nxt) > 0.1 else 0))
            continue
        sog = round(moved / 6, 1) if k else round(speeds[0] * (0 if waiting and k == 0 and dists[0] == dists[-1] else 1), 1)
        cog = round((math.degrees(math.atan2((lng - prev[1]) * math.cos(math.radians(lat)), lat - prev[0])) + 360) % 360) if moved > 0.1 else 0
        out.append((now - timedelta(hours=18 - 6 * k), lat, lng, sog if moved > 0.1 or not k else 0.0, cog))
    return out


def main() -> int:
    load_dotenv(".env")
    from backend.app import crypto
    from backend.app.db import connect
    from backend.app.schemas.customs import mask_bl
    from backend.app.services.cargo import cargo_key

    email = sys.argv[1].strip().lower() if len(sys.argv) > 1 else None
    today = datetime.now(KST).date()
    now = datetime.now(timezone.utc)
    with connect() as conn:
        if email:
            row = conn.execute("select id from auth.users where lower(email) = %s", (email,)).fetchone()
        else:
            row = conn.execute("select id from auth.users order by last_sign_in_at desc nulls last limit 1").fetchone()
        if not row:
            print("로그인한 사용자를 찾지 못했습니다. 웹에서 한 번 로그인한 뒤 다시 돌리세요")
            return 1
        uid = str(row[0])
        on_land = _land_check()  # 위치 점이 육지 위에 놓이지 않게 확인하는 함수 (track_points)
        keys = [cargo_key(c[0]) for c in CARGO]
        if None in keys:
            print("BL_ENCRYPTION_KEY가 없습니다. .env를 확인하세요")
            return 1
        mmsis = [440000001 + i for i in range(len(CARGO))]

        # 같은 가짜 화물·신용장·위치만 지운다
        conn.execute("delete from user_cargo where user_id = %s and cargo_key = any(%s)", (uid, keys))
        conn.execute("delete from user_lc where user_id = %s and lc_no like 'DEMO-LC-%%'", (uid,))
        conn.execute("delete from watch_positions where mmsi = any(%s)", (mmsis,))

        lc_ids = {}
        for n, opened in ((1, today - timedelta(days=48)), (2, today - timedelta(days=27))):
            lc_ids[n] = conn.execute(
                "insert into user_lc (user_id, lc_no, opened_on) values (%s, %s, %s) returning id",
                (uid, f"DEMO-LC-000{n}", opened)).fetchone()[0]

        for i, (bl, goods, verdict, ship, flag, eta_d, sale_d, stage, lc, prev) in enumerate(CARGO):
            eta, sale = today + timedelta(days=eta_d), today + timedelta(days=sale_d)
            conn.execute(
                """insert into user_cargo (user_id, cargo_key, bl_masked, bl_encrypted, bl_type, label, status,
                     sale_due_date, sale_due_asked, vessel_mmsi, vessel_source, last_verdict, last_checked_at, last_days_margin,
                     last_eta, last_stage, pending, watch, changed_from, changed_at, change_seen, lc_id, departed_on,
                     forwarder_name, carrier_name, container_no, goods_name, vessel_name, vessel_flag)
                   values (%s,%s,%s,%s,'HOUSE',%s,'active', %s,true,%s,'mrn',%s,%s,%s, %s,%s,'[]'::jsonb,true,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s)""",
                (uid, keys[i], mask_bl(bl), crypto.encrypt(bl), goods,
                 sale, mmsis[i], verdict, now - timedelta(minutes=30 + 17 * i), sale_d - eta_d,
                 eta, stage, prev, (now - timedelta(hours=1 + 2 * i)) if prev else None, prev is None,
                 lc_ids.get(lc) if lc else None, eta - timedelta(days=12),
                 FORWARDER, CARRIER, f"DEMU{1234560 + i}", goods, ship, flag))
            # 18시간 전부터 6시간 간격 4점, 마지막 점이 지금 위치. 바다 항로를 따라가고 점마다 속력·방향이 다르다 (track_points).
            # 출처는 정기 수집(worker)으로 둔다: 지도에서 보통 핀으로 보이는 시연 데이터다 (migrations/031)
            for ts, lat, lng, sog, cog in track_points(i, eta_d, now, on_land):
                conn.execute(
                    "insert into watch_positions (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat, source) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,'worker')",
                    (mmsis[i], ts, now, lat, lng, sog, cog, 1 if sog == 0 else 0))  # navistat 1 = 닻 내림(묘박 대기)
        conn.commit()
    print(json.dumps({"user": uid[:8] + "...", "cargo": len(CARGO), "lc": 2, "positions": 4 * len(CARGO),
                      "changes": sum(1 for c in CARGO if c[9])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
