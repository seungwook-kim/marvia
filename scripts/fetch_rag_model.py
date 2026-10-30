"""임베딩 모델 파일을 고정 리비전으로 내려받아 검증하는 스크립트 (docs/specs/v2-rag.md 9절, R4).

    python -m scripts.fetch_rag_model                       # 후보 1(multilingual-e5-small int8)을 models/rag/<id>/ 에 받는다
    python -m scripts.fetch_rag_model --model <id> --dest <폴더>
    python -m scripts.fetch_rag_model --verify-only         # 받지 않고 있는 파일만 검사한다 (빌드·점검용)

받은 파일은 레포에 커밋하지 않는다 (models/는 .gitignore). Docker 빌드 때 이 스크립트를 돌려 이미지에 굽고, 서버는 런타임에
인터넷에서 모델을 받지 않는다 (Spec 9절). 고정값(리비전, 파일 sha256)은 backend/app/services/rag/embed.py MODEL_SPECS에 있다.

실패하는 경우(종료 코드 1):
  - 받은 파일의 sha256이 고정값과 다르다 (같은 이름의 다른 파일일 수 있다. 받은 파일은 지운다)
  - docs/rag/index_manifest.json의 embedding 칸이 이 모델의 고정값과 다르다 (R4 완료 기준 b).
    그 색인은 다른 모델이나 다른 파일로 만든 것이라 질의 벡터와 같은 공간이 아니다. 새 모델로 갈아탈 때는
    색인을 다시 만들어 manifest를 바꾼 뒤 받거나, 일부러 건너뛰려면 --skip-manifest-check
네트워크는 모델 파일을 받을 때만 쓴다. 키는 필요 없다. 받은 파일은 데이터로만 다루고(해시 확인 뒤 onnxruntime이 읽는다) 실행하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from backend.app.services.rag.embed import (
    DEFAULT_MODEL_ID,
    MODEL_SPECS,
    REPO_ROOT,
    ModelSpec,
    PinnedFile,
    default_model_dir,
    manifest_problems,
    sha256_file,
    verify_model_dir,
    write_model_meta,
)

DEFAULT_MANIFEST = REPO_ROOT / "docs" / "rag" / "index_manifest.json"
# 한 번 읽기 대기 상한(초). 큰 파일이라 전체 시간이 아니라 "소식이 끊긴 시간"을 본다
_READ_TIMEOUT_SEC = 60
_CHUNK = 1024 * 1024


def _download(spec: ModelSpec, pinned: PinnedFile, dest: Path) -> None:
    """고정 리비전 주소에서 파일 하나를 받아 sha256을 확인한 뒤 dest에 놓는다. 다르면 지우고 예외."""
    url = spec.download_url(pinned)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    print(f"받는 중: {pinned.local_name} ({pinned.size_bytes / 1e6:.1f} MB) <- {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "marvia-fetch-rag-model"})
    with urllib.request.urlopen(request, timeout=_READ_TIMEOUT_SEC) as response, part.open("wb") as out:  # noqa: S310 - https 고정 주소
        for block in iter(lambda: response.read(_CHUNK), b""):
            digest.update(block)
            out.write(block)
    if digest.hexdigest() != pinned.sha256:
        part.unlink(missing_ok=True)
        raise ValueError(
            f"{pinned.local_name} sha256 불일치: 고정값 {pinned.sha256}, 받은 값 {digest.hexdigest()}")
    part.replace(dest)


def _manifest_check(spec: ModelSpec, manifest_path: Path) -> list[str]:
    """manifest가 있으면 embedding 칸을 이 모델의 고정값과 견준다. 파일이 없으면 견줄 것이 없다."""
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return [f"{manifest_path} 가 JSON이 아니다"]
    return manifest_problems(spec, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 임베딩 모델을 고정 리비전으로 받는다")
    parser.add_argument("--model", default=DEFAULT_MODEL_ID, choices=sorted(MODEL_SPECS), help="MODEL_SPECS의 모델 id")
    parser.add_argument("--dest", type=Path, default=None, help="저장 폴더 (기본 models/rag/<모델 id>)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="비교할 색인 manifest")
    parser.add_argument("--skip-manifest-check", action="store_true", help="manifest와 견주지 않는다")
    parser.add_argument("--verify-only", action="store_true", help="받지 않고 있는 파일만 검사한다")
    args = parser.parse_args(argv)

    spec = MODEL_SPECS[args.model]
    dest = args.dest or default_model_dir(spec.model_id)

    # manifest 검사는 받기 전에 먼저 한다. 어긋난 채 수백 MB를 받아 봐야 쓸 수 없다
    if not args.skip_manifest_check:
        problems = _manifest_check(spec, args.manifest)
        if problems:
            print("실패: 색인 manifest와 모델 고정값이 다르다", *problems, sep="\n  - ", file=sys.stderr)
            return 1

    if args.verify_only:
        problems = verify_model_dir(dest, spec)
        if problems:
            print(f"실패: {dest}", *problems, sep="\n  - ", file=sys.stderr)
            return 1
        print(f"정상: {dest} ({spec.model_id}, 리비전 {spec.revision[:12]})")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    for pinned in (spec.model_file, spec.tokenizer_file):
        target = dest / pinned.local_name
        if target.is_file() and sha256_file(target) == pinned.sha256:
            print(f"이미 있음(해시 일치): {pinned.local_name}")
            continue
        try:
            _download(spec, pinned, target)
        except (ValueError, OSError) as exc:
            print(f"실패: {exc}", file=sys.stderr)
            return 1
    write_model_meta(dest, spec)
    print(f"완료: {dest} ({spec.model_id}, 리비전 {spec.revision[:12]}, 라이선스 {spec.license})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
