"""Unified MCP tool for direct API execution."""

import json
import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from ..config import settings
from ..context import MissingHeaderError, get_request_context
from ..graphql import execute_query
from ..grpc.client import execute_unary_rpc
from ..grpc.reflection import GrpcSchema, MethodInfo
from ..grpc.reflection import fetch_schema as fetch_grpc_schema
from ..rest.client import execute_request
from ..rest.schema_loader import fetch_schema_context

logger = logging.getLogger(__name__)


def _find_grpc_method(schema: GrpcSchema, method_path: str) -> MethodInfo | None:
    """Find a method in the schema by full method path or short name."""
    needle = method_path.lstrip("/")
    for svc in schema.services:
        for m in svc.methods:
            if m.full_method_path.lstrip("/") == needle:
                return m
            if f"{svc.full_name}/{m.name}" == needle:
                return m
    return None


def register_execute_tool(mcp: FastMCP) -> None:
    """Register the unified execute tool with generic internal name."""

    @mcp.tool(
        name="_execute",
        description="""Execute a specific API call directly.

For GraphQL: provide query (and optional variables)
For REST: provide method and path (and optional params/body)
For gRPC: provide grpc_method and optional grpc_request (JSON string)

Use this to re-run queries from the query tool or execute known operations.""",
        tags={"execute"},
    )
    async def execute(
        # GraphQL params
        query: Annotated[str | None, Field(description="GraphQL query string")] = None,
        variables: Annotated[dict[str, Any] | None, Field(description="GraphQL variables")] = None,
        # REST params
        method: Annotated[str | None, Field(description="HTTP method (GET, POST, etc.)")] = None,
        path: Annotated[str | None, Field(description="API path (e.g., /users/{id})")] = None,
        path_params: Annotated[
            dict[str, Any] | None, Field(description="Path parameter values")
        ] = None,
        query_params: Annotated[
            dict[str, Any] | None, Field(description="Query string parameters")
        ] = None,
        body: Annotated[
            dict[str, Any] | None, Field(description="Request body (for POST/PUT/PATCH)")
        ] = None,
        # gRPC params
        grpc_method: Annotated[
            str | None,
            Field(description="Full gRPC method path (e.g., package.Service/Method)"),
        ] = None,
        grpc_request: Annotated[
            str | None,
            Field(description="gRPC request body as JSON string"),
        ] = None,
    ) -> dict:
        """Execute API call directly."""
        try:
            ctx = get_request_context()
        except MissingHeaderError as e:
            return {"ok": False, "error": str(e)}

        if ctx.api_type == "graphql":
            # GraphQL execution
            if not query:
                return {"ok": False, "error": "query param required for GraphQL"}

            result = await execute_query(query, variables, ctx.target_url, ctx.target_headers)

            if not result.get("success"):
                return {"ok": False, "error": result.get("error", "Query failed")}

            data = result.get("data", {})
            data_str = json.dumps(data, indent=2)

            if len(data_str) > settings.MAX_RESPONSE_CHARS:
                return {
                    "ok": True,
                    "data": f"{data_str[: settings.MAX_RESPONSE_CHARS]}\n\n[TRUNCATED - Use pagination to fetch smaller chunks.]",
                }

            return {"ok": True, "data": data}

        elif ctx.api_type == "grpc":
            # gRPC execution
            if not grpc_method:
                return {
                    "ok": False,
                    "error": "grpc_method param required for gRPC",
                }

            # Parse request JSON
            try:
                request_json: dict[str, Any] = (
                    json.loads(grpc_request) if grpc_request else {}
                )
            except json.JSONDecodeError as e:
                return {
                    "ok": False,
                    "error": f"Invalid JSON in grpc_request: {e}",
                }

            # Build metadata from target headers
            metadata: list[tuple[str, str]] | None = None
            if ctx.target_headers:
                metadata = [(k, v) for k, v in ctx.target_headers.items()]

            # Fetch schema via reflection
            try:
                schema = await fetch_grpc_schema(
                    ctx.target_url, metadata=metadata
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Failed to fetch gRPC schema: {e}",
                }

            # Find the method in the schema
            method_info = _find_grpc_method(schema, grpc_method)
            if method_info is None:
                available = [
                    m.full_method_path
                    for svc in schema.services
                    for m in svc.methods
                    if not m.client_streaming and not m.server_streaming
                ]
                return {
                    "ok": False,
                    "error": f"Method '{grpc_method}' not found in schema. "
                    f"Available unary methods: {available}",
                }

            # Block streaming methods
            if method_info.client_streaming or method_info.server_streaming:
                return {
                    "ok": False,
                    "error": f"Method '{grpc_method}' uses streaming, which is not "
                    "supported by the execute tool. Only unary RPCs are supported.",
                }

            # Execute the RPC
            result = await execute_unary_rpc(
                target_url=ctx.target_url,
                method_path=method_info.full_method_path,
                request_json=request_json,
                pool=schema.pool,
                input_type_name=method_info.input_type,
                output_type_name=method_info.output_type,
                metadata=metadata,
            )

            if not result.get("success"):
                return {"ok": False, "error": result.get("error", "RPC call failed")}

            data = result.get("data", {})
            data_str = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)

            if len(data_str) > settings.MAX_RESPONSE_CHARS:
                return {
                    "ok": True,
                    "data": f"{data_str[: settings.MAX_RESPONSE_CHARS]}\n\n[TRUNCATED - Use grpc_request to narrow results.]",
                }

            return {"ok": True, "data": data}

        else:
            # REST execution
            if not method or not path:
                return {"ok": False, "error": "method and path params required for REST"}

            # Get base URL from header override or spec
            base_url = ctx.base_url
            if not base_url:
                _, base_url, _ = await fetch_schema_context(ctx.target_url, ctx.target_headers)
            if not base_url:
                return {"ok": False, "error": "Could not extract base URL from OpenAPI spec"}

            result = await execute_request(
                method,
                path,
                path_params,
                query_params,
                body,
                base_url=base_url,
                headers=ctx.target_headers,
                allow_unsafe_paths=list(ctx.allow_unsafe_paths),
            )

            if not result.get("success"):
                return {"ok": False, "error": result.get("error", "Request failed")}

            data = result.get("data", {})
            data_str = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)

            if len(data_str) > settings.MAX_RESPONSE_CHARS:
                return {
                    "ok": True,
                    "data": f"{data_str[: settings.MAX_RESPONSE_CHARS]}\n\n[TRUNCATED - Use query params to limit results.]",
                }

            return {"ok": True, "data": data}
