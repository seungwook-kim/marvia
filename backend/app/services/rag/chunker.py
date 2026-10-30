"""마크다운 문서를 검색용 조각으로 나눈다 (docs/specs/v2-rag.md 2절, 작업 R1). 순수 함수만 있다.

색인 빌드와 테스트가 같이 쓴다. 파일을 읽거나 DB·모델을 부르지 않는다 (글을 받아 조각 목록을 돌려준다).
규칙 (Spec 2절 번호):
  1. 맨 앞 YAML 프론트매터와 첫 `>` 인용 머리말(작성 메모)은 조각에서 뺀다
  2. `#`/`##`/`###` 제목으로 절을 나누고 조각마다 heading_path를 붙인다
  3. 절 안은 글 덩어리(문단, 불릿 항목) 순서대로 모으되 목표 글자 수에 가까워지면 끊는다. 덩어리 경계에서만 자른다.
     최소 글자 수는 한 절 안에서만 쓰는 무른 기준이다(짧은 끝 조각을 같은 절 앞 조각에 붙인다). 절 경계를 넘어 합치지 않으므로
     짧은 절도 자기 heading_path를 가진 조각이 된다 (평가 정답이 heading_path 접두어로 조각을 찾는다)
  4. 표는 행 하나가 조각 하나다. 헤더 행과 구분선을 모든 행 조각에 붙여 열 뜻을 잃지 않게 한다
  5. `출처:` 줄은 조각 글에서 빼서 그 절 조각들의 source_url 메타로 옮긴다. URL이 없는 줄은 source_note 메타로 남긴다
  6. 임베딩·BM25에 넣는 글(embed_text)은 제목 경로를 앞에 붙인 글이다 (rules.md rag_context_header)
크기(목표·최대·최소)는 코드에 적지 않고 skills/rag-search/rules.md에서 읽는다 (ChunkParams.from_rules).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# 청킹 알고리즘 버전. index_manifest.json chunking.version에 들어가 색인 지문을 바꾼다 (Spec 1절).
# 같은 파일·같은 파라미터에서 조각이 달라지게 chunker 로직을 고쳤다면 올린다 (그래야 옛 색인이 지문 불일치로 걸린다)
CHUNKING_VERSION = 1
# chunk_id 길이. Spec 2절 "sha256(...) 앞 12자". 임계값이 아니라 id 표기 규격이라 코드에 둔다
CHUNK_ID_LEN = 12
# anchor에서 표 행을 가리키는 꼬리표 (Spec 2절 `source_path#heading_path[#행]`)
ROW_ANCHOR_MARK = "#행"

# 제목은 1~3단계만 절을 나눈다. 4단계 이상은 본문 글로 둔다 (Spec 2절 2번)
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_START = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_TABLE_LINE = re.compile(r"^\s*\|")
_TABLE_SEP = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
_SOURCE_LINE = re.compile(r"^출처\s*[:：]\s*(.*)$")
_URL = re.compile(r"https?://\S+")
# 문장 끝 뒤 공백. 너무 긴 한 덩어리를 문장 단위로 쪼갤 때만 쓴다 (Spec 2절 3번 "문장 중간 금지")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")


@dataclass(frozen=True)
class ChunkParams:
    """청킹 파라미터. 값은 rules.md가 원본이고 코드는 ChunkParams.from_rules()로만 만든다 (테스트는 직접 만든다)."""

    target_chars: int
    max_chars: int
    min_chars: int
    context_header: bool

    def __post_init__(self) -> None:
        # 어긋난 값(목표가 최대보다 큼 등)은 조용히 이상한 조각을 만드므로 바로 실패시킨다
        if not (0 < self.min_chars <= self.target_chars <= self.max_chars):
            raise ValueError(
                f"청킹 크기는 0 < min <= target <= max 여야 합니다: {self.min_chars}, {self.target_chars}, {self.max_chars}")

    @classmethod
    def from_rules(cls) -> "ChunkParams":
        """skills/rag-search/rules.md rag_chunk_*, rag_context_header를 읽는다 (코드에 숫자를 적지 않는다, CLAUDE.md)."""
        from backend.app.services.rules import rule

        return cls(
            target_chars=int(rule("rag_chunk_target_chars")),
            max_chars=int(rule("rag_chunk_max_chars")),
            min_chars=int(rule("rag_chunk_min_chars")),
            context_header=bool(rule("rag_context_header")),
        )

    def as_manifest(self, version: int) -> dict:
        """index_manifest.json `chunking` 칸의 재료 (Spec 1절). 지문에 들어가므로 키 순서에 상관없이 같은 값이어야 한다."""
        return {"version": version, "target_chars": self.target_chars, "max_chars": self.max_chars,
                "min_chars": self.min_chars, "context_header": self.context_header}


@dataclass(frozen=True)
class Chunk:
    """검색 조각 하나와 메타 (Spec 2절 "조각 메타" 표). effective_date, supersedes는 칸만 예약한다 (EC-14)."""

    corpus_id: str
    source_path: str
    heading_path: str
    chunk_id: str
    anchor: str
    ordinal: int
    last_verified: str | None
    source_url: tuple[str, ...]
    text: str
    embed_text: str
    table_row: int | None = None  # 표 행 조각이면 본문 행 번호(1부터). 아니면 None
    source_note: tuple[str, ...] = ()  # URL 없는 `출처:` 줄의 글 ("수입 실무자 확인 (공식 문서 아님)"). 조각 글·임베딩 글에는 없다
    effective_date: str | None = None
    supersedes: str | None = None

    def as_dict(self) -> dict:
        """chunks.jsonl 한 줄과 DB 행의 재료. source_url은 목록으로 둔다."""
        return {
            "corpus_id": self.corpus_id, "source_path": self.source_path, "heading_path": self.heading_path,
            "chunk_id": self.chunk_id, "anchor": self.anchor, "ordinal": self.ordinal,
            "last_verified": self.last_verified, "source_url": list(self.source_url),
            "source_note": list(self.source_note),
            "text": self.text, "embed_text": self.embed_text, "table_row": self.table_row,
            "effective_date": self.effective_date, "supersedes": self.supersedes,
        }


# ── 내부 모양 ──

@dataclass
class _Unit:
    """절 안의 글 덩어리 하나. kind는 para(문단), item(불릿 항목, 들여쓴 하위 줄 포함), frag(너무 긴 덩어리를 쪼갠 조각)."""

    text: str
    kind: str
    glue: str | None = None  # 앞 덩어리와 한 조각으로 이을 때 사이에 넣는 글. None이면 kind로 정한다


@dataclass
class _Table:
    """표 하나. rows는 본문 행 원문(맨 앞 `|` 포함)이다."""

    header: str
    sep: str
    rows: list[str]


@dataclass
class _Section:
    """제목 하나와 그 아래 본문 줄. heading_line은 원문 제목 줄(짧은 절을 앞 조각에 붙일 때 같이 붙인다)."""

    heading_path: str
    heading_line: str
    lines: list[str] = field(default_factory=list)


@dataclass
class _Draft:
    """id를 붙이기 전 조각. 절 순서대로 모두 모은 뒤 chunk_id와 ordinal을 붙인다."""

    heading_path: str
    text: str
    row: int | None
    urls: list[str]
    notes: list[str]


def _strip_front_matter(text: str) -> str:
    """맨 앞 `---` 로 둘러싼 YAML 프론트매터를 지운다 (Spec 2절 1번). 닫는 `---`가 없으면 프론트매터가 아니라고 본다."""
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def _split_sections(body: str) -> list[_Section]:
    """제목(1~3단계)으로 절을 나눈다. 코드 펜스 안의 `#`은 제목이 아니다.

    첫 절의 맨 앞 `>` 인용 머리말은 작성 메모(검수자, 작업 ID)라 질의와 상관없이 임베딩만 흐리므로 뺀다 (Spec 2절 1번).
    """
    sections: list[_Section] = [_Section(heading_path="", heading_line="")]
    stack: dict[int, str] = {}
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            for lv in [lv for lv in stack if lv >= level]:
                del stack[lv]
            stack[level] = title
            path = " > ".join(f"{'#' * lv} {stack[lv]}" for lv in sorted(stack))
            sections.append(_Section(heading_path=path, heading_line=line.strip()))
        else:
            sections[-1].lines.append(line)
    # 첫 제목 앞의 빈 절(preamble)은 제목 경로가 없다. 본문이 없으면 버리고, 있으면 제목 경로 없는 조각이 된다
    sections = [s for s in sections if s.heading_path or any(ln.strip() for ln in s.lines)]
    if sections:
        _drop_leading_memo(sections[0])
    return sections


def _drop_leading_memo(section: _Section) -> None:
    """첫 절의 맨 앞 `>` 인용 덩어리를 지운다. 빈 줄은 건너뛰고, `>`가 아닌 첫 줄에서 멈춘다."""
    i = 0
    while i < len(section.lines) and not section.lines[i].strip():
        i += 1
    start = i
    while i < len(section.lines) and section.lines[i].lstrip().startswith(">"):
        i += 1
    if i > start:
        del section.lines[start:i]


def _take_sources(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """`출처: ...` 줄을 본문에서 빼고 (본문 줄, URL 목록, 출처 메모 목록)을 돌려준다 (Spec 2절 5번).
    URL이 든 줄은 URL을 source_url로 옮기고, URL이 없는 줄(예: "수입 실무자 확인 (공식 문서 아님)")은 줄 글 그대로 source_note로 남긴다.
    출처 메모는 조각 글과 임베딩 글에 넣지 않는 출처 정보다. 원문 파일에는 줄이 그대로 있으므로 V14 원문 대조에는 영향이 없다."""
    kept: list[str] = []
    urls: list[str] = []
    notes: list[str] = []
    for line in lines:
        m = _SOURCE_LINE.match(line.strip())
        if not m:
            kept.append(line)
            continue
        found = _URL.findall(m.group(1))
        for url in found:
            if url not in urls:
                urls.append(url)
        note = m.group(1).strip()
        if not found and note and note not in notes:
            notes.append(note)
    return kept, urls, notes


def _parse_blocks(lines: list[str]) -> list[_Unit | _Table]:
    """본문 줄을 문단, 불릿 항목, 표로 나눈다. 순서는 원문 순서 그대로다."""
    blocks: list[_Unit | _Table] = []
    para: list[str] = []
    item: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(_Unit("\n".join(para), "para"))
            para.clear()

    def flush_item() -> None:
        if item:
            blocks.append(_Unit("\n".join(item), "item"))
            item.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            flush_para()
            flush_item()
            i += 1
        elif _FENCE.match(line):
            # 코드 펜스는 쪼개지 않고 한 문단으로 둔다
            flush_para()
            flush_item()
            fence = [line]
            i += 1
            while i < len(lines):
                fence.append(lines[i].rstrip())
                closed = bool(_FENCE.match(lines[i]))
                i += 1
                if closed:
                    break
            blocks.append(_Unit("\n".join(fence), "para"))
        elif _TABLE_LINE.match(line):
            flush_para()
            flush_item()
            rows = []
            while i < len(lines) and _TABLE_LINE.match(lines[i]):
                rows.append(lines[i].rstrip())
                i += 1
            if len(rows) >= 2 and _TABLE_SEP.match(rows[1]):
                blocks.append(_Table(header=rows[0], sep=rows[1], rows=rows[2:]))
            else:
                blocks.append(_Unit("\n".join(rows), "para"))  # 구분선이 없으면 표로 보지 않는다
        elif _LIST_START.match(line):
            flush_para()
            flush_item()
            item.append(line)
            i += 1
        elif item and (line[:1].isspace() or not _LIST_START.match(line)):
            item.append(line)  # 들여쓴 하위 줄, 또는 이어지는 글은 앞 항목에 속한다
            i += 1
        elif line[:1].isspace() and _LIST_START.match(line.lstrip()) and not para:
            item.append(line)
            i += 1
        else:
            para.append(line)
            i += 1
    flush_para()
    flush_item()
    return blocks


def _split_oversize(unit: _Unit, max_chars: int) -> list[_Unit]:
    """최대 글자 수를 넘는 덩어리를 줄 경계, 그래도 넘으면 문장 경계에서 쪼갠다. 문장 중간은 자르지 않는다.
    한 문장이 최대보다 길어도 그대로 둔다 (자르면 뜻이 끊기는 쪽이 더 나쁘다)."""
    if len(unit.text) <= max_chars:
        return [unit]
    pieces: list[tuple[str, str]] = []  # (글, 앞 조각과 이을 때 글). 첫 조각은 빈 글
    for line in unit.text.split("\n"):
        parts = [line] if len(line) <= max_chars else [p for p in _SENTENCE_END.split(line) if p]
        for p_idx, part in enumerate(parts):
            glue = "\n" if p_idx == 0 else " "
            pieces.append((part, glue if pieces else ""))
    # 쪼갠 조각을 최대 글자 수 안에서 다시 이어 붙인다 (너무 잘게 나뉘지 않게)
    out: list[_Unit] = []
    cur, cur_glue = "", None
    for text, glue in pieces:
        if cur and len(cur) + len(glue) + len(text) <= max_chars:
            cur += glue + text
            continue
        if cur:
            out.append(_Unit(cur, "frag", cur_glue))
        cur, cur_glue = text, (glue if out or cur else None)
    if cur:
        out.append(_Unit(cur, "frag", cur_glue))
    return out


def _glue(prev: _Unit, unit: _Unit) -> str:
    """같은 조각에 두 덩어리를 이을 때 사이에 넣을 글. 불릿 항목끼리는 줄바꿈 하나, 그 밖은 빈 줄 하나."""
    if unit.glue is not None:
        return unit.glue
    return "\n" if prev.kind == "item" and unit.kind == "item" else "\n\n"


def _pack_units(units: list[_Unit], params: ChunkParams) -> list[tuple[str, str]]:
    """덩어리를 순서대로 모아 조각 글 목록으로 만든다. 반환은 (조각 글, 앞 조각과 이을 때 글).

    끊는 규칙 (Spec 2절 3번): 붙여도 목표 글자 수 이하면 붙인다. 목표를 넘겨도 지금 조각이 최소보다 짧고 최대 안이면 붙인다.
    그 밖에는 새 조각을 시작한다. 끝 조각이 최소보다 짧으면 앞 조각에 붙인다 (최대 안일 때).
    """
    expanded: list[_Unit] = []
    for u in units:
        expanded.extend(_split_oversize(u, params.max_chars))
    packed: list[list] = []  # [글, 시작 glue]
    cur_text, cur_start_glue, last_unit = "", "\n\n", None
    for unit in expanded:
        if last_unit is None:
            cur_text, cur_start_glue = unit.text, "\n\n"
        else:
            joined = cur_text + _glue(last_unit, unit) + unit.text
            if len(joined) <= params.target_chars or (len(cur_text) < params.min_chars and len(joined) <= params.max_chars):
                cur_text = joined
            else:
                packed.append([cur_text, cur_start_glue])
                cur_text, cur_start_glue = unit.text, _glue(last_unit, unit)
        last_unit = unit
    if last_unit is not None:
        packed.append([cur_text, cur_start_glue])
    if len(packed) >= 2 and len(packed[-1][0]) < params.min_chars \
            and len(packed[-2][0]) + len(packed[-1][1]) + len(packed[-1][0]) <= params.max_chars:
        last = packed.pop()
        packed[-1][0] += last[1] + last[0]
    return [(t, g) for t, g in packed]


def _section_drafts(section: _Section, params: ChunkParams) -> list[_Draft]:
    """절 하나를 조각 초안으로 만든다. 글 덩어리 묶음과 표 행이 원문 순서대로 섞여 나온다."""
    lines, urls, notes = _take_sources(section.lines)
    drafts: list[_Draft] = []
    run: list[_Unit] = []

    def flush_run() -> None:
        for text, _ in _pack_units(run, params):
            drafts.append(_Draft(section.heading_path, text, None, list(urls), list(notes)))
        run.clear()

    for block in _parse_blocks(lines):
        if isinstance(block, _Unit):
            run.append(block)
            continue
        flush_run()  # 표 앞 설명 문단은 첫 행 조각이 아니라 따로 둔다 (Spec 2절 4번)
        for n, row in enumerate(block.rows, start=1):
            drafts.append(_Draft(section.heading_path, f"{block.header}\n{block.sep}\n{row}", n, list(urls), list(notes)))
    flush_run()
    return drafts


def make_chunk_id(source_path: str, heading_path: str, row: int | None, text: str) -> str:
    """sha256(source_path + heading_path + 표 행 번호 + 조각 글) 앞 CHUNK_ID_LEN자 (Spec 2절 메타 표).
    글이 바뀌면 바뀐다(증거용). 같은 입력이면 항상 같다. 필드 사이에 줄바꿈을 넣어 경계가 모호해지지 않게 한다."""
    raw = "\n".join([source_path, heading_path, "" if row is None else str(row), text])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:CHUNK_ID_LEN]


def chunk_markdown(text: str, *, corpus_id: str, source_path: str, last_verified: str | None,
                   params: ChunkParams) -> list[Chunk]:
    """마크다운 글 하나를 조각 목록으로 나눈다. 같은 입력이면 같은 결과다 (chunk_id 포함).

    source_path는 레포 기준 슬래시 경로, last_verified는 corpus.yaml의 검수일이다 (둘 다 메타로만 쓴다).
    같은 파일에서 chunk_id가 겹치면(글·제목 경로·행이 모두 같은 조각) 색인 키가 겹치므로 ValueError로 알린다.
    """
    body = _strip_front_matter(text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿"))
    drafts: list[_Draft] = []
    for section in _split_sections(body):
        # 절 경계를 넘어 합치지 않는다: 평가 정답이 heading_path 접두어로 조각을 찾으므로(Spec 평가 절) 짧은 절도 자기 조각이다
        drafts.extend(_section_drafts(section, params))
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for ordinal, d in enumerate(drafts):
        chunk_id = make_chunk_id(source_path, d.heading_path, d.row, d.text)
        if chunk_id in seen:
            raise ValueError(f"{source_path}에 같은 조각이 둘 있습니다 ({d.heading_path}): 문서를 고칩니다")
        seen.add(chunk_id)
        anchor = f"{source_path}#{d.heading_path}" + (f"{ROW_ANCHOR_MARK}{d.row}" if d.row is not None else "")
        embed = f"{d.heading_path}\n{d.text}" if params.context_header and d.heading_path else d.text
        chunks.append(Chunk(corpus_id=corpus_id, source_path=source_path, heading_path=d.heading_path,
                            chunk_id=chunk_id, anchor=anchor, ordinal=ordinal, last_verified=last_verified,
                            source_url=tuple(d.urls), source_note=tuple(d.notes), text=d.text, embed_text=embed,
                            table_row=d.row))
    return chunks
