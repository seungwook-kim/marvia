"""화면 계약 검사(web/scripts/verify-contract.mjs)가 읽는 백엔드 응답 스냅샷을 Pydantic 모델에서 다시 만든다 (10-08).

    python -m scripts.make_backend_contract          # web/scripts/backend-contract.json을 다시 쓴다
    python -m scripts.make_backend_contract --check  # 지금 파일이 모델과 같은지만 본다 (다르면 종료 코드 1)

왜 만드나:
  예전 스냅샷은 손으로 옮긴 사본이라, 응답에 칸이 늘어도(customs_detail, departed_on, llm_error 등) 아무도 몰랐다.
  화면 검사는 통과하는데 실제 응답 모양은 지키지 못하는 상태였다. 모델에서 바로 뽑으면 둘이 어긋날 수 없다.

모양 (verify-contract.mjs validate가 읽는 작은 스키마):
  {"type": "object", "properties": {...}, "required": [모든 칸]}  응답은 기본값 칸도 늘 직렬화되므로 칸 전부가 필수다
  {"ref": "이름"}, {"anyOf": [...]}, {"enum": [...]}, {"type": "array", "items": ...}, {"type": "string", "format": ...}
  dict처럼 칸이 정해지지 않은 객체는 properties 없이 {"type": "object"}만 둔다
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend.app.routers.history import HistoryResponse
from backend.app.schemas.agent import AgentRequest, AgentResponse
from backend.app.schemas.document import DocumentResponse
from backend.app.schemas.memory import MemoryResponse

OUT = Path(__file__).resolve().parents[1] / "web" / "scripts" / "backend-contract.json"
# 화면 fixture가 검사받는 최상위 모델. 안쪽 모델은 $defs에서 같이 나온다
TOP = (AgentRequest, AgentResponse, DocumentResponse, HistoryResponse, MemoryResponse)
SOURCE = ("scripts/make_backend_contract.py가 Pydantic 모델(schemas/agent.py, schemas/document.py, schemas/memory.py, "
          "routers/history.py)의 직렬화 스키마에서 만든다. 손으로 고치지 않는다. 직렬화하면 모든 칸이 나오므로 칸 전부가 필수다.")


def _ref_name(ref: str) -> str:
    """'#/$defs/VesselInfo' → 'VesselInfo'."""
    return ref.rsplit("/", 1)[-1]


def convert(node: dict) -> dict:
    """JSON 스키마 한 덩어리를 검사기의 작은 모양으로 바꾼다. 설명·제목·기본값·길이 제한은 모양 검사에 쓰지 않아 버린다."""
    if "$ref" in node:
        return {"ref": _ref_name(node["$ref"])}
    if "anyOf" in node:
        return {"anyOf": [convert(x) for x in node["anyOf"]]}
    if "enum" in node:
        return {"enum": list(node["enum"])}
    if "const" in node:
        return {"enum": [node["const"]]}
    kind = node.get("type")
    if kind == "array":
        return {"type": "array", "items": convert(node.get("items", {}))}
    if kind == "object":
        props = node.get("properties")
        if not props:
            return {"type": "object"}
        return {"type": "object", "properties": {k: convert(v) for k, v in props.items()}, "required": list(props)}
    if kind is None:
        return {}  # Any: 검사기가 값을 따지지 않는다
    out = {"type": kind}
    if node.get("format") in ("date-time", "date"):
        out["format"] = "datetime" if node["format"] == "date-time" else "date"
    return out


def build() -> dict:
    """최상위 모델과 그 안의 모델을 모두 definitions로 모은다. 같은 이름이 둘이면 먼저 나온 것을 쓴다(같은 모델이다)."""
    definitions: dict[str, dict] = {}
    for model in TOP:
        schema = model.model_json_schema(mode="serialization")
        for name, sub in schema.get("$defs", {}).items():
            definitions.setdefault(name, convert(sub))
        definitions[model.__name__] = convert(schema)
    return {"source": SOURCE, "definitions": dict(sorted(definitions.items()))}


def main(argv: list[str]) -> int:
    text = json.dumps(build(), ensure_ascii=False, indent=1) + "\n"
    if "--check" in argv:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("backend-contract.json이 모델과 같다" if same else "backend-contract.json이 모델과 다르다. 다시 만들어야 한다")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"썼다: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
