"""채팅 RAG 검색 지연 측정 (docs/specs/v2-rag.md 작업 R10). 결과는 evals/results/rag_latency_<시각>.json에 쓴다.

무엇을 재나: 진짜 코퍼스(docs/rag/corpus.yaml)와 저장소의 파일 색인(docs/rag/index/)으로, search_knowledge 도구가 하는 일 전체
(services/rag/runtime.run_search: 질의 만들기 + BM25 + (hybrid면) 질의 임베딩 + 코사인 + RRF)를 방식별로 잰다.
질의는 코퍼스 조각의 제목(절 이름)에서 고정된 규칙으로 만든 목록이다: (1) 제목 하나, (2) 이웃한 제목 둘을 이은 것.
사용자 평가 질문이나 대화 기록은 쓰지 않는다. 같은 코퍼스면 같은 질의 목록이 나온다(순서와 중복 제거 규칙이 고정이다).
실행: python -m scripts.measure_rag_latency [--repeats N] [--threads 기본,1] [--out-dir DIR]
hybrid에는 로컬 임베딩 모델(models/rag/<모델 id>/, scripts/fetch_rag_model)이 있어야 하고, 색인이 그 모델로 만든 것이어야 한다.
없으면 그 방식은 건너뛰고 이유를 결과에 적는다 (지어낸 숫자는 쓰지 않는다).

보고하는 값: 방식과 스레드 설정마다 질의 수, 측정 횟수, p50/p95/최대/평균 ms(도구 전체 시간), hybrid의 질의 임베딩 시간,
첫 질의(모델 로드 직후) 시간, Searcher 만들기 시간. 임계값을 정하는 일은 사람이 한다: 이 스크립트는 rules.md를 고치지 않는다.
한계: 한 대의 개발 기계에서 잰 값이다. Render 무료 인스턴스(CPU 0.1, RAM 512 MB)의 값이 아니다. 스레드 1은 느린 CPU의 대용품일 뿐이다.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app.services.rag import runtime  # noqa: E402
from backend.app.services.rag.corpus import chunk_corpus  # noqa: E402
from backend.app.services.rag.query import MaskedText  # noqa: E402

MS_PER_SEC = 1000
DEFAULT_REPEATS = 5
HEADING_MARK = re.compile(r"^#+\s*")


def heading_queries() -> dict[str, list[str]]:
    """코퍼스 조각의 제목에서 질의 목록을 만든다. single: 마지막 제목 조각마다 하나(중복 제거, 처음 나온 순서).
    pairs: single의 이웃한 둘을 공백으로 이은 것(더 긴 질의의 대용). 가리기 검사(MaskedText.of)를 통과한 것만 쓴다."""
    seen: list[str] = []
    for chunk in chunk_corpus():
        last = chunk.heading_path.split(">")[-1].strip()
        title = HEADING_MARK.sub("", last).strip()
        if title and title not in seen:
            seen.append(title)
    singles = [t for t in seen if _masked_ok(t)]
    pairs = [f"{a} {b}" for a, b in zip(singles, singles[1:]) if _masked_ok(f"{a} {b}")]
    return {"single": singles, "pairs": pairs}


def _masked_ok(text: str) -> bool:
    try:
        MaskedText.of(text)
        return True
    except (ValueError, TypeError):
        return False


def percentile(values: list[float], q: float) -> float:
    """선형 보간 백분위 (numpy 기본과 같다). 표본이 하나면 그 값."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def summarize(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": round(percentile(values, 0.5), 2), "p95_ms": round(percentile(values, 0.95), 2),
            "max_ms": round(max(values), 2), "mean_ms": round(statistics.fmean(values), 2)}


def measure(mode: str, threads: str | None, queries: dict[str, list[str]], repeats: int) -> dict:
    """방식 하나, 스레드 설정 하나를 잰다. Searcher를 새로 만들고(만들기 시간 기록), 첫 질의(콜드)를 따로 재고,
    한 바퀴 예열한 뒤 repeats바퀴를 잰다."""
    os.environ["RAG_MODE"] = mode
    os.environ["RAG_STORE"] = "file"
    os.environ["RAG_EMBED_BACKEND"] = "onnx"
    if threads:
        os.environ["RAG_EMBED_THREADS"] = threads
    else:
        os.environ.pop("RAG_EMBED_THREADS", None)
    runtime.reset()
    started = time.perf_counter()
    searcher = runtime.active_searcher()
    build_ms = (time.perf_counter() - started) * MS_PER_SEC
    if searcher is None:
        return {"mode": mode, "embed_threads": threads, "skipped": "Searcher를 만들 수 없다(모델 파일 없음, 색인 지문 불일치, 저장소 오류 중 하나). 서버 로그의 marvia.rag 경고를 본다"}
    if mode == "hybrid" and searcher.embedder is None:
        return {"mode": mode, "embed_threads": threads, "skipped": "임베더를 만들 수 없어 BM25 단독으로 내려갔다(hybrid 측정이 아니다)"}
    first_query = queries["single"][0]
    started = time.perf_counter()
    runtime.run_search(searcher, [first_query])
    first_ms = (time.perf_counter() - started) * MS_PER_SEC
    out: dict = {"mode": mode, "embed_threads": threads, "searcher_build_ms": round(build_ms, 1), "first_query_ms": round(first_ms, 1),
                 "index_id": searcher._index_id, "chunks": len(searcher._chunks), "embed_model": getattr(searcher.embedder, "model_id", None)}
    for name, texts in queries.items():
        for text in texts:  # 예열 한 바퀴 (측정에 넣지 않는다)
            runtime.run_search(searcher, [text])
        total, embed, search_ms, wrong = [], [], [], 0
        for _ in range(repeats):
            for text in texts:
                started = time.perf_counter()
                result, found = runtime.run_search(searcher, [text])
                total.append((time.perf_counter() - started) * MS_PER_SEC)
                if found is None or "error" in result:
                    wrong += 1
                    continue
                search_ms.append(found.search_ms)
                if found.embed_ms is not None:
                    embed.append(found.embed_ms)
        out[name] = {"queries": len(texts), "failed_calls": wrong, "tool_total": summarize(total), "searcher_search_ms": summarize(search_ms)}
        if embed:
            out[name]["query_embedding"] = summarize(embed)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 검색 지연 측정 (R10)")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="질의 목록을 측정하는 바퀴 수")
    parser.add_argument("--threads", default=",1", help="hybrid의 RAG_EMBED_THREADS 설정 목록(쉼표). 빈 칸은 라이브러리 기본값")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "evals" / "results"))
    args = parser.parse_args()

    queries = heading_queries()
    thread_settings = [t.strip() or None for t in args.threads.split(",")]
    runs = [measure("bm25", None, queries, args.repeats)]
    runs += [measure("hybrid", t, queries, args.repeats) for t in thread_settings]
    try:
        import onnxruntime
        ort_version = onnxruntime.__version__
    except ImportError:
        ort_version = None
    report = {
        "what": "search_knowledge 도구 전체(runtime.run_search)의 지연. 질의는 코퍼스 제목에서 만든 고정 목록(사용자 질문 아님)",
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "machine": {"platform": platform.platform(), "processor": platform.processor(), "cpu_count": os.cpu_count(),
                    "python": platform.python_version(), "onnxruntime": ort_version},
        "query_sets": {name: {"queries": len(texts), "examples": texts[:3]} for name, texts in queries.items()},
        "repeats": args.repeats,
        "runs": runs,
        "caveat": "개발 기계 한 대의 값이다. Render 무료(CPU 0.1, RAM 512 MB)의 값이 아니다. 스레드 1은 느린 CPU의 대용품일 뿐이다.",
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rag_latency_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(path)
    for run in runs:
        if "skipped" in run:
            print(run["mode"], run["embed_threads"], "SKIPPED", run["skipped"])
            continue
        for name in queries:
            tool = run[name]["tool_total"]
            print(run["mode"], f"threads={run['embed_threads']}", name, f"n={tool['n']} p50={tool['p50_ms']}ms p95={tool['p95_ms']}ms max={tool['max_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
