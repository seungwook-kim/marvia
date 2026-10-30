"""채팅 RAG 런타임: RAG_MODE를 읽고, 쓸 수 있는 Searcher를 한 번 만들어 두고, search_knowledge 도구 결과를 만든다 (docs/specs/v2-rag.md 7절, 작업 R8).

services/chat.py가 이 모듈 하나만 부른다. 이 모듈이 맡는 일:
  - rag_mode(): RAG_MODE(off | bm25 | hybrid)를 요청마다 읽는다. 알 수 없는 값과 REPLAY_MODE=true는 off다 (녹화된 답은 off 입력에만 맞는다, .env.example)
  - active_searcher(): off면 None. 아니면 저장소, 임베더, 색인 지문을 한 번 확인해 만든 Searcher를 돌려준다. 못 쓰면 None이고,
    부르는 쪽은 v1 경로(Skill 통째 주입)로 응답한다 (Spec EC-04). 사용자 화면에 오류는 가지 않는다
  - run_search(): 가린 글만으로 질의를 만들어 검색하고 LLM에 줄 도구 결과(dict)를 만든다. 예외를 던지지 않는다 (SDD 3.4).
    tool span rag:search를 남기고(Spec 10절), rag_search_timeout_ms가 있으면 그 시간을 넘긴 검색을 도구 실패로 돌려보낸다(null이면 상한 없음)
  - passages_text(), source_files_text(): 이번 턴 검색 결과에서 답 검사(V7·V9·V10, V14)의 근거 글을 만든다 (Spec 7, 8절)
이 모듈은 사용자 글과 조각 글을 로그에 남기지 않는다. 실패 로그에는 종류(예외 이름)만 쓴다 (Spec 10절).
무거운 모듈(검색, 임베더, 색인 빌드)은 RAG_MODE가 켜진 뒤 처음 쓸 때만 읽는다: off일 때는 이 파일이 import하는 것이 없어 v1과 같다.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from backend.app.clients.replay import replay_enabled
from backend.app.observability.langfuse import tool_span
from backend.app.services.rules import rule

if TYPE_CHECKING:  # 형식 표시만. 실행 때는 읽지 않는다 (off일 때 검색 모듈을 안 읽으려고)
    from backend.app.services.rag.search import SearchHit, Searcher, SearchResult

log = logging.getLogger("marvia.rag")

MODE_OFF, MODE_BM25, MODE_HYBRID = "off", "bm25", "hybrid"
MODES = (MODE_OFF, MODE_BM25, MODE_HYBRID)
# 도구 결과의 source 칸 (모든 도구 응답에 source와 fetched_at을 붙인다, CLAUDE.md)
TOOL_SOURCE = "knowledge"
# 도구 실패 코드 (SDD 3.4 {error: {code, message, retryable}} 모양은 chat._tool_error와 같다)
CODE_UNAVAILABLE = "search_unavailable"
CODE_QUERY_UNSAFE = "query_unsafe"
CODE_TIMEOUT = "search_timeout"
# Langfuse tool span 이름 (이름 모양은 unipass:fetch, checkpoint:read와 같다, Spec 10절)
SPAN_NAME = "rag:search"
# span metadata의 index_id 길이: 지문 앞 몇 자만 남긴다 (Spec 10절). 표시용 단위라 rules.md 임계값이 아니다
INDEX_ID_SHOWN = 8
SPAN_DIGITS = 4
MS_PER_SEC = 1000  # 밀리초를 초로 바꾸는 단위 환산
# 질의를 가릴 수 없었을 때 모델에게 알리는 말 (영어: 도구 설명과 같은 방식)
MSG_UNAVAILABLE = "The knowledge search is not available now. Do not answer from the documents. Use answer_kind not_in_skills."
MSG_QUERY_UNSAFE = "The message still holds an identifier that must not be searched. Do not answer from the documents. Use answer_kind not_in_skills."

class SearchTimeout(Exception):
    """검색이 rag_search_timeout_ms 안에 끝나지 않았다. run_search가 도구 실패로 바꾼다."""


_lock = threading.Lock()
_cache: dict[tuple, "Searcher | None"] = {}
_warned_modes: set[str] = set()


def rag_mode() -> str:
    """RAG_MODE의 값 (off | bm25 | hybrid). 요청마다 읽어 테스트와 배포가 켜고 끌 수 있다.
    알 수 없는 값은 off로 두고 경고를 한 번 남긴다. REPLAY_MODE=true도 off다: 녹화된 답은 off 입력에만 맞는다 (.env.example)."""
    raw = (os.environ.get("RAG_MODE") or MODE_OFF).strip().lower()
    if raw not in MODES:
        if raw not in _warned_modes:
            _warned_modes.add(raw)
            log.warning("rag: 알 수 없는 RAG_MODE라 off로 둔다 (off, bm25, hybrid 중 하나)")
        return MODE_OFF
    if replay_enabled():
        return MODE_OFF
    return raw


def reset() -> None:
    """만들어 둔 Searcher와 경고 기억을 비운다 (테스트, 색인을 다시 만든 뒤). 서버는 다시 켜면 같다."""
    with _lock:
        _cache.clear()
        _warned_modes.clear()


def active_searcher() -> "Searcher | None":
    """이번 요청이 쓸 Searcher. off이거나 검색을 쓸 수 없으면 None (부르는 쪽은 v1 경로로 응답한다, Spec EC-04).
    만들기는 환경 설정(모드, 저장소, 임베더)이 같으면 한 번만 한다. 만들다 실패한 결과(None)도 기억한다:
    색인이 어긋난 채로 요청마다 청킹과 지문 계산을 되풀이하지 않으려는 것이다. 색인을 고치면 서버를 다시 켠다."""
    mode = rag_mode()
    if mode == MODE_OFF:
        return None
    key = (mode, os.environ.get("RAG_STORE"), os.environ.get("RAG_EMBED_BACKEND"), os.environ.get("RAG_EMBED_MODEL_DIR"))
    with _lock:
        if key not in _cache:
            _cache[key] = _build(mode)
        return _cache[key]


def _build(mode: str) -> "Searcher | None":
    """저장소와 임베더를 열고 색인 지문을 확인해 Searcher를 만든다. 어느 단계든 실패하면 None (종류만 경고로 남긴다).

    hybrid에서 임베더를 못 만들면 BM25 단독으로 내려간다 (Spec EC-02). 색인 지문이 현재 코퍼스, 청킹, 모델과 다르면
    옛 색인으로 답하지 않고 None이다 (Spec EC-05): 틀린 근거로 답하는 것보다 v1이 낫다.
    """
    try:
        from backend.app.services.rag import embed as embed_mod
        from backend.app.services.rag import store as store_mod
        from backend.app.services.rag.search import Searcher

        store = store_mod.make_store()
        embedder = None
        if mode == MODE_HYBRID:
            try:
                embedder = embed_mod.make_embedder()
            except Exception as exc:  # noqa: BLE001 - 모델 파일 없음, 잘못된 설정 등 어느 것이든 BM25 단독으로 간다 (EC-02)
                log.warning("rag: 임베더를 만들 수 없어 BM25 단독으로 검색한다 (%s)", type(exc).__name__)
        problems = _index_problems(store, embedder)
        if problems:
            log.warning("rag: 색인이 현재 코퍼스나 설정과 달라 v1 경로로 응답한다 (%s)", "; ".join(problems))
            return None
        return Searcher(store, embedder)
    except Exception as exc:  # noqa: BLE001 - StoreError, SearchUnavailable, LeakError, 파일 오류 모두 v1로 내려가는 이유다 (EC-04)
        log.warning("rag: 검색을 쓸 수 없어 v1 경로로 응답한다 (%s)", type(exc).__name__)
        return None


def _index_problems(store, embedder) -> list[str]:
    """색인이 지금 코퍼스와 설정으로 만든 것과 같은지 본다 (Spec EC-05). 빈 목록이면 쓸 수 있다.

    - 모든 모드: 색인의 chunk_id 집합이 지금 코퍼스를 청킹한 결과와 같아야 한다 (chunk_id는 글을 해시에 넣어 글이 바뀌면 바뀐다)
    - 임베더를 쓰는 모드: 색인을 만든 모델이 지금 모델과 같고, 지문이 있으면 지금 계산한 지문과 같아야 한다
    지문이 없는 색인(manifest 없음)은 지문 비교를 건너뛰고 chunk_id 집합만 본다.
    """
    from backend.app.services.rag.index_build import make_plan

    plan = make_plan(embedder.describe() if embedder is not None else {})  # 누출 게이트도 같이 돈다 (걸리면 LeakError)
    problems: list[str] = []
    indexed = {record.chunk_id for record in store.chunks()}
    current = {chunk.chunk_id for chunk in plan.chunks}
    if indexed != current:
        problems.append("색인의 조각이 현재 코퍼스와 다름")
    if embedder is not None:
        info = store.info()
        if info is not None and info.embed_model not in (None, embedder.model_id):
            problems.append("색인을 만든 임베딩 모델이 지금 모델과 다름")
        elif info is not None and info.fingerprint and info.fingerprint != plan.fingerprint:
            problems.append("색인 지문이 현재 코퍼스, 청킹, 모델 설정과 다름")
    return problems


def run_search(searcher: "Searcher", masked_texts: Sequence[str]) -> tuple[dict, "SearchResult | None"]:
    """search_knowledge 도구 한 번. (LLM에 줄 도구 결과, 검색 결과) 를 돌려준다. 검색 결과는 실패하면 None이다.

    masked_texts는 이미 가린 사용자 말을 오래된 것부터 놓은 목록이고 마지막이 이번 말이다. 질의는 build_query가 만든다:
    MaskedText.of가 B/L, MRN, 컨테이너 번호 모양이 남은 글을 거부하므로, 이번 말이 거부되면 검색하지 않고 실패를 돌려준다.
    앞 말이 거부되면 그 말만 질의에서 뺀다 (이번 말이 질문이다).
    결과의 hits에는 조각의 제목 경로, 출처 파일, 검수일, 글만 담는다 (점수, 조각 id는 모델이 쓸 데가 없다).
    게이트가 막으면 조각 없이 {"hits": [], "gated": true, "reason": "low_score"} (Spec 6절): 모델이 환각할 근거를 주지 않는다.
    예외를 던지지 않는다. 검색을 못 했으면 {error: {code, message, retryable}} (SDD 3.4).
    한 번 부를 때마다 tool span "rag:search"를 남긴다 (Spec 10절, _trace가 칸을 채운다). 요청 문맥(노드 span, 요청 루트 span)을 따라 붙는다.
    """
    with tool_span(SPAN_NAME) as span:  # input은 비운다: 질의는 가렸어도 사용자 글이 span에 남지 않게 한다 (Spec 10절)
        result_dict, found = _run_search(searcher, masked_texts, span)
    return result_dict, found


def _run_search(searcher: "Searcher", masked_texts: Sequence[str], span) -> tuple[dict, "SearchResult | None"]:
    """run_search의 본문. span.metadata, span.output, span.error에 안전한 값만 적는다: 질의 글자 수, 방식, 점수, 조각 id, 지연, 색인 이름."""
    from backend.app.services.rag.query import MaskedText

    stamp = {"source": TOOL_SOURCE, "fetched_at": datetime.now(timezone.utc).isoformat()}
    span.metadata["store"] = getattr(getattr(searcher, "store", None), "name", None)
    if not masked_texts:
        span.metadata["blocked"] = CODE_QUERY_UNSAFE
        return _error(CODE_QUERY_UNSAFE, MSG_QUERY_UNSAFE, stamp), None
    try:
        current = MaskedText.of(masked_texts[-1])
    except (ValueError, TypeError):
        span.metadata["blocked"] = CODE_QUERY_UNSAFE  # 검색하지 않았다. 사고가 아니라 막은 것이라 ERROR로 올리지 않는다
        return _error(CODE_QUERY_UNSAFE, MSG_QUERY_UNSAFE, stamp), None
    earlier: list = []
    for text in masked_texts[:-1]:
        try:
            earlier.append(MaskedText.of(text))
        except (ValueError, TypeError):
            continue
    try:
        result = _search_with_timeout([*earlier, current], searcher, _timeout_ms())
    except SearchTimeout:
        log.warning("rag: 검색이 시간 상한을 넘었다")
        span.error = CODE_TIMEOUT
        return _error(CODE_TIMEOUT, MSG_UNAVAILABLE, stamp), None
    except Exception as exc:  # noqa: BLE001 - SearchUnavailable 포함. 도구는 예외를 던지지 않는다 (SDD 3.4)
        log.warning("rag: 검색 실패 (%s)", type(exc).__name__)
        span.error = CODE_UNAVAILABLE
        span.metadata["failure"] = type(exc).__name__  # 예외 이름만 (메시지에 글이 섞일 수 있다)
        return _error(CODE_UNAVAILABLE, MSG_UNAVAILABLE, stamp), None
    _trace(span, result)
    if result.gated:
        return {**stamp, "hits": [], "gated": True, "reason": result.reason}, result
    if not result.hits:
        return {**stamp, "hits": [], "reason": result.reason or "no_match"}, result
    hits = [{"heading_path": h.chunk.heading_path, "source_path": h.chunk.source_path,
             "last_verified": h.chunk.last_verified, "text": h.chunk.text} for h in result.hits]
    return {**stamp, "hits": hits}, result


def _trace(span, result: "SearchResult") -> None:
    """rag:search span에 남길 값 (Spec 10절). 조각 id와 점수, 방식, 게이트가 본 값, 지연, 색인 이름만이다.
    조각 글, 질의 글, 사용자 원문은 어디에도 넣지 않는다 (질의는 글자 수만). index_id는 앞 INDEX_ID_SHOWN자다."""
    span.output = {
        "chunk_ids": [h.chunk.chunk_id for h in result.hits],
        "scores": [{"rrf": _round(h.rrf), "dense_cos": _round(h.dense_cos), "bm25_terms": h.bm25_terms_matched} for h in result.hits],
    }
    span.metadata.update({
        "query_chars": result.query_chars, "mode": result.mode, "top_k": int(rule("rag_top_k")), "n_hits": len(result.hits),
        "gated": result.gated, "reason": result.reason, "gate_enabled": result.gate_enabled,
        "dense_top1": _round(result.dense_top1), "bm25_terms": result.bm25_terms,
        "embed_ms": _round(result.embed_ms), "search_ms": _round(result.search_ms),
        "index_id": (result.index_id or "")[:INDEX_ID_SHOWN] or None, "store": result.store,
        "notes": list(result.notes),
    })


def _round(value: float | None) -> float | None:
    """점수와 지연을 span에 적기 좋은 자릿수로. 자릿수 SPAN_DIGITS는 표시용이다."""
    return None if value is None else round(float(value), SPAN_DIGITS)


def _timeout_ms() -> float | None:
    """rules.md rag_search_timeout_ms. null이면 상한 없음 (R10 지연 측정 뒤에 정한다, Spec 6절)."""
    value = rule("rag_search_timeout_ms")
    return None if value is None else float(value)


def _search_with_timeout(turns: list, searcher: "Searcher", timeout_ms: float | None):
    """검색 한 번. timeout_ms가 None이면 그냥 부르고, 값이 있으면 별도 스레드에서 돌려 그 시간 안에 못 끝나면 SearchTimeout이다.
    넘긴 스레드는 멈출 수 없어 뒤에서 끝까지 돈다(읽기 전용 계산이라 부작용이 없다). 요청은 기다리지 않고 도구 실패로 내려간다 (Spec 10절, EC-04)."""
    if timeout_ms is None:
        return searcher.search(turns)
    box: dict = {}

    def work() -> None:
        try:
            box["result"] = searcher.search(turns)
        except BaseException as exc:  # noqa: BLE001 - 스레드 밖으로 예외를 옮겨 호출한 쪽이 같은 방식으로 처리하게 한다
            box["error"] = exc

    thread = threading.Thread(target=work, name="rag-search", daemon=True)
    thread.start()
    thread.join(timeout_ms / MS_PER_SEC)
    if thread.is_alive():
        raise SearchTimeout()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _error(code: str, message: str, stamp: dict) -> dict:
    """도구 실패 모양 (chat._tool_error와 같다). retryable은 false다: 같은 질의로 다시 불러도 같다."""
    return {**stamp, "error": {"code": code, "message": message, "retryable": False}}


def passages_text(hits: Sequence["SearchHit"]) -> str:
    """이번 턴에 모델이 본 조각의 글(제목 경로 + 글). 답 검사 V7·V9·V10의 근거에 더한다 (Spec 7절: 모델이 본 글만 근거다)."""
    return " ".join(f"{h.chunk.heading_path} {h.chunk.text}" for h in hits)


def source_files_text(hits: Sequence["SearchHit"]) -> str:
    """V14가 인용을 찾는 원문: 이번 턴 검색된 조각의 출처 파일 전문 (Spec 8절). 조각 경계에서 잘린 인용도 찾게 하려고 조각이 아니라
    파일 단위다. 파일을 읽지 못하면 그 파일의 검색된 조각 글만 쓴다. 형식은 llm._skills_text와 같다(파일 이름 머리글)."""
    parts: list[str] = []
    done: set[str] = set()
    for hit in hits:
        path = hit.chunk.source_path
        if path in done:
            continue
        done.add(path)
        body = _read_corpus_file(path)
        if body is None:
            body = "\n".join(h.chunk.text for h in hits if h.chunk.source_path == path)
        parts.append(f"# 파일: {path}\n\n{body}")
    return "\n\n".join(parts)


def _read_corpus_file(path: str) -> str | None:
    """레포 안의 마크다운 파일 전문. 레포 밖 경로, .md가 아닌 파일, 읽기 실패는 None (저장소 값이라도 믿고 열지 않는다)."""
    from backend.app.services.rag.corpus import REPO_ROOT

    try:
        target = (REPO_ROOT / path).resolve()
        if target.suffix != ".md" or not target.is_relative_to(REPO_ROOT.resolve()):
            return None
        return target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
