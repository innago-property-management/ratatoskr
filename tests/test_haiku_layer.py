"""Tests for HaikuLayer — Phase 3 of Smart Schema Reduction (TDD).

Tests mock anthropic.AsyncAnthropic at the instance level to avoid real API calls.
Pattern follows tests/test_llm/test_anthropic_complete.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api_agent.schema.reducer import HaikuLayer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_anthropic_message_response(text: str) -> MagicMock:
    """Build a mock Anthropic Messages API response with a single text block."""
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.stop_reason = "end_turn"
    return response


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


class TestHaikuLayer:
    """Tests for HaikuLayer.reduce()."""

    @pytest.mark.asyncio
    async def test_haiku_reduces_oversized_schema(self):
        """Mock Anthropic returns shorter schema text; HaikuLayer returns it."""
        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                return_value=_make_anthropic_message_response(SAMPLE_REDUCED_SCHEMA)
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is True
        assert result_text == SAMPLE_REDUCED_SCHEMA
        assert len(result_text) < len(SAMPLE_LARGE_SCHEMA)

    @pytest.mark.asyncio
    async def test_haiku_timeout_degrades_gracefully(self):
        """Mock raises timeout; HaikuLayer returns original schema."""
        import httpx

        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                side_effect=httpx.TimeoutException("Request timed out")
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_haiku_auth_error_degrades_gracefully(self):
        """Mock raises AuthenticationError; HaikuLayer returns original schema."""
        import anthropic as real_anthropic

        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            # Create a real AuthenticationError
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.headers = {}
            auth_error = real_anthropic.AuthenticationError(
                message="Invalid API key",
                response=mock_response,
                body=None,
            )
            mock_client.messages.create = AsyncMock(side_effect=auth_error)
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client
            # Expose AuthenticationError on the mock module so isinstance checks work
            mock_anthropic_mod.AuthenticationError = real_anthropic.AuthenticationError

            layer = HaikuLayer(api_key="bad-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_haiku_empty_response_degrades(self):
        """Mock returns empty string; HaikuLayer returns original (sanity: len < 100)."""
        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                return_value=_make_anthropic_message_response("")
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_haiku_strips_markdown_fences(self):
        """Mock returns ```json\\n...\\n```; fences are stripped."""
        fenced_response = "```json\n" + SAMPLE_REDUCED_SCHEMA + "\n```"

        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                return_value=_make_anthropic_message_response(fenced_response)
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is True
        # Fences should be stripped
        assert "```" not in result_text
        assert result_text.strip() == SAMPLE_REDUCED_SCHEMA.strip()

    @pytest.mark.asyncio
    async def test_haiku_output_longer_than_input_discarded(self):
        """Returns text longer than input; output is discarded, returns original."""
        # Create output that is longer than input
        bloated_output = SAMPLE_LARGE_SCHEMA + "\n## EXTRA endpoint\n" + "y" * 500

        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                return_value=_make_anthropic_message_response(bloated_output)
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_haiku_prompt_includes_question(self):
        """Verify the user's question appears in the API call messages."""
        question = "How do I list all users with pagination?"

        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_create = AsyncMock(
                return_value=_make_anthropic_message_response(SAMPLE_REDUCED_SCHEMA)
            )
            mock_client.messages.create = mock_create
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            await layer.reduce(SAMPLE_LARGE_SCHEMA, question)

        # Verify create was called
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        # The question must appear in the messages
        messages = call_kwargs["messages"]
        all_content = " ".join(
            msg["content"] for msg in messages if isinstance(msg.get("content"), str)
        )
        assert question in all_content

    @pytest.mark.asyncio
    async def test_haiku_suspiciously_short_response_discarded(self):
        """Response shorter than 100 chars is discarded as suspicious."""
        short_response = "## GET /users\nGet users."  # < 100 chars

        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                return_value=_make_anthropic_message_response(short_response)
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_haiku_prompt_includes_untrusted_framing(self):
        """Verify the untrusted framing markers wrap the schema text."""
        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_create = AsyncMock(
                return_value=_make_anthropic_message_response(SAMPLE_REDUCED_SCHEMA)
            )
            mock_client.messages.create = mock_create
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            await layer.reduce(SAMPLE_LARGE_SCHEMA, "How do I get users?")

        call_kwargs = mock_create.call_args[1]
        messages = call_kwargs["messages"]
        all_content = " ".join(
            msg["content"] for msg in messages if isinstance(msg.get("content"), str)
        )
        assert "[BEGIN UNTRUSTED SCHEMA" in all_content
        assert "[END UNTRUSTED SCHEMA]" in all_content

    @pytest.mark.asyncio
    async def test_haiku_generic_exception_degrades_gracefully(self):
        """Any unexpected exception returns original schema gracefully."""
        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                side_effect=RuntimeError("Something unexpected")
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "How do I get users?"
            )

        assert was_applied is False
        assert result_text == SAMPLE_LARGE_SCHEMA

    @pytest.mark.asyncio
    async def test_haiku_question_with_curly_braces(self):
        """Questions containing {curly braces} must not crash prompt building."""
        with patch("api_agent.schema.reducer.anthropic") as mock_anthropic_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                return_value=_make_anthropic_message_response(SAMPLE_REDUCED_SCHEMA)
            )
            mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

            layer = HaikuLayer(api_key="test-key")
            result_text, was_applied = await layer.reduce(
                SAMPLE_LARGE_SCHEMA, "Get /users/{id} endpoint"
            )

        assert was_applied is True
        assert result_text == SAMPLE_REDUCED_SCHEMA
