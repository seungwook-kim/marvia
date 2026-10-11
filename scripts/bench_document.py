"""서류 사진 판독의 정확도와 속도를 잰다 (POST /api/document 전체 경로, 실제 LLM 호출).

쓰는 법 (레포 루트에서):
  python -m scripts.bench_document --truth <정답 JSON> [--variants] [--repeat 2]

정답 JSON 모양: [{"image": "사진 경로", "fields": {"bl_no": "...", "vessel": "...", "voyage": "...", ...}, "mrn": "..."}]
실제 서류 사진과 정답에는 진짜 B/L이 들어 있으므로 레포 밖에 둔다 (CLAUDE.md "테스트, 평가, 문서에 실제 B/L 번호를 쓰지 않는다").
출력에도 B/L·MRN 값은 맞음/틀림만 적고 번호는 적지 않는다.

--variants: 같은 사진을 흐림, JPEG 저화질, 75% 축소, 2° 기울임으로 바꿔 함께 잰다. 사진이 적을 때 한 장에만 맞춘 수정인지 가리려고 둔다.
첫 요청은 OCR 모델과 프롬프트를 올리는 시간이 붙어 따로 적는다(warm 0회차).
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi.testclient import TestClient
from PIL import Image, ImageFilter

SECRET_FIELDS = {"bl_no"}  # 값을 출력하지 않는 칸


def _variants(data: bytes, on: bool):
    yield "orig", data
    if not on:
        return
    img = Image.open(io.BytesIO(data)).convert("RGB")

    def jpeg(im, q=90):
        b = io.BytesIO(); im.save(b, "JPEG", quality=q); return b.getvalue()

    yield "blur", jpeg(img.filter(ImageFilter.GaussianBlur(1.0)))
    yield "jpeg30", jpeg(img, 30)
    yield "small75", jpeg(img.resize((int(img.width * .75), int(img.height * .75)), Image.LANCZOS))
    yield "rot2", jpeg(img.rotate(2, expand=True, fillcolor=(255, 255, 255)))


def _norm(v) -> str:
    return "".join(str(v or "").upper().split()).replace(",", "").replace(".", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", required=True)
    ap.add_argument("--variants", action="store_true")
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()
    load_dotenv(".env", override=True)
    from backend.app.main import app  # .env를 읽은 뒤 불러온다
    from backend.app.auth import current_user_id

    # 로그인 없이 잰다. 사용자 없음(None)이면 서류 사실을 DB에 저장하지 않는다 (routers/document.py)
    app.dependency_overrides[current_user_id] = lambda: None

    client = TestClient(app)
    cases = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    client.post("/api/document", files={"file": ("w.jpg", Path(cases[0]["image"]).read_bytes(), "image/jpeg")})  # warm 0회차
    total_ok = total_n = 0
    for case in cases:
        raw = Path(case["image"]).read_bytes()
        for name, data in _variants(raw, args.variants):
            times, ocr_times = [], []
            body = {}
            for _ in range(args.repeat):
                t = time.monotonic()
                res = client.post("/api/document", files={"file": ("a.jpg", data, "image/jpeg")})
                times.append(int((time.monotonic() - t) * 1000))
                body = res.json()
                ocr_times.append(body.get("ocr", {}).get("ocr_ms", 0))
            fields = body.get("fields", {})
            marks = []
            for key, want in case["fields"].items():
                got = (fields.get(key) or {}).get("value")
                ok = _norm(got) == _norm(want)
                total_ok += ok; total_n += 1
                marks.append(f"{key}={'OK' if ok else 'BAD'}" + ("" if ok or key in SECRET_FIELDS else f"({got})"))
            if case.get("mrn"):
                ok = body.get("mrn") == case["mrn"]
                total_ok += ok; total_n += 1
                marks.append(f"mrn={'OK' if ok else 'BAD'}")
            print(f"{Path(case['image']).name[:12]} {name:8} total {statistics.median(times)}ms "
                  f"ocr {statistics.median(ocr_times)}ms bl_status={body.get('bl_status')} | " + " ".join(marks))
    print(f"정확도 {total_ok}/{total_n}")


if __name__ == "__main__":
    main()
