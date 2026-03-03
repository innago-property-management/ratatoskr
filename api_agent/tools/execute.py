"""Unified MCP tool for direct API execution."""

import json
import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from ..config import settings
from ..context import MissingHeaderError, get_request_context
from ..graphql import execute_query
from ..grpc.client import (
    execute_bidi_streaming_rpc,
    execute_client_streaming_rpc,
    execute_server_streaming_rpc,
    execute_unary_rpc,
)
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


def _parse_single_request(grpc_request: str | None) -> dict[str, Any] | str:
    """Parse grpc_request JSON string into a dict, or return error string."""
    if not grpc_request:
        return {}
    try:
        return json.loads(grpc_request)
    except json.JSONDecodeError as e:
        return f"Invalid JSON in grpc_request: {e}"


def register_execute_tool(mcp: FastMCP) -> None:
    """Register the unified execute tool with generic internal name."""

    @mcp.tool(
        name="_execute",
        description="""Execute a specific API call directly.

For GraphQL: provide query (and optional variables)
For REST: provide method and path (and optional params/body)
For gRPC: provide grpc_method and grpc_request (JSON string) for unary/server-streaming,
  or grpc_requests (JSON array) for client-streaming/bidi-streaming

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
            Field(description="gRPC request body as JSON string (for unary/server-streaming)"),
        ] = None,
        grpc_requests: Annotated[
            str | None,
            Field(
                description="JSON array of request objects (for client-streaming/bidi-streaming)"
            ),
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

            # Build metadata from target headers
            metadata: list[tuple[str, str]] | None = None
            if ctx.target_headers:
                metadata = [(k, v) for k, v in ctx.target_headers.items()]

            # Fetch schema via reflection
            try:
                schema = await fetch_grpc_schema(ctx.target_url, metadata=metadata)
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Failed to fetch gRPC schema: {e}",
                }

            # Find the method in the schema
            method_info = _find_grpc_method(schema, grpc_method)
            if method_info is None:
                available = [m.full_method_path for svc in schema.services for m in svc.methods]
                return {
                    "ok": False,
                    "error": f"Method '{grpc_method}' not found in schema. "
                    f"Available methods: {available}",
                }

            # Route by streaming type
            is_client_stream = method_info.client_streaming and not method_info.server_streaming
            is_bidi = method_info.client_streaming and method_info.server_streaming
            is_server_stream = method_info.server_streaming and not method_info.client_streaming

            if is_client_stream or is_bidi:
                # Client-streaming or bidi: need grpc_requests (JSON array)
                # or fall back to wrapping grpc_request as single-element array
                if grpc_requests:
                    try:
                        requests_list: list[dict[str, Any]] = json.loads(grpc_requests)
                    except json.JSONDecodeError as e:
                        return {
                            "ok": False,
                            "error": f"Invalid JSON in grpc_requests: {e}",
                        }
                    if not isinstance(requests_list, list):
                        return {
                            "ok": False,
                            "error": "grpc_requests must be a JSON array, "
                            f"got {type(requests_list).__name__}",
                        }
                elif grpc_request:
                    try:
                        requests_list = [json.loads(grpc_request)]
                    except json.JSONDecodeError as e:
                        return {
                            "ok": False,
                            "error": f"Invalid JSON in grpc_request: {e}",
                        }
                else:
                    stream_type = "bidi-streaming" if is_bidi else "client-streaming"
                    return {
                        "ok": False,
                        "error": f"Method '{grpc_method}' is {stream_type}. "
                        "Provide grpc_requests (JSON array) or grpc_request (single JSON object).",
                    }

                rpc_fn = execute_bidi_streaming_rpc if is_bidi else execute_client_streaming_rpc
                result = await rpc_fn(
                    target_url=ctx.target_url,
                    method_path=method_info.full_method_path,
                    requests_json=requests_list,
                    pool=schema.pool,
                    input_type_name=method_info.input_type,
                    output_type_name=method_info.output_type,
                    metadata=metadata,
                )
            elif is_server_stream:
                # Server-streaming: single request, stream responses
                parsed = _parse_single_request(grpc_request)
                if isinstance(parsed, str):
                    return {"ok": False, "error": parsed}
                request_json: dict[str, Any] = parsed

                result = await execute_server_streaming_rpc(
                    target_url=ctx.target_url,
                    method_path=method_info.full_method_path,
                    request_json=request_json,
                    pool=schema.pool,
                    input_type_name=method_info.input_type,
                    output_type_name=method_info.output_type,
                    metadata=metadata,
                )
            else:
                # Unary RPC
                parsed = _parse_single_request(grpc_request)
                if isinstance(parsed, str):
                    return {"ok": False, "error": parsed}
                request_json = parsed

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
