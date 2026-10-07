"""화면 예시 데이터(web/lib/fixtures/*.json)에 응답 모델에서 빠진 칸을 채운다.

    python -m scripts.fill_web_fixtures

왜: 실제 응답은 기본값 칸도 늘 직렬화된다. 예시 데이터에 그 칸이 없으면 화면 계약 검사(verify-contract.mjs "exact fields")가 걸리고,
화면이 실제 서버 응답과 다른 모양을 보고 만들어진다. 기존 값은 하나도 바꾸지 않고, 빠진 칸만 모델이 직렬화한 값(기본값)으로 넣는다.
예외: 서버 코드가 계산하는 칸(document.json의 bl_status·important·flagged)은 서버 규칙으로 다시 채운다 (computed, 10-08 QA D8).
날짜 글자 모양 같은 기존 값의 표기도 그대로 둔다 (데모 시각 옮기기가 그 표기를 읽는다).
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.app.routers.history import HistoryResponse
from backend.app.schemas.agent import AgentResponse
from backend.app.schemas.document import DocumentResponse
from backend.app.schemas.memory import MemoryResponse

FIXTURES = Path(__file__).resolve().parents[1] / "web" / "lib" / "fixtures"
# 파일 → 모델. verify-contract.mjs가 검사하는 목록과 같다
TARGETS = {**{f"{i}.json": AgentResponse for i in ("ok", "partial", "insufficient", "failed", "unsupported", "watch", "act")},
           "history.json": HistoryResponse, "document.json": DocumentResponse, "memory.json": MemoryResponse}


def fill(have, full):
    """have에 없는 칸만 full에서 가져온다. 목록은 같은 자리끼리 안으로 들어간다. 있는 값은 건드리지 않는다."""
    if isinstance(have, dict) and isinstance(full, dict):
        out = dict(have)
        for key, value in full.items():
            out[key] = fill(have[key], value) if key in have else value
        return out
    if isinstance(have, list) and isinstance(full, list) and len(have) == len(full):
        return [fill(a, b) for a, b in zip(have, full)]
    return have


def computed(name: str, data: dict) -> dict:
    """서버 코드가 계산하는 칸은 기본값이 아니라 서버 규칙으로 다시 채운다. 지금은 document.json의 bl_status·important·flagged.

    10-08 QA D8: 기본값(bl_status "none")이 그대로 들어가 체험 모드의 사진 첨부가 조회를 하지 않았다.
    규칙은 routers/document.py의 함수를 그대로 부른다(같은 판단을 두 곳에 쓰지 않는다). 이미 들어 있는 값도 규칙 값으로 덮는다:
    fields를 고치면 계산 칸도 따라가야 실제 서버 응답과 같은 모양이 된다. bl_candidates는 OCR 줄이 있어야 알 수 있어 그대로 둔다.
    """
    if name != "document.json":
        return data
    from backend.app.routers.document import bl_status_of, flagged_of, important_of

    fields = DocumentResponse.model_validate(data).fields
    return {**data, "bl_status": bl_status_of(fields), "important": important_of(fields), "flagged": flagged_of(fields)}


def main() -> None:
    for name, model in TARGETS.items():
        path = FIXTURES / name
        have = json.loads(path.read_text(encoding="utf-8"))
        full = model.model_validate(have).model_dump(mode="json")
        filled = computed(name, fill(have, full))
        if filled != have:
            path.write_text(json.dumps(filled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("채움:", name)


if __name__ == "__main__":
    main()
