"""Tests for reduce_schema() orchestration — Phase 4 of Smart Schema Reduction (TDD).

Tests the top-level pipeline: ToonLayer -> HaikuLayer -> hard truncation fallback.
ToonLayer and HaikuLayer are tested in isolation in test_schema_reducer.py and
test_haiku_layer.py respectively; these tests focus on how the layers compose.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api_agent.schema.reducer import ReductionResult, _get_haiku_layer, reduce_schema


@pytest.fixture(autouse=True)
def _bypass_keyword_ranking():
    """Bypass Layer 0 keyword ranking — these tests focus on TOON/Haiku layers."""
    with patch(
        "api_agent.schema.reducer.rank_and_truncate",
        side_effect=lambda text, q, t: text,
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_haiku_layer_cache():
    """Clear the HaikuLayer lru_cache between tests to avoid cross-test leakage."""
    _get_haiku_layer.cache_clear()
    yield
    _get_haiku_layer.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A small schema that fits under any reasonable threshold
SMALL_SCHEMA = json.dumps([{"name": "Alice", "age": 30}])

# A large DSL schema (non-JSON, so TOON skips it)
LARGE_DSL_SCHEMA = ("## GET /users\nRetrieve users.\n\n" * 200).strip()


def _make_anthropic_response(text: str) -> MagicMock:
    """Build a mock Anthropic Messages API response."""
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    return response


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
        """Schema already under threshold -> TOON may apply, but no Haiku call."""
        result = await reduce_schema(
            schema_text=SMALL_SCHEMA,
            question="anything",
            threshold=10_000,
            api_key="test-key",
        )

        assert result.was_ai_applied is False
        assert result.final_chars <= 10_000

    @pytest.mark.asyncio
    async def test_toon_brings_under_threshold(self):
        """JSON schema where TOON compression alone brings it under threshold."""
        # Build a large homogeneous JSON array — TOON excels at these
        data = [
            {"name": f"user_{i}", "email": f"user{i}@example.com", "active": True}
            for i in range(100)
        ]
        large_json = json.dumps(data)

        # Set threshold between TOON-encoded size and original size
        # TOON should compress this significantly
        result = await reduce_schema(
            schema_text=large_json,
            question="list users",
            threshold=len(large_json),  # Original size as threshold — TOON output should be smaller
            api_key="test-key",
        )

        assert result.was_toon_applied is True
        assert result.was_ai_applied is False
        assert result.final_chars < result.original_chars

    @pytest.mark.asyncio
    async def test_over_threshold_no_api_key_truncates(self):
        """Over threshold with no API key -> TOON attempt + hard truncation."""
        threshold = 200
        result = await reduce_schema(
            schema_text=LARGE_DSL_SCHEMA,
            question="list users",
            threshold=threshold,
            api_key="",  # No API key
        )

        assert result.was_ai_applied is False
        # Final text should be truncated with marker
        assert "[SCHEMA" in result.schema_text and "TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_over_threshold_haiku_invoked(self):
        """Over threshold with API key -> Haiku layer is invoked."""
        # A reduced schema that Haiku "returns"
        reduced = "## GET /users\nRetrieve users.\n" + "x" * 150  # > _MIN_OUTPUT_CHARS

        with patch("api_agent.schema.reducer.anthropic") as mock_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(return_value=_make_anthropic_response(reduced))
            mock_mod.AsyncAnthropic.return_value = mock_client

            result = await reduce_schema(
                schema_text=LARGE_DSL_SCHEMA,
                question="list users",
                threshold=500,
                api_key="test-key",
            )

        assert result.was_ai_applied is True
        assert result.schema_text == reduced

    @pytest.mark.asyncio
    async def test_haiku_fails_falls_back_to_truncation(self):
        """Haiku raises -> falls back to hard truncation."""
        threshold = 200

        with patch("api_agent.schema.reducer.anthropic") as mock_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))
            mock_mod.AsyncAnthropic.return_value = mock_client

            result = await reduce_schema(
                schema_text=LARGE_DSL_SCHEMA,
                question="list users",
                threshold=threshold,
                api_key="test-key",
            )

        assert result.was_ai_applied is False
        assert "[SCHEMA" in result.schema_text and "TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_max_input_chars_skips_haiku(self):
        """Schema exceeding max_input_chars skips Haiku even with API key."""
        threshold = 200

        with patch("api_agent.schema.reducer.anthropic") as mock_mod:
            mock_client = MagicMock()
            mock_create = AsyncMock()
            mock_client.messages.create = mock_create
            mock_mod.AsyncAnthropic.return_value = mock_client

            result = await reduce_schema(
                schema_text=LARGE_DSL_SCHEMA,
                question="list users",
                threshold=threshold,
                api_key="test-key",
                max_input_chars=10,  # Very low — schema exceeds this
            )

        # Haiku should NOT have been called
        mock_create.assert_not_called()
        assert result.was_ai_applied is False
        assert "[SCHEMA" in result.schema_text and "TRUNCATED" in result.schema_text

    @pytest.mark.asyncio
    async def test_api_key_resolved_from_env(self, monkeypatch: pytest.MonkeyPatch):
        """When no explicit api_key, falls back to ANTHROPIC_API_KEY env var."""
        reduced = "## GET /users\nRetrieve users.\n" + "x" * 150

        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")

        with patch("api_agent.schema.reducer.anthropic") as mock_mod:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(return_value=_make_anthropic_response(reduced))
            mock_mod.AsyncAnthropic.return_value = mock_client

            result = await reduce_schema(
                schema_text=LARGE_DSL_SCHEMA,
                question="list users",
                threshold=500,
                api_key="",  # Empty — should resolve from env
            )

        # Haiku should have been called (env key resolved)
        assert result.was_ai_applied is True
        # Verify the client was created with the env key
        mock_mod.AsyncAnthropic.assert_called_once()
        call_kwargs = mock_mod.AsyncAnthropic.call_args
        assert call_kwargs[1]["api_key"] == "env-key"

    def test_get_haiku_layer_caches_instances(self):
        """_get_haiku_layer returns the same instance for identical args."""
        _get_haiku_layer.cache_clear()
        with patch("api_agent.schema.reducer.anthropic"):
            layer1 = _get_haiku_layer("key1", "model", 30000, 8192)
            layer2 = _get_haiku_layer("key1", "model", 30000, 8192)
            layer3 = _get_haiku_layer("key2", "model", 30000, 8192)

        assert layer1 is layer2  # Same args → cached
        assert layer1 is not layer3  # Different key → new instance
        _get_haiku_layer.cache_clear()

    @pytest.mark.asyncio
    async def test_max_output_tokens_threaded_to_haiku(self):
        """max_output_tokens is passed through to HaikuLayer."""
        reduced = "## GET /users\nRetrieve users.\n" + "x" * 150

        _get_haiku_layer.cache_clear()
        with patch("api_agent.schema.reducer.anthropic") as mock_mod:
            mock_client = MagicMock()
            mock_create = AsyncMock(return_value=_make_anthropic_response(reduced))
            mock_client.messages.create = mock_create
            mock_mod.AsyncAnthropic.return_value = mock_client

            await reduce_schema(
                schema_text=LARGE_DSL_SCHEMA,
                question="list users",
                threshold=500,
                api_key="test-key",
                max_output_tokens=16384,
            )

        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["max_tokens"] == 16384
        _get_haiku_layer.cache_clear()
