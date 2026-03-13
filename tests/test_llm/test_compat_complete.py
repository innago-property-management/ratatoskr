"""Tests for OpenAICompatProvider.complete() — T020."""

from unittest.mock import AsyncMock, MagicMock

import openai
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


def _make_openai_api_error(cls, message, status_code=None):
    """Build an openai API error with a mock response."""
    mock_response = MagicMock()
    mock_response.status_code = status_code or 400
    mock_response.json.return_value = {"error": {"message": message}}
    mock_response.headers = {}
    return cls(message=message, response=mock_response, body=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenAICompatProviderComplete:
    """Tests for OpenAICompatProvider.complete()."""

    @pytest.mark.asyncio
    async def test_success_path(self):
        """Basic successful completion with custom base_url."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")
        mock_resp = _make_openai_response(
            content="Local model says hi",
            usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        )
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Local model says hi"
        assert not result.has_tool_calls
        assert result.usage is not None
        assert result.usage["total_tokens"] == 12

    @pytest.mark.asyncio
    async def test_tool_call_success(self):
        """Tool calls parsed the same way as the OpenAI provider."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")
        tc = _make_tool_call("call_1", "my_tool", '{"x": 1}')
        mock_resp = _make_openai_response(content=None, tool_calls=[tc], usage=None)
        provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)  # type: ignore[invalid-assignment]

        result = await provider.complete(
            [{"role": "user", "content": "use tool"}],
            tools=[{"type": "function", "function": {"name": "my_tool"}}],
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].name == "my_tool"
        assert result.tool_calls[0].arguments == {"x": 1}

    @pytest.mark.asyncio
    async def test_retry_without_tools_on_bad_request_tool_error(self):
        """BadRequestError (400) with 'tool' in message triggers retry without tools."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                assert "tools" in kwargs
                raise _make_openai_api_error(
                    openai.BadRequestError,
                    "This model does not support tool calling",
                    status_code=400,
                )
            # Second call should NOT have tools
            assert "tools" not in kwargs
            return _make_openai_response(content="Fallback response")

        provider.client.chat.completions.create = mock_create  # type: ignore[invalid-assignment]

        result = await provider.complete(
            [{"role": "user", "content": "Hi"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
        )

        assert result.content == "Fallback response"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_without_tools_on_unprocessable_function_error(self):
        """UnprocessableEntityError (422) with 'function' in message triggers retry."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _make_openai_api_error(
                    openai.UnprocessableEntityError,
                    "Unsupported function calling feature",
                    status_code=422,
                )
            return _make_openai_response(content="Recovered")

        provider.client.chat.completions.create = mock_create  # type: ignore[invalid-assignment]

        result = await provider.complete(
            [{"role": "user", "content": "Hi"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
        )

        assert result.content == "Recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_bad_request_without_tool_keyword_propagates(self):
        """BadRequestError WITHOUT 'tool'/'function' in message is re-raised."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")

        provider.client.chat.completions.create = AsyncMock(  # type: ignore[invalid-assignment]
            side_effect=_make_openai_api_error(
                openai.BadRequestError,
                "Invalid request: max_tokens exceeds limit",
                status_code=400,
            ),
        )

        with pytest.raises(openai.BadRequestError, match="max_tokens exceeds limit"):
            await provider.complete(
                [{"role": "user", "content": "Hi"}],
                tools=[{"type": "function", "function": {"name": "test"}}],
            )

    @pytest.mark.asyncio
    async def test_auth_error_not_caught(self):
        """AuthenticationError is NOT caught by the retry logic — re-raised immediately."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")

        provider.client.chat.completions.create = AsyncMock(  # type: ignore[invalid-assignment]
            side_effect=_make_openai_api_error(
                openai.AuthenticationError,
                "Invalid API key with tool info",
                status_code=401,
            ),
        )

        with pytest.raises(openai.AuthenticationError):
            await provider.complete(
                [{"role": "user", "content": "Hi"}],
                tools=[{"type": "function", "function": {"name": "test"}}],
            )

    @pytest.mark.asyncio
    async def test_rate_limit_error_not_caught(self):
        """RateLimitError is NOT caught by the retry logic — re-raised immediately."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")

        provider.client.chat.completions.create = AsyncMock(  # type: ignore[invalid-assignment]
            side_effect=_make_openai_api_error(
                openai.RateLimitError,
                "Rate limit exceeded, tool quota exhausted",
                status_code=429,
            ),
        )

        with pytest.raises(openai.RateLimitError):
            await provider.complete(
                [{"role": "user", "content": "Hi"}],
                tools=[{"type": "function", "function": {"name": "test"}}],
            )

    @pytest.mark.asyncio
    async def test_error_without_tools_propagates(self):
        """BadRequestError when no tools are provided always propagates (no retry)."""
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")

        provider.client.chat.completions.create = AsyncMock(  # type: ignore[invalid-assignment]
            side_effect=_make_openai_api_error(
                openai.BadRequestError,
                "This model does not support tool calling",
                status_code=400,
            ),
        )

        with pytest.raises(openai.BadRequestError, match="does not support tool"):
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
        provider = OpenAICompatProvider(model="local-model", base_url="http://localhost:11434/v1")
        assert provider.api_key == "not-needed"
