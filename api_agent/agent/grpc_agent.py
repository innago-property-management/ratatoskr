"""gRPC agent using reflection-based discovery and unary RPC execution."""

import json
import logging
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from ..config import settings
from ..context import RequestContext
from ..executor import (
    execute_sql,
    extract_tables_from_response,
    truncate_for_context,
)
from ..grpc.client import (
    execute_bidi_streaming_rpc,
    execute_client_streaming_rpc,
    execute_server_streaming_rpc,
    execute_unary_rpc,
)
from ..grpc.reflection import GrpcSchema, MethodInfo, fetch_schema
from ..llm.provider import MaxTurnsExceeded
from ..llm.tools import tool
from ..recipe import _return_directly_flag, build_partial_result
from ..tracing import trace_metadata
from .contextvar_utils import safe_append_contextvar_list
from .model import get_inject_instructions, provider
from .progress import get_turn_context, reset_progress
from .prompts import (
    CONTEXT_SECTION,
    DECISION_GUIDANCE,
    EFFECTIVE_PATTERNS,
    PERSISTENCE_SPEC,
    SEARCH_TOOL_DESC,
    SQL_RULES,
    SQL_TOOL_DESC,
    TOOL_USAGE_RULES,
    UNCERTAINTY_SPEC,
)
from .schema_search import create_search_schema_tool

logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    if settings.DEBUG:
        logger.info(f"[gRPC] {msg}")


# Context-local storage (isolated per async request)
_rpc_calls: ContextVar[list[dict[str, Any]]] = ContextVar("grpc_rpc_calls")
_query_results: ContextVar[dict[str, Any]] = ContextVar("grpc_query_results")
_last_result: ContextVar[list] = ContextVar("grpc_last_result")
_raw_schema: ContextVar[str] = ContextVar("grpc_raw_schema")
_sql_steps: ContextVar[list[str]] = ContextVar("grpc_sql_steps")


def _build_system_prompt() -> str:
    """Build system prompt for gRPC agent."""
    current_date = datetime.now().strftime("%Y-%m-%d")

    return f"""You are a gRPC API agent that answers questions by calling RPC methods and returning data.

{SQL_RULES}

## gRPC-Specific
- All request fields must be provided as JSON matching the message type in <message_types>
- Use exact field names as shown in the schema (proto field names, snake_case)
- Use grpc_call for unary methods
- Use grpc_stream for server-streaming methods
- Use grpc_client_stream for client-streaming methods (send array of requests, get single response)
- Use grpc_bidi_stream for bidi-streaming methods (send array of requests, collect responses)

<tools>
grpc_call(method, request, name?, return_directly?)
  Execute unary gRPC RPC. Result stored as DuckDB table.
  - method: "package.Service/MethodName" (from <services> above)
  - request: JSON string matching input message type
  - return_directly: Skip LLM analysis, return raw data directly

grpc_stream(method, request, name?, max_messages?)
  Execute server-streaming gRPC RPC. Collects streamed messages into a list.
  - method: "package.Service/MethodName" (must be a server-streaming method)
  - request: JSON string matching input message type
  - max_messages: Max messages to collect (default 100)
  Result stored as DuckDB table for sql_query.

grpc_client_stream(method, requests, name?, return_directly?)
  Execute client-streaming gRPC RPC. Send multiple requests, get single response.
  - method: "package.Service/MethodName" (must be a client-streaming method)
  - requests: JSON array of request objects, e.g. '[{{"item": "a"}}, {{"item": "b"}}]'
  - return_directly: Skip LLM analysis, return raw data directly
  Result stored as DuckDB table for sql_query.

grpc_bidi_stream(method, requests, name?, max_messages?)
  Execute bidi-streaming gRPC RPC. Send multiple requests, collect responses.
  - method: "package.Service/MethodName" (must be a bidi-streaming method)
  - requests: JSON array of request objects
  - max_messages: Max response messages to collect (default 100)
  Result stored as DuckDB table for sql_query.

{SQL_TOOL_DESC}

{SEARCH_TOOL_DESC}
</tools>
<workflow>
1. Read <services> and <message_types> below
2. Identify the correct service method for the question
3. Build request JSON matching the input message type
4. Execute grpc_call()
5. Use sql_query to filter/aggregate if needed
</workflow>

{CONTEXT_SECTION.format(current_date=current_date, max_turns=settings.MAX_AGENT_TURNS)}

{DECISION_GUIDANCE}

{UNCERTAINTY_SPEC}

{PERSISTENCE_SPEC.format(max_turns=settings.MAX_AGENT_TURNS)}

{EFFECTIVE_PATTERNS}

{TOOL_USAGE_RULES}

<examples>
Simple: grpc_call("helloworld.Greeter/SayHello", '{{"name": "world"}}')
With SQL: grpc_call("payments.PaymentService/ListPayments", '{{"limit": 100}}', name="payments"); sql_query('SELECT currency, SUM(amount) FROM payments GROUP BY currency')
</examples>
"""


def _find_method(schema: GrpcSchema, method_path: str) -> MethodInfo | None:
    """Find a method in the schema by full method path or short name."""
    # Normalize: strip leading /
    needle = method_path.lstrip("/")

    for svc in schema.services:
        for m in svc.methods:
            # Match full path (without leading /)
            if m.full_method_path.lstrip("/") == needle:
                return m
            # Match ServiceName/MethodName
            if f"{svc.full_name}/{m.name}" == needle:
                return m
    return None


def _find_streaming_method(schema: GrpcSchema, method_path: str) -> MethodInfo | None:
    """Find a server-streaming method. Returns None if method is not server-streaming."""
    method = _find_method(schema, method_path)
    if method and method.server_streaming and not method.client_streaming:
        return method
    return None


def _find_client_streaming_method(schema: GrpcSchema, method_path: str) -> MethodInfo | None:
    """Find a client-streaming (not bidi) method."""
    method = _find_method(schema, method_path)
    if method and method.client_streaming and not method.server_streaming:
        return method
    return None


def _find_bidi_streaming_method(schema: GrpcSchema, method_path: str) -> MethodInfo | None:
    """Find a bidi-streaming method (both client and server streaming)."""
    method = _find_method(schema, method_path)
    if method and method.client_streaming and method.server_streaming:
        return method
    return None


def _create_grpc_call_tool(
    ctx: RequestContext, schema: GrpcSchema
) -> Any:
    """Create grpc_call tool with bound context."""

    async def grpc_call(
        method: str,
        request: str = "{}",
        name: str = "data",
        return_directly: bool = False,
    ) -> str:
        """Execute unary gRPC RPC call.

        Args:
            method: Full method path (e.g., "package.Service/MethodName")
            request: JSON string with request fields
            name: Table name for sql_query (default: "data")
            return_directly: Return raw data directly without LLM processing

        Returns:
            JSON string with RPC response
        """
        # Resolve method
        method_info = _find_method(schema, method)
        if not method_info:
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if not m.server_streaming and not m.client_streaming
            ]
            return json.dumps({
                "success": False,
                "error": f"Method '{method}' not found. Available unary methods: {available}",
            })

        # Block streaming methods
        if method_info.server_streaming:
            return json.dumps({
                "success": False,
                "error": (
                    f"Method '{method}' is a server-streaming method. "
                    "Use grpc_stream instead of grpc_call."
                ),
            })
        if method_info.client_streaming and method_info.server_streaming:
            return json.dumps({
                "success": False,
                "error": (
                    f"Method '{method}' is a bidi-streaming method. "
                    "Use grpc_bidi_stream instead of grpc_call."
                ),
            })
        if method_info.client_streaming:
            return json.dumps({
                "success": False,
                "error": (
                    f"Method '{method}' is a client-streaming method. "
                    "Use grpc_client_stream instead of grpc_call."
                ),
            })

        # Parse request JSON
        try:
            request_json = json.loads(request)
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid request JSON: {e.msg}",
            })

        # Build metadata from target headers
        metadata = (
            [(k, v) for k, v in ctx.target_headers.items()]
            if ctx.target_headers
            else None
        )

        # Execute RPC
        result = await execute_unary_rpc(
            target_url=ctx.target_url,
            method_path=method_info.full_method_path,
            request_json=request_json,
            pool=schema.pool,
            input_type_name=method_info.input_type,
            output_type_name=method_info.output_type,
            metadata=metadata,
        )

        # Track call
        safe_append_contextvar_list(
            _rpc_calls,
            {
                "method": method_info.full_method_path,
                "request": request,
                "name": name,
                "success": bool(result.get("success")),
            },
        )

        # Store result for sql_query
        stored_data = None
        schema_info = None
        if result.get("success"):
            try:
                results = _query_results.get()
                data = result.get("data", {})
                tables, schema_info = extract_tables_from_response(data, name)
                results.update(tables)
                _query_results.set(results)
                stored_data = tables.get(name)
                if stored_data is not None:
                    _last_result.get()[0] = stored_data
            except LookupError:
                pass

        _log(f"RPC {method_info.full_method_path} -> {json.dumps(result)[:200]}")

        if return_directly and result.get("success"):
            try:
                _return_directly_flag.get().append(True)
            except LookupError:
                pass

        # Smart context optimization
        if result.get("success") and stored_data:
            if schema_info:
                return json.dumps(
                    {"success": True, "table": name, **schema_info},
                    indent=2,
                )
            if isinstance(stored_data, list):
                return json.dumps(
                    {"success": True, **truncate_for_context(stored_data, name)},
                    indent=2,
                )

        return json.dumps(result, indent=2)

    return tool(grpc_call)


def _create_grpc_stream_tool(
    ctx: RequestContext, schema: GrpcSchema
) -> Any:
    """Create grpc_stream tool with bound context."""

    async def grpc_stream(
        method: str,
        request: str = "{}",
        name: str = "data",
        max_messages: int = 100,
    ) -> str:
        """Execute server-streaming gRPC RPC call.

        Args:
            method: Full method path (e.g., "package.Service/MethodName")
            request: JSON string with request fields
            name: Table name for sql_query (default: "data")
            max_messages: Maximum messages to collect (default: 100)

        Returns:
            JSON string with streamed response data
        """
        # Resolve method — must be server-streaming (not bidi)
        method_info = _find_streaming_method(schema, method)
        if not method_info:
            # Check if it's a valid method but wrong streaming type
            existing = _find_method(schema, method)
            if existing:
                if existing.client_streaming and existing.server_streaming:
                    return json.dumps({
                        "success": False,
                        "error": (
                            f"Method '{method}' is bidi-streaming. "
                            "Use grpc_bidi_stream instead."
                        ),
                    })
                if existing.client_streaming and not existing.server_streaming:
                    return json.dumps({
                        "success": False,
                        "error": (
                            f"Method '{method}' is client-streaming. "
                            "Use grpc_client_stream instead."
                        ),
                    })
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Method '{method}' is not a server-streaming method. "
                        "Use grpc_call instead."
                    ),
                })
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if m.server_streaming
            ]
            return json.dumps({
                "success": False,
                "error": f"Streaming method '{method}' not found. Available: {available}",
            })

        # Parse request JSON
        try:
            request_json = json.loads(request)
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid request JSON: {e.msg}",
            })

        # Build metadata from target headers
        metadata = (
            [(k, v) for k, v in ctx.target_headers.items()]
            if ctx.target_headers
            else None
        )

        # Execute streaming RPC
        result = await execute_server_streaming_rpc(
            target_url=ctx.target_url,
            method_path=method_info.full_method_path,
            request_json=request_json,
            pool=schema.pool,
            input_type_name=method_info.input_type,
            output_type_name=method_info.output_type,
            metadata=metadata,
            max_messages=max_messages,
        )

        # Track call
        safe_append_contextvar_list(
            _rpc_calls,
            {
                "method": method_info.full_method_path,
                "request": request,
                "name": name,
                "success": bool(result.get("success")),
                "streaming": True,
            },
        )

        # Store result for sql_query — streaming returns a list in "data"
        stored_data = None
        if result.get("success"):
            try:
                results = _query_results.get()
                data = result.get("data", [])
                tables, _ = extract_tables_from_response(data, name)
                results.update(tables)
                _query_results.set(results)
                stored_data = tables.get(name)
                if stored_data is not None:
                    _last_result.get()[0] = stored_data
            except LookupError:
                pass

        msg_count = result.get("message_count", 0)
        _log(f"STREAM {method_info.full_method_path} -> {msg_count} messages")

        if result.get("success") and stored_data:
            if isinstance(stored_data, list):
                return json.dumps(
                    {
                        "success": True,
                        "message_count": msg_count,
                        **truncate_for_context(stored_data, name),
                    },
                    indent=2,
                )

        return json.dumps(result, indent=2)

    return tool(grpc_stream)


def _create_grpc_client_stream_tool(
    ctx: RequestContext, schema: GrpcSchema
) -> Any:
    """Create grpc_client_stream tool with bound context."""

    async def grpc_client_stream(
        method: str,
        requests: str = "[]",
        name: str = "data",
        return_directly: bool = False,
    ) -> str:
        """Execute client-streaming gRPC RPC call (batch).

        Args:
            method: Full method path (e.g., "package.Service/Upload")
            requests: JSON array of request objects
            name: Table name for sql_query (default: "data")
            return_directly: Return raw data directly without LLM processing

        Returns:
            JSON string with RPC response
        """
        # Resolve method — must be client-streaming (not bidi)
        method_info = _find_client_streaming_method(schema, method)
        if not method_info:
            existing = _find_method(schema, method)
            if existing:
                if existing.server_streaming and existing.client_streaming:
                    return json.dumps({
                        "success": False,
                        "error": (
                            f"Method '{method}' is bidi-streaming. "
                            "Use grpc_bidi_stream instead."
                        ),
                    })
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Method '{method}' is not a client-streaming method. "
                        "Use grpc_call or grpc_stream instead."
                    ),
                })
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if m.client_streaming and not m.server_streaming
            ]
            return json.dumps({
                "success": False,
                "error": f"Client-streaming method '{method}' not found. Available: {available}",
            })

        # Parse requests JSON array
        try:
            requests_json = json.loads(requests)
            if not isinstance(requests_json, list):
                return json.dumps({
                    "success": False,
                    "error": "requests must be a JSON array of request objects",
                })
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid requests JSON: {e.msg}",
            })

        # Build metadata from target headers
        metadata = (
            [(k, v) for k, v in ctx.target_headers.items()]
            if ctx.target_headers
            else None
        )

        # Execute client-streaming RPC
        result = await execute_client_streaming_rpc(
            target_url=ctx.target_url,
            method_path=method_info.full_method_path,
            requests_json=requests_json,
            pool=schema.pool,
            input_type_name=method_info.input_type,
            output_type_name=method_info.output_type,
            metadata=metadata,
        )

        # Track call
        safe_append_contextvar_list(
            _rpc_calls,
            {
                "method": method_info.full_method_path,
                "requests": requests,
                "name": name,
                "success": bool(result.get("success")),
                "streaming": "client",
            },
        )

        # Store result for sql_query
        stored_data = None
        schema_info = None
        if result.get("success"):
            try:
                results = _query_results.get()
                data = result.get("data", {})
                tables, schema_info = extract_tables_from_response(data, name)
                results.update(tables)
                _query_results.set(results)
                stored_data = tables.get(name)
                if stored_data is not None:
                    _last_result.get()[0] = stored_data
            except LookupError:
                pass

        _log(f"CLIENT_STREAM {method_info.full_method_path} -> {json.dumps(result)[:200]}")

        if return_directly and result.get("success"):
            try:
                _return_directly_flag.get().append(True)
            except LookupError:
                pass

        if result.get("success") and stored_data:
            if schema_info:
                return json.dumps(
                    {"success": True, "table": name, **schema_info},
                    indent=2,
                )
            if isinstance(stored_data, list):
                return json.dumps(
                    {"success": True, **truncate_for_context(stored_data, name)},
                    indent=2,
                )

        return json.dumps(result, indent=2)

    return tool(grpc_client_stream)


def _create_grpc_bidi_stream_tool(
    ctx: RequestContext, schema: GrpcSchema
) -> Any:
    """Create grpc_bidi_stream tool with bound context."""

    async def grpc_bidi_stream(
        method: str,
        requests: str = "[]",
        name: str = "data",
        max_messages: int = 100,
    ) -> str:
        """Execute bidi-streaming gRPC RPC call (fire-and-collect).

        Args:
            method: Full method path (e.g., "package.Service/Chat")
            requests: JSON array of request objects
            name: Table name for sql_query (default: "data")
            max_messages: Maximum response messages to collect (default: 100)

        Returns:
            JSON string with streamed response data
        """
        # Resolve method — must be bidi-streaming
        method_info = _find_bidi_streaming_method(schema, method)
        if not method_info:
            existing = _find_method(schema, method)
            if existing:
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Method '{method}' is not a bidi-streaming method. "
                        "Use grpc_call, grpc_stream, or grpc_client_stream instead."
                    ),
                })
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if m.client_streaming and m.server_streaming
            ]
            return json.dumps({
                "success": False,
                "error": f"Bidi-streaming method '{method}' not found. Available: {available}",
            })

        # Parse requests JSON array
        try:
            requests_json = json.loads(requests)
            if not isinstance(requests_json, list):
                return json.dumps({
                    "success": False,
                    "error": "requests must be a JSON array of request objects",
                })
        except json.JSONDecodeError as e:
            return json.dumps({
                "success": False,
                "error": f"Invalid requests JSON: {e.msg}",
            })

        # Build metadata from target headers
        metadata = (
            [(k, v) for k, v in ctx.target_headers.items()]
            if ctx.target_headers
            else None
        )

        # Execute bidi-streaming RPC
        result = await execute_bidi_streaming_rpc(
            target_url=ctx.target_url,
            method_path=method_info.full_method_path,
            requests_json=requests_json,
            pool=schema.pool,
            input_type_name=method_info.input_type,
            output_type_name=method_info.output_type,
            metadata=metadata,
            max_messages=max_messages,
        )

        # Track call
        safe_append_contextvar_list(
            _rpc_calls,
            {
                "method": method_info.full_method_path,
                "requests": requests,
                "name": name,
                "success": bool(result.get("success")),
                "streaming": "bidi",
            },
        )

        # Store result for sql_query — bidi returns a list in "data"
        stored_data = None
        if result.get("success"):
            try:
                results = _query_results.get()
                data = result.get("data", [])
                tables, _ = extract_tables_from_response(data, name)
                results.update(tables)
                _query_results.set(results)
                stored_data = tables.get(name)
                if stored_data is not None:
                    _last_result.get()[0] = stored_data
            except LookupError:
                pass

        msg_count = result.get("message_count", 0)
        _log(f"BIDI_STREAM {method_info.full_method_path} -> {msg_count} messages")

        if result.get("success") and stored_data:
            if isinstance(stored_data, list):
                return json.dumps(
                    {
                        "success": True,
                        "message_count": msg_count,
                        **truncate_for_context(stored_data, name),
                    },
                    indent=2,
                )

        return json.dumps(result, indent=2)

    return tool(grpc_bidi_stream)


def _sql_query(sql: str, return_directly: bool = False) -> str:
    """Run DuckDB SQL on stored gRPC results."""
    try:
        data = _query_results.get()
    except LookupError:
        return json.dumps({"success": False, "error": "No data. Call grpc_call first."})

    if not data:
        return json.dumps({"success": False, "error": "No data. Call grpc_call first."})

    result = execute_sql(data, sql)
    _log(f"SQL {json.dumps(result)[:200]}")

    if result.get("success"):
        rows = result.get("result", [])
        try:
            _last_result.get()[0] = rows
        except LookupError:
            pass

        safe_append_contextvar_list(_sql_steps, sql)

        if return_directly:
            try:
                _return_directly_flag.get().append(True)
            except LookupError:
                pass

        if isinstance(rows, list):
            return json.dumps(
                {"success": True, **truncate_for_context(rows, "sql_result")},
                indent=2,
            )

    return json.dumps(result, indent=2)


sql_query_tool = tool(_sql_query)

# Create search_schema tool bound to gRPC schema context var
search_schema = create_search_schema_tool(_raw_schema)


async def process_grpc_query(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Process natural language query against gRPC API.

    Args:
        question: Natural language question
        ctx: Request context with target_url (grpc:// or grpcs://) and target_headers
    """
    try:
        _log(f"QUERY {question[:80]}")

        # Reset per-request storage
        _rpc_calls.set([])
        _sql_steps.set([])
        _query_results.set({})
        _last_result.set([None])
        _return_directly_flag.set([])
        reset_progress()

        # Fetch schema via reflection
        try:
            schema = await fetch_schema(
                target_url=ctx.target_url,
                metadata=(
                    [(k, v) for k, v in ctx.target_headers.items()]
                    if ctx.target_headers
                    else None
                ),
            )
        except Exception as e:
            error_msg = str(e)
            if "UNIMPLEMENTED" in error_msg or "reflection" in error_msg.lower():
                return {
                    "ok": False,
                    "data": None,
                    "rpc_calls": [],
                    "error": (
                        "gRPC server reflection not enabled. "
                        "Enable it on the server to use the gRPC agent."
                    ),
                }
            return {
                "ok": False,
                "data": None,
                "rpc_calls": [],
                "error": f"Failed to connect to gRPC server: {error_msg}",
            }

        if not schema.services:
            return {
                "ok": False,
                "data": None,
                "rpc_calls": [],
                "error": "No services found via reflection. The server may not expose any services.",
            }

        # Store raw schema for search_schema
        _raw_schema.set(schema.raw_schema_text)

        # Truncate schema for LLM context
        schema_text = schema.raw_schema_text
        max_schema = settings.MAX_TOOL_RESPONSE_CHARS
        if len(schema_text) > max_schema:
            schema_text = (
                schema_text[:max_schema]
                + "\n[SCHEMA TRUNCATED - use search_schema() to explore]"
            )

        # Create tools
        grpc_tool = _create_grpc_call_tool(ctx, schema)
        grpc_stream_tool = _create_grpc_stream_tool(ctx, schema)
        grpc_client_stream_tool = _create_grpc_client_stream_tool(ctx, schema)
        grpc_bidi_stream_tool = _create_grpc_bidi_stream_tool(ctx, schema)
        tools = [
            grpc_tool,
            grpc_stream_tool,
            grpc_client_stream_tool,
            grpc_bidi_stream_tool,
            sql_query_tool,
            search_schema,
        ]

        # Build prompt
        instructions = _build_system_prompt()
        augmented_query = f"{schema_text}\n\nQuestion: {question}"

        def _should_stop(results):
            try:
                return bool(_return_directly_flag.get())
            except LookupError:
                return False

        # Run tool-calling loop
        rpc_calls = []
        last_data = None
        turn_info = ""
        try:
            with trace_metadata({"mcp_name": settings.MCP_SLUG, "agent_type": "grpc"}):
                result = await provider.run_tool_loop(
                    instructions=instructions,
                    user_message=augmented_query,
                    tool_defs=tools,
                    max_turns=settings.MAX_AGENT_TURNS,
                    should_stop=_should_stop,
                    inject_instructions=get_inject_instructions(),
                )

            rpc_calls = _rpc_calls.get()
            last_data = _last_result.get()[0]
            turn_info = get_turn_context(settings.MAX_AGENT_TURNS)

        except MaxTurnsExceeded:
            rpc_calls = _rpc_calls.get()
            last_data = _last_result.get()[0]
            turn_info = get_turn_context(settings.MAX_AGENT_TURNS)
            return build_partial_result(last_data, rpc_calls, turn_info, "rpc_calls")

        # Check direct return
        is_direct_return = False
        try:
            is_direct_return = result.final_output == "__DIRECT_RETURN__" or bool(
                _return_directly_flag.get()
            )
        except LookupError:
            pass

        if not result.final_output and not is_direct_return:
            if last_data:
                return {
                    "ok": True,
                    "data": f"[Partial - {turn_info}] Data retrieved but agent didn't complete.",
                    "result": last_data,
                    "rpc_calls": rpc_calls,
                    "error": None,
                }
            return {
                "ok": False,
                "data": None,
                "result": None,
                "rpc_calls": rpc_calls,
                "error": f"No output ({turn_info})",
            }

        if is_direct_return:
            agent_output = None
        else:
            agent_output = str(result.final_output)
            _log(f"DONE calls={len(rpc_calls)} output={agent_output[:100]}")

        return {
            "ok": True,
            "data": agent_output,
            "result": last_data,
            "rpc_calls": rpc_calls,
            "error": None,
        }

    except Exception as e:
        logger.exception("gRPC Agent error")
        return {
            "ok": False,
            "data": None,
            "rpc_calls": [],
            "error": str(e),
        }
