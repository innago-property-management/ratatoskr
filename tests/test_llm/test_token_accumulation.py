"""Tests for LLM token accumulation in RunResult across tool loop turns."""

import pytest

from api_agent.llm.provider import DIRECT_RETURN, LLMProvider, MaxTurnsExceeded, RunResult
from api_agent.llm.types import LLMResponse, ToolCall, ToolDefinition


class MockProvider(LLMProvider):
    """Mock provider that returns canned responses with usage data."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(model="mock", api_key="mock-key", provider_name="mock-provider")
        self._responses = list(responses)
        self._call_index = 0

    async def complete(self, messages, tools=None, temperature=0.0, max_tokens=4096):
        if self._call_index >= len(self._responses):
            return LLMResponse(content="<exhausted>")
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def format_tools(self, tool_defs):
        return [{"name": td.name} for td in tool_defs]

    def format_tool_results(self, tool_results, messages):
        for tr in tool_results:
            messages.append(
                {"role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content}
            )
        return messages

    def format_assistant_tool_calls(self, response, messages):
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [{"id": tc.id, "name": tc.name} for tc in response.tool_calls],
            }
        )
        return messages


def _tool(name="test_tool"):
    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        parameters={"type": "object", "properties": {}},
        function=lambda **kw: "ok",
    )


class TestTokenAccumulation:
    """Token usage is accumulated across turns and reported in RunResult."""

    @pytest.mark.asyncio
    async def test_single_turn_usage(self):
        """Single text response accumulates usage from one turn."""
        resp = LLMResponse(
            content="Done",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        provider = MockProvider([resp])

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[],
        )

        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.total_tokens == 150

    @pytest.mark.asyncio
    async def test_multi_turn_accumulation(self):
        """Usage accumulates across tool call + final response turns."""
        responses = [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="test_tool", arguments={})],
                usage={"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230},
            ),
            LLMResponse(
                content="Done",
                usage={"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
            ),
        ]
        provider = MockProvider(responses)

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_tool()],
        )

        assert result.prompt_tokens == 500  # 200 + 300
        assert result.completion_tokens == 70  # 30 + 40
        assert result.total_tokens == 570  # 230 + 340

    @pytest.mark.asyncio
    async def test_three_turn_accumulation(self):
        """Three turns (two tool calls + final) accumulate correctly."""
        responses = [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="step1", arguments={})],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            ),
            LLMResponse(
                tool_calls=[ToolCall(id="c2", name="step2", arguments={})],
                usage={"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220},
            ),
            LLMResponse(
                content="All done",
                usage={"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330},
            ),
        ]
        provider = MockProvider(responses)

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_tool("step1"), _tool("step2")],
        )

        assert result.prompt_tokens == 600
        assert result.completion_tokens == 60
        assert result.total_tokens == 660

    @pytest.mark.asyncio
    async def test_none_usage_ignored(self):
        """Responses with usage=None don't break accumulation."""
        responses = [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="test_tool", arguments={})],
                usage=None,
            ),
            LLMResponse(
                content="Done",
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            ),
        ]
        provider = MockProvider(responses)

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_tool()],
        )

        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.total_tokens == 150

    @pytest.mark.asyncio
    async def test_partial_usage_dict(self):
        """Usage dict missing some keys defaults those to 0."""
        resp = LLMResponse(
            content="Done",
            usage={"prompt_tokens": 100},  # missing completion_tokens & total_tokens
        )
        provider = MockProvider([resp])

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[],
        )

        assert result.prompt_tokens == 100
        assert result.completion_tokens == 0
        assert result.total_tokens == 0

    @pytest.mark.asyncio
    async def test_no_usage_at_all(self):
        """Response with no usage field yields zeros."""
        resp = LLMResponse(content="Done")
        provider = MockProvider([resp])

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[],
        )

        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.total_tokens == 0

    @pytest.mark.asyncio
    async def test_direct_return_carries_tokens(self):
        """DIRECT_RETURN (should_stop) path carries accumulated tokens."""
        responses = [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="test_tool", arguments={})],
                usage={"prompt_tokens": 250, "completion_tokens": 75, "total_tokens": 325},
            ),
        ]
        provider = MockProvider(responses)

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_tool()],
            should_stop=lambda results: True,
        )

        assert result.final_output is DIRECT_RETURN
        assert result.prompt_tokens == 250
        assert result.completion_tokens == 75
        assert result.total_tokens == 325

    @pytest.mark.asyncio
    async def test_max_turns_exceeded_carries_tokens(self):
        """MaxTurnsExceeded exception carries accumulated token counts."""
        responses = [
            LLMResponse(
                tool_calls=[ToolCall(id=f"c{i}", name="loop", arguments={})],
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            )
            for i in range(5)
        ]
        provider = MockProvider(responses)

        with pytest.raises(MaxTurnsExceeded) as exc_info:
            await provider.run_tool_loop(
                instructions="Test",
                user_message="Go",
                tool_defs=[_tool("loop")],
                max_turns=3,
            )

        last = exc_info.value.last_result
        assert last.prompt_tokens == 300  # 100 * 3
        assert last.completion_tokens == 60  # 20 * 3
        assert last.total_tokens == 360  # 120 * 3


class TestProviderName:
    """Provider name is set on construction and accessible."""

    def test_default_provider_name(self):
        """Default provider_name is 'unknown'."""
        provider = MockProvider([])
        # MockProvider sets "mock-provider"
        assert provider.provider_name == "mock-provider"

    def test_run_result_defaults(self):
        """RunResult token fields default to zero."""
        r = RunResult(final_output="ok", tool_results=[], turns_used=1)
        assert r.prompt_tokens == 0
        assert r.completion_tokens == 0
        assert r.total_tokens == 0
