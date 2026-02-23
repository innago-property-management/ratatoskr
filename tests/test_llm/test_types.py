"""Tests for api_agent.llm.types — shared LLM types."""

from api_agent.llm.types import LLMResponse, ToolCall, ToolDefinition, ToolResult


class TestToolCall:
    def test_create(self):
        tc = ToolCall(id="call_1", name="search", arguments={"q": "hello"})
        assert tc.id == "call_1"
        assert tc.name == "search"
        assert tc.arguments == {"q": "hello"}

    def test_empty_arguments(self):
        tc = ToolCall(id="call_2", name="noop", arguments={})
        assert tc.arguments == {}


class TestToolResult:
    def test_create(self):
        tr = ToolResult(tool_call_id="call_1", name="search", content='{"results": []}')
        assert tr.tool_call_id == "call_1"
        assert tr.name == "search"
        assert tr.content == '{"results": []}'


class TestLLMResponse:
    def test_text_only(self):
        r = LLMResponse(content="Hello world")
        assert r.content == "Hello world"
        assert r.tool_calls == []
        assert r.has_tool_calls is False

    def test_with_tool_calls(self):
        tc = ToolCall(id="c1", name="fn", arguments={"x": 1})
        r = LLMResponse(tool_calls=[tc])
        assert r.has_tool_calls is True
        assert r.content is None

    def test_with_usage(self):
        r = LLMResponse(content="ok", usage={"prompt_tokens": 10, "completion_tokens": 5})
        assert r.usage is not None
        assert r.usage["prompt_tokens"] == 10

    def test_defaults(self):
        r = LLMResponse()
        assert r.content is None
        assert r.tool_calls == []
        assert r.usage is None
        assert r.has_tool_calls is False


class TestToolDefinition:
    def test_create(self):
        def dummy():
            pass

        td = ToolDefinition(
            name="dummy",
            description="A dummy tool",
            parameters={"type": "object", "properties": {}},
            function=dummy,
        )
        assert td.name == "dummy"
        assert td.description == "A dummy tool"
        assert td.function is dummy
