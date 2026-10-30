"""채팅 RAG 색인을 오프라인으로 만든다 (docs/specs/v2-rag.md 3절, 작업 R7). 서버가 켜질 때는 색인을 만들지 않는다.

    python -m scripts.build_rag_index            # 청킹 + 누출 게이트 + 임베딩 + 파일 색인(docs/rag/index/) + manifest 쓰기
    python -m scripts.build_rag_index --db       # 위 + DATABASE_URL 의 rag_* 표에 반영(index_id 단위, 활성 색인 전환은 한 트랜잭션)
    python -m scripts.build_rag_index --check    # 아무것도 쓰지 않고 manifest 지문과 현재 상태를 견준다 (CI)

임베더는 RAG_EMBED_BACKEND(onnx 기본, fake)와 RAG_EMBED_MODEL_DIR로 고른다 (--backend로 덮어쓸 수 있다). 모델은 scripts/fetch_rag_model.py로 먼저 받는다.
종료 코드: 0 정상, 1 점검·누출 게이트·DB 반영 실패, 2 임베더를 만들 수 없음(빌드). --check는 모델 파일이 없으면 manifest의 모델 설정으로 견주고 알린다.
이 스크립트는 값(조각 글, 질의)을 출력하지 않는다. 누출이 발견되면 종류와 파일, 조각 번호만 보여 준다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from backend.app.services.rag import index_build
from backend.app.services.rag.corpus import CORPUS_FILE, load_corpus
from backend.app.services.rag.embed import EmbedderUnavailable, make_embedder
from backend.app.services.rag.store import StoreError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="채팅 RAG 색인 빌드 (v2-rag.md 3절)")
    parser.add_argument("--db", action="store_true", help="파일 색인에 더해 DATABASE_URL의 rag_* 표에도 반영한다")
    parser.add_argument("--check", action="store_true", help="아무것도 쓰지 않고 manifest 지문과 현재 상태를 견준다")
    parser.add_argument("--backend", choices=("onnx", "fake"), help="RAG_EMBED_BACKEND를 덮어쓴다")
    parser.add_argument("--corpus", type=Path, default=CORPUS_FILE, help="코퍼스 입력 목록 (기본 docs/rag/corpus.yaml)")
    parser.add_argument("--index-dir", type=Path, default=index_build.INDEX_DIR, help="파일 색인 폴더")
    parser.add_argument("--manifest", type=Path, default=index_build.MANIFEST_FILE, help="index_manifest.json 위치")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.backend:
        os.environ["RAG_EMBED_BACKEND"] = args.backend
    corpus = load_corpus(args.corpus)
    try:
        embedder = make_embedder()
    except EmbedderUnavailable as exc:
        if not args.check:
            print(f"임베더를 만들 수 없다: {exc}", file=sys.stderr)
            return 2
        embedder = None  # 점검은 모델 없이도 코퍼스와 청킹 변화를 잡는다
    if args.check:
        problems, notes = index_build.check(embedder, manifest_path=args.manifest, index_dir=args.index_dir, corpus=corpus)
        for note in notes:
            print(f"알림: {note}")
        for problem in problems:
            print(f"실패: {problem}", file=sys.stderr)
        print("색인이 manifest와 같다" if not problems else f"점검 실패 {len(problems)}건")
        return 1 if problems else 0
    try:
        manifest = index_build.build(embedder, corpus=corpus, index_dir=args.index_dir, manifest_path=args.manifest, to_db=args.db)
    except index_build.LeakError as exc:
        print(f"빌드 실패: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"DB 반영 실패: {exc}", file=sys.stderr)
        return 1
    print(f"색인 만듦: 조각 {manifest['chunk_count']}개, 모델 {manifest['embedding']['model']} ({manifest['embedding']['backend']}), "
          f"index_id {manifest['index_id']}" + (", DB 반영함" if args.db else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
