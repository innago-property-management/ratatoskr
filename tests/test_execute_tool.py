"""Tests for the _execute MCP tool in api_agent.tools.execute.

T011: Direct API execution — GraphQL path, REST path, truncation, missing params, missing headers.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastmcp import FastMCP

from api_agent.context import MissingHeaderError, RequestContext
from api_agent.tools.execute import register_execute_tool


@pytest_asyncio.fixture
async def execute_tool():
    """Register the _execute tool on a test FastMCP instance and return the inner function."""
    mcp = FastMCP("test")
    register_execute_tool(mcp)
    tool = await mcp.get_tool("_execute")
    assert tool is not None
    return tool.fn  # type: ignore[unresolved-attribute]


@pytest.fixture
def graphql_ctx():
    """RequestContext configured for GraphQL."""
    return RequestContext(
        target_url="https://api.example.com/graphql",
        api_type="graphql",
        target_headers={"Authorization": "Bearer test-token"},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
    )


@pytest.fixture
def rest_ctx_for_execute():
    """RequestContext configured for REST with a base_url."""
    return RequestContext(
        target_url="https://api.example.com/openapi.json",
        api_type="rest",
        target_headers={"Authorization": "Bearer test-token"},
        allow_unsafe_paths=(),
        base_url="https://api.example.com/v1",
        include_result=False,
        poll_paths=(),
    )


@pytest.fixture
def rest_ctx_no_base_url():
    """RequestContext configured for REST without base_url (needs spec lookup)."""
    return RequestContext(
        target_url="https://api.example.com/openapi.json",
        api_type="rest",
        target_headers={},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
    )


class TestExecuteGraphQL:
    """T011: GraphQL execution path."""

    @pytest.mark.asyncio
    async def test_graphql_success(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL query returns ok=True with data."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        mock_execute_query = AsyncMock(
            return_value={
                "success": True,
                "data": {"users": [{"id": 1, "name": "Alice"}]},
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        result = await execute_tool(query="{ users { id name } }")

        assert result["ok"] is True
        assert result["data"]["users"][0]["name"] == "Alice"
        mock_execute_query.assert_awaited_once_with(
            "{ users { id name } }",
            None,
            "https://api.example.com/graphql",
            {"Authorization": "Bearer test-token"},
        )

    @pytest.mark.asyncio
    async def test_graphql_with_variables(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL query with variables passes them through."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        mock_execute_query = AsyncMock(
            return_value={
                "success": True,
                "data": {"user": {"id": 1, "name": "Alice"}},
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        result = await execute_tool(
            query="query($id: ID!) { user(id: $id) { id name } }",
            variables={"id": "1"},
        )

        assert result["ok"] is True
        assert result["data"]["user"]["name"] == "Alice"
        mock_execute_query.assert_awaited_once_with(
            "query($id: ID!) { user(id: $id) { id name } }",
            {"id": "1"},
            "https://api.example.com/graphql",
            {"Authorization": "Bearer test-token"},
        )

    @pytest.mark.asyncio
    async def test_graphql_query_failure(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL query that returns success=False propagates error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        mock_execute_query = AsyncMock(
            return_value={"success": False, "error": "Syntax error in query"}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        result = await execute_tool(query="{ invalid }")

        assert result["ok"] is False
        assert "Syntax error" in result["error"]

    @pytest.mark.asyncio
    async def test_graphql_missing_query(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL without query param returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        result = await execute_tool()

        assert result["ok"] is False
        assert "query" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_graphql_truncation(self, execute_tool, graphql_ctx, monkeypatch):
        """Large GraphQL response is truncated."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        # Create data larger than MAX_RESPONSE_CHARS
        large_data = {"items": [{"value": "x" * 1000} for _ in range(100)]}
        mock_execute_query = AsyncMock(
            return_value={"success": True, "data": large_data}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        # Set a small max for testing
        monkeypatch.setattr("api_agent.tools.execute.settings.MAX_RESPONSE_CHARS", 500)

        result = await execute_tool(query="{ items { value } }")

        assert result["ok"] is True
        assert isinstance(result["data"], str)
        assert "[TRUNCATED" in result["data"]


class TestExecuteREST:
    """T011: REST execution path."""

    @pytest.mark.asyncio
    async def test_rest_success_with_base_url(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """REST call with base_url in context succeeds."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        mock_execute_request = AsyncMock(
            return_value={
                "success": True,
                "data": [{"id": 1, "name": "Alice"}],
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(method="GET", path="/users")

        assert result["ok"] is True
        assert result["data"][0]["name"] == "Alice"
        mock_execute_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rest_with_all_params(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """REST call with path_params, query_params, and body."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        mock_execute_request = AsyncMock(
            return_value={"success": True, "data": {"updated": True}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(
            method="GET",
            path="/users/{id}",
            path_params={"id": "123"},
            query_params={"fields": "name,email"},
            body=None,
        )

        assert result["ok"] is True
        call_args = mock_execute_request.call_args
        assert call_args[0][0] == "GET"  # method
        assert call_args[0][1] == "/users/{id}"  # path
        assert call_args[0][2] == {"id": "123"}  # path_params

    @pytest.mark.asyncio
    async def test_rest_missing_method(self, execute_tool, rest_ctx_for_execute, monkeypatch):
        """REST call without method returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        result = await execute_tool(path="/users")

        assert result["ok"] is False
        assert "method" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rest_missing_path(self, execute_tool, rest_ctx_for_execute, monkeypatch):
        """REST call without path returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        result = await execute_tool(method="GET")

        assert result["ok"] is False
        assert "path" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rest_no_base_url_falls_back_to_schema(
        self, execute_tool, rest_ctx_no_base_url, monkeypatch
    ):
        """When ctx.base_url is None, fetches base URL from schema."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_no_base_url,
        )

        mock_fetch_schema = AsyncMock(
            return_value=(
                "endpoints text",
                "https://api.example.com/v2",
                '{"paths": {}}',
            )
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_schema_context", mock_fetch_schema
        )

        mock_execute_request = AsyncMock(
            return_value={"success": True, "data": {"status": "ok"}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(method="GET", path="/health")

        assert result["ok"] is True
        # Verify base_url came from schema fetch
        call_kwargs = mock_execute_request.call_args[1]
        assert call_kwargs["base_url"] == "https://api.example.com/v2"

    @pytest.mark.asyncio
    async def test_rest_no_base_url_anywhere(
        self, execute_tool, rest_ctx_no_base_url, monkeypatch
    ):
        """When neither ctx nor schema provides base URL, return error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_no_base_url,
        )

        mock_fetch_schema = AsyncMock(return_value=("", "", ""))
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_schema_context", mock_fetch_schema
        )

        result = await execute_tool(method="GET", path="/users")

        assert result["ok"] is False
        assert "base url" in result["error"].lower() or "base_url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rest_request_failure(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """REST request that fails propagates error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        mock_execute_request = AsyncMock(
            return_value={"success": False, "error": "HTTP 500: Internal Server Error"}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(method="GET", path="/broken")

        assert result["ok"] is False
        assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_rest_truncation(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """Large REST response is truncated."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        large_data = [{"value": "y" * 1000} for _ in range(100)]
        mock_execute_request = AsyncMock(
            return_value={"success": True, "data": large_data}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        monkeypatch.setattr("api_agent.tools.execute.settings.MAX_RESPONSE_CHARS", 500)

        result = await execute_tool(method="GET", path="/large")

        assert result["ok"] is True
        assert isinstance(result["data"], str)
        assert "[TRUNCATED" in result["data"]


class TestExecuteMissingHeader:
    """T011: Missing header error path."""

    @pytest.mark.asyncio
    async def test_missing_header_error(self, execute_tool, monkeypatch):
        """When get_request_context raises MissingHeaderError, return error dict."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: (_ for _ in ()).throw(
                MissingHeaderError("X-Target-URL header required")
            ),
        )

        result = await execute_tool(query="{ users { id } }")

        assert result["ok"] is False
        assert "X-Target-URL" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_api_type_header(self, execute_tool, monkeypatch):
        """When X-API-Type header is missing, return error."""

        def raise_missing():
            raise MissingHeaderError("X-API-Type header required (graphql|rest)")

        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", raise_missing
        )

        result = await execute_tool(method="GET", path="/users")

        assert result["ok"] is False
        assert "X-API-Type" in result["error"]
