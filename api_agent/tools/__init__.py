"""MCP tools."""

import structlog
from fastmcp import FastMCP

from .execute import register_execute_tool
from .query import register_query_tool

logger = structlog.get_logger(__name__)


def register_all_tools(mcp: FastMCP) -> None:
    """Register all tools with generic internal names.

    Internal names (_query, _execute) are transformed by middleware
    to session-specific names (e.g., flights_query, catalog_execute).
    """
    register_query_tool(mcp)
    register_execute_tool(mcp)

    logger.info("tools_registered", tools=["_query", "_execute"])
