"""Anthropic(Claude) 직접 호출 테스트 (services/llm._anthropic_call, 10-08 사용자 결정).

키·네트워크 없이 돈다. httpx.MockTransport로 Messages API 응답을 흉내 내고, 보낸 요청 본문을 검사한다.
확인하는 것: OpenAI 형식 대화·도구·구조화 출력이 Messages API 모양으로 바뀌는지, 응답이 LLMResult로 돌아오는지,
오류가 기존 LLMError 종류(auth·bad_request·rate_limited·overloaded)로 올라오는지.
"""
import json

import httpx
import pytest

from backend.app.services import llm

MODEL = "claude/claude-haiku-5-5"


def _use(monkeypatch, handler):
    """_anthropic_http를 가짜 transport 클라이언트로 바꾸고, 보낸 요청을 모아 돌려준다."""
    sent: list[dict] = []

    def wrapped(request: httpx.Request):
        sent.append(json.loads(request.content))
        return handler(request)
    monkeypatch.setattr(llm, "_anthropic_http", lambda: httpx.Client(transport=httpx.MockTransport(wrapped)))
    return sent


def _ok(content, stop="end_turn", usage=None):
    return lambda request: httpx.Response(200, json={
        "id": "msg_test", "model": "claude-haiku-5-5", "content": content, "stop_reason": stop,
        "usage": usage or {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}})


def test_structured_output_uses_forced_schema_tool_and_returns_json_content(monkeypatch):
    sent = _use(monkeypatch, _ok([{"type": "tool_use", "id": "t1", "name": llm.STRUCTURED_TOOL,
                                   "input": {"verdict": "fine", "answer": "순항"}}], stop="tool_use"))
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}, "answer": {"type": "string"}},
              "required": ["verdict", "answer"], "additionalProperties": False}
    result = llm.chat([{"role": "system", "content": "SYS"}, {"role": "user", "content": "Q"}], model=MODEL,
                      response_format={"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": schema}})
    body = sent[0]
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == llm.ANTHROPIC_MAX_TOKENS
    assert body["system"][0]["text"] == "SYS" and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Q"}]}]
    assert body["tool_choice"] == {"type": "tool", "name": llm.STRUCTURED_TOOL}
    assert body["tools"][-1]["input_schema"] == schema
    assert json.loads(result.content) == {"verdict": "fine", "answer": "순항"} and result.tool_calls == []
    assert result.finish_reason == "stop" and result.provider == "anthropic"
    # 입력 토큰은 캐시에서 읽은 것까지 합친 전체, cached_tokens는 캐시에서 읽은 것
    assert (result.prompt_tokens, result.completion_tokens, result.cached_tokens) == (150, 20, 50)


def test_tool_round_trip_converts_calls_and_results(monkeypatch):
    sent = _use(monkeypatch, _ok([{"type": "text", "text": "확인할게요"},
                                  {"type": "tool_use", "id": "toolu_2", "name": "get_live_position", "input": {"mmsi": 440999001}}],
                                 stop="tool_use"))
    tools = [{"type": "function", "function": {"name": "get_live_position", "description": "위치",
                                               "parameters": {"type": "object", "properties": {"mmsi": {"type": "integer"}}}}}]
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "배 어디야"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "toolu_1", "type": "function", "function": {"name": "get_live_position", "arguments": "{\"mmsi\": 1}"}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "{\"lat\": 35.0}"},
    ]
    result = llm.chat(history, model=MODEL, tools=tools)
    body = sent[0]
    assert body["tools"] == [{"name": "get_live_position", "description": "위치",
                              "input_schema": {"type": "object", "properties": {"mmsi": {"type": "integer"}}}}]
    assert "tool_choice" not in body
    assert body["messages"][1] == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "get_live_position", "input": {"mmsi": 1}}]}
    assert body["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"lat\": 35.0}"}]}
    assert result.content == "확인할게요" and result.finish_reason == "tool_calls"
    assert result.tool_calls == [{"id": "toolu_2", "name": "get_live_position", "arguments": "{\"mmsi\": 440999001}"}]


def test_tool_choice_none_and_consecutive_roles_are_merged(monkeypatch):
    sent = _use(monkeypatch, _ok([{"type": "text", "text": "답"}]))
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}]
    llm.chat([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], model=MODEL, tools=tools, tool_choice="none")
    body = sent[0]
    assert body["tool_choice"] == {"type": "none"}
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]


def test_max_tokens_stop_is_length(monkeypatch):
    _use(monkeypatch, _ok([{"type": "text", "text": "{\"verdict\": "}], stop="max_tokens"))
    assert llm.chat([{"role": "user", "content": "q"}], model=MODEL).finish_reason == "length"


@pytest.mark.parametrize("status, kind, retry", [(401, "auth", False), (400, "bad_request", False),
                                                  (429, "rate_limited", True), (529, "overloaded", True)])
def test_errors_map_to_llm_error_kinds(monkeypatch, status, kind, retry):
    calls = _use(monkeypatch, lambda r: httpx.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}}))
    monkeypatch.setattr(llm, "RETRY_WAIT_SEC", 0)
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "q"}], model=MODEL)
    assert e.value.kind == kind and e.value.code == status
    assert len(calls) == (llm.MAX_ATTEMPTS if retry else 1)  # 과부하·한도만 한 번 더 부른다


def test_missing_key_is_auth_error_without_network(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "q"}], model=MODEL)
    assert e.value.kind == "auth"


def test_other_models_still_go_to_openrouter(monkeypatch):
    """claude/ 앞머리가 없으면 Anthropic 경로를 타지 않는다 (OpenRouter의 anthropic/... 모델 포함)."""
    monkeypatch.setattr(llm, "_anthropic_call", lambda kwargs: pytest.fail("Anthropic 경로를 타면 안 된다"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.chat([{"role": "user", "content": "q"}], model="anthropic/claude-haiku-4.5")
