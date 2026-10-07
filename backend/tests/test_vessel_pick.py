"""호출부호로 배 한 척 고르기 테스트 (services/vessel_pick.py, 10-08 결정). 외부 호출 없이 돈다. 배 번호는 모의값이다."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.app.services import vessel_pick as pick

NOW = datetime(2026, 10, 8, 6, tzinfo=timezone.utc)


def _hit(mmsi, call_sign="TST01", imo=None, days=0):
    return SimpleNamespace(mmsi=mmsi, call_sign=call_sign, imo=imo, last_time=NOW - timedelta(days=days) if days is not None else None)


def test_old_holders_of_reused_call_sign_are_dropped():
    # 09-28 실측 모양: 한 호출부호에 IMO 3척, 마지막 신호 0일·123일·552일 전 → 지금 쓰는 배 하나
    hits = [_hit(1, imo=9900001, days=0), _hit(2, imo=9900002, days=123), _hit(3, imo=9900003, days=552)]
    assert pick.pick_by_call_sign(hits, "TST01", now=NOW).mmsi == 1


def test_two_recent_ships_are_not_guessed():
    hits = [_hit(1, imo=9900001, days=0), _hit(2, imo=9900002, days=3)]
    assert pick.pick_by_call_sign(hits, "TST01", now=NOW) is None


def test_same_imo_keeps_newest_mmsi():
    # 같은 배(IMO)가 MMSI를 바꿨다: 최근 MMSI 하나만 남아 정해진다
    hits = [_hit(1, imo=9900001, days=1), _hit(2, imo=9900001, days=0)]
    assert pick.pick_by_call_sign(hits, "TST01", now=NOW).mmsi == 2


def test_call_sign_match_ignores_case_and_spaces_but_not_lookalikes():
    hits = [_hit(1, call_sign=" t st01 "), _hit(2, call_sign="TST011"), _hit(3, call_sign=None)]
    assert pick.pick_by_call_sign(hits, "TST 01", now=NOW).mmsi == 1
    assert pick.pick_by_call_sign(hits, "", now=NOW) is None


def test_recent_window_comes_from_rules_and_needs_signal_time():
    from backend.app.services.rules import rule
    days = rule("vessel_recent_days")
    assert pick.pick_by_call_sign([_hit(1, days=days + 1)], "TST01", now=NOW) is None
    assert pick.pick_by_call_sign([_hit(1, days=None)], "TST01", now=NOW) is None  # 신호 시각 모름
    assert pick.pick_by_call_sign([_hit(1, days=days - 1)], "TST01", now=NOW).mmsi == 1


def test_single_counts_distinct_mmsi_for_dicts_and_objects():
    assert pick.single([{"mmsi": 1}, {"mmsi": 1}]) == {"mmsi": 1}
    assert pick.single([{"mmsi": 1}, {"mmsi": 2}]) is None and pick.single([]) is None
