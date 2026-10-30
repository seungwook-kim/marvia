"""채팅 RAG 연결 테스트 (services/rag/runtime.py, services/chat.py search_knowledge, services/llm.py chat_agent rag), 작업 R8 (docs/specs/v2-rag.md 7절).

완료 기준 (Spec R8 행):
  (a) RAG_MODE=off에서 프롬프트, Skill 주입, 도구 목록이 v1과 같다 (기존 test_chat*가 그대로 통과하는 것은 전체 pytest가 본다)
  (b) hybrid/bm25 + 가짜 임베더에서 실무 질문이 search_knowledge를 부르고 answer_kind=skill로 끝난다 (가짜 LLM)
  (c) 색인이 어긋나거나(모델, 조각) 저장소가 예외를 내면 v1 경로로 응답한다 (사용자 화면에 오류 없음)
  (d) RAG_MODE off/on x AGENT_GRAPH off/on 네 조합
키, DB, 모델 파일 없이 돈다: 가짜 임베더로 진짜 코퍼스의 파일 색인을 임시 폴더에 만들어 쓴다. LLM은 가짜 llm.chat_agent다.
"""
import json
from types import SimpleNamespace

import pytest

from backend.app.services import chat as svc
from backend.app.services import llm, rules, validate
from backend.app.services.rag import runtime
from backend.app.services.rag import store as store_mod
from backend.app.services.rag.embed import FakeEmbedder
from backend.app.services.rag.index_build import build
from backend.app.services.rag.search import SearchHit, SearchResult, SearchUnavailable
from backend.app.services.rag.store import ChunkRecord, FileChunkStore, StoreError
from backend.tests.test_chat_agent import IMPORT_QUOTE, MCP_TOOLS, agent_path  # noqa: F401 - agent_path는 autouse 픽스처

QUESTION = "A/N 도착통지서 못 받으면 어떻게 해?"
ANSWER = "도착통지서를 못 받았다면 B/L에 적힌 선사나 포워더에 직접 전화해 받아 보세요."
CUSTOMS_QUOTE_FILE = "skills/customs-stages/SKILL.md"


# ── 준비 ──

@pytest.fixture(scope="module")
def built_index(tmp_path_factory):
    """진짜 코퍼스(skills/*/SKILL.md 5개)를 가짜 임베더로 색인해 임시 폴더에 둔다. 모듈에서 한 번만 만든다."""
    root = tmp_path_factory.mktemp("rag_chat")
    index_dir, manifest = root / "index", root / "index_manifest.json"
    build(FakeEmbedder(), index_dir=index_dir, manifest_path=manifest, built_at="2026-01-01T00:00:00Z")
    return index_dir, manifest


@pytest.fixture(autouse=True)
def _fresh_runtime():
    runtime.reset()
    yield
    runtime.reset()


@pytest.fixture
def rag_on(monkeypatch, built_index):
    """RAG_MODE를 켜고 저장소를 임시 파일 색인으로 바꾼다. 모드는 테스트가 setenv로 정한다."""
    index_dir, manifest = built_index
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: FileChunkStore(index_dir, manifest))
    monkeypatch.setenv("RAG_MODE", "hybrid")


def scripted(monkeypatch, *steps):
    """가짜 llm.chat_agent. steps는 호출 순서대로 {"search": bool, "out": (reply, kind, quote)}. 마지막 칸은 반복한다.
    search가 참이고 도구 목록에 search_knowledge가 있으면 run으로 부른다. 받은 도구 이름, feedback, rag 인자, 검색 결과를 모은다."""
    record = []

    def fake(masked, refs, has_cargo, today, history, tools, run, *, model=None, feedback=None, evidence=None,
             on_screen=None, rag=False):
        step = steps[min(len(record), len(steps) - 1)]
        names = [t["function"]["name"] for t in tools]
        call = {"masked": masked, "tools": names, "feedback": feedback, "rag": rag, "result": None}
        record.append(call)
        if step.get("search") and "search_knowledge" in names:
            call["result"] = run("search_knowledge", {})
        reply, kind, quote = step["out"]
        return llm.LLMAnswer(output=llm.ChatAgentOutput(reply=reply, answer_kind=kind, skill_quote=quote), llm=None,
                             prompt_version="test", tool_calls=[])
    monkeypatch.setattr(llm, "chat_agent", fake)
    monkeypatch.setattr(llm, "route_chat", lambda *a, **k: pytest.fail("의도 분류 방식을 부르면 안 된다"))
    return record


def ask(message=QUESTION, history=None):
    return svc.handle(message, "s1", None, lambda req: pytest.fail("조회하면 안 된다"), history=history)


GOOD = {"search": True, "out": (ANSWER, "skill", IMPORT_QUOTE)}


# ── (a) RAG_MODE=off는 v1과 같다 ──

def test_off_sends_v1_tools_and_no_rag_argument(monkeypatch):
    """off(기본)에서는 search_knowledge 도구도, rag 인자도, Searcher 만들기도 없다."""
    monkeypatch.setattr(runtime, "_build", lambda mode: pytest.fail("off일 때 Searcher를 만들면 안 된다"))
    calls = scripted(monkeypatch, {"out": (ANSWER, "skill", IMPORT_QUOTE)})
    assert ask().reply == ANSWER
    assert "search_knowledge" not in calls[0]["tools"] and calls[0]["rag"] is False
    assert calls[0]["tools"] == [t["function"]["name"] for t in svc.AGENT_TOOLS]


def test_agent_tools_list_is_unchanged_by_rag():
    """도구 정의는 따로 두었다: AGENT_TOOLS에는 search_knowledge가 없다 (off의 도구 목록이 v1과 한 글자도 다르지 않으려고)."""
    assert "search_knowledge" not in {t["function"]["name"] for t in svc.AGENT_TOOLS}
    assert svc.SEARCH_KNOWLEDGE_TOOL["function"]["name"] == "search_knowledge"
    assert svc.SEARCH_KNOWLEDGE_TOOL["function"]["parameters"]["properties"] == {}  # 인자 없음: 질의는 코드가 만든다


def _capture_system(monkeypatch, **kwargs):
    """진짜 llm.chat_agent를 부르되 llm.chat만 가짜로 두고, 모델에 간 system 글과 도구 목록을 돌려준다."""
    sent = {}

    def fake_chat(messages, **k):
        sent["system"], sent["tools"] = messages[0]["content"], k.get("tools")
        body = json.dumps({"reply": "", "answer_kind": "unrelated", "skill_quote": "", "watch_cue": False})
        return llm.LLMResult(content=body, model="m")
    monkeypatch.setattr(llm, "chat", fake_chat)
    llm.chat_agent("안녕", [], False, "2026-10-05", [], svc.AGENT_TOOLS, lambda n, a: {}, **kwargs)
    return sent


def test_off_prompt_is_v1_prompt_plus_whole_skills(monkeypatch):
    sent = _capture_system(monkeypatch)
    v1 = llm.get_prompt("marvia-chat-agent").text
    assert sent["system"] == v1 + "\n\n" + llm._skills_text(llm.CHAT_AGENT_SKILLS)
    assert "skills/import-basics/SKILL.md" in sent["system"]  # v1은 SKILL.md를 통째로 넣는다
    assert sent["tools"] == svc.AGENT_TOOLS


def test_rag_prompt_has_rag_text_and_no_skill_md(monkeypatch):
    sent = _capture_system(monkeypatch, rag=True)
    rag_prompt = llm.get_prompt("marvia-chat-agent-rag").text
    assert sent["system"] == rag_prompt + "\n\n" + llm._skills_text(llm.CHAT_AGENT_SKILLS, True)
    injected = sent["system"].split(rag_prompt, 1)[1]
    headers = [line for line in injected.splitlines() if line.startswith("# 파일:")]
    assert headers and not any(h.endswith("/SKILL.md") for h in headers)  # 주입분에는 SKILL.md 파일 머리글이 없다
    assert "# 파일: skills/import-basics/SKILL.md" not in sent["system"]
    assert "skills/customs-stages/rules.md" in sent["system"] and "skills/cargo-verdict/rules.md" in sent["system"]
    assert "search_knowledge" in rag_prompt and "passages returned by search_knowledge" in rag_prompt


def test_rag_prompt_differs_from_v1_only_where_expected():
    """새 프롬프트는 v1 사본에서 도구 줄과 1층 규칙, 근거 조각 문구만 바꾼 것이다 (바뀐 줄 수를 고정해 우연한 수정을 잡는다)."""
    v1 = llm.get_prompt("marvia-chat-agent").text.splitlines()
    v2 = llm.get_prompt("marvia-chat-agent-rag").text.splitlines()
    changed = [b for a, b in zip(v1, [line for line in v2 if not line.startswith("- search_knowledge:")]) if a != b]
    assert len(v2) == len(v1) + 1 and len(changed) == 3
    assert all("search_knowledge" in line or "retrieved passages" in line for line in changed)


def test_skills_text_rules_only_keeps_non_skill_md_files():
    full = llm._skills_text(llm.CHAT_AGENT_SKILLS)
    rules_only = llm._skills_text(llm.CHAT_AGENT_SKILLS, True)
    headers = [line for line in rules_only.splitlines() if line.startswith("# 파일:")]
    assert "# 파일: skills/import-basics/SKILL.md" in full
    assert headers and not any(h.endswith("/SKILL.md") for h in headers)
    assert not validate.quote_found(IMPORT_QUOTE, rules_only)  # EC-08 테스트가 쓰는 인용은 SKILL.md에만 있다


def test_replay_mode_and_unknown_mode_are_off(monkeypatch):
    monkeypatch.setenv("RAG_MODE", "hybrid")
    monkeypatch.setenv("REPLAY_MODE", "true")
    assert runtime.rag_mode() == "off"
    monkeypatch.delenv("REPLAY_MODE")
    monkeypatch.setenv("RAG_MODE", "maybe")
    assert runtime.rag_mode() == "off"
    monkeypatch.setenv("RAG_MODE", " BM25 ")
    assert runtime.rag_mode() == "bm25"


# ── (b) 켜면 search_knowledge로 답한다 ──

@pytest.mark.parametrize("mode", ["hybrid", "bm25"])
def test_practical_question_calls_search_and_ends_as_skill(monkeypatch, rag_on, mode):
    monkeypatch.setenv("RAG_MODE", mode)
    calls = scripted(monkeypatch, GOOD)
    reply = ask().reply
    assert reply == ANSWER
    assert len(calls) == 1 and "search_knowledge" in calls[0]["tools"] and calls[0]["rag"] is True
    result = calls[0]["result"]
    assert "error" not in result and result["source"] == "knowledge" and result["fetched_at"]
    assert any(h["source_path"] == "skills/import-basics/SKILL.md" for h in result["hits"])
    for hit in result["hits"]:
        # 점수, chunk_id는 모델에 가지 않는다. [E#] 표시에 쓰는 evidence_id만 더해진다 (R13)
        assert set(hit) == {"heading_path", "source_path", "last_verified", "text", "evidence_id"}


def test_search_query_is_built_from_masked_text_only(monkeypatch, rag_on):
    """질의는 가린 문장에서만 만든다: 원래 B/L은 검색기에도 닿지 않는다 (CLAUDE.md, Spec 5절 1번)."""
    seen = []
    searcher = runtime.active_searcher()
    real = searcher._search_query
    monkeypatch.setattr(searcher, "_search_query", lambda query: seen.append(query) or real(query))
    calls = scripted(monkeypatch, GOOD)
    earlier = [{"role": "user", "text": "TESTBL99 어디쯤이야"}, {"role": "assistant", "text": "조회 결과를 보여 드렸어요."}]
    ask(QUESTION, history=earlier)  # 앞 사용자 말에 B/L이 있다: 가린 글([BL1])로만 질의가 만들어진다
    assert seen and all("TESTBL99" not in q and "BL1" not in q for q in seen)
    assert "TESTBL99" not in str(calls)


def test_second_call_in_a_turn_reuses_first_result(monkeypatch, rag_on):
    searcher = runtime.active_searcher()
    count = []
    real = searcher.search
    monkeypatch.setattr(searcher, "search", lambda turns: count.append(1) or real(turns))
    scripted(monkeypatch, GOOD)
    ask()
    assert len(count) == 1


def test_quote_outside_retrieved_chunk_but_in_same_file_passes(monkeypatch):
    """V14는 조각이 아니라 파일 전문으로 대조한다 (Spec 8절, AC-03): 검색된 조각 밖이어도 같은 파일 문장이면 통과한다."""
    chunk = ChunkRecord(corpus_id="skill-import-basics", source_path="skills/import-basics/SKILL.md", heading_path="# 수입 실무",
                        chunk_id="x1", anchor="a", ordinal=0, text="조각에는 이 문장만 있다. 선사와 포워더가 도착통지서를 보낸다.",
                        embed_text="e")
    stub = SimpleNamespace(search=lambda turns: SearchResult(hits=(SearchHit(chunk, 0.1, None, 1, None, 1),), mode="bm25"))
    monkeypatch.setattr(runtime, "active_searcher", lambda: stub)
    assert not validate.quote_found(IMPORT_QUOTE, chunk.text)  # 인용은 조각에 없다
    calls = scripted(monkeypatch, GOOD)
    assert ask().reply == ANSWER
    assert calls[0]["result"]["hits"][0]["text"] == chunk.text


def test_quote_from_a_file_that_was_not_retrieved_is_rejected(monkeypatch):
    """검색된 파일이 아닌 파일의 문장은 걸린다: 통관 단계 SKILL.md 문장은 import-basics 조각만 받은 턴의 근거가 아니다."""
    chunk = ChunkRecord(corpus_id="skill-import-basics", source_path="skills/import-basics/SKILL.md", heading_path="# 수입 실무",
                        chunk_id="x1", anchor="a", ordinal=0, text="선사와 포워더가 도착통지서를 보낸다.", embed_text="e")
    stub = SimpleNamespace(search=lambda turns: SearchResult(hits=(SearchHit(chunk, 0.1, None, 1, None, 1),), mode="bm25"))
    monkeypatch.setattr(runtime, "active_searcher", lambda: stub)
    other = (llm.SKILL_DIR / "customs-stages" / "SKILL.md").read_text(encoding="utf-8")
    quote = next(line.strip() for line in other.splitlines() if len(line.strip()) > 20 and not line.startswith("#"))
    assert CUSTOMS_QUOTE_FILE.endswith("SKILL.md") and not validate.quote_found(quote, chunk.text)
    scripted(monkeypatch, {"search": True, "out": (ANSWER, "skill", quote)})
    assert ask().reply == svc.NOT_IN_SKILLS


# ── EC-08: 검색 없이 skill ──

def test_skill_answer_without_search_is_asked_again_to_search(monkeypatch, rag_on):
    calls = scripted(monkeypatch, {"search": False, "out": (ANSWER, "skill", IMPORT_QUOTE)}, GOOD)
    assert ask().reply == ANSWER
    assert len(calls) == 2
    assert "search_knowledge" in calls[1]["feedback"] and "TOOL" in calls[1]["feedback"]  # 다시 물을 때 검색을 부르라고 한다
    assert calls[1]["result"] is not None


def test_skill_answer_without_search_twice_ends_with_fixed_notice(monkeypatch, rag_on):
    calls = scripted(monkeypatch, {"search": False, "out": (ANSWER, "skill", IMPORT_QUOTE)})
    assert ask().reply == svc.NOT_IN_SKILLS
    assert len(calls) == 2  # 다시 묻기는 한 번이다


def test_v1_does_not_add_the_search_hint(monkeypatch):
    """off에서는 같은 위반이 없고 힌트도 붙지 않는다 (인용이 v1 원문에 있으니 통과)."""
    calls = scripted(monkeypatch, {"search": False, "out": (ANSWER, "skill", IMPORT_QUOTE)})
    assert ask().reply == ANSWER and len(calls) == 1


# ── 게이트 ──

def _gate_everything(monkeypatch):
    """BM25 일치 명사 수 최소값을 아주 크게 둬 모든 질의를 막는다 (실제 임계값은 R9가 정한다. 여기서는 막힌 경로만 본다)."""
    original = rules.load_rules()
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: {**original, "rag_min_terms": 10 ** 6})


def test_gated_search_gives_no_passages_and_answer_is_fixed_notice(monkeypatch, rag_on):
    monkeypatch.setenv("RAG_MODE", "bm25")
    _gate_everything(monkeypatch)
    calls = scripted(monkeypatch, {"search": True, "out": ("그 내용은 자료에 없어요.", "not_in_skills", "")})
    assert ask("컨테이너 보관료 얼마야?").reply == svc.NOT_IN_SKILLS
    result = calls[0]["result"]
    assert result["hits"] == [] and result["gated"] is True and result["reason"] == "low_score"
    assert "text" not in json.dumps(result)  # 조각 글이 모델에 가지 않는다


def test_gated_search_then_skill_claim_is_rejected(monkeypatch, rag_on):
    """게이트가 막았는데 모델이 SKILL.md 문장을 인용해 skill로 답해도, 근거에 조각이 없어 고정 안내가 된다."""
    monkeypatch.setenv("RAG_MODE", "bm25")
    _gate_everything(monkeypatch)
    scripted(monkeypatch, GOOD)
    assert ask().reply == svc.NOT_IN_SKILLS


# ── (c) 못 쓰면 v1 경로 (EC-04, EC-05) ──

def _assert_v1(monkeypatch):
    calls = scripted(monkeypatch, {"out": (ANSWER, "skill", IMPORT_QUOTE)})
    assert ask().reply == ANSWER  # 사용자는 오류를 보지 않고 v1 답을 받는다
    assert "search_knowledge" not in calls[0]["tools"] and calls[0]["rag"] is False


def test_fingerprint_mismatch_with_committed_onnx_index_falls_back_to_v1(monkeypatch):
    """저장소의 기본 파일 색인은 ONNX 모델로 만들었다. 가짜 임베더(다른 모델)와는 지문이 달라 v1로 내려간다 (EC-05)."""
    monkeypatch.setenv("RAG_MODE", "hybrid")
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: FileChunkStore(store_mod.DEFAULT_INDEX_DIR))
    assert runtime.active_searcher() is None
    _assert_v1(monkeypatch)


def test_changed_corpus_falls_back_to_v1(monkeypatch, built_index):
    """색인을 만든 뒤 코퍼스가 바뀌었으면(조각 집합이 다르면) v1이다. 한 조각이 빠진 색인으로 흉내 낸다."""
    index_dir, manifest = built_index

    class Missing(FileChunkStore):
        def chunks(self):
            return super().chunks()[1:]
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: Missing(index_dir, manifest))
    monkeypatch.setenv("RAG_MODE", "bm25")
    assert runtime.active_searcher() is None
    _assert_v1(monkeypatch)


def test_store_error_falls_back_to_v1(monkeypatch):
    def broken(*a, **k):
        raise StoreError("파일 색인을 읽지 못했다")
    monkeypatch.setattr(store_mod, "make_store", broken)
    monkeypatch.setenv("RAG_MODE", "hybrid")
    _assert_v1(monkeypatch)


def test_unexpected_exception_while_building_falls_back_to_v1(monkeypatch):
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: 1 / 0)
    monkeypatch.setenv("RAG_MODE", "bm25")
    _assert_v1(monkeypatch)


def test_failed_build_is_remembered_not_retried_each_request(monkeypatch):
    tries = []
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: tries.append(1) or (_ for _ in ()).throw(StoreError("x")))
    monkeypatch.setenv("RAG_MODE", "bm25")
    assert runtime.active_searcher() is None and runtime.active_searcher() is None
    assert len(tries) == 1


def test_embedder_unavailable_degrades_to_bm25_only(monkeypatch, rag_on):
    from backend.app.services.rag import embed as embed_mod

    def no_model():
        raise embed_mod.EmbedderUnavailable("모델 파일 없음")
    monkeypatch.setattr(embed_mod, "make_embedder", no_model)
    searcher = runtime.active_searcher()
    assert searcher is not None and searcher.embedder is None  # EC-02: BM25 단독
    scripted(monkeypatch, GOOD)
    assert ask().reply == ANSWER


def test_search_failure_at_tool_time_returns_error_not_exception(monkeypatch):
    class Dead:
        def search(self, turns):
            raise SearchUnavailable("둘 다 불가")
    monkeypatch.setattr(runtime, "active_searcher", lambda: Dead())
    calls = scripted(monkeypatch, {"search": True, "out": ("자료에 없어요.", "not_in_skills", "")})
    assert ask().reply == svc.NOT_IN_SKILLS
    error = calls[0]["result"]["error"]
    assert error["code"] == "search_unavailable" and error["retryable"] is False and calls[0]["result"]["source"] == "knowledge"


def test_unmasked_identifier_in_message_blocks_the_search():
    """MaskedText.of가 거부하는 글(가리지 않은 B/L 모양)은 검색하지 않는다."""
    searcher = SimpleNamespace(search=lambda turns: pytest.fail("검색하면 안 된다"))
    result, found = runtime.run_search(searcher, ["TESTBL99 도착통지서"])
    assert found is None and result["error"]["code"] == "query_unsafe"


# ── (d) RAG_MODE x AGENT_GRAPH ──

@pytest.mark.parametrize("graph", ["off", "on"])
@pytest.mark.parametrize("rag", ["off", "hybrid"])
def test_rag_mode_and_agent_graph_combinations(monkeypatch, built_index, rag, graph):
    """RAG_MODE는 채팅 실무 질문 길만 바꾸고 AGENT_GRAPH(/api/agent 판정 그래프)와 서로 영향이 없다 (Spec 11절)."""
    index_dir, manifest = built_index
    monkeypatch.setattr(store_mod, "make_store", lambda *a, **k: FileChunkStore(index_dir, manifest))
    monkeypatch.setenv("RAG_MODE", rag)
    monkeypatch.setenv("AGENT_GRAPH", graph)
    calls = scripted(monkeypatch, GOOD)
    assert ask().reply == ANSWER
    assert ("search_knowledge" in calls[0]["tools"]) == (rag != "off")
    assert calls[0]["rag"] == (rag != "off")


def test_mcp_tools_still_offered_with_rag(monkeypatch, rag_on):
    """읽기 전용 MCP 도구는 RAG 모드에서도 그대로 준다. search_knowledge는 그 앞에 더해질 뿐이다."""
    monkeypatch.setattr(svc.mcp_client, "read_only_tools", lambda: MCP_TOOLS)
    calls = scripted(monkeypatch, GOOD)
    svc.handle(QUESTION, "s1", None, lambda req: pytest.fail("조회하면 안 된다"), run_tool=lambda name, args: {})
    assert calls[0]["tools"][-2:] == ["search_knowledge", "get_live_position"]
