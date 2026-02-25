"""Tests for OpenAICompatProvider.complete() — T020."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api_agent.llm.openai_compat import OpenAICompatProvider
from api_agent.llm.types import LLMResponse

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

class TestOpenAICompatProviderComplete:
    """Tests for OpenAICompatProvider.complete()."""

    @pytest.mark.asyncio
    async def test_success_path(self):
        """Basic successful completion with custom base_url."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )
        mock_resp = _make_openai_response(
            content="Local model says hi",
            usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        )
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Local model says hi"
        assert not result.has_tool_calls
        assert result.usage["total_tokens"] == 12

    @pytest.mark.asyncio
    async def test_tool_call_success(self):
        """Tool calls parsed the same way as the OpenAI provider."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )
        tc = _make_tool_call("call_1", "my_tool", '{"x": 1}')
        mock_resp = _make_openai_response(content=None, tool_calls=[tc], usage=None)
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete(
            [{"role": "user", "content": "use tool"}],
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].name == "my_tool"
        assert result.tool_calls[0].arguments == {"x": 1}

    @pytest.mark.asyncio
    async def test_retry_without_tools_on_tool_error(self):
        """First call with tools raises tool-related error; retries without tools."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                assert "tools" in kwargs
                raise Exception("This model does not support tool calling")
            # Second call should NOT have tools
            assert "tools" not in kwargs
            return _make_openai_response(content="Fallback response")

        provider.client.chat.completions.create = mock_create

        result = await provider.complete(
            [{"role": "user", "content": "Hi"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
        )

        assert result.content == "Fallback response"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_without_tools_on_function_error(self):
        """'function' keyword in error message also triggers retry."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Unsupported function calling feature")
            return _make_openai_response(content="Recovered")

        provider.client.chat.completions.create = mock_create

        result = await provider.complete(
            [{"role": "user", "content": "Hi"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
        )

        assert result.content == "Recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_non_tool_error_propagates(self):
        """Exception without 'tool' or 'function' in message raises normally."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )

        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("Connection refused")
        )

        with pytest.raises(Exception, match="Connection refused"):
            await provider.complete(
                [{"role": "user", "content": "Hi"}],
                tools=[{"type": "function", "function": {"name": "test"}}],
            )

    @pytest.mark.asyncio
    async def test_error_without_tools_propagates(self):
        """Exception when no tools are provided always propagates (no retry)."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )

        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("This model does not support tool calling")
        )

        with pytest.raises(Exception, match="does not support tool"):
            await provider.complete(
                [{"role": "user", "content": "Hi"}],
                tools=None,
            )

    def test_base_url_required(self):
        """Omitting base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url is required"):
            OpenAICompatProvider(model="local-model", base_url=None)

    def test_base_url_empty_string_required(self):
        """Empty string base_url also raises ValueError."""
        with pytest.raises(ValueError, match="base_url is required"):
            OpenAICompatProvider(model="local-model", base_url="")

    def test_api_key_defaults_to_not_needed(self):
        """api_key defaults to 'not-needed' for local models."""
        provider = OpenAICompatProvider(
            model="local-model", base_url="http://localhost:11434/v1"
        )
        assert provider.api_key == "not-needed"
