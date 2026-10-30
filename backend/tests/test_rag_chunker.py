"""RAG 청커 (services/rag/chunker.py, corpus.py), 작업 R1 (docs/specs/v2-rag.md 2절, 완료 기준 a~e).

- 실제 SKILL.md 5개로: 프론트매터·머리말 메모 없음, 표 행마다 헤더 포함 한 조각, 문장 중간에서 안 잘림,
  chunk_id 결정성, 출처 줄이 source_url로 옮겨짐
- 작은 합성 마크다운으로: 제목 경로, 병합·분할 규칙, 짧은 절 병합, 오류 경로
키도 DB도 모델도 없이 돈다.
"""
import re

import pytest

from backend.app.services.rag import corpus as corpus_mod
from backend.app.services.rag.chunker import ChunkParams, chunk_markdown
from backend.app.services.rag.corpus import REPO_ROOT, chunk_corpus, load_corpus
from backend.app.services.rules import rule

PARAMS = ChunkParams(target_chars=200, max_chars=320, min_chars=40, context_header=True)


def _chunk(text, params=PARAMS, **kw):
    return chunk_markdown(text, corpus_id="c1", source_path="skills/x/SKILL.md", last_verified="2026-01-02", params=params, **kw)


# ── 실제 SKILL.md 5개 ──

@pytest.fixture(scope="module")
def real():
    """코퍼스 A 5개 파일의 (항목, 원문, 조각 목록). rules.md의 실제 청킹 값을 쓴다."""
    corpus = load_corpus()
    params = ChunkParams.from_rules()
    out = []
    for entry in corpus.entries:
        text = (REPO_ROOT / entry.path).read_text(encoding="utf-8")
        chunks = chunk_markdown(text, corpus_id=entry.corpus_id, source_path=entry.path,
                                last_verified=entry.last_verified, params=params)
        out.append((entry, text, chunks))
    return out


def test_corpus_is_the_five_skill_files(real):
    assert [e.path for e, _, _ in real] == [f"skills/{f}/SKILL.md" for f in rule("rag_corpus_folders")]
    assert all(chunks for _, _, chunks in real)


def test_real_chunks_exclude_front_matter_and_memo(real):
    """(a) 맨 앞 YAML 프론트매터와 첫 `>` 머리말 메모(검수자·작업 ID)가 조각에 없다."""
    for entry, text, chunks in real:
        folder = entry.path.split("/")[1]
        for c in chunks:
            for line in c.text.split("\n"):
                assert not line.lstrip().startswith(">"), (entry.path, line)
            assert "검수:" not in c.text and "작업 C-" not in c.text, entry.path
            assert not re.search(r"^description:", c.text, re.M) and f"name: {folder}" not in c.text


def _source_tables(text):
    """원문에서 표를 독립적으로 찾는다: (헤더, 구분선, [본문 행]) 목록."""
    tables, run = [], []
    for line in text.split("\n") + [""]:
        if line.startswith("|"):
            run.append(line.rstrip())
        else:
            if len(run) >= 2:
                tables.append((run[0], run[1], run[2:]))
            run = []
    return tables


def test_real_table_rows_are_one_chunk_each_with_header(real):
    """(b) 표 본문 행마다 조각이 정확히 하나, 글은 헤더 + 구분선 + 그 행이다. 행 번호는 1부터 차례로 붙는다."""
    total_rows = 0
    for entry, text, chunks in real:
        row_chunks = [c for c in chunks if c.table_row is not None]
        expected = [f"{h}\n{s}\n{r}" for h, s, rows in _source_tables(text) for r in rows]
        total_rows += len(expected)
        assert sorted(c.text for c in row_chunks) == sorted(expected), entry.path
        for c in row_chunks:
            assert c.text.startswith("|") and c.text.count("\n") == 2
            assert c.anchor.endswith(f"#행{c.table_row}")
    assert total_rows > 0  # 표가 있는 파일이 실제로 검사됐다


def test_real_chunks_never_cut_inside_a_line(real):
    """(c) 조각의 모든 줄은 원문의 한 줄과 글자까지 같다 (줄 중간, 문장 중간에서 잘리지 않음).
    크기: 최대 글자 수를 넘는 조각은 쪼갤 수 없는 한 덩어리(표 행 등)뿐이어야 한다."""
    max_chars = int(rule("rag_chunk_max_chars"))
    for entry, text, chunks in real:
        source_lines = {ln.rstrip() for ln in text.split("\n")}
        for c in chunks:
            for line in c.text.split("\n"):
                if line.strip():
                    assert line.rstrip() in source_lines or line.rstrip().startswith("## "), (entry.path, line)
            if len(c.text) > max_chars:
                assert c.table_row is not None, (entry.path, c.heading_path, len(c.text))


def test_real_chunks_cover_all_body_lines(real):
    """본문이 조각에서 빠지지 않는다: 프론트매터, 머리말, 제목, 출처 줄, 빈 줄을 뺀 모든 줄이 어느 조각엔가 있다."""
    for entry, text, chunks in real:
        in_chunks = {ln.rstrip() for c in chunks for ln in c.text.split("\n")}
        body = text.split("---", 2)[2]
        for line in body.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith((">", "#", "출처:")):
                continue
            assert line.rstrip() in in_chunks, (entry.path, line)


def test_real_chunk_id_is_deterministic_and_follows_text(real):
    """(d) 같은 입력이면 같은 chunk_id, 글이 한 글자 바뀐 조각만 id가 바뀐다."""
    entry, text, chunks = real[3]  # import-basics
    params = ChunkParams.from_rules()
    again = chunk_markdown(text, corpus_id=entry.corpus_id, source_path=entry.path, last_verified=entry.last_verified, params=params)
    assert [c.chunk_id for c in again] == [c.chunk_id for c in chunks]
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    old = "포워더에게 B/L 번호를 알려 달라고 한다"
    assert old in text
    changed = chunk_markdown(text.replace(old, old + "."), corpus_id=entry.corpus_id, source_path=entry.path,
                             last_verified=entry.last_verified, params=params)
    assert len(changed) == len(chunks)
    diff = [(a, b) for a, b in zip(chunks, changed) if a.chunk_id != b.chunk_id]
    assert len(diff) == 1 and diff[0][0].heading_path == diff[0][1].heading_path
    assert all(len(c.chunk_id) == 12 for c in chunks)


def test_real_source_lines_move_to_source_url(real):
    """(e) `출처:` 줄은 조각 글에 없고, 그 절 조각들의 source_url에 URL이 옮겨져 있다."""
    for _, _, chunks in real:
        assert all("출처:" not in c.text for c in chunks)
    chunks = real[3][2]  # import-basics
    section = [c for c in chunks if c.heading_path.endswith("## 1. B/L 번호 찾기")]
    assert section and all(c.source_url == (
        "https://www.kita.net/board/format/formatDetail.do?ContentsID=KI_cmmrcFormat_1719002&postIndex=1719002",
        "https://www.customs.go.kr/download/UNIPASS_FAQ_210524.pdf") for c in section)
    # URL이 없는 출처 줄만 있는 절은 source_url이 비어 있다. 출처 줄이 없는 파일도 같다
    assert all(c.source_url == () for c in chunks if c.heading_path.endswith("## 2. A/N(도착통지서)"))
    assert all(c.source_note == ("수입 실무자 확인 (공식 문서 아님)",) for c in chunks if c.heading_path.endswith("## 2. A/N(도착통지서)"))
    assert all(c.source_url == () for c in real[0][2])  # ais-reliability는 출처 줄이 없다


def test_real_metadata_fields(real):
    for entry, _, chunks in real:
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))
        for c in chunks:
            assert (c.corpus_id, c.source_path) == (entry.corpus_id, entry.path)
            assert c.heading_path.startswith("# ") and c.anchor.startswith(f"{entry.path}#{c.heading_path}")
            assert c.embed_text == f"{c.heading_path}\n{c.text}"
            assert c.effective_date is None and c.supersedes is None
            assert set(c.as_dict()) >= {"corpus_id", "source_path", "heading_path", "chunk_id", "anchor", "ordinal",
                                        "last_verified", "source_url", "text", "embed_text"}


# ── corpus.yaml ──

def test_corpus_yaml_matches_rules_folders_and_covers_every_skill_md():
    corpus = load_corpus()
    assert {e.path for e in corpus.entries} == {f"skills/{f}/SKILL.md" for f in rule("rag_corpus_folders")}
    assert "skills/referrals/SKILL.md" not in {e.path for e in corpus.entries}
    # skills 아래 모든 .md는 코퍼스에 있거나 제외 이유가 적혀 있다 (조용히 빠지는 파일이 없다)
    listed = {e.path for e in corpus.entries}
    for path in (REPO_ROOT / "skills").rglob("*.md"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert rel in listed or corpus.is_excluded(rel), rel
    assert all(reason for _, reason in corpus.excluded)
    assert all(e.kind == "skill" and e.license == "internal" for e in corpus.entries)


def test_corpus_yaml_validation(tmp_path):
    def write(body):
        p = tmp_path / "corpus.yaml"
        p.write_text(body, encoding="utf-8")
        return p

    ok = "corpora:\n  - {corpus_id: a, path: skills/a/SKILL.md, kind: skill, license: internal, last_verified: 2026-10-01}\n"
    assert load_corpus(write(ok)).entries[0].last_verified == "2026-10-01"
    with pytest.raises(ValueError, match="kind"):
        load_corpus(write(ok.replace("kind: skill", "kind: web")))
    with pytest.raises(ValueError, match="license"):
        load_corpus(write(ok.replace("license: internal, ", "")))
    with pytest.raises(ValueError, match="excluded"):
        load_corpus(write(ok + "excluded:\n  - {path: 'skills/a/**', reason: x}\n"))
    with pytest.raises(ValueError, match="reason"):
        load_corpus(write(ok + "excluded:\n  - {path: 'skills/b/**'}\n"))
    with pytest.raises(ValueError, match="겹칩니다"):
        load_corpus(write(ok + "  - {corpus_id: a, path: skills/b/SKILL.md, kind: skill, license: internal}\n"))


def test_chunk_corpus_reads_files_in_list_order(tmp_path):
    (tmp_path / "skills" / "a").mkdir(parents=True)
    source = tmp_path / "skills" / "a" / "SKILL.md"
    source.write_text("# T\n\n## S\n\n- 하나다.\n", encoding="utf-8")
    cfg = tmp_path / "corpus.yaml"
    cfg.write_text("corpora:\n  - {corpus_id: a, path: skills/a/SKILL.md, kind: skill, license: internal}\n", encoding="utf-8")
    [chunk] = chunk_corpus(load_corpus(cfg), root=tmp_path, params=PARAMS)
    assert (chunk.corpus_id, chunk.source_path, chunk.last_verified) == ("a", "skills/a/SKILL.md", None)
    assert chunk.text == "- 하나다."
    before = corpus_mod.file_sha256(source)
    source.write_text("# T\n\n## S\n\n- 하나다!\n", encoding="utf-8")
    assert corpus_mod.file_sha256(source) != before


# ── 합성 마크다운 ──

def test_front_matter_and_memo_removed_but_later_quotes_kept():
    md = "---\nname: x\ndescription: d\n---\n\n# 제목\n\n> 작성 메모. 검수: 누구\n> 둘째 줄\n\n## 절\n\n본문 문장이다.\n\n> 나중 인용은 본문이다.\n"
    [chunk] = _chunk(md)
    assert chunk.heading_path == "# 제목 > ## 절"
    assert chunk.text == "본문 문장이다.\n\n> 나중 인용은 본문이다."
    assert "name:" not in chunk.text and "메모" not in chunk.text


def test_heading_path_nests_and_resets():
    long = "충분히 길어서 짧은 절 병합에 걸리지 않는 본문 문장이다. " * 2
    md = f"# A\n\n## B\n\nb {long}\n\n### C\n\nc {long}\n\n## D\n\nd {long}\n\n#### 네 단계는 본문\n\n끝이다.\n"
    chunks = _chunk(md)
    assert [c.heading_path for c in chunks] == ["# A > ## B", "# A > ## B > ### C", "# A > ## D"]
    assert "#### 네 단계는 본문" in chunks[-1].text


def test_packs_units_up_to_target_and_starts_new_chunk_after():
    items = [f"- 항목 {i}번은 다음과 같이 길게 적은 한 줄 설명이다 " + "가" * 40 for i in range(6)]
    chunks = _chunk("# T\n\n## S\n\n" + "\n".join(items) + "\n")
    assert len(chunks) > 1
    assert "\n".join(c.text for c in chunks) == "\n".join(items)  # 항목 경계에서만 잘리고 글은 그대로
    assert all(len(c.text) <= PARAMS.target_chars for c in chunks)
    assert all(line.startswith("- 항목") for c in chunks for line in c.text.split("\n"))


def test_nested_bullets_stay_with_parent_item():
    md = "# T\n\n## S\n\n- 부모 항목이다:\n  - 자식 하나다\n  - 자식 둘이다\n- 다음 항목이다\n"
    [chunk] = _chunk(md)
    assert chunk.text == "- 부모 항목이다:\n  - 자식 하나다\n  - 자식 둘이다\n- 다음 항목이다"


def test_long_paragraph_splits_only_at_sentence_ends():
    sentences = [f"이 {i}번째 문장은 일부러 길게 쓴 한 문장으로 끝에 마침표가 있다 " + "나" * 60 + "." for i in range(6)]
    text = " ".join(sentences)
    chunks = _chunk(f"# T\n\n## S\n\n{text}\n")
    assert len(chunks) > 1
    assert all(c.text.endswith(".") and len(c.text) <= PARAMS.max_chars for c in chunks)
    assert " ".join(c.text for c in chunks) == text


def test_single_sentence_longer_than_max_is_kept_whole():
    text = "끝나지 않는 한 문장 " + "다" * 400 + "."
    [chunk] = _chunk(f"# T\n\n## S\n\n{text}\n")
    assert chunk.text == text


def test_table_rows_each_get_header_and_paragraph_before_is_separate():
    md = ("# T\n\n## 표 절\n\n표 앞 설명 문단이다. 열 뜻을 말한다.\n\n| 경우 | 뜻 |\n|---|---|\n| 가 | 첫째 |\n| 나 | 둘째 |\n\n"
          "- 표 뒤 항목이다\n")
    chunks = _chunk(md)
    assert [c.table_row for c in chunks] == [None, 1, 2, None]
    assert chunks[0].text == "표 앞 설명 문단이다. 열 뜻을 말한다."
    assert chunks[1].text == "| 경우 | 뜻 |\n|---|---|\n| 가 | 첫째 |"
    assert chunks[2].text == "| 경우 | 뜻 |\n|---|---|\n| 나 | 둘째 |"
    assert chunks[3].text == "- 표 뒤 항목이다"
    assert chunks[1].anchor == "skills/x/SKILL.md#" + chunks[1].heading_path + "#행1"
    assert chunks[0].anchor == "skills/x/SKILL.md#" + chunks[0].heading_path


def test_table_without_separator_is_plain_text():
    [chunk] = _chunk("# T\n\n## S\n\n| 가 | 나 |\n| 다 | 라 |\n")
    assert chunk.table_row is None and chunk.text == "| 가 | 나 |\n| 다 | 라 |"


def test_short_section_stays_its_own_chunk_with_its_own_heading_path():
    """절 경계를 넘어 합치지 않는다: 최소 글자 수보다 짧은 절도 자기 heading_path 조각이다 (평가 정답이 heading_path 접두어로 찾는다)."""
    md = "# T\n\n## 앞 절\n\n" + "앞 절 본문이다. " * 8 + "\n\n## 짧은 절\n\n짧다.\n"
    chunks = _chunk(md)
    assert [c.heading_path for c in chunks] == ["# T > ## 앞 절", "# T > ## 짧은 절"]
    assert chunks[1].text == "짧다." and "짧은 절" not in chunks[0].text
    table = "# T\n\n## 표\n\n| 가 | 나 |\n|---|---|\n| 1 | 2 |\n\n## 짧은 절\n\n짧다.\n"
    assert [c.heading_path for c in _chunk(table)] == ["# T > ## 표", "# T > ## 짧은 절"]


def test_min_chars_is_a_soft_target_only_inside_one_section():
    """같은 절 안의 짧은 끝 조각은 앞 조각에 붙는다 (최대 글자 수 안일 때)."""
    tight = ChunkParams(target_chars=60, max_chars=100, min_chars=30, context_header=True)
    md = "# T\n\n## S\n\n- " + "가" * 55 + "\n- 짧은 끝\n"
    [chunk] = _chunk(md, params=tight)
    assert chunk.text.endswith("- 짧은 끝")


def test_every_h2_heading_of_the_real_files_is_a_chunk_heading_path(real):
    """모든 `##` 제목이 어느 조각의 heading_path에 나온다 (정답을 heading_path 접두어로 쓰는 평가가 빠짐없이 맞출 수 있다)."""
    seen = 0
    for entry, text, chunks in real:
        in_fence = False
        for line in text.split("\n"):
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m and not in_fence:
                seen += 1
                assert any(f"## {m.group(1)}" in c.heading_path.split(" > ") for c in chunks), (entry.path, line)
    assert seen >= 20  # Spec 근거: 5개 파일에 `##` 절이 25개 안팎


def test_source_lines_become_urls_and_notes_for_that_section_only():
    md = ("# T\n\n## 가\n\n- 본문 하나다\n\n출처: https://a.example/x (설명)\n출처: https://b.example/y\n출처: 사람이 확인 (링크 없음)\n"
          "출처: https://a.example/x\n\n## 나\n\n- 둘째 절이다\n")
    first, second = _chunk(md)
    assert first.source_url == ("https://a.example/x", "https://b.example/y")
    assert first.source_note == ("사람이 확인 (링크 없음)",)  # URL 없는 줄은 메모로 남는다
    assert "출처" not in first.text and "https" not in first.text and "사람이 확인" not in first.text
    assert "사람이 확인" not in first.embed_text  # 임베딩 글에도 넣지 않는다
    assert second.source_url == () and second.source_note == ()
    assert first.as_dict()["source_note"] == ["사람이 확인 (링크 없음)"]


def test_source_note_does_not_change_chunk_id():
    base = "# T\n\n## 가\n\n- 본문 하나다\n"
    plain = _chunk(base)[0]
    noted = _chunk(base + "\n출처: 사람이 확인\n")[0]
    assert plain.chunk_id == noted.chunk_id and noted.source_note == ("사람이 확인",)


def test_context_header_off_embeds_plain_text():
    off = ChunkParams(target_chars=200, max_chars=320, min_chars=40, context_header=False)
    [chunk] = _chunk("# T\n\n## S\n\n본문이다.\n", params=off)
    assert chunk.embed_text == chunk.text == "본문이다."
    [chunk] = _chunk("# T\n\n## S\n\n본문이다.\n")
    assert chunk.embed_text == "# T > ## S\n본문이다."


def test_same_input_same_result_and_crlf_is_the_same_as_lf():
    md = "# T\n\n## S\n\n- 하나다\n- 둘이다\n\n| 가 | 나 |\n|---|---|\n| 1 | 2 |\n"
    assert _chunk(md) == _chunk(md)
    assert [c.chunk_id for c in _chunk(md.replace("\n", "\r\n"))] == [c.chunk_id for c in _chunk(md)]


def test_chunk_id_depends_on_path_heading_row_and_text():
    base = _chunk("# T\n\n## S\n\n본문이다.\n")[0].chunk_id
    assert _chunk("# T\n\n## S\n\n본문이다!\n")[0].chunk_id != base
    assert _chunk("# T\n\n## U\n\n본문이다.\n")[0].chunk_id != base
    other = chunk_markdown("# T\n\n## S\n\n본문이다.\n", corpus_id="c1", source_path="skills/y/SKILL.md", last_verified=None, params=PARAMS)
    assert other[0].chunk_id != base
    assert _chunk("# T\n\n## S\n\n본문이다.\n")[0].last_verified == "2026-01-02"


def test_duplicate_chunk_in_one_file_fails():
    # 같은 제목의 절을 두 번 쓰고 같은 글을 넣으면 제목 경로와 글이 같아 색인 키가 겹친다
    with pytest.raises(ValueError, match="같은 조각"):
        _chunk("# T\n\n## S\n\n- 같은 글이다\n\n## S\n\n- 같은 글이다\n", params=ChunkParams(200, 320, 5, True))


def test_empty_and_heading_only_documents():
    assert _chunk("") == []
    assert _chunk("---\nname: x\n---\n# 제목만\n\n> 메모뿐\n") == []


def test_chunk_params_validation_and_from_rules():
    with pytest.raises(ValueError):
        ChunkParams(target_chars=100, max_chars=50, min_chars=10, context_header=True)
    with pytest.raises(ValueError):
        ChunkParams(target_chars=100, max_chars=200, min_chars=0, context_header=True)
    params = ChunkParams.from_rules()
    assert (params.target_chars, params.max_chars, params.min_chars) == (
        rule("rag_chunk_target_chars"), rule("rag_chunk_max_chars"), rule("rag_chunk_min_chars"))
    assert params.as_manifest(1)["version"] == 1
