"""H6: 그래프 노드 span (docs/specs/v2-langgraph.md 7절, 10절 H6, SDD 5.11). 키 없이 돈다: 가짜 Langfuse 클라이언트로 확인한다.

확인하는 것:
  - 정상 경로에서 "node:<이름>" span이 State path와 같은 순서로 열리고, 요청 루트 span 아래에 걸린다
  - 노드 안에서 여는 generation(llm.judge)과 tool 관측의 부모가 그 노드 span이다 (부모 우선순위: 노드 > 요청 루트)
  - 판정 재시도마다 node:judge, node:validate가 시도 수만큼 생기고 attempt metadata가 시도 번호다
  - 후속 질문의 후속 답 generation은 node:answer_follow_up 아래에 걸린다
  - 어떤 span의 이름, input, output, metadata에도 B/L 원문, 컨테이너 번호, 전화번호, 이메일이 없다 (SDD 3.5)
  - 키가 없으면 아무것도 안 하고, AGENT_GRAPH=off(v1)에서는 node span이 생기지 않는다
  - 노드가 예외를 던지면 span이 ERROR로 닫히고 노드 문맥이 되돌려진다
가짜 UNI-PASS·도구는 test_routers.py의 fake를 같이 쓴다.
"""
from __future__ import annotations

import json

import pytest

from backend.app.graph.build import build_graph
from backend.app.graph.observe import node_span
from backend.app.observability import langfuse as tracing
from backend.app.routers import agent as agent_router
from backend.app.schemas.agent import AgentRequest
from backend.app.schemas.customs import CustomsRecord, mask_bl
from backend.app.services import llm
from backend.tests.test_routers import (  # noqa: F401  (fake는 pytest 픽스처)
    JUDGED, NOW, POSITION, _candidate, _request, _signals, fake,
)

# 가공값이다. 실제 B/L, 컨테이너 번호, 연락처가 아니다
BL = "TESTBL99XYZ0001"
CONTAINER = "TSTU1234560"
PHONE = "010-1234-5678"
EMAIL = "someone@example.com"
SECRETS = (BL, CONTAINER, PHONE, "01012345678", EMAIL)

WATCH = {"eta_conflict": "triggered"}                     # 표: watch, 회색 지대 아님
BAD = {**JUDGED, "answer": "10월 3일에 들어옵니다."}        # 3은 근거에 없는 숫자라 V7 위반
GOOD_ANSWER = "여유는 5일입니다."                           # 지난 결과의 days_margin 5와 같은 숫자라 통과
BAD_ANSWER = "10월 3일에 들어옵니다."

# H3까지 정해진 정상 경로 (judge는 한 번에 통과)
NORMAL_PATH = ["guard_input", "load_memory", "load_saved", "fetch_customs", "scope_gate", "identify", "collect",
               "compute_signals", "table_verdict", "judge", "validate", "finalize", "decorate"]


class FakeSpan:
    """Langfuse 관측 대신. 열릴 때의 인자(input 포함)와 update·end 결과를 모은다."""

    def __init__(self, n, name, as_type, input, trace_context, metadata):
        self.id = f"span{n}"
        self.trace_id = (trace_context or {}).get("trace_id") or f"trace{n}"
        self.name, self.as_type, self.input, self.trace_context = name, as_type, input, trace_context
        self.fields = {"metadata": dict(metadata or {})}
        self.ended = False

    @property
    def parent_id(self):
        return (self.trace_context or {}).get("parent_span_id")

    @property
    def metadata(self):
        return self.fields["metadata"]

    def update(self, **fields):
        self.fields.update(fields)

    def end(self):
        self.ended = True


class FakeClient:
    def __init__(self):
        self.spans: list[FakeSpan] = []

    def create_trace_id(self, seed=None):
        return f"trace-{seed}"

    def start_observation(self, *, name, as_type, input=None, trace_context=None, metadata=None, **extra):
        span = FakeSpan(len(self.spans) + 1, name, as_type, input, trace_context, metadata)
        self.spans.append(span)
        return span

    def named(self, name):
        return [s for s in self.spans if s.name == name]

    def nodes(self):
        return [s for s in self.spans if s.name.startswith("node:")]


@pytest.fixture
def lf(monkeypatch):
    """키가 있는 것처럼 보이게 하고 langfuse.get_client를 가짜로 바꾼다."""
    import langfuse

    client = FakeClient()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(langfuse, "get_client", lambda: client)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    # 검증(V9, V10)이 부르는 프롬프트 조회가 가짜 키로 Langfuse 서버에 가지 않게, 로컬 사본을 읽는 것으로 바꾼다
    monkeypatch.setattr(llm, "get_prompt", lambda name, label="production": tracing.Prompt(
        name=name, text=(tracing.PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8"), version=tracing.LOCAL_VERSION))
    return client


def _customs() -> CustomsRecord:
    """컨테이너 번호가 든 세관 기록(가공값). State 안에 식별 값이 있어도 span에 새지 않는지 보려고 넣는다."""
    return CustomsRecord(ship_name="TEST STAR", ship_nat="PA", stage_name="반입신고", container_no=CONTAINER,
                         bl_masked=mask_bl(BL), fetched_at=NOW)


def _judge_steps(steps: list):
    """판정 LLM 가짜. 실제 llm.py처럼 호출마다 generation 하나(와 안쪽 tool 관측 하나)를 연다. 부모는 트레이스 코드가 정한다.
    generation의 input은 고정 문자열이다(실제 llm.py는 B/L을 가린 입력을 쓴다)."""
    calls = []

    def judge(signal_set, customs, margin, feedback=None, **kwargs):
        calls.append(feedback)
        gen = tracing.start_generation(llm.JUDGE_PROMPT, "masked prompt")
        with tracing.tool_span("get_live_position", {"mmsi": 440999001}):
            pass
        tracing.end_generation(gen, None, "test")
        step = steps[min(len(calls), len(steps)) - 1]
        if isinstance(step, Exception):
            raise step
        return llm.LLMAnswer(output=llm.JudgeOutput.model_validate(step),
                             llm=llm.LLMResult(content="", model="test/model"), prompt_version="test")
    return judge, calls


def _follow_steps(steps: list):
    """후속 답 LLM 가짜. 호출마다 generation 하나를 연다."""
    calls = []

    def follow(question, response, feedback=None, tools=None, run_tool=None, memory=None, **kwargs):
        calls.append(feedback)
        gen = tracing.start_generation(llm.FOLLOW_UP_PROMPT, "masked prompt")
        tracing.end_generation(gen, None, "test")
        step = steps[min(len(calls), len(steps)) - 1]
        return llm.LLMAnswer(output=llm.FollowUpOutput(in_scope=True, answer=step),
                             llm=llm.LLMResult(content="", model="test/model"), prompt_version="test")
    return follow, calls


def _setup_judge(fake, monkeypatch, steps: list) -> list:
    fake(customs=_customs(), candidates=[_candidate(440999001)], position=POSITION)
    _signals(monkeypatch, WATCH)
    judge, calls = _judge_steps(steps)
    monkeypatch.setattr(agent_router.llm, "judge", judge)
    return calls


def _invoke(request: AgentRequest, graph=None) -> dict:
    """요청 루트 span 안에서 그래프를 돌린다(라우터가 하는 것과 같다). 최종 State를 돌려준다."""
    graph = graph or build_graph()
    with tracing.request_span("/api/agent"):
        return graph.invoke({"request": request, "user_id": None, "bl_masked": mask_bl(request.bl_no), "path": []})


def _req(**over) -> AgentRequest:
    return AgentRequest(**_request(bl_no=BL, **over))


def _everything(client: FakeClient) -> str:
    """모든 span의 이름, input, output, metadata, 부모 정보를 한 문자열로. 비밀 값 검사에 쓴다."""
    return json.dumps([{"name": s.name, "input": s.input, "fields": s.fields, "ctx": s.trace_context}
                       for s in client.spans], ensure_ascii=False, default=str)


# ── 정상 경로 ──

def test_normal_path_node_spans_follow_state_path_under_the_root(fake, lf, monkeypatch):
    """node span 이름 순서가 State path와 같고 H3 정상 경로와 같다. 전부 요청 루트 span의 자식이고 열었던 것은 모두 닫혔다."""
    _setup_judge(fake, monkeypatch, [JUDGED])
    out = _invoke(_req())
    names = [s.name for s in lf.nodes()]
    assert names == [f"node:{n}" for n in out["path"]] == [f"node:{n}" for n in NORMAL_PATH]
    root = lf.named("request:/api/agent")[0]
    assert all(s.parent_id == root.id and s.trace_id == root.trace_id for s in lf.nodes())
    assert all(s.ended for s in lf.spans)
    assert out["response"].status == "ok"


def test_node_span_has_no_input_or_output_and_only_safe_metadata(fake, lf, monkeypatch):
    """input·output은 비어 있고, metadata는 node, request_id, latency_ms, attempt, outcome뿐이다."""
    _setup_judge(fake, monkeypatch, [JUDGED])
    _invoke(_req())
    for span in lf.nodes():
        assert span.input is None and "output" not in span.fields
        assert set(span.metadata) <= {"node", "request_id", "latency_ms", "attempt", "outcome", "resumed"}
        assert span.metadata["node"] == span.name.removeprefix("node:") and span.metadata["latency_ms"] >= 0
    judge, validate, decorate = (lf.named(f"node:{n}")[0] for n in ("judge", "validate", "decorate"))
    assert (judge.metadata["attempt"], judge.metadata["outcome"]) == (1, "pass")
    assert (validate.metadata["attempt"], validate.metadata["outcome"]) == (1, "pass")
    assert decorate.metadata["outcome"] == "status:ok"  # 응답이 정해진 노드는 응답 status만 남는다


def test_generation_and_tool_parents_are_the_node_span(fake, lf, monkeypatch):
    """노드 안에서 연 llm.judge generation과 tool 관측의 부모는 node:judge다 (노드 > 요청 루트). 노드 밖 관측은 다시 루트 아래로 간다."""
    _setup_judge(fake, monkeypatch, [JUDGED])
    with tracing.request_span("/api/agent"):
        root = lf.named("request:/api/agent")[0]
        build_graph().invoke({"request": _req(), "user_id": None, "bl_masked": mask_bl(BL), "path": []})
        assert tracing._node_var.get() is None  # 노드가 끝나면 문맥이 되돌아온다
        after = tracing.start_tool("after_graph", None)
    node_judge = lf.named("node:judge")[0]
    generation = lf.named(llm.JUDGE_PROMPT)[0]
    tool = lf.named("get_live_position")[0]
    assert generation.as_type == "generation" and generation.parent_id == node_judge.id
    assert tool.as_type == "tool" and tool.parent_id == node_judge.id
    assert generation.trace_id == tool.trace_id == root.trace_id
    assert after.parent_id == root.id  # 노드 밖에서는 요청 루트가 부모다 (v1과 같다)


# ── 재시도 ──

def test_retries_make_one_judge_span_per_attempt(fake, lf, monkeypatch):
    """위반 두 번 뒤 통과: node:judge, node:validate가 3번씩, attempt는 1, 2, 3. 각 judge 시도의 generation은 그 시도의 node:judge 아래다."""
    calls = _setup_judge(fake, monkeypatch, [BAD, BAD, JUDGED])
    out = _invoke(_req())
    judges, validates = lf.named("node:judge"), lf.named("node:validate")
    assert len(calls) == len(judges) == len(validates) == 3
    assert [s.metadata["attempt"] for s in judges] == [1, 2, 3]
    assert [s.metadata["outcome"] for s in validates] == ["retry", "retry", "pass"]
    assert [g.parent_id for g in lf.named(llm.JUDGE_PROMPT)] == [s.id for s in judges]
    assert [f"node:{n}" for n in out["path"]] == [s.name for s in lf.nodes()]


def test_give_up_after_three_violations_ends_in_fallback_partial(fake, lf, monkeypatch):
    """세 번 모두 위반이면 node:judge 3개 다음 node:fallback_partial. 마지막 validate의 갈림길은 retry로 남고 엣지가 접는다."""
    _setup_judge(fake, monkeypatch, [BAD])
    out = _invoke(_req())
    assert len(lf.named("node:judge")) == 3
    assert [s.name for s in lf.nodes()][-3:] == ["node:validate", "node:fallback_partial", "node:decorate"]
    assert out["response"].status == "partial"


# ── 후속 질문 ──

def test_follow_up_generation_nests_under_answer_follow_up(fake, lf, monkeypatch):
    """후속 답 generation의 부모는 node:answer_follow_up이다. 위반 뒤 다시 물으면 node 쌍이 시도 수만큼 생긴다."""
    fake(judge=JUDGED)
    previous = agent_router._response(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5, answer="지난 답")
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: previous)
    follow, calls = _follow_steps([BAD_ANSWER, GOOD_ANSWER])
    monkeypatch.setattr(agent_router.llm, "answer_follow_up", follow)
    out = _invoke(_req(follow_up_question="여유가 며칠이야?"))
    answers, validates = lf.named("node:answer_follow_up"), lf.named("node:validate_follow_up")
    assert len(calls) == len(answers) == len(validates) == 2
    assert [g.parent_id for g in lf.named(llm.FOLLOW_UP_PROMPT)] == [s.id for s in answers]
    assert [s.metadata["attempt"] for s in answers] == [1, 2]
    assert [s.metadata["outcome"] for s in validates] == ["retry", "pass"]
    assert [s.name for s in lf.nodes()] == [f"node:{n}" for n in out["path"]]
    assert out["response"].answer == GOOD_ANSWER


# ── 비밀 값 ──

def _assert_no_secrets(client: FakeClient):
    text = _everything(client)
    for secret in SECRETS:
        assert secret not in text, f"span에 {secret[:4]}... 가 남았다"
    assert "TESTBL99" not in text  # B/L 앞부분도 (가린 값은 앞 4자만 남는다)


def test_no_identifier_reaches_any_span_on_the_judge_path(fake, lf, monkeypatch):
    """B/L 원문, 컨테이너 번호가 어떤 span의 이름, input, output, metadata에도 없다. 재시도 경로 포함."""
    _setup_judge(fake, monkeypatch, [BAD, JUDGED])
    _invoke(_req())
    assert len(lf.nodes()) > 10
    _assert_no_secrets(lf)


def test_no_identifier_reaches_any_span_on_the_follow_up_path(fake, lf, monkeypatch):
    """후속 질문에 전화번호와 이메일이 들어 있어도 어떤 span에도 가지 않는다."""
    fake(judge=JUDGED, customs=_customs())
    previous = agent_router._response(status="ok", verdict="watch", vessel_match="confirmed", days_margin=5, answer="지난 답")
    monkeypatch.setattr(agent_router, "_previous_response", lambda request, user_id=None: previous)
    follow, _ = _follow_steps([GOOD_ANSWER])
    monkeypatch.setattr(agent_router.llm, "answer_follow_up", follow)
    _invoke(_req(follow_up_question=f"연락처 {PHONE}, {EMAIL}로 알려줘. 여유가 며칠이야?"))
    assert lf.named("node:answer_follow_up")
    _assert_no_secrets(lf)


def test_annotate_reads_only_whitelisted_fields():
    """node_span이 State 갱신에서 읽는 칸은 시도 번호와 갈림길 이름, 응답 status뿐이다. 문장 칸에 든 값은 옮기지 않는다."""
    from backend.app.graph.observe import _annotate

    class H:
        def __init__(self):
            self.metadata = {}

    h = H()
    _annotate(h, "judge", {}, {"judge_attempt": 2, "judge_route": "retry", "judge_feedback": [BL], "request": _req(),
                               "follow_answer": [PHONE]})
    assert h.metadata == {"attempt": 2, "outcome": "retry"}
    h = H()
    _annotate(h, "validate", {"judge_attempt": 3}, {"judge_route": BL})  # 목록 밖의 값은 갈림길 이름으로 쓰지 않는다
    assert h.metadata == {"attempt": 3}
    h = H()
    _annotate(h, "decorate", {"judge_attempt": 3}, {})  # 루프 밖 노드에는 지난 루프의 시도 번호를 붙이지 않는다
    assert h.metadata == {}


# ── 키 없음, v1, 오류 ──

def test_without_keys_nothing_is_traced_and_graph_still_runs(fake, monkeypatch):
    """키가 없으면 Langfuse 클라이언트를 건드리지 않고 문맥도 만들지 않는다. 결과는 그대로다."""
    import langfuse

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(langfuse, "get_client", lambda: pytest.fail("키가 없는데 Langfuse 클라이언트를 만들었다"))
    _setup_judge(fake, monkeypatch, [JUDGED])
    out = _invoke(_req())
    assert out["path"] == NORMAL_PATH and out["response"].status == "ok"
    assert tracing._node_var.get() is None


def test_v1_path_creates_no_node_spans(fake, lf, monkeypatch):
    """AGENT_GRAPH=off(기본)의 run_agent는 node span을 만들지 않는다. generation 부모는 요청 루트 그대로다.
    run_agent가 요청 루트 span을 직접 열므로 이 테스트는 바깥에서 따로 열지 않는다."""
    monkeypatch.setenv("AGENT_GRAPH", "off")
    _setup_judge(fake, monkeypatch, [JUDGED])
    agent_router.run_agent(_req(), user_id=None)
    root = lf.named("request:/api/agent")[0]
    assert lf.nodes() == []
    assert lf.named(llm.JUDGE_PROMPT)[0].parent_id == root.id


def test_failing_node_closes_span_as_error_and_restores_context(lf):
    """노드가 예외를 던지면 span은 ERROR(예외 종류만)로 닫히고 예외는 그대로 올라가며 노드 문맥은 비워진다."""
    @node_span("boom")
    def boom(state):
        raise KeyError(BL)  # 예외 메시지에 비밀이 있어도 종류만 남는다

    with pytest.raises(KeyError):
        boom({})
    span = lf.named("node:boom")[0]
    assert span.ended and span.fields["level"] == "ERROR" and span.fields["status_message"] == "KeyError"
    assert BL not in _everything(lf)
    assert tracing._node_var.get() is None
