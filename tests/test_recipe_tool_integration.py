"""Integration tests for recipe tool registration with agent."""

from unittest.mock import patch

import pytest

from api_agent.agent.graphql_agent import _create_individual_recipe_tools as graphql_create_tools
from api_agent.agent.rest_agent import _create_individual_recipe_tools as rest_create_tools
from api_agent.context import RequestContext


@pytest.fixture
def mock_context():
    """Create a mock request context."""
    return RequestContext(
        target_url="https://test.api.com/graphql",
        target_headers={},
        api_type="graphql",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


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


def test_graphql_create_tools_basic(mock_context, sample_recipe_suggestions):
    """Test that recipe tools are created successfully."""

    with patch("api_agent.agent.graphql_agent.RECIPE_STORE") as mock_store:
        # Mock the recipe store to return our test recipe
        mock_store.get_recipe.return_value = {
            "params": sample_recipe_suggestions[0]["params"],
            "steps": sample_recipe_suggestions[0]["steps"],
            "sql_steps": sample_recipe_suggestions[0]["sql_steps"],
        }

        # Create recipe tools
        tools = graphql_create_tools(mock_context, sample_recipe_suggestions)

        # Verify tools were created
        assert len(tools) == 1
        tool = tools[0]

        # Verify tool has correct name
        assert tool.name == "list_managers_starting_with_b"

        # Verify tool is a ToolDefinition
        from api_agent.llm.types import ToolDefinition

        assert isinstance(tool, ToolDefinition)


def test_create_multiple_recipe_tools(mock_context):
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

    with patch("api_agent.agent.graphql_agent.RECIPE_STORE") as mock_store:
        # Mock recipe store for both recipes
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

        # Create tools
        tools = graphql_create_tools(mock_context, suggestions)

        # Verify both tools created
        assert len(tools) == 2

        # Verify tool names are different
        tool_names = [t.name for t in tools]
        assert "get_users_by_role" in tool_names
        assert "list_teams_with_limit" in tool_names
        assert len(set(tool_names)) == 2  # All unique


def test_recipe_tool_has_correct_signature(mock_context, sample_recipe_suggestions):
    """Test that recipe tool has correct parameter signature."""

    with patch("api_agent.agent.graphql_agent.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": sample_recipe_suggestions[0]["params"],
            "steps": sample_recipe_suggestions[0]["steps"],
            "sql_steps": sample_recipe_suggestions[0]["sql_steps"],
        }

        tools = graphql_create_tools(mock_context, sample_recipe_suggestions)
        tool = tools[0]

        # Verify tool has parameters schema
        assert isinstance(tool.parameters, dict)
        assert "properties" in tool.parameters

        # Verify tool name is correct
        assert tool.name == "list_managers_starting_with_b"


def test_recipe_tool_without_params(mock_context):
    """Test creating recipe tool with no parameters."""

    suggestions = [
        {
            "recipe_id": "r_noparams",
            "question": "Get all users",
            "tool_name": "get_all_users",
            "params": {},  # No params
            "steps": [{"kind": "graphql"}],
            "sql_steps": [],
        }
    ]

    with patch("api_agent.agent.graphql_agent.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": {},
            "steps": suggestions[0]["steps"],
            "sql_steps": suggestions[0]["sql_steps"],
        }

        # Should still create tool successfully
        tools = graphql_create_tools(mock_context, suggestions)
        assert len(tools) == 1
        assert tools[0].name == "get_all_users"


def test_recipe_tool_name_deduplication(mock_context):
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

    with patch("api_agent.agent.graphql_agent.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {"params": s["params"], "steps": s["steps"], "sql_steps": s["sql_steps"]}
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = graphql_create_tools(mock_context, suggestions)

        # Should have 2 tools with unique names
        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "get_users" in tool_names
        assert "get_users_2" in tool_names  # Second one gets _2 suffix


def test_recipe_tool_return_directly_default(mock_context, sample_recipe_suggestions):
    """Test that recipe tools have return_directly=True by default."""

    with patch("api_agent.agent.graphql_agent.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": sample_recipe_suggestions[0]["params"],
            "steps": sample_recipe_suggestions[0]["steps"],
            "sql_steps": sample_recipe_suggestions[0]["sql_steps"],
        }

        tools = graphql_create_tools(mock_context, sample_recipe_suggestions)
        tool = tools[0]

        # Check the schema has return_directly with default=True
        schema = tool.parameters
        return_directly_param = schema["properties"]["return_directly"]
        assert return_directly_param["default"] is True
        # Recipe params are wrapped in a 'params' Pydantic model field
        assert "params" in schema["properties"]
        assert "return_directly" in schema["properties"]

        # Verify tool name
        assert tool.name == "list_managers_starting_with_b"


# REST Agent Integration Tests


@pytest.fixture
def rest_context():
    """Create a REST request context."""
    return RequestContext(
        target_url="https://test.api.com",
        target_headers={},
        api_type="rest",
        base_url="/v1",
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


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


def test_rest_create_tools_basic(rest_context, rest_recipe_suggestions):
    """Test REST recipe tools are created successfully."""
    with patch("api_agent.agent.rest_agent.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": rest_recipe_suggestions[0]["params"],
            "steps": rest_recipe_suggestions[0]["steps"],
            "sql_steps": rest_recipe_suggestions[0]["sql_steps"],
        }

        tools = rest_create_tools(rest_context, "/v1", rest_recipe_suggestions)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "get_user_by_id"

        from api_agent.llm.types import ToolDefinition

        assert isinstance(tool, ToolDefinition)


def test_rest_create_multiple_tools(rest_context):
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

    with patch("api_agent.agent.rest_agent.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {"params": s["params"], "steps": s["steps"], "sql_steps": s["sql_steps"]}
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = rest_create_tools(rest_context, "/v1", suggestions)

        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "list_users" in tool_names
        assert "get_user_posts" in tool_names


def test_rest_tool_strict_schema(rest_context, rest_recipe_suggestions):
    """Test REST recipe tools have strict JSON schema."""
    with patch("api_agent.agent.rest_agent.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": rest_recipe_suggestions[0]["params"],
            "steps": rest_recipe_suggestions[0]["steps"],
            "sql_steps": rest_recipe_suggestions[0]["sql_steps"],
        }

        tools = rest_create_tools(rest_context, "/v1", rest_recipe_suggestions)
        tool = tools[0]

        assert isinstance(tool.parameters, dict)
        assert "properties" in tool.parameters
        assert tool.name == "get_user_by_id"


def test_rest_tool_name_deduplication(rest_context):
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

    with patch("api_agent.agent.rest_agent.RECIPE_STORE") as mock_store:

        def get_recipe(recipe_id):
            for s in suggestions:
                if s["recipe_id"] == recipe_id:
                    return {"params": s["params"], "steps": s["steps"], "sql_steps": s["sql_steps"]}
            return None

        mock_store.get_recipe.side_effect = get_recipe

        tools = rest_create_tools(rest_context, "/v1", suggestions)

        assert len(tools) == 2
        tool_names = [t.name for t in tools]
        assert "get_data" in tool_names
        assert "get_data_2" in tool_names


def test_rest_tool_return_directly_default(rest_context, rest_recipe_suggestions):
    """Test REST recipe tools have return_directly=True by default."""
    with patch("api_agent.agent.rest_agent.RECIPE_STORE") as mock_store:
        mock_store.get_recipe.return_value = {
            "params": rest_recipe_suggestions[0]["params"],
            "steps": rest_recipe_suggestions[0]["steps"],
            "sql_steps": rest_recipe_suggestions[0]["sql_steps"],
        }

        tools = rest_create_tools(rest_context, "/v1", rest_recipe_suggestions)
        tool = tools[0]

        schema = tool.parameters
        return_directly_param = schema["properties"]["return_directly"]
        assert return_directly_param["default"] is True
