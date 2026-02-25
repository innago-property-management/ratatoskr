"""Tests for AnthropicProvider.complete() — T019."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api_agent.llm.anthropic_provider import AnthropicProvider
from api_agent.llm.types import LLMResponse, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_anthropic_response(content_blocks, usage=None, stop_reason="end_turn"):
    """Build a mock Anthropic Messages API response."""
    response = MagicMock()
    response.content = []
    for block in content_blocks:
        b = MagicMock()
        b.type = block["type"]
        if block["type"] == "text":
            b.text = block["text"]
        elif block["type"] == "tool_use":
            b.id = block["id"]
            b.name = block["name"]
            b.input = block["input"]
        response.content.append(b)

    if usage:
        response.usage = MagicMock()
        response.usage.input_tokens = usage.get("input_tokens", 0)
        response.usage.output_tokens = usage.get("output_tokens", 0)
    else:
        response.usage = None

    response.stop_reason = stop_reason
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAnthropicProviderComplete:
    """Tests for AnthropicProvider.complete()."""

    @pytest.mark.asyncio
    async def test_text_response(self):
        """Text-only content blocks produce content, no tool_calls."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[{"type": "text", "text": "Hello from Claude!"}],
            usage={"input_tokens": 10, "output_tokens": 8},
        )
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete([{"role": "user", "content": "Hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello from Claude!"
        assert not result.has_tool_calls
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        """tool_use blocks are parsed into ToolCall objects."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "search",
                    "input": {"query": "flights to Paris"},
                },
            ],
            usage={"input_tokens": 20, "output_tokens": 15},
        )
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete(
            [{"role": "user", "content": "Search flights"}],
            tools=[{"name": "search", "input_schema": {}}],
        )

        assert result.has_tool_calls
        assert len(result.tool_calls) == 1
        assert isinstance(result.tool_calls[0], ToolCall)
        assert result.tool_calls[0].id == "toolu_01"
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "flights to Paris"}
        # Content should be None when there are no text blocks
        assert result.content is None

    @pytest.mark.asyncio
    async def test_mixed_text_and_tool_use(self):
        """Both text and tool_use blocks: content has text, tool_calls populated."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[
                {"type": "text", "text": "Let me search for that."},
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "lookup",
                    "input": {"id": 42},
                },
                {"type": "text", "text": " And here is more text."},
            ],
            usage={"input_tokens": 30, "output_tokens": 25},
        )
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete([{"role": "user", "content": "Look up item 42"}])

        # Text parts joined with newline
        assert result.content == "Let me search for that.\n And here is more text."
        assert result.has_tool_calls
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "lookup"
        assert result.tool_calls[0].arguments == {"id": 42}

    @pytest.mark.asyncio
    async def test_system_message_extraction(self):
        """System message is extracted from messages and passed as 'system' kwarg."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[{"type": "text", "text": "ok"}],
            usage={"input_tokens": 5, "output_tokens": 2},
        )
        mock_create = AsyncMock(return_value=mock_resp)
        provider.client.messages.create = mock_create

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]

        await provider.complete(messages)

        call_kwargs = mock_create.call_args[1]
        # System must be passed as separate kwarg
        assert call_kwargs["system"] == "You are a helpful assistant."
        # Messages must NOT contain the system message
        api_messages = call_kwargs["messages"]
        for msg in api_messages:
            assert msg["role"] != "system"
        assert len(api_messages) == 1
        assert api_messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_no_system_message(self):
        """When no system message is present, 'system' kwarg is not included."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[{"type": "text", "text": "ok"}],
            usage={"input_tokens": 5, "output_tokens": 2},
        )
        mock_create = AsyncMock(return_value=mock_resp)
        provider.client.messages.create = mock_create

        messages = [{"role": "user", "content": "Hello"}]
        await provider.complete(messages)

        call_kwargs = mock_create.call_args[1]
        assert "system" not in call_kwargs

    @pytest.mark.asyncio
    async def test_usage_mapping(self):
        """input_tokens/output_tokens mapped to prompt_tokens/completion_tokens/total_tokens."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[{"type": "text", "text": "ok"}],
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete([{"role": "user", "content": "test"}])

        assert result.usage is not None
        assert result.usage["prompt_tokens"] == 100
        assert result.usage["completion_tokens"] == 50
        assert result.usage["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_usage_none_when_absent(self):
        """If response.usage is None, LLMResponse.usage is None."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[{"type": "text", "text": "ok"}],
            usage=None,
        )
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete([{"role": "user", "content": "test"}])

        assert result.usage is None

    @pytest.mark.asyncio
    async def test_tool_use_non_dict_input_defaults_to_empty(self):
        """If tool_use block.input is not a dict, arguments default to {}."""
        provider = AnthropicProvider(model="claude-sonnet-4-20250514", api_key="test-key")
        mock_resp = _make_anthropic_response(
            content_blocks=[
                {
                    "type": "tool_use",
                    "id": "toolu_03",
                    "name": "broken",
                    "input": "not-a-dict",
                },
            ],
            usage={"input_tokens": 5, "output_tokens": 5},
        )
        provider.client.messages.create = AsyncMock(return_value=mock_resp)

        result = await provider.complete([{"role": "user", "content": "test"}])

        assert result.has_tool_calls
        assert result.tool_calls[0].arguments == {}
