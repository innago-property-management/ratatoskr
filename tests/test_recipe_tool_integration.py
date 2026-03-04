"""Integration tests for recipe tool registration with agent."""

from contextvars import ContextVar
from unittest.mock import patch

import pytest

from api_agent.agent.orchestrator import AgentContextVars, create_recipe_tools

# Shared test fixtures


@pytest.fixture
def ctx_vars():
    """Create a test AgentContextVars bundle."""
    return AgentContextVars(
        api_calls=ContextVar("test_api_calls"),
        recipe_steps=ContextVar("test_recipe_steps"),
        query_results=ContextVar("test_query_results"),
        last_result=ContextVar("test_last_result"),
        raw_schema=ContextVar("test_raw_schema"),
        sql_steps=ContextVar("test_sql_steps"),
    )


def _dummy_step_executor_factory(recipe_id):
    """Dummy executor — tests don't exercise recipe execution."""

    async def executor(step_idx, step, params, results):
        return False, None, '{"success": false, "error": "dummy"}', None

    return executor


@pytest.fixture
def sample_recipe_suggestions():
    """Sample recipe suggestions with parameters."""
    return [
        {
            "recipe_id": "r_test123",
            "question": "List managers starting with B",
            "tool_name": "list_managers_starting_with_b",
            "params": {"startsWith": {"type": "str", "default": "B"}},
            "steps": [{"kind": "graphql", "query_template": "{ users { name } }"}],
            "sql_steps": [],
        }
    ]


def test_graphql_create_tools_basic(ctx_vars, sample_recipe_suggestions):
    """Test that recipe tools are created successfully."""

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": sample_recipe_suggestions[0]["params"],
            "steps": sample_recipe_suggestions[0]["steps"],
            "sql_steps": sample_recipe_suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(
            ctx_vars, sample_recipe_suggestions, "graphql", _dummy_step_executor_factory
        )

        assert len(tools) == 1
        tool = tools[0]

        assert tool.name == "list_managers_starting_with_b"

        from api_agent.llm.types import ToolDefinition

        assert isinstance(tool, ToolDefinition)


def test_create_multiple_recipe_tools(ctx_vars):
    """Test creating multiple recipe tools with different params."""

    suggestions = [
        {
            "recipe_id": "r_recipe1",
            "question": "Get users by role",
            "tool_name": "get_users_by_role",
            "params": {"role": {"type": "str", "default": "admin"}},
            "steps": [{"kind": "graphql"}],
            "sql_steps": [],
        },
        {
            "recipe_id": "r_recipe2",
            "question": "List teams with limit",
            "tool_name": "list_teams_with_limit",
            "params": {"limit": {"type": "int", "default": 10}},
            "steps": [{"kind": "graphql"}],
            "sql_steps": [],
        },
    ]

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {
                        "params": s["params"],
                        "steps": s["steps"],
                        "sql_steps": s["sql_steps"],
                    }
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = create_recipe_tools(ctx_vars, suggestions, "graphql", _dummy_step_executor_factory)

        assert len(tools) == 2

        tool_names = [t.name for t in tools]
        assert "get_users_by_role" in tool_names
        assert "list_teams_with_limit" in tool_names
        assert len(set(tool_names)) == 2  # All unique


def test_recipe_tool_has_correct_signature(ctx_vars, sample_recipe_suggestions):
    """Test that recipe tool has correct parameter signature."""

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": sample_recipe_suggestions[0]["params"],
            "steps": sample_recipe_suggestions[0]["steps"],
            "sql_steps": sample_recipe_suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(
            ctx_vars, sample_recipe_suggestions, "graphql", _dummy_step_executor_factory
        )
        tool = tools[0]

        assert isinstance(tool.parameters, dict)
        assert "properties" in tool.parameters

        assert tool.name == "list_managers_starting_with_b"


def test_recipe_tool_without_params(ctx_vars):
    """Test creating recipe tool with no parameters."""

    suggestions = [
        {
            "recipe_id": "r_noparams",
            "question": "Get all users",
            "tool_name": "get_all_users",
            "params": {},
            "steps": [{"kind": "graphql"}],
            "sql_steps": [],
        }
    ]

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": {},
            "steps": suggestions[0]["steps"],
            "sql_steps": suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(ctx_vars, suggestions, "graphql", _dummy_step_executor_factory)
        assert len(tools) == 1
        assert tools[0].name == "get_all_users"


def test_recipe_tool_name_deduplication(ctx_vars):
    """Test that duplicate tool names get numbered suffixes."""

    suggestions = [
        {
            "recipe_id": "r_dup1",
            "question": "Get users",
            "tool_name": "get_users",
            "params": {},
            "steps": [{"kind": "graphql"}],
            "sql_steps": [],
        },
        {
            "recipe_id": "r_dup2",
            "question": "Get users different",
            "tool_name": "get_users",  # Same name
            "params": {},
            "steps": [{"kind": "graphql"}],
            "sql_steps": [],
        },
    ]

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {
                        "params": s["params"],
                        "steps": s["steps"],
                        "sql_steps": s["sql_steps"],
                    }
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = create_recipe_tools(ctx_vars, suggestions, "graphql", _dummy_step_executor_factory)

        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "get_users" in tool_names
        assert "get_users_2" in tool_names


def test_recipe_tool_return_directly_default(ctx_vars, sample_recipe_suggestions):
    """Test that recipe tools have return_directly=True by default."""

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": sample_recipe_suggestions[0]["params"],
            "steps": sample_recipe_suggestions[0]["steps"],
            "sql_steps": sample_recipe_suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(
            ctx_vars, sample_recipe_suggestions, "graphql", _dummy_step_executor_factory
        )
        tool = tools[0]

        schema = tool.parameters
        return_directly_param = schema["properties"]["return_directly"]
        assert return_directly_param["default"] is True
        assert "params" in schema["properties"]
        assert "return_directly" in schema["properties"]

        assert tool.name == "list_managers_starting_with_b"


# REST-specific tests (now use same create_recipe_tools with "rest" api_type)


@pytest.fixture
def rest_recipe_suggestions():
    """Sample REST recipe suggestions."""
    return [
        {
            "recipe_id": "r_rest123",
            "question": "Get user by ID",
            "tool_name": "get_user_by_id",
            "params": {"user_id": {"type": "int", "default": 1}},
            "steps": [{"kind": "rest", "method": "GET", "path": "/users/{{user_id}}"}],
            "sql_steps": [],
        }
    ]


def test_rest_create_tools_basic(ctx_vars, rest_recipe_suggestions):
    """Test REST recipe tools are created successfully."""
    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": rest_recipe_suggestions[0]["params"],
            "steps": rest_recipe_suggestions[0]["steps"],
            "sql_steps": rest_recipe_suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(
            ctx_vars, rest_recipe_suggestions, "rest", _dummy_step_executor_factory
        )

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "get_user_by_id"

        from api_agent.llm.types import ToolDefinition

        assert isinstance(tool, ToolDefinition)


def test_rest_create_multiple_tools(ctx_vars):
    """Test creating multiple REST recipe tools."""
    suggestions = [
        {
            "recipe_id": "r_rest1",
            "question": "List users",
            "tool_name": "list_users",
            "params": {"limit": {"type": "int", "default": 10}},
            "steps": [{"kind": "rest"}],
            "sql_steps": [],
        },
        {
            "recipe_id": "r_rest2",
            "question": "Get user posts",
            "tool_name": "get_user_posts",
            "params": {"user_id": {"type": "int", "default": 1}},
            "steps": [{"kind": "rest"}],
            "sql_steps": [],
        },
    ]

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {
                        "params": s["params"],
                        "steps": s["steps"],
                        "sql_steps": s["sql_steps"],
                    }
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = create_recipe_tools(ctx_vars, suggestions, "rest", _dummy_step_executor_factory)

        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "list_users" in tool_names
        assert "get_user_posts" in tool_names


def test_rest_tool_strict_schema(ctx_vars, rest_recipe_suggestions):
    """Test REST recipe tools have strict JSON schema."""
    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": rest_recipe_suggestions[0]["params"],
            "steps": rest_recipe_suggestions[0]["steps"],
            "sql_steps": rest_recipe_suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(
            ctx_vars, rest_recipe_suggestions, "rest", _dummy_step_executor_factory
        )
        tool = tools[0]

        assert isinstance(tool.parameters, dict)
        assert "properties" in tool.parameters
        assert tool.name == "get_user_by_id"


def test_rest_tool_name_deduplication(ctx_vars):
    """Test REST recipe tool name deduplication."""
    suggestions = [
        {
            "recipe_id": "r_dup1",
            "question": "Get data",
            "tool_name": "get_data",
            "params": {},
            "steps": [{"kind": "rest"}],
            "sql_steps": [],
        },
        {
            "recipe_id": "r_dup2",
            "question": "Get data v2",
            "tool_name": "get_data",  # Same name
            "params": {},
            "steps": [{"kind": "rest"}],
            "sql_steps": [],
        },
    ]

    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {
                        "params": s["params"],
                        "steps": s["steps"],
                        "sql_steps": s["sql_steps"],
                    }
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = create_recipe_tools(ctx_vars, suggestions, "rest", _dummy_step_executor_factory)

        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "get_data" in tool_names
        assert "get_data_2" in tool_names


def test_rest_tool_return_directly_default(ctx_vars, rest_recipe_suggestions):
    """Test REST recipe tools have return_directly=True by default."""
    with patch("api_agent.agent.orchestrator.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": rest_recipe_suggestions[0]["params"],
            "steps": rest_recipe_suggestions[0]["steps"],
            "sql_steps": rest_recipe_suggestions[0]["sql_steps"],
        }

        tools = create_recipe_tools(
            ctx_vars, rest_recipe_suggestions, "rest", _dummy_step_executor_factory
        )
        tool = tools[0]

        schema = tool.parameters
        return_directly_param = schema["properties"]["return_directly"]
        assert return_directly_param["default"] is True
