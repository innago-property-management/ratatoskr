"""Tests for provider format methods — no real API calls."""

from api_agent.llm.anthropic_provider import AnthropicProvider
from api_agent.llm.openai_compat import OpenAICompatProvider
from api_agent.llm.openai_provider import OpenAIProvider
from api_agent.llm.types import LLMResponse, ToolCall, ToolDefinition, ToolResult


def _sample_tool_def():
    return ToolDefinition(
        name="search",
        description="Search for items",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        function=lambda q: "result",
    )


def _sample_tool_result():
    return ToolResult(tool_call_id="call_1", name="search", content='{"items": []}')


def _sample_response_with_tools():
    return LLMResponse(
        content="Thinking...",
        tool_calls=[ToolCall(id="call_1", name="search", arguments={"q": "test"})],
    )


class TestAnthropicFormat:
    def setup_method(self):
        self.provider = AnthropicProvider(api_key="test-key")

    def test_format_tools(self):
        tools = self.provider.format_tools([_sample_tool_def()])
        assert len(tools) == 1
        t = tools[0]
        assert t["name"] == "search"
        assert t["description"] == "Search for items"
        assert "input_schema" in t  # Anthropic uses input_schema, not parameters
        assert t["input_schema"]["properties"]["q"]["type"] == "string"

    def test_format_tool_results(self):
        messages = []
        messages = self.provider.format_tool_results([_sample_tool_result()], messages)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert msg["content"][0]["type"] == "tool_result"
        assert msg["content"][0]["tool_use_id"] == "call_1"

    def test_format_assistant_tool_calls(self):
        messages = []
        messages = self.provider.format_assistant_tool_calls(
            _sample_response_with_tools(), messages
        )
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "assistant"
        content = msg["content"]
        # Should have text block + tool_use block
        types = [c["type"] for c in content]
        assert "text" in types
        assert "tool_use" in types


class TestOpenAIFormat:
    def setup_method(self):
        self.provider = OpenAIProvider(model="gpt-4o", api_key="test-key")

    def test_format_tools(self):
        tools = self.provider.format_tools([_sample_tool_def()])
        assert len(tools) == 1
        t = tools[0]
        assert t["type"] == "function"
        assert t["function"]["name"] == "search"
        assert t["function"]["parameters"]["properties"]["q"]["type"] == "string"

    def test_format_tool_results(self):
        messages = []
        messages = self.provider.format_tool_results([_sample_tool_result()], messages)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_1"

    def test_format_assistant_tool_calls(self):
        messages = []
        messages = self.provider.format_assistant_tool_calls(
            _sample_response_with_tools(), messages
        )
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        tc = msg["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"


class TestOpenAICompatFormat:
    """OpenAI-compat uses same format as OpenAI."""

    def setup_method(self):
        self.provider = OpenAICompatProvider(
            model="llama3", api_key="not-needed", base_url="http://localhost:11434/v1"
        )

    def test_format_tools(self):
        tools = self.provider.format_tools([_sample_tool_def()])
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "search"

    def test_format_tool_results(self):
        messages = []
        messages = self.provider.format_tool_results([_sample_tool_result()], messages)
        assert messages[0]["role"] == "tool"

    def test_format_assistant_tool_calls(self):
        messages = []
        messages = self.provider.format_assistant_tool_calls(
            _sample_response_with_tools(), messages
        )
        assert messages[0]["role"] == "assistant"
        assert "tool_calls" in messages[0]
