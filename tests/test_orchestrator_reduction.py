"""Tests for centralized schema reduction in the orchestrator.

Verifies that run_agent_orchestration calls reduce_schema() with the correct
arguments, applies schema_pre_hook before reduction, and passes the reduced
schema (not the original) to the LLM.
"""

from __future__ import annotations

from contextvars import ContextVar
from unittest.mock import AsyncMock

import pytest

from api_agent.agent.orchestrator import (
    AgentContextVars,
    ProtocolConfig,
    run_agent_orchestration,
)
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
    defaults = dict(
        agent_type="test",
        log_prefix="[TEST]",
        call_key="test_calls",
        ctx_vars=_make_ctx_vars(),
        unreduced_schema_text=SAMPLE_SCHEMA,
        raw_schema=SAMPLE_SCHEMA,
        provider=provider,
        tools=[],
        instructions="You are a test agent.",
        api_id="test-api",
    )
    defaults.update(overrides)
    return ProtocolConfig(**defaults)


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
        """run_agent_orchestration calls reduce_schema() with threshold, question, and schema text."""
        provider = FakeLLMProvider([make_text_response("Done.")])

        mock_reduce = AsyncMock(return_value=_make_reduction_result(SAMPLE_SCHEMA))

        config = _make_config(provider)

        monkeypatch.setattr("api_agent.agent.orchestrator.reduce_schema", mock_reduce)
        # Disable recipes to simplify the test path
        monkeypatch.setattr("api_agent.agent.orchestrator.settings.ENABLE_RECIPES", False)

        question = "What fields does hello have?"
        await run_agent_orchestration(question, config)

        mock_reduce.assert_awaited_once()
        call_kwargs = mock_reduce.call_args
        # Positional or keyword — verify the schema text and question reach reduce_schema
        assert SAMPLE_SCHEMA in str(call_kwargs)
        assert question in str(call_kwargs)

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
        # The original unreduced schema should NOT appear (unless it happens to
        # be a substring of the reduced text, which it isn't in this test)
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
        monkeypatch.setattr(
            "api_agent.agent.orchestrator.settings.SCHEMA_REDUCTION_ENABLED", False
        )

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
        # The raw schema text should pass through without pre-hook transformation
        call_kwargs = mock_reduce.call_args
        assert SAMPLE_SCHEMA in str(call_kwargs)
