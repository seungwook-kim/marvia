"""평가 결과 두 파일을 문항별로 비교한다 (docs/specs/v2-langgraph.md 10절 H9).

    python -m evals.compare_runs evals/results/a_<v1>.json evals/results/a_<graph>.json

하는 일: 같은 id의 문항마다 응답의 status, verdict, vessel_match, mmsi, missing_inputs, decided_by와 오류(error) 칸이 같은지 본다.
정답과의 일치 여부(채점)가 아니라 두 실행의 응답이 서로 같은지를 보는 것이다. v1(AGENT_GRAPH=off) 결과와 그래프(AGENT_GRAPH=on) 결과를 맞대 볼 때 쓴다.
다른 문항을 id와 칸 이름으로 찍고, 마지막에 "차이 N건"을 출력한다. 차이가 있으면 종료 코드 1이다.
앞에 두 파일의 engine 칸(어느 판정 경로로 풀었나, run_eval.py engine_name)을 찍는다. 읽어 보여 줄 뿐 차이 수에 넣지 않는다.
engine 칸이 생기기 전 파일은 "engine 없음"으로 찍는다.
비교하지 않는 것: 지연(latency_ms), trace_id, 답 문장 시각처럼 실행마다 달라지는 값. 파일을 읽기만 하고 고치지 않는다. LLM·네트워크를 부르지 않는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 응답(got)에서 비교하는 칸. 등급·식별·되묻기 입력·누가 정했는지까지 본다 (PRD 8절 축과 같다)
GOT_FIELDS = ("status", "verdict", "vessel_match", "mmsi", "missing_inputs", "decided_by")


def _rows(path: Path) -> dict[str, dict]:
    """결과 파일의 문항을 id별로 모은다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {row["id"]: row for row in data["rows"]}


def _engine(path: Path) -> str:
    """결과 파일의 engine 칸. 칸이 생기기 전 파일에는 없다."""
    return json.loads(path.read_text(encoding="utf-8")).get("engine") or "engine 없음"


def diff(left: dict[str, dict], right: dict[str, dict]) -> list[str]:
    """두 결과의 차이를 "id 칸: 왼쪽 -> 오른쪽" 줄로 돌려준다. 한쪽에만 있는 문항도 차이다."""
    lines = []
    for item in sorted(set(left) | set(right)):
        a, b = left.get(item), right.get(item)
        if a is None or b is None:
            lines.append(f"{item} 문항이 {'왼쪽' if a is None else '오른쪽'} 파일에 없다")
            continue
        for field in GOT_FIELDS:
            va, vb = (a.get("got") or {}).get(field), (b.get("got") or {}).get(field)
            if va != vb:
                lines.append(f"{item} {field}: {va!r} -> {vb!r}")
        if a.get("error") != b.get("error"):
            lines.append(f"{item} error: {a.get('error')!r} -> {b.get('error')!r}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    print(f"왼쪽 {Path(argv[0]).name}: {_engine(Path(argv[0]))}, 오른쪽 {Path(argv[1]).name}: {_engine(Path(argv[1]))}")
    left, right = _rows(Path(argv[0])), _rows(Path(argv[1]))
    lines = diff(left, right)
    for line in lines:
        print(line)
    print(f"문항 {len(left)}건 대 {len(right)}건, 차이 {len(lines)}건")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
