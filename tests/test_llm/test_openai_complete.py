"""Tests for OpenAIProvider.complete() — T018."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api_agent.llm.openai_provider import OpenAIProvider
from api_agent.llm.types import LLMResponse, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_openai_response(content=None, tool_calls=None, usage=None):
    """Build a mock OpenAI ChatCompletion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]

    if usage:
        response.usage = MagicMock()
        response.usage.prompt_tokens = usage.get("prompt_tokens", 0)
        response.usage.completion_tokens = usage.get("completion_tokens", 0)
        response.usage.total_tokens = usage.get("total_tokens", 0)
    else:
        response.usage = None

    return response


def _make_tool_call(id, name, arguments_json):
    """Build a mock OpenAI tool call object."""
    tc = MagicMock()
    tc.id = id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments_json
    return tc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOpenAIProviderComplete:
    """Tests for OpenAIProvider.complete()."""

    @pytest.mark.asyncio
    async def test_text_response(self):
        """Text-only response: content populated, no tool_calls."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")
        mock_resp = _make_openai_response(
            content="Hello!",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello!"
        assert not result.has_tool_calls
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        """Tool call response: tool_calls parsed into ToolCall objects."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")

        tc1 = _make_tool_call("call_1", "search", '{"query": "flights"}')
        tc2 = _make_tool_call("call_2", "filter", '{"field": "price", "max": 500}')
        mock_resp = _make_openai_response(
            content=None,
            tool_calls=[tc1, tc2],
            usage={"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
        )
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete(
            [{"role": "user", "content": "Find cheap flights"}],
            tools=[{"type": "function", "function": {"name": "search"}}],
        )

        assert result.has_tool_calls
        assert len(result.tool_calls) == 2

        assert isinstance(result.tool_calls[0], ToolCall)
        assert result.tool_calls[0].id == "call_1"
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "flights"}

        assert result.tool_calls[1].id == "call_2"
        assert result.tool_calls[1].name == "filter"
        assert result.tool_calls[1].arguments == {"field": "price", "max": 500}

    @pytest.mark.asyncio
    async def test_usage_tracking(self):
        """Usage stats are extracted from the response."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")
        mock_resp = _make_openai_response(
            content="ok",
            usage={"prompt_tokens": 100, "completion_tokens": 42, "total_tokens": 142},
        )
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete([{"role": "user", "content": "test"}])

        assert result.usage is not None
        assert result.usage["prompt_tokens"] == 100
        assert result.usage["completion_tokens"] == 42
        assert result.usage["total_tokens"] == 142

    @pytest.mark.asyncio
    async def test_usage_none_when_absent(self):
        """If response.usage is None, LLMResponse.usage is None."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")
        mock_resp = _make_openai_response(content="ok", usage=None)
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete([{"role": "user", "content": "test"}])

        assert result.usage is None

    @pytest.mark.asyncio
    async def test_malformed_tool_arguments_default_to_empty_dict(self):
        """Tool call with invalid JSON arguments falls back to {}."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")

        tc = _make_tool_call("call_bad", "broken_tool", "NOT VALID JSON {{{{")
        mock_resp = _make_openai_response(
            content=None,
            tool_calls=[tc],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        )
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete(
            [{"role": "user", "content": "do something"}],
            tools=[{"type": "function", "function": {"name": "broken_tool"}}],
        )

        assert result.has_tool_calls
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_tools_kwarg_passed_only_when_provided(self):
        """When tools=None, 'tools' key is not in the create() kwargs."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")
        mock_resp = _make_openai_response(content="ok", usage=None)
        mock_create = AsyncMock(return_value=mock_resp)
        provider.client.chat.completions.create = mock_create  # type: ignore[invalid-assignment]

        await provider.complete([{"role": "user", "content": "test"}])

        call_kwargs = mock_create.call_args[1]
        assert "tools" not in call_kwargs

    @pytest.mark.asyncio
    async def test_tools_kwarg_included_when_provided(self):
        """When tools list is given, it is forwarded to the create() call."""
        provider = OpenAIProvider(model="gpt-4o-mini", api_key="test-key")
        mock_resp = _make_openai_response(content="ok", usage=None)
        mock_create = AsyncMock(return_value=mock_resp)
        provider.client.chat.completions.create = mock_create  # type: ignore[invalid-assignment]

        tools = [{"type": "function", "function": {"name": "my_tool"}}]
        await provider.complete(
            [{"role": "user", "content": "test"}], tools=tools
        )

        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["tools"] is tools
