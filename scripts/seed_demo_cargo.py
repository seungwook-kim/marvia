"""시연용 가공 화물을 한 사용자에게 넣는다 (10-07, docs/mockups/v2/A-enterprise-navy.html의 데이터).

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
# 부산(35.08, 129.08)으로 들어오는 점: 배마다 출발 쪽 위치에서 부산 쪽으로 4점
START = [(33.6, 126.2), (34.2, 127.4), (35.3, 129.9), (34.9, 128.6), (33.9, 127.9),
         (36.2, 124.6), (34.6, 131.5), (33.0, 129.4), (32.5, 125.8)]
BUSAN = (35.08, 129.08)


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
            (la, lo) = START[i]
            for k in range(4):  # 18시간 전부터 6시간 간격, 마지막 점이 지금 위치
                f = (k + 1) / 6
                conn.execute(
                    "insert into watch_positions (mmsi, ts, fetched_at, lat, lng, sog, cog, navistat) values (%s,%s,%s,%s,%s,%s,%s,0)",
                    (mmsis[i], now - timedelta(hours=18 - 6 * k), now, la + (BUSAN[0] - la) * f, lo + (BUSAN[1] - lo) * f, 12.5, 45.0))
        conn.commit()
    print(json.dumps({"user": uid[:8] + "...", "cargo": len(CARGO), "lc": 2, "positions": 4 * len(CARGO),
                      "changes": sum(1 for c in CARGO if c[9])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
