"""Tests for centralized schema reduction in the orchestrator.

Verifies that run_agent_orchestration calls reduce_schema() with the correct
arguments, applies schema_pre_hook before reduction, and passes the reduced
schema (not the original) to the LLM.
"""

from __future__ import annotations

from contextvars import ContextVar
from unittest.mock import AsyncMock

import pytest

from api_agent.agent.graphql_agent import _strip_descriptions
from api_agent.agent.orchestrator import (
    AgentContextVars,
    ProtocolConfig,
    run_agent_orchestration,
    set_agent_semaphore,
)
from api_agent.config import settings
from api_agent.schema.reducer import ReductionResult
from tests.conftest import FakeLLMProvider, make_text_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_SCHEMA = "type Query { hello: String }"


def _make_ctx_vars() -> AgentContextVars:
    """Create a fresh AgentContextVars bundle for each test."""
    return AgentContextVars(
        api_calls=ContextVar("test_api_calls"),
        recipe_steps=ContextVar("test_recipe_steps"),
        query_results=ContextVar("test_query_results"),
        last_result=ContextVar("test_last_result"),
        raw_schema=ContextVar("test_raw_schema"),
        sql_steps=ContextVar("test_sql_steps"),
    )


def _make_config(provider, **overrides) -> ProtocolConfig:
    """Build a minimal ProtocolConfig with sensible defaults."""
    return ProtocolConfig(
        agent_type=overrides.get("agent_type", "test"),
        log_prefix=overrides.get("log_prefix", "[TEST]"),
        call_key=overrides.get("call_key", "test_calls"),
        ctx_vars=overrides.get("ctx_vars", _make_ctx_vars()),
        unreduced_schema_text=overrides.get("unreduced_schema_text", SAMPLE_SCHEMA),
        raw_schema=overrides.get("raw_schema", SAMPLE_SCHEMA),
        provider=overrides.get("provider", provider),
        tools=overrides.get("tools", []),
        instructions=overrides.get("instructions", "You are a test agent."),
        api_id=overrides.get("api_id", "test-api"),
        schema_pre_hook=overrides.get("schema_pre_hook"),
    )


def _make_reduction_result(schema_text: str = "REDUCED_SCHEMA") -> ReductionResult:
    """Build a canned ReductionResult."""
    return ReductionResult(
        schema_text=schema_text,
        was_toon_applied=False,
        was_ai_applied=False,
        original_chars=len(SAMPLE_SCHEMA),
        final_chars=len(schema_text),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOrchestratorReduction:
    """Centralized reduce_schema() integration in the orchestrator."""

    @pytest.mark.asyncio
    async def test_orchestrator_calls_reduce_schema(self, monkeypatch):
        """run_agent_orchestration calls reduce_schema() with correct wiring."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        mock_reduce = AsyncMock(return_value=_make_reduction_result(SAMPLE_SCHEMA))

        config = _make_config(provider)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", mock_reduce)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

        question = "What fields does hello have?"
        await run_agent_orchestration(question, config)

        mock_reduce.assert_awaited_once()
        _, kwargs = mock_reduce.call_args

        # Assert concrete wiring into reduce_schema
        assert kwargs["schema_text"] == SAMPLE_SCHEMA
        assert kwargs["question"] == question
        assert kwargs["threshold"] == settings.MAX_SCHEMA_CHARS
        assert kwargs["enabled"] == settings.SCHEMA_REDUCTION_ENABLED

    @pytest.mark.asyncio
    async def test_orchestrator_applies_pre_hook_before_reduction(self, monkeypatch):
        """schema_pre_hook transforms the schema BEFORE it reaches reduce_schema."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        # Pre-hook that uppercases the schema (easy to detect)
        def upper_hook(text: str) -> str:
            return text.upper()

        received_schema: list[str] = []

        async def capturing_reduce(schema_text, **kwargs):
            received_schema.append(schema_text)
            return _make_reduction_result(schema_text)

        config = _make_config(provider, schema_pre_hook=upper_hook)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", capturing_reduce)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

        await run_agent_orchestration("test question", config)

        assert len(received_schema) == 1
        # The hook should have uppercased the schema before passing to reduce_schema
        assert received_schema[0] == SAMPLE_SCHEMA.upper()

    @pytest.mark.asyncio
    async def test_orchestrator_no_pre_hook_passes_raw(self, monkeypatch):
        """Without a schema_pre_hook, unreduced_schema_text passes through unchanged."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        received_schema: list[str] = []

        async def capturing_reduce(schema_text, **kwargs):
            received_schema.append(schema_text)
            return _make_reduction_result(schema_text)

        config = _make_config(provider, schema_pre_hook=None)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", capturing_reduce)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

        await run_agent_orchestration("test question", config)

        assert len(received_schema) == 1
        assert received_schema[0] == SAMPLE_SCHEMA

    @pytest.mark.asyncio
    async def test_orchestrator_uses_reduced_schema_in_prompt(self, monkeypatch):
        """The augmented query sent to the LLM contains the REDUCED schema, not the original."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        reduced_text = "REDUCED_SCHEMA_FOR_PROMPT"
        mock_reduce = AsyncMock(return_value=_make_reduction_result(reduced_text))

        config = _make_config(provider)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", mock_reduce)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

        await run_agent_orchestration("What is hello?", config)

        # The provider's call_log captures the messages sent to the LLM
        assert len(provider.call_log) >= 1
        # The user message in the first call should contain the reduced schema
        first_call_messages = provider.call_log[0]["messages"]
        user_messages = [m for m in first_call_messages if m.get("role") == "user"]
        assert len(user_messages) >= 1
        user_content = user_messages[0]["content"]
        assert reduced_text in user_content
        # The original unreduced schema should NOT appear
        assert SAMPLE_SCHEMA not in user_content

    @pytest.mark.asyncio
    async def test_orchestrator_reduction_disabled(self, monkeypatch):
        """When SCHEMA_REDUCTION_ENABLED=False, reduce_schema returns the original unchanged."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        # reduce_schema with enabled=False returns original text unchanged
        async def passthrough_reduce(schema_text, **kwargs):
            return ReductionResult(
                schema_text=schema_text,
                was_toon_applied=False,
                was_ai_applied=False,
                original_chars=len(schema_text),
                final_chars=len(schema_text),
            )

        config = _make_config(provider)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", passthrough_reduce)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.SCHEMA_REDUCTION_ENABLED", False)

        result = await run_agent_orchestration("What is hello?", config)

        # End-to-end success — the orchestrator doesn't break when reduction is disabled
        assert result.result_dict["ok"] is True

        # The original schema text should appear in the LLM prompt (no reduction applied)
        first_call_messages = provider.call_log[0]["messages"]
        user_messages = [m for m in first_call_messages if m.get("role") == "user"]
        assert any(SAMPLE_SCHEMA in m["content"] for m in user_messages)

    @pytest.mark.asyncio
    async def test_mcp_agent_unaffected_by_reduction(self, monkeypatch):
        """MCP agent (no pre_hook) goes through reduce_schema without pre-processing."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        mock_reduce = AsyncMock(return_value=_make_reduction_result(SAMPLE_SCHEMA))

        config = _make_config(
            provider,
            agent_type="mcp",
            log_prefix="[MCP]",
            schema_pre_hook=None,
        )

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", mock_reduce)
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

        await run_agent_orchestration("List available tools", config)

        # reduce_schema is still called (centralized) even for MCP
        mock_reduce.assert_awaited_once()
        _, kwargs = mock_reduce.call_args
        assert kwargs["schema_text"] == SAMPLE_SCHEMA

    @pytest.mark.asyncio
    async def test_orchestrator_reduction_with_recipes_enabled(self, monkeypatch):
        """Reduction happens exactly once even when recipes are enabled."""
        reduced_text = "REDUCED_WITH_RECIPES"
        provider = FakeLLMProvider([make_text_response("Done.")])
        mock_reduce = AsyncMock(return_value=_make_reduction_result(reduced_text))

        config = _make_config(provider)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", mock_reduce)
        # Leave ENABLE_RECIPES at default (True)

        question = "What fields does hello have?"
        await run_agent_orchestration(question, config)

        # Reduction should happen exactly once regardless of recipe path
        mock_reduce.assert_awaited_once()

        # The LLM prompt should contain the reduced schema
        assert len(provider.call_log) >= 1
        first_call_messages = provider.call_log[0]["messages"]
        user_messages = [m for m in first_call_messages if m.get("role") == "user"]
        assert any(reduced_text in m["content"] for m in user_messages)


class TestStripDescriptionsPreHook:
    """GraphQL _strip_descriptions conditional behavior (used as schema_pre_hook)."""

    def test_no_op_below_threshold(self):
        """Small schemas pass through unchanged — comments preserved."""
        small = "type Query { hello: String # a comment }"
        assert _strip_descriptions(small) == small

    def test_strips_above_threshold(self, monkeypatch):
        """Oversized schemas get # comments stripped."""
        monkeypatch.setattr(settings, "MAX_SCHEMA_CHARS", 10)
        large = "type Query { hello: String # inline comment }"
        result = _strip_descriptions(large)
        assert " #" not in result
        assert "type Query" in result


class TestAgentConcurrencyLimiter:
    """Agent-level semaphore limits concurrent orchestrations."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, monkeypatch):
        """Only MAX_CONCURRENT_AGENTS orchestrations run simultaneously."""
        import asyncio

        set_agent_semaphore(2)  # Allow only 2 concurrent
        try:
            barrier = asyncio.Event()
            running_count: list[int] = [0]
            max_observed: list[int] = [0]

            async def slow_reduce(schema_text, **kwargs):
                running_count[0] += 1
                max_observed[0] = max(max_observed[0], running_count[0])
                await barrier.wait()
                running_count[0] -= 1
                return _make_reduction_result(schema_text)

            provider = FakeLLMProvider([make_text_response("Done.")] * 4)
            monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", slow_reduce)
            monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

            configs = [_make_config(provider) for _ in range(4)]
            tasks = [asyncio.create_task(run_agent_orchestration("q", c)) for c in configs]

            # Let the event loop schedule all tasks
            await asyncio.sleep(0.05)

            # Release the barrier so they can complete
            barrier.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # No task should have raised an exception
            assert all(not isinstance(r, BaseException) for r in results)
            # At most 2 should have been running at once
            assert max_observed[0] <= 2
        finally:
            set_agent_semaphore(10)

    @pytest.mark.asyncio
    async def test_semaphore_releases_on_error(self, monkeypatch):
        """A failing orchestration releases its semaphore slot."""
        set_agent_semaphore(1)
        try:

            async def failing_reduce(schema_text, **kwargs):
                raise RuntimeError("boom")

            provider = FakeLLMProvider([make_text_response("Done.")])
            monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", failing_reduce)
            monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

            config = _make_config(provider)

            # First call fails
            result = await run_agent_orchestration("q", config)
            assert result.result_dict["ok"] is False

            # Second call should still work (slot was released)
            async def ok_reduce(schema_text, **kwargs):
                return _make_reduction_result(schema_text)

            provider2 = FakeLLMProvider([make_text_response("Done.")])
            monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", ok_reduce)
            config2 = _make_config(provider2)
            result2 = await run_agent_orchestration("q", config2)
            assert result2.result_dict["ok"] is True
        finally:
            set_agent_semaphore(10)
