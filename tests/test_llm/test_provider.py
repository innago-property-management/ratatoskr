"""Tests for api_agent.llm.provider — LLMProvider ABC and run_tool_loop."""

import pytest

from api_agent.llm.provider import LLMProvider, MaxTurnsExceeded, RunResult
from api_agent.llm.types import LLMResponse, ToolCall, ToolDefinition


class MockProvider(LLMProvider):
    """Mock provider that returns canned responses in sequence."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(model="mock", api_key="mock-key")
        self._responses = list(responses)
        self._call_index = 0
        self.call_log: list[dict] = []

    async def complete(self, messages, tools=None, temperature=0.0, max_tokens=4096):
        self.call_log.append({"messages": messages, "tools": tools})
        if self._call_index >= len(self._responses):
            return LLMResponse(content="<exhausted>")
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def format_tools(self, tool_defs):
        return [{"name": td.name, "params": td.parameters} for td in tool_defs]

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


def _make_tool(name: str, fn=None):
    """Helper to create a ToolDefinition."""
    if fn is None:

        def fn(**kwargs):
            return f"result-of-{name}"

    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        parameters={"type": "object", "properties": {}},
        function=fn,
    )


class TestRunToolLoop:
    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        """No tool calls — returns immediately."""
        provider = MockProvider([LLMResponse(content="Done!")])

        result = await provider.run_tool_loop(
            instructions="You are helpful.",
            user_message="Say hello",
            tool_defs=[],
        )

        assert isinstance(result, RunResult)
        assert result.final_output == "Done!"
        assert result.turns_used == 1
        assert result.tool_results == []

    @pytest.mark.asyncio
    async def test_single_tool_call(self):
        """One tool call then text response."""
        provider = MockProvider(
            [
                LLMResponse(tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "test"})]),
                LLMResponse(content="Found results."),
            ]
        )

        results_captured = []

        def search_fn(**kwargs):
            results_captured.append(kwargs)
            return '{"items": ["a", "b"]}'

        td = ToolDefinition(
            name="search",
            description="Search",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            function=search_fn,
        )

        result = await provider.run_tool_loop(
            instructions="Search for things.",
            user_message="Find test",
            tool_defs=[td],
        )

        assert result.final_output == "Found results."
        assert result.turns_used == 2
        assert len(result.tool_results) == 1
        assert result.tool_results[0].name == "search"
        assert results_captured == [{"q": "test"}]

    @pytest.mark.asyncio
    async def test_multi_turn_tool_calls(self):
        """Multiple rounds of tool calling."""
        provider = MockProvider(
            [
                LLMResponse(tool_calls=[ToolCall(id="c1", name="step1", arguments={})]),
                LLMResponse(tool_calls=[ToolCall(id="c2", name="step2", arguments={})]),
                LLMResponse(content="All done."),
            ]
        )

        result = await provider.run_tool_loop(
            instructions="Do steps.",
            user_message="Go",
            tool_defs=[_make_tool("step1"), _make_tool("step2")],
        )

        assert result.final_output == "All done."
        assert result.turns_used == 3
        assert len(result.tool_results) == 2

    @pytest.mark.asyncio
    async def test_max_turns_exceeded(self):
        """Raises MaxTurnsExceeded when loop exceeds limit."""
        # Always returns tool calls — never stops
        infinite_responses = [
            LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="loop", arguments={})])
            for i in range(10)
        ]
        provider = MockProvider(infinite_responses)

        with pytest.raises(MaxTurnsExceeded) as exc_info:
            await provider.run_tool_loop(
                instructions="Loop forever.",
                user_message="Go",
                tool_defs=[_make_tool("loop")],
                max_turns=3,
            )

        assert exc_info.value.turns == 3
        assert exc_info.value.last_result.turns_used == 3
        assert len(exc_info.value.last_result.tool_results) == 3

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        """Unknown tool name returns an error result."""
        provider = MockProvider(
            [
                LLMResponse(tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})]),
                LLMResponse(content="ok"),
            ]
        )

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_make_tool("real_tool")],
        )

        assert result.turns_used == 2
        assert len(result.tool_results) == 1
        assert "Unknown tool" in result.tool_results[0].content

    @pytest.mark.asyncio
    async def test_tool_exception_captured(self):
        """Tool that raises an exception returns error result."""

        def failing_fn(**kwargs):
            raise ValueError("something broke")

        provider = MockProvider(
            [
                LLMResponse(tool_calls=[ToolCall(id="c1", name="bad", arguments={})]),
                LLMResponse(content="recovered"),
            ]
        )

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_make_tool("bad", fn=failing_fn)],
        )

        assert result.turns_used == 2
        assert "something broke" in result.tool_results[0].content

    @pytest.mark.asyncio
    async def test_should_stop_callback(self):
        """should_stop callback triggers early return with __DIRECT_RETURN__."""
        provider = MockProvider(
            [
                LLMResponse(tool_calls=[ToolCall(id="c1", name="fetch", arguments={})]),
                # Would continue here but should_stop cuts it short
            ]
        )

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_make_tool("fetch")],
            should_stop=lambda results: True,  # Always stop after first tool execution
        )

        assert result.final_output == "__DIRECT_RETURN__"
        assert result.turns_used == 1

    @pytest.mark.asyncio
    async def test_inject_instructions_callback(self):
        """inject_instructions mutates system prompt each turn."""
        provider = MockProvider([LLMResponse(content="Done")])

        def injector(base: str, turn: int) -> str:
            return f"{base} [Turn {turn}]"

        await provider.run_tool_loop(
            instructions="Base prompt",
            user_message="Go",
            tool_defs=[],
            inject_instructions=injector,
        )

        # Check the system message was mutated
        messages = provider.call_log[0]["messages"]
        assert messages[0]["content"] == "Base prompt [Turn 1]"

    @pytest.mark.asyncio
    async def test_async_tool_function(self):
        """Async tool functions are awaited properly."""

        async def async_fn(**kwargs):
            return "async-result"

        provider = MockProvider(
            [
                LLMResponse(tool_calls=[ToolCall(id="c1", name="async_tool", arguments={})]),
                LLMResponse(content="Done"),
            ]
        )

        result = await provider.run_tool_loop(
            instructions="Test",
            user_message="Go",
            tool_defs=[_make_tool("async_tool", fn=async_fn)],
        )

        assert result.tool_results[0].content == "async-result"


class TestRunResult:
    def test_fields(self):
        r = RunResult(final_output="ok", tool_results=[], turns_used=1)
        assert r.final_output == "ok"
        assert r.turns_used == 1


class TestMaxTurnsExceeded:
    def test_message(self):
        r = RunResult(final_output=None, tool_results=[], turns_used=5)
        exc = MaxTurnsExceeded(5, r)
        assert "5" in str(exc)
        assert exc.last_result is r
