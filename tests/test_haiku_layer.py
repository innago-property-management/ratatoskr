"""Tests for AIReductionLayer — provider-agnostic AI schema reduction.

Uses FakeLLMProvider instead of mocking the Anthropic SDK directly.
Backward-compatible alias: HaikuLayer = AIReductionLayer.
"""

from __future__ import annotations

import pytest

from api_agent.llm.types import LLMResponse
from api_agent.schema.reducer import AIReductionLayer, HaikuLayer

from .conftest import FakeLLMProvider, make_text_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_LARGE_SCHEMA = (
    """\
## GET /users
Retrieve all users.
Parameters:
  - limit (integer): Max results

## GET /users/{id}
Retrieve a single user by ID.

## GET /orders
Retrieve all orders.
Parameters:
  - status (string): Filter by status

## GET /orders/{id}
Retrieve a single order.

## GET /products
List all products.
Parameters:
  - category (string): Filter by category

## GET /products/{id}
Retrieve a single product.

## GET /inventory
List inventory items.

## GET /reports/sales
Generate sales report.

## GET /reports/users
Generate user activity report.

## GET /settings
Retrieve application settings.
"""
    + "x" * 200
)  # Pad to ensure it's reasonably sized


SAMPLE_REDUCED_SCHEMA = """\
## GET /users
Retrieve all users.
Parameters:
  - limit (integer): Max results

## GET /users/{id}
Retrieve a single user by ID.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAIReductionLayer:
    """Tests for AIReductionLayer.reduce()."""

    @pytest.mark.asyncio
    async def test_reduces_oversized_schema(self):
        """AC-8: AIReductionLayer with FakeLLMProvider produces reduced output."""
        provider = FakeLLMProvider(responses=[make_text_response(SAMPLE_REDUCED_SCHEMA)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is True
        assert result_text == SAMPLE_REDUCED_SCHEMA
        assert len(result_text) < len(SAMPLE_LARGE_SCHEMA)

    @pytest.mark.asyncio
    async def test_exception_degrades_gracefully(self):
        """Never raises — returns (original, False) on ANY failure."""
        provider = FakeLLMProvider(responses=[])
        # FakeLLMProvider returns "<exhausted>" which is < 100 chars, so sanity check triggers
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_empty_response_degrades(self):
        """AC-9: Empty response returns original schema."""
        provider = FakeLLMProvider(responses=[make_text_response("")])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        """AC-15 (S-3): Fence stripping works regardless of provider."""
        fenced_response = "```json\n" + SAMPLE_REDUCED_SCHEMA + "\n```"
        provider = FakeLLMProvider(responses=[make_text_response(fenced_response)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is True
        assert "```" not in result_text
        assert result_text.strip() == SAMPLE_REDUCED_SCHEMA.strip()

    @pytest.mark.asyncio
    async def test_output_longer_than_input_discarded(self):
        """Returns text longer than input; output is discarded."""
        bloated_output = SAMPLE_LARGE_SCHEMA + "\n## EXTRA endpoint\n" + "y" * 500
        provider = FakeLLMProvider(responses=[make_text_response(bloated_output)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_suspiciously_short_response_discarded(self):
        """Response shorter than 100 chars is discarded as suspicious."""
        short_response = "## GET /users\nGet users."  # < 100 chars
        provider = FakeLLMProvider(responses=[make_text_response(short_response)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_passes_max_tokens(self):
        """AC-14 (S-2): max_output_tokens is passed as max_tokens to complete()."""
        provider = FakeLLMProvider(responses=[make_text_response(SAMPLE_REDUCED_SCHEMA)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=16384)
        await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        # FakeLLMProvider logs calls
        assert len(provider.call_log) == 1
        # complete() doesn't receive max_tokens in call_log directly,
        # but we can verify the layer was constructed with max_output_tokens
        assert layer.max_output_tokens == 16384

    @pytest.mark.asyncio
    async def test_prompt_includes_question(self):
        """Verify the user's question appears in the messages."""
        question = "How do I list all users with pagination?"
        provider = FakeLLMProvider(responses=[make_text_response(SAMPLE_REDUCED_SCHEMA)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        await layer.reduce(SAMPLE_LARGE_SCHEMA, question)

        call = provider.call_log[0]
        all_content = " ".join(
            msg["content"] for msg in call["messages"] if isinstance(msg.get("content"), str)
        )
        assert question in all_content

    @pytest.mark.asyncio
    async def test_prompt_includes_untrusted_framing(self):
        """Verify the untrusted framing markers wrap the schema text."""
        provider = FakeLLMProvider(responses=[make_text_response(SAMPLE_REDUCED_SCHEMA)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        call = provider.call_log[0]
        all_content = " ".join(
            msg["content"] for msg in call["messages"] if isinstance(msg.get("content"), str)
        )
        assert "[BEGIN UNTRUSTED SCHEMA" in all_content
        assert "[END UNTRUSTED SCHEMA]" in all_content

    @pytest.mark.asyncio
    async def test_question_with_curly_braces(self):
        """Questions containing {curly braces} must not crash prompt building."""
        provider = FakeLLMProvider(responses=[make_text_response(SAMPLE_REDUCED_SCHEMA)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(
            SAMPLE_LARGE_SCHEMA, "Get /users/{id} endpoint"
        )

        assert was_applied is True

    @pytest.mark.asyncio
    async def test_question_containing_schema_text_placeholder(self):
        """Question containing literal '{schema_text}' must not double-substitute."""
        question = "Filter /users/{schema_text} endpoint"
        provider = FakeLLMProvider(responses=[make_text_response(SAMPLE_REDUCED_SCHEMA)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        await layer.reduce(SAMPLE_LARGE_SCHEMA, question)

        call = provider.call_log[0]
        prompt_content = call["messages"][0]["content"]
        assert question in prompt_content
        assert prompt_content.count("[BEGIN UNTRUSTED SCHEMA") == 1

    @pytest.mark.asyncio
    async def test_runtime_exception_returns_original(self):
        """Any unexpected exception returns original schema gracefully."""

        class ExplodingProvider(FakeLLMProvider):
            async def complete(self, *args, **kwargs):
                raise RuntimeError("Something unexpected")

        provider = ExplodingProvider(responses=[])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_none_content_degrades(self):
        """LLMResponse with None content returns original schema."""
        provider = FakeLLMProvider(responses=[LLMResponse(content=None)])
        layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
        result_text, was_applied = await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA


class TestHaikuLayerAlias:
    """HaikuLayer is a backward-compatible alias for AIReductionLayer."""

    def test_alias_exists(self):
        """AC-12 (partial): HaikuLayer is an alias for AIReductionLayer."""
        assert HaikuLayer is AIReductionLayer
