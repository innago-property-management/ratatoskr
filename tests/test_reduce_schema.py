"""Tests for reduce_schema() orchestration — provider-generalized.

Tests the top-level pipeline: ToonLayer -> AIReductionLayer -> hard truncation fallback.
AIReductionLayer is tested in isolation in test_haiku_layer.py;
these tests focus on how the layers compose.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from api_agent.llm.types import LLMResponse
from api_agent.schema.reducer import ReductionResult, reduce_schema

from .conftest import FakeLLMProvider, make_text_response


@pytest.fixture(autouse=True)
def _bypass_keyword_ranking():
    """Bypass Layer 0 keyword ranking — these tests focus on TOON/AI layers."""
    with patch(
        "api_agent.schema.reducer.rank_and_truncate",
        side_effect=lambda text, q, t: text,
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A small schema that fits under any reasonable threshold
SMALL_SCHEMA = json.dumps([{"name": "Alice", "age": 30}])

# A large DSL schema (non-JSON, so TOON skips it)
LARGE_DSL_SCHEMA = ("## GET /users\nRetrieve users.\n\n" * 200).strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReduceSchemaOrchestration:
    """Tests for the reduce_schema() pipeline."""

    @pytest.mark.asyncio
    async def test_disabled_returns_original(self):
        """enabled=False bypasses all layers, returns original unchanged."""
        result = await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="anything",
            threshold=100,
            enabled=False,
        )

        assert isinstance(result, ReductionResult)
        assert result.schema_text == LARGE_DSL_SCHEMA
        assert result.was_toon_applied is False
        assert result.was_ai_applied is False
        assert result.original_chars == len(LARGE_DSL_SCHEMA)
        assert result.final_chars == len(LARGE_DSL_SCHEMA)

    @pytest.mark.asyncio
    async def test_empty_schema_returns_empty(self):
        """Empty string input returns empty string without crashing."""
        result = await reduce_schema(
            schema_text="",
            question="anything",
            threshold=1000,
        )

        assert result.schema_text == ""
        assert result.was_toon_applied is False
        assert result.was_ai_applied is False
        assert result.original_chars == 0
        assert result.final_chars == 0

    @pytest.mark.asyncio
    async def test_under_threshold_no_ai_call(self):
        """Schema already under threshold -> TOON may apply, but no AI call."""
        provider = FakeLLMProvider(responses=[make_text_response("should not be called")])
        result = await reduce_schema(
            schema_text=SMALL_SCHEMA,
            question="anything",
            threshold=10_000,
            provider=provider,
        )

        assert result.was_ai_applied is False
        assert result.final_chars <= 10_000

    @pytest.mark.asyncio
    async def test_toon_brings_under_threshold(self):
        """JSON schema where TOON compression alone brings it under threshold."""
        data = [
            {"name": f"user_{i}", "email": f"user{i}@example.com", "active": True}
            for i in range(100)
        ]
        large_json = json.dumps(data)

        provider = FakeLLMProvider(responses=[make_text_response("should not be called")])
        result = await reduce_schema(
            schema_text=large_json,
            question="list users",
            threshold=len(large_json),
            provider=provider,
        )

        assert result.was_toon_applied is True
        assert result.was_ai_applied is False
        assert result.final_chars < result.original_chars

    @pytest.mark.asyncio
    async def test_over_threshold_no_provider_truncates(self):
        """Over threshold with no provider -> TOON attempt + hard truncation."""
        threshold = 200
        result = await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="list users",
            threshold=threshold,
            provider=None,
        )

        assert result.was_ai_applied is False
        assert "[SCHEMA" in result.schema_text and "TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_over_threshold_ai_invoked(self):
        """Over threshold with provider -> AI reduction layer is invoked."""
        reduced = "## GET /users\nRetrieve users.\n" + "x" * 150

        provider = FakeLLMProvider(responses=[make_text_response(reduced)])
        result = await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="list users",
            threshold=500,
            provider=provider,
        )

        assert result.was_ai_applied is True
        assert result.schema_text == reduced

    @pytest.mark.asyncio
    async def test_ai_fails_falls_back_to_truncation(self):
        """AI raises -> falls back to hard truncation."""
        threshold = 200

        class ExplodingProvider(FakeLLMProvider):
            async def complete(self, *args, **kwargs):
                raise RuntimeError("API down")

        provider = ExplodingProvider(responses=[])
        result = await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="list users",
            threshold=threshold,
            provider=provider,
        )

        assert result.was_ai_applied is False
        assert "[SCHEMA" in result.schema_text and "TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_max_input_chars_skips_ai(self):
        """Schema exceeding max_input_chars skips AI even with provider."""
        provider = FakeLLMProvider(responses=[make_text_response("should not be called")])
        result = await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="list users",
            threshold=200,
            provider=provider,
            max_input_chars=10,
        )

        # AI should NOT have been called (no calls logged)
        assert len(provider.call_log) == 0
        assert result.was_ai_applied is False
        assert "[SCHEMA" in result.schema_text and "TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_max_output_tokens_threaded_to_ai(self):
        """max_output_tokens is passed through to AIReductionLayer."""
        reduced = "## GET /users\nRetrieve users.\n" + "x" * 150

        provider = FakeLLMProvider(responses=[make_text_response(reduced)])
        await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="list users",
            threshold=500,
            provider=provider,
            max_output_tokens=16384,
        )

        # The provider was called — verify it received the call
        assert len(provider.call_log) == 1


# ---------------------------------------------------------------------------
# AI reduction threshold tests
# ---------------------------------------------------------------------------


class TestAIReductionThreshold:
    """Tests for ai_reduction_threshold gating AI invocation."""

    @pytest.mark.asyncio
    async def test_schema_below_ai_threshold_skips_ai(self):
        """Original schema smaller than ai_threshold -> AI never called."""
        schema = "GET /users " + "x" * 500  # 511 chars
        threshold = 300
        ai_threshold = 1000  # original must be >= 1000

        provider = FakeLLMProvider(responses=[make_text_response("should not be called")])

        with patch("api_agent.schema.reducer.rank_and_truncate", return_value=schema):
            result = await reduce_schema(
                schema_text=schema,
                question="find users",
                threshold=threshold,
                provider=provider,
                ai_reduction_threshold=ai_threshold,
            )

        assert len(provider.call_log) == 0
        assert result.was_ai_applied is False
        assert "[SCHEMA TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_schema_above_ai_threshold_invokes_ai(self):
        """Original schema larger than ai_threshold -> AI called."""
        schema = "GET /users " + "x" * 500  # 511 chars
        reduced = "GET /users " + "y" * 150
        threshold = 300
        ai_threshold = 400  # original (511) > 400

        provider = FakeLLMProvider(responses=[make_text_response(reduced)])

        with patch("api_agent.schema.reducer.rank_and_truncate", return_value=schema):
            result = await reduce_schema(
                schema_text=schema,
                question="find users",
                threshold=threshold,
                provider=provider,
                ai_reduction_threshold=ai_threshold,
            )

        assert result.was_ai_applied is True
        assert result.schema_text == reduced

    @pytest.mark.asyncio
    async def test_zero_ai_threshold_uses_current_behavior(self):
        """ai_threshold=0 (default) -> AI fires when schema > threshold."""
        schema = "GET /users " + "x" * 500
        reduced = "GET /users " + "y" * 150
        threshold = 300

        provider = FakeLLMProvider(responses=[make_text_response(reduced)])

        with patch("api_agent.schema.reducer.rank_and_truncate", return_value=schema):
            result = await reduce_schema(
                schema_text=schema,
                question="find users",
                threshold=threshold,
                provider=provider,
                ai_reduction_threshold=0,
            )

        assert result.was_ai_applied is True

    @pytest.mark.asyncio
    async def test_ai_threshold_equal_to_schema_size_invokes_ai(self):
        """Boundary: original == ai_threshold -> AI fires (>=, not >)."""
        schema = "x" * 500
        reduced = "y" * 200
        threshold = 300
        ai_threshold = 500  # exactly equal

        provider = FakeLLMProvider(responses=[make_text_response(reduced)])

        with patch("api_agent.schema.reducer.rank_and_truncate", return_value=schema):
            result = await reduce_schema(
                schema_text=schema,
                question="find users",
                threshold=threshold,
                provider=provider,
                ai_reduction_threshold=ai_threshold,
            )

        assert result.was_ai_applied is True
