"""Tests for api_agent.exceptions hierarchy."""

import pytest

from api_agent.context import MissingHeaderError
from api_agent.exceptions import APIAgentError, ProviderError, RecipeError, SchemaError
from api_agent.llm.provider import MaxTurnsExceeded, RunResult


class TestExceptionHierarchy:
    """All domain exceptions inherit from APIAgentError."""

    def test_missing_header_is_api_agent_error(self):
        assert issubclass(MissingHeaderError, APIAgentError)

    def test_max_turns_exceeded_is_api_agent_error(self):
        assert issubclass(MaxTurnsExceeded, APIAgentError)

    def test_schema_error_is_api_agent_error(self):
        assert issubclass(SchemaError, APIAgentError)

    def test_provider_error_is_api_agent_error(self):
        assert issubclass(ProviderError, APIAgentError)

    def test_recipe_error_is_api_agent_error(self):
        assert issubclass(RecipeError, APIAgentError)

    def test_all_still_catchable_as_exception(self):
        """Backward compat: all are still Exception subclasses."""
        for exc_cls in [
            MissingHeaderError,
            MaxTurnsExceeded,
            SchemaError,
            ProviderError,
            RecipeError,
        ]:
            assert issubclass(exc_cls, Exception)

    def test_catch_api_agent_error_catches_missing_header(self):
        with pytest.raises(APIAgentError):
            raise MissingHeaderError("test")

    def test_catch_api_agent_error_catches_max_turns(self):
        dummy = RunResult(final_output=None, tool_results=[], turns_used=5)
        with pytest.raises(APIAgentError):
            raise MaxTurnsExceeded(5, dummy)
