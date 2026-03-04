"""Tests for the query() tool handler — T021."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastmcp import FastMCP
from mcp.types import ToolListChangedNotification

from api_agent.context import MissingHeaderError, RequestContext
from api_agent.tools.query import register_query_tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request_context(api_type="graphql", **overrides):
    """Build a RequestContext with sensible defaults."""
    return RequestContext(
        target_url=overrides.get("target_url", "https://api.example.com/graphql"),
        api_type=overrides.get("api_type", api_type),
        target_headers=overrides.get("target_headers", {"Authorization": "Bearer test"}),
        allow_unsafe_paths=overrides.get("allow_unsafe_paths", ()),
        base_url=overrides.get("base_url", None),
        include_result=overrides.get("include_result", False),
        poll_paths=overrides.get("poll_paths", ()),
    )


@pytest_asyncio.fixture
async def query_fn():
    """Register the _query tool on a fresh FastMCP app and return its inner function."""
    mcp = FastMCP("test")
    register_query_tool(mcp)
    tool = await mcp.get_tool("_query")
    assert tool is not None
    return tool.fn  # type: ignore[unresolved-attribute]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestQueryToolRouting:
    """Tests for query tool routing and error handling."""

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_routes_to_graphql(
        self, mock_get_ctx, mock_process_query, mock_reset, mock_consume, query_fn
    ):
        """GraphQL api_type routes to process_query."""
        ctx = _make_request_context(api_type="graphql")
        mock_get_ctx.return_value = ctx
        mock_process_query.return_value = {
            "ok": True,
            "data": {"flights": [1, 2, 3]},
            "queries": ["{ flights { id } }"],
        }

        result = await query_fn(question="List flights")

        mock_process_query.assert_awaited_once_with("List flights", ctx)
        assert result["ok"] is True
        assert result["data"] == {"flights": [1, 2, 3]}
        assert "queries" in result

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_grpc_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_routes_to_grpc(
        self, mock_get_ctx, mock_process_grpc, mock_reset, mock_consume, query_fn
    ):
        """gRPC api_type routes to process_grpc_query."""
        ctx = _make_request_context(
            api_type="grpc",
            target_url="grpc://localhost:50051",
        )
        mock_get_ctx.return_value = ctx
        mock_process_grpc.return_value = {
            "ok": True,
            "data": "The server replied: Hello!",
            "rpc_calls": [{"method": "/helloworld.Greeter/SayHello"}],
        }

        result = await query_fn(question="Say hello")

        mock_process_grpc.assert_awaited_once_with("Say hello", ctx)
        assert result["ok"] is True
        assert "rpc_calls" in result

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_rest_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_routes_to_rest(
        self, mock_get_ctx, mock_process_rest, mock_reset, mock_consume, query_fn
    ):
        """REST api_type routes to process_rest_query."""
        ctx = _make_request_context(
            api_type="rest",
            target_url="https://api.example.com/openapi.json",
        )
        mock_get_ctx.return_value = ctx
        mock_process_rest.return_value = {
            "ok": True,
            "data": [{"name": "item1"}],
            "api_calls": [{"method": "GET", "path": "/items"}],
        }

        result = await query_fn(question="List items")

        mock_process_rest.assert_awaited_once_with("List items", ctx)
        assert result["ok"] is True
        assert "api_calls" in result

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.get_request_context")
    async def test_missing_header_returns_error(self, mock_get_ctx, query_fn):
        """MissingHeaderError produces an error dict, not an exception."""
        mock_get_ctx.side_effect = MissingHeaderError("X-Target-URL header required")

        result = await query_fn(question="anything")

        assert result == {"ok": False, "error": "X-Target-URL header required"}

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=True)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_recipe_change_sends_tool_list_changed(
        self, mock_get_ctx, mock_process_query, mock_reset, mock_consume, query_fn
    ):
        """When recipes change, send_tool_list_changed is called on the MCP context."""
        ctx = _make_request_context(api_type="graphql")
        mock_get_ctx.return_value = ctx
        mock_process_query.return_value = {
            "ok": True,
            "data": {"x": 1},
            "queries": [],
        }

        mock_mcp_ctx = AsyncMock()
        result = await query_fn(question="test", ctx=mock_mcp_ctx)

        mock_mcp_ctx.send_notification.assert_awaited_once_with(ToolListChangedNotification())
        assert result["ok"] is True

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=True)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_recipe_change_notification_failure_swallowed(
        self, mock_get_ctx, mock_process_query, mock_reset, mock_consume, query_fn
    ):
        """If send_tool_list_changed raises, the error is swallowed."""
        ctx = _make_request_context(api_type="graphql")
        mock_get_ctx.return_value = ctx
        mock_process_query.return_value = {
            "ok": True,
            "data": {"x": 1},
            "queries": [],
        }

        mock_mcp_ctx = AsyncMock()
        mock_mcp_ctx.send_notification.side_effect = RuntimeError("notify failed")

        # Should not raise
        result = await query_fn(question="test", ctx=mock_mcp_ctx)
        assert result["ok"] is True
        mock_mcp_ctx.send_notification.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_no_recipe_change_skips_notification(
        self, mock_get_ctx, mock_process_query, mock_reset, mock_consume, query_fn
    ):
        """When recipes did not change, send_tool_list_changed is NOT called."""
        ctx = _make_request_context(api_type="graphql")
        mock_get_ctx.return_value = ctx
        mock_process_query.return_value = {
            "ok": True,
            "data": {"x": 1},
            "queries": [],
        }

        mock_mcp_ctx = AsyncMock()
        await query_fn(question="test", ctx=mock_mcp_ctx)

        mock_mcp_ctx.send_tool_list_changed.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_direct_return_csv_path(
        self, mock_get_ctx, mock_process_query, mock_reset, mock_consume, query_fn
    ):
        """When result is present and data is None, returns CSV string."""
        ctx = _make_request_context(api_type="graphql")
        mock_get_ctx.return_value = ctx
        mock_process_query.return_value = {
            "ok": True,
            "data": None,
            "result": [{"name": "Alice", "age": 30}],
            "queries": [],
        }

        with patch("api_agent.tools.query.to_csv", return_value="name,age\nAlice,30\n") as mock_csv:
            result = await query_fn(question="list people")

        mock_csv.assert_called_once_with([{"name": "Alice", "age": 30}])
        assert result == "name,age\nAlice,30\n"

    @pytest.mark.asyncio
    @patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
    @patch("api_agent.tools.query.reset_recipe_change_flag")
    @patch("api_agent.tools.query.process_query", new_callable=AsyncMock)
    @patch("api_agent.tools.query.get_request_context")
    async def test_reset_recipe_flag_called(
        self, mock_get_ctx, mock_process_query, mock_reset, mock_consume, query_fn
    ):
        """reset_recipe_change_flag is always called before processing."""
        ctx = _make_request_context(api_type="graphql")
        mock_get_ctx.return_value = ctx
        mock_process_query.return_value = {
            "ok": True,
            "data": {"x": 1},
            "queries": [],
        }

        await query_fn(question="test")

        mock_reset.assert_called_once()
