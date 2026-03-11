"""Tests for MCP routing in context.py and tools/query.py (Task A4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastmcp import FastMCP

from api_agent.context import MissingHeaderError, RequestContext, validate_target_url
from api_agent.tools.query import register_query_tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mcp_request_context(**overrides) -> RequestContext:
    """Build an MCP RequestContext with sensible defaults."""
    return RequestContext(
        target_url=overrides.get("target_url", "mcp://github"),
        api_type=overrides.get("api_type", "mcp"),
        target_headers=overrides.get("target_headers", {}),
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
# context.py — api_type validation
# ---------------------------------------------------------------------------


def test_api_type_mcp_accepted():
    """X-API-Type: mcp does not raise MissingHeaderError during context extraction."""
    with patch("api_agent.context.get_http_headers") as mock_headers:
        mock_headers.return_value = {
            "x-target-url": "mcp://github",
            "x-api-type": "mcp",
        }
        from api_agent.context import get_request_context

        # Should not raise
        ctx = get_request_context()
        assert ctx.api_type == "mcp"


def test_api_type_mcp_validation_error_message():
    """Error message for invalid api_type mentions 'mcp' as a valid option."""
    with patch("api_agent.context.get_http_headers") as mock_headers:
        mock_headers.return_value = {
            "x-target-url": "https://example.com",
            "x-api-type": "invalid-type",
        }
        from api_agent.context import get_request_context

        with pytest.raises(MissingHeaderError) as exc_info:
            get_request_context()

        # Error message should mention mcp as a valid option
        assert "mcp" in str(exc_info.value).lower()


def test_mcp_scheme_allowed_in_validate_target_url():
    """mcp:// scheme passes validate_target_url when api_type is 'mcp'."""
    # Should not raise
    result = validate_target_url("mcp://github", "mcp")
    assert result == "mcp://github"


def test_mcp_http_target_validated_by_ssrf():
    """http://localhost is blocked by SSRF check even with api_type=mcp."""
    with pytest.raises(MissingHeaderError, match="[Pp]rivate|[Bb]locked"):
        validate_target_url("http://127.0.0.1/mcp", "mcp")


# ---------------------------------------------------------------------------
# tools/query.py — dispatch to process_mcp_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
@patch("api_agent.tools.query.reset_recipe_change_flag")
@patch("api_agent.tools.query.process_mcp_query", new_callable=AsyncMock)
@patch("api_agent.tools.query.get_request_context")
async def test_query_routes_to_mcp_agent(
    mock_get_ctx, mock_process_mcp, mock_reset, mock_consume, query_fn
):
    """query() dispatches to process_mcp_query when api_type='mcp'."""
    ctx = _make_mcp_request_context()
    mock_get_ctx.return_value = ctx
    mock_process_mcp.return_value = {
        "ok": True,
        "data": [{"id": 1}],
        "mcp_calls": [{"tool": "get_pr", "args": {}}],
        "error": None,
    }

    result = await query_fn(question="list pull requests", ctx=None)

    mock_process_mcp.assert_awaited_once_with("list pull requests", ctx)
    assert result["ok"] is True


@pytest.mark.asyncio
@patch("api_agent.tools.query.consume_recipe_changes", return_value=False)
@patch("api_agent.tools.query.reset_recipe_change_flag")
@patch("api_agent.tools.query.process_mcp_query", new_callable=AsyncMock)
@patch("api_agent.tools.query.get_request_context")
async def test_query_mcp_calls_key_in_response(
    mock_get_ctx, mock_process_mcp, mock_reset, mock_consume, query_fn
):
    """Response uses 'mcp_calls' key for MCP api_type."""
    ctx = _make_mcp_request_context()
    mock_get_ctx.return_value = ctx
    mock_process_mcp.return_value = {
        "ok": True,
        "data": {"items": []},
        "mcp_calls": [{"tool": "list_issues", "args": {}}],
        "error": None,
    }

    result = await query_fn(question="list issues", ctx=None)

    assert "mcp_calls" in result
    assert result["mcp_calls"] == [{"tool": "list_issues", "args": {}}]
