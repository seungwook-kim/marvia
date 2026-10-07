"""화면 계약 스냅샷(web/scripts/backend-contract.json)이 지금 Pydantic 모델과 같은지 본다.

모델을 바꾸고 `python -m scripts.make_backend_contract`를 다시 돌리지 않으면 여기서 걸린다. 그다음
`python -m scripts.fill_web_fixtures`로 화면 예시 데이터의 빠진 칸을 채우고 `node scripts/verify-contract.mjs`(web/)로 확인한다.
"""
import json

from scripts import make_backend_contract as contract


def test_snapshot_matches_current_models():
    on_disk = json.loads(contract.OUT.read_text(encoding="utf-8"))
    assert on_disk == contract.build(), "모델이 바뀌었다. python -m scripts.make_backend_contract 를 다시 돌린다"


def test_every_object_lists_all_fields_as_required():
    # 응답은 기본값 칸도 늘 직렬화되므로 칸 전부가 필수다 (verify-contract.mjs "exact fields")
    for name, schema in contract.build()["definitions"].items():
        if schema.get("properties"):
            assert schema["required"] == list(schema["properties"]), name
