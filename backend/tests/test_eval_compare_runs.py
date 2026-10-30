"""evals/compare_runs.py: 두 평가 결과 파일의 문항별 응답 비교 (docs/specs/v2-langgraph.md H9). 파일만 읽고 키·네트워크를 쓰지 않는다."""
import json

from evals import compare_runs


def _file(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def _row(item, **got):
    base = {"status": "ok", "verdict": "act", "vessel_match": "confirmed", "mmsi": 440999001, "missing_inputs": [], "decided_by": "rule"}
    return {"id": item, "got": {**base, **got}, "error": None, "latency_ms": 5}


def test_identical_runs_have_no_difference_even_when_latency_differs(tmp_path, capsys):
    a = _file(tmp_path, "a.json", [_row("N01"), _row("N02", verdict="fine")])
    rows_b = [_row("N01"), _row("N02", verdict="fine")]
    rows_b[0]["latency_ms"] = 99
    b = _file(tmp_path, "b.json", rows_b)
    assert compare_runs.main([str(a), str(b)]) == 0
    assert "차이 0건" in capsys.readouterr().out


def test_grade_status_match_and_missing_item_are_reported(tmp_path, capsys):
    a = _file(tmp_path, "a.json", [_row("N01"), _row("N02"), _row("N03")])
    b = _file(tmp_path, "b.json", [_row("N01", verdict="watch"), _row("N02", vessel_match="uncertain", status="insufficient")])
    assert compare_runs.main([str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "N01 verdict: 'act' -> 'watch'" in out and "N02 status" in out and "N02 vessel_match" in out
    assert "N03 문항이 오른쪽 파일에 없다" in out and "차이 4건" in out
