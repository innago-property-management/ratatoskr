"""Unified MCP tool for natural language API queries."""

from typing import Annotated

import structlog
from fastmcp import FastMCP
from fastmcp.server.context import Context
from mcp.types import ToolListChangedNotification
from pydantic import Field

from ..agent.graphql_agent import process_query
from ..agent.grpc_agent import process_grpc_query
from ..agent.mcp_agent import process_mcp_query
from ..agent.rest_agent import process_rest_query
from ..context import MissingHeaderError, get_request_context
from ..recipe import consume_recipe_changes, reset_recipe_change_flag
from ..utils.csv import to_csv

logger = structlog.get_logger(__name__)


def _build_response(result: dict, calls_key: str, ctx) -> dict:
    """Build unified response dict from agent result."""
    response = {
        "ok": result.get("ok", False),
        "data": result.get("data"),
        calls_key: result.get(calls_key, []),
        "error": result.get("error"),
    }
    if ctx.include_result or result.get("result") is not None:
        response["result"] = result.get("result")
    return response


def register_query_tool(mcp: FastMCP) -> None:
    """Register the unified query tool."""

    @mcp.tool(
        name="_query",
        description="""Ask questions about the API in natural language.

The agent reads the schema, builds queries, executes them, and can do multi-step data processing.

Returns answer and the queries/calls made (reusable with execute tool).""",
        tags={"query", "nl"},
    )
    async def query(
        question: Annotated[str, Field(description="Natural language question about the API")],
        ctx: Context | None = None,
    ) -> dict | str:
        """Process natural language query against configured API."""
        try:
            req_ctx = get_request_context()
        except MissingHeaderError as e:
            return {"ok": False, "error": str(e)}

        # Track recipe creation in this request
        reset_recipe_change_flag()

        if req_ctx.api_type == "graphql":
            result = await process_query(question, req_ctx)
        elif req_ctx.api_type == "grpc":
            result = await process_grpc_query(question, req_ctx)
        elif req_ctx.api_type == "mcp":
            result = await process_mcp_query(question, req_ctx)
        else:
            result = await process_rest_query(question, req_ctx)

        # Notify clients if recipes changed
        if ctx and consume_recipe_changes():
            try:
                await ctx.send_notification(ToolListChangedNotification())
            except Exception:
                logger.debug("tool_list_notification_failed", exc_info=True)

        # Direct return: just CSV, no wrapper
        if result.get("result") is not None and result.get("data") is None:
            return to_csv(result["result"])

        calls_key_map = {"graphql": "queries", "grpc": "rpc_calls", "mcp": "mcp_calls"}
        calls_key = calls_key_map.get(req_ctx.api_type, "api_calls")
        response = _build_response(result, calls_key, req_ctx)

        return response
