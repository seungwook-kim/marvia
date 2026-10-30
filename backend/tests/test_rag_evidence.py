"""검색 조각 근거 카드와 V14 위반 문장 테스트, 작업 R13 (docs/specs/v2-rag.md 8절, services/citations.py, services/chat.py).

완료 기준 (Spec R13 행) 중 이 파일이 보는 것:
  (d) build_evidence_list가 서로 다른 chunk_id 카드를 합치지 않고, 조각 속 숫자가 [E#] 숫자 검사에 쓰인다
  채팅 응답 evidence_items에 chunk_id, source_path, heading_path, last_verified, corpus_id 칸이 더해진다 (칸 추가만)
  V14 위반 설명이 RAG 모드에서는 검색 조각을 가리키고 RAG_MODE=off에서는 한 글자도 다르지 않다
(a) 조각 경계를 넘는 인용, (b) 검색 안 한 skill 답, (c) 다른 파일 문장 인용은 test_rag_chat.py가 이미 본다 (R8).
(e) off일 때의 test_validate, test_citations, test_chat_citations는 고치지 않았고 전체 pytest가 같이 돈다.
키, DB, 모델 파일 없이 돈다. 조각은 가짜 임베더로 만든 임시 색인(test_rag_chat.py 픽스처)에서 온다.
"""
from types import SimpleNamespace

import pytest

from backend.app.services import citations as cite
from backend.app.services import validate
from backend.app.services.rag.store import ChunkRecord
from backend.tests.test_chat_agent import IMPORT_QUOTE, agent_path  # noqa: F401 - agent_path는 autouse 픽스처
from backend.tests.test_rag_chat import (  # noqa: F401 - 픽스처
    ANSWER, GOOD, QUESTION, _fresh_runtime, ask, built_index, rag_on, scripted,
)

STAMP = "2026-10-31T01:02:03+00:00"


def _chunk(chunk_id: str, text: str, path: str = "skills/import-basics/SKILL.md", url=()) -> ChunkRecord:
    return ChunkRecord(corpus_id="skill-import-basics", source_path=path, heading_path="# 수입 기초 > ## 절", chunk_id=chunk_id,
                       anchor=f"{path}#절", ordinal=0, text=text, embed_text=text, last_verified="2026-10-01", source_url=url)


def _hits(*chunks):
    return [SimpleNamespace(chunk=c) for c in chunks]


# ── build_evidence_list ──

def test_different_chunks_of_one_file_in_one_minute_are_not_merged():
    cards = cite.chunk_cards(_hits(_chunk("aaa111", "첫 조각 글입니다."), _chunk("bbb222", "둘째 조각 글입니다.")), STAMP)
    items = cite.build_evidence_list(cards)
    assert [i["chunk_id"] for i in items] == ["aaa111", "bbb222"] and [i["id"] for i in items] == ["E1", "E2"]
    assert items[0]["values"] == [{"field": "passage", "value": "첫 조각 글입니다."}]


def test_chunk_card_carries_the_added_fields_and_first_url():
    items = cite.build_evidence_list(cite.chunk_cards(_hits(_chunk("aaa111", "글", url=("https://example.com/a", "https://example.com/b"))), STAMP))
    item = items[0]
    assert item["source"] == "knowledge:skills/import-basics/SKILL.md" and item["fetched_at"] == STAMP
    assert (item["source_path"], item["heading_path"], item["last_verified"], item["corpus_id"], item["url"]) == (
        "skills/import-basics/SKILL.md", "# 수입 기초 > ## 절", "2026-10-01", "skill-import-basics", "https://example.com/a")


def test_cards_without_chunk_id_keep_their_old_shape_and_merge_as_before():
    cards = [{"source": "mof", "field": "a", "value": "1", "fetched_at": STAMP}, {"source": "mof", "field": "b", "value": "2", "fetched_at": STAMP}]
    items = cite.build_evidence_list(cards)
    assert items == [{"source": "mof", "fetched_at": STAMP, "values": [{"field": "a", "value": "1"}, {"field": "b", "value": "2"}], "id": "E1"}]


def test_chunk_value_is_cut_at_chunk_max_chars_not_200():
    text = "가" * 300 + " 마지막 숫자는 777입니다."
    long_chunk = cite.build_evidence_list(cite.chunk_cards(_hits(_chunk("aaa111", text)), STAMP))[0]
    assert long_chunk["values"][0]["value"] == " ".join(text.split())  # 800자 안이라 자르지 않는다
    plain = cite.build_evidence_list([{"source": "mof", "field": "a", "value": text, "fetched_at": STAMP}])[0]
    assert len(plain["values"][0]["value"]) == cite.MAX_VALUE_CHARS  # 조각이 아닌 카드는 그대로 200자


def test_number_late_in_a_chunk_counts_for_the_citation_check():
    """200자 뒤에 있는 숫자도 [E#] 숫자 검사에서 근거로 쓰인다 (자르면 맞는 표시가 지워진다)."""
    text = "가" * 300 + " 서류는 777부 필요합니다."
    items = cite.build_evidence_list(cite.chunk_cards(_hits(_chunk("aaa111", text)), STAMP))
    marked, marks = cite.parse_and_validate("서류는 777부 필요합니다 [E1].", items)
    assert marks and marks[0]["evidence_ref"] == "E1" and "[E1]" in marked
    _, wrong = cite.parse_and_validate("서류는 778부 필요합니다 [E1].", items)
    assert wrong == []  # 조각에 없는 숫자는 표시가 지워진다


def test_forged_mark_inside_a_passage_is_neutralised():
    items = cite.build_evidence_list(cite.chunk_cards(_hits(_chunk("aaa111", "정상 글 [E9] 뒤")), STAMP))
    assert "[E9" not in items[0]["values"][0]["value"]


# ── V14 위반 설명 ──

def test_v14_text_points_at_search_passages_only_in_rag_mode():
    kwargs = dict(grounded="x", user_text="y", quote="없는 인용문장입니다 여러 글자", skills_text="원문 전체")
    off = validate.check_chat_reply("답", **kwargs)
    on = validate.check_chat_reply("답", **kwargs, rag=True)
    off_text = next(v.detail for v in off if v.rule == "V14")
    on_text = next(v.detail for v in on if v.rule == "V14")
    assert off_text == ("skill_quote is missing or is not copied exactly from the Skill documents. "
                        "Copy one sentence or table cell from the Skill documents that supports your reply, "
                        "and say only what that text says. If the Skill documents do not cover the question, "
                        "set answer_kind to not_in_skills.")
    assert "the passages returned by search_knowledge" in on_text and "Skill documents" not in on_text
    assert [v.rule for v in off] == [v.rule for v in on]  # 규칙 번호와 판정은 같다


# ── 채팅 응답 ──

def test_skill_answer_carries_chunk_cards_and_marks(monkeypatch, rag_on):
    marked = ANSWER[:-1] + " [E1]."
    calls = scripted(monkeypatch, {"search": True, "out": (marked, "skill", IMPORT_QUOTE)})
    result = ask()
    assert result.reply.startswith(ANSWER[:-1]) and "[E1]" in result.reply
    hits = calls[0]["result"]["hits"]
    chunk_items = [i for i in result.evidence_items if "chunk_id" in i]
    assert len(chunk_items) == len(hits) >= 1
    for hit, item in zip(hits, chunk_items):
        assert hit["evidence_id"] == item["id"]
        assert item["source"] == f"knowledge:{hit['source_path']}" and item["source_path"] == hit["source_path"]
        assert item["heading_path"] == hit["heading_path"] and item["corpus_id"]
        assert item.get("last_verified") == hit["last_verified"]  # 검수일이 없는 조각은 칸 자체를 싣지 않는다
        assert item["values"][0]["field"] == "passage"
    assert result.citations and result.citations[0]["evidence_ref"] == "E1"
    assert "evidence" in calls[0]["result"] and "[E1]" in calls[0]["result"]["evidence"]


def test_tool_result_keeps_scores_and_chunk_id_away_from_the_model(monkeypatch, rag_on):
    calls = scripted(monkeypatch, GOOD)
    ask()
    shown = str(calls[0]["result"])
    for item in ask().evidence_items:
        if "chunk_id" in item:
            assert item["chunk_id"] not in shown
    assert "rrf" not in shown and "dense_cos" not in shown


def test_answer_that_is_not_a_skill_answer_leaves_no_chunk_cards(monkeypatch, rag_on):
    """검색은 했지만 문서에 없다고 끝난 답에는 조각 근거 칩이 남지 않는다."""
    scripted(monkeypatch, {"search": True, "out": ("", "not_in_skills", "")})
    result = ask()
    assert not [i for i in result.evidence_items if "chunk_id" in i]


def test_rag_off_has_no_chunk_cards(monkeypatch):
    monkeypatch.setenv("RAG_MODE", "off")
    scripted(monkeypatch, {"search": False, "out": (ANSWER, "skill", IMPORT_QUOTE)})
    result = ask()
    assert all("chunk_id" not in i for i in result.evidence_items)


@pytest.mark.parametrize("graph", ["off", "on"])
def test_chunk_cards_reach_the_http_response_in_both_graph_modes(monkeypatch, rag_on, graph):
    from fastapi.testclient import TestClient

    from backend.app.main import app

    monkeypatch.setenv("AGENT_GRAPH", graph)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    scripted(monkeypatch, {"search": True, "out": (ANSWER[:-1] + " [E1].", "skill", IMPORT_QUOTE)})
    with TestClient(app) as client:
        body = client.post("/api/chat", json={"message": QUESTION, "session_id": "s1"}).json()
    cards = [i for i in body["evidence_items"] if "chunk_id" in i]
    assert cards and set(cards[0]) >= {"id", "source", "fetched_at", "values", "chunk_id", "source_path", "heading_path",
                                       "corpus_id"}
    assert body["citations"] and body["citations"][0]["evidence_ref"] == "E1"
