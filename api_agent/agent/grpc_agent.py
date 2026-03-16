"""gRPC agent using reflection-based discovery and unary RPC execution."""

import fnmatch
import json
import time
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Literal

import structlog

from ..config import settings
from ..context import RequestContext
from ..executor import extract_tables_from_response, truncate_for_context_async
from ..filtering import filter_grpc_services, parse_config_allowlist
from ..grpc.client import (
    execute_bidi_streaming_rpc,
    execute_client_streaming_rpc,
    execute_server_streaming_rpc,
    execute_unary_rpc,
)
from ..grpc.reflection import GrpcSchema, MethodInfo, build_schema_text, fetch_schema
from ..llm.tools import tool
from ..metrics import record_request, record_schema_fetch
from ..recipe import (
    _set_return_directly,
    build_api_id,
    maybe_extract_and_save_recipe,
)
from ..recipe.store import render_text_template
from ..sanitize import sanitize_error
from ..tracing import trace_span
from .contextvar_utils import safe_append_contextvar_list
from .model import provider
from .orchestrator import (
    AgentContextVars,
    ProtocolConfig,
    create_sql_query_tool,
    format_tool_response,
    make_logger,
    run_agent_orchestration,
    store_result,
)
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

logger = structlog.get_logger(__name__)

_log = make_logger("[gRPC]")

# Context-local storage (isolated per async request)
_rpc_calls: ContextVar[list[dict[str, Any]]] = ContextVar("grpc_rpc_calls")
_recipe_steps: ContextVar[list[dict[str, Any]]] = ContextVar("grpc_recipe_steps")
_query_results: ContextVar[dict[str, Any]] = ContextVar("grpc_query_results")
_last_result: ContextVar[list] = ContextVar("grpc_last_result")
_raw_schema: ContextVar[str] = ContextVar("grpc_raw_schema")
_sql_steps: ContextVar[list[str]] = ContextVar("grpc_sql_steps")

# Bundle for orchestrator
_ctx_vars = AgentContextVars(
    api_calls=_rpc_calls,
    recipe_steps=_recipe_steps,
    query_results=_query_results,
    last_result=_last_result,
    raw_schema=_raw_schema,
    sql_steps=_sql_steps,
)


# ---------------------------------------------------------------------------
# gRPC-specific: mutation safety
# ---------------------------------------------------------------------------

_UNSAFE_PATTERNS_CACHE: list[str] | None = None


def _get_unsafe_patterns() -> list[str]:
    """Lazily compute and cache unsafe method patterns from settings.

    Note: The cache is never invalidated within a process.  Tests that
    monkeypatch ``settings.GRPC_UNSAFE_METHOD_PATTERNS`` after the cache
    is populated must call ``_reset_unsafe_patterns_cache()`` first.
    """
    global _UNSAFE_PATTERNS_CACHE
    if _UNSAFE_PATTERNS_CACHE is None:
        _UNSAFE_PATTERNS_CACHE = [
            p.strip() for p in settings.GRPC_UNSAFE_METHOD_PATTERNS.split(",") if p.strip()
        ]
    return _UNSAFE_PATTERNS_CACHE


def _reset_unsafe_patterns_cache() -> None:
    """Clear the cached unsafe patterns — for testing only."""
    global _UNSAFE_PATTERNS_CACHE
    _UNSAFE_PATTERNS_CACHE = None


def _is_grpc_method_safe(method_path: str, allow_unsafe_rpcs: tuple[str, ...]) -> bool:
    """Check if a gRPC method is safe (read-only) to call.

    Extracts the method name (last segment after '/') and checks against
    configured unsafe patterns (e.g., Create*, Delete*). If the method is
    unsafe, checks the allow_unsafe_rpcs allowlist for an override.

    Note: Leading '/' is stripped from method_path before allowlist matching.
    Allowlist entries should NOT include a leading slash
    (e.g., "users.UserService/CreateUser", not "/users.UserService/CreateUser").

    Returns True if safe to call, False if blocked.
    """
    method_name = method_path.rsplit("/", 1)[-1]
    is_unsafe = any(fnmatch.fnmatch(method_name, p) for p in _get_unsafe_patterns())
    if not is_unsafe:
        return True
    # Check allowlist — matches against the full method path (without leading /)
    clean_path = method_path.lstrip("/")
    return any(fnmatch.fnmatch(clean_path, p) for p in allow_unsafe_rpcs)


def _blocked_method_response(method: str) -> str:
    """Return JSON error for a blocked unsafe method."""
    return json.dumps(
        {
            "success": False,
            "error": (
                f"Method '{method}' is not allowed (read-only mode). "
                "Use X-Allow-Unsafe-RPCs header to permit mutations."
            ),
        }
    )


# ---------------------------------------------------------------------------
# gRPC-specific: system prompt
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# gRPC-specific: method resolution
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# gRPC-specific: API call tools — shared execution pipeline
# ---------------------------------------------------------------------------

RpcKind = Literal["unary", "server_stream", "client_stream", "bidi_stream"]


def _resolve_method_for_kind(
    schema: GrpcSchema,
    method: str,
    rpc_kind: RpcKind,
) -> tuple[MethodInfo | None, str | None]:
    """Resolve a gRPC method and validate it matches the expected streaming type.

    Returns ``(method_info, None)`` on success or ``(None, error_json)`` on failure.
    Error messages are intentionally specific per *rpc_kind* because tests assert
    on the exact wording.
    """
    if rpc_kind == "unary":
        method_info = _find_method(schema, method)
        if not method_info:
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if not m.server_streaming and not m.client_streaming
            ]
            return None, json.dumps(
                {
                    "success": False,
                    "error": f"Method '{method}' not found. Available unary methods: {available}",
                }
            )
        if method_info.client_streaming and method_info.server_streaming:
            return None, json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Method '{method}' is a bidi-streaming method. "
                        "Use grpc_bidi_stream instead of grpc_call."
                    ),
                }
            )
        if method_info.server_streaming:
            return None, json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Method '{method}' is a server-streaming method. "
                        "Use grpc_stream instead of grpc_call."
                    ),
                }
            )
        if method_info.client_streaming:
            return None, json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Method '{method}' is a client-streaming method. "
                        "Use grpc_client_stream instead of grpc_call."
                    ),
                }
            )
        return method_info, None

    if rpc_kind == "server_stream":
        method_info = _find_streaming_method(schema, method)
        if not method_info:
            existing = _find_method(schema, method)
            if existing:
                if existing.client_streaming and existing.server_streaming:
                    return None, json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Method '{method}' is bidi-streaming. "
                                "Use grpc_bidi_stream instead."
                            ),
                        }
                    )
                if existing.client_streaming and not existing.server_streaming:
                    return None, json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Method '{method}' is client-streaming. "
                                "Use grpc_client_stream instead."
                            ),
                        }
                    )
                return None, json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Method '{method}' is not a server-streaming method. "
                            "Use grpc_call instead."
                        ),
                    }
                )
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if m.server_streaming
            ]
            return None, json.dumps(
                {
                    "success": False,
                    "error": f"Streaming method '{method}' not found. Available: {available}",
                }
            )
        return method_info, None

    if rpc_kind == "client_stream":
        method_info = _find_client_streaming_method(schema, method)
        if not method_info:
            existing = _find_method(schema, method)
            if existing:
                if existing.server_streaming and existing.client_streaming:
                    return None, json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Method '{method}' is bidi-streaming. "
                                "Use grpc_bidi_stream instead."
                            ),
                        }
                    )
                return None, json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Method '{method}' is not a client-streaming method. "
                            "Use grpc_call or grpc_stream instead."
                        ),
                    }
                )
            available = [
                m.full_method_path
                for svc in schema.services
                for m in svc.methods
                if m.client_streaming and not m.server_streaming
            ]
            return None, json.dumps(
                {
                    "success": False,
                    "error": f"Client-streaming method '{method}' not found. Available: {available}",
                }
            )
        return method_info, None

    # bidi_stream
    method_info = _find_bidi_streaming_method(schema, method)
    if not method_info:
        existing = _find_method(schema, method)
        if existing:
            return None, json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Method '{method}' is not a bidi-streaming method. "
                        "Use grpc_call, grpc_stream, or grpc_client_stream instead."
                    ),
                }
            )
        available = [
            m.full_method_path
            for svc in schema.services
            for m in svc.methods
            if m.client_streaming and m.server_streaming
        ]
        return None, json.dumps(
            {
                "success": False,
                "error": f"Bidi-streaming method '{method}' not found. Available: {available}",
            }
        )
    return method_info, None


_LOG_PREFIX: dict[RpcKind, str] = {
    "unary": "RPC",
    "server_stream": "STREAM",
    "client_stream": "CLIENT_STREAM",
    "bidi_stream": "BIDI_STREAM",
}


async def _execute_grpc_common(
    ctx: RequestContext,
    schema: GrpcSchema,
    method: str,
    payload_str: str,
    name: str,
    rpc_kind: RpcKind,
    max_messages: int | None = None,
    return_directly: bool = False,
) -> str:
    """Shared execution pipeline for all four gRPC tool types."""
    # 1. Resolve method
    method_info, error = _resolve_method_for_kind(schema, method, rpc_kind)
    if error:
        return error
    assert method_info is not None  # for type-checker

    # 2. Mutation safety
    if not _is_grpc_method_safe(method_info.full_method_path, ctx.grpc_allow_unsafe_rpcs):
        return _blocked_method_response(method)

    # 3. Parse JSON payload
    expects_array = rpc_kind in ("client_stream", "bidi_stream")
    try:
        parsed = json.loads(payload_str)
        if expects_array and not isinstance(parsed, list):
            return json.dumps(
                {
                    "success": False,
                    "error": "requests must be a JSON array of request objects",
                }
            )
    except json.JSONDecodeError as e:
        label = "requests" if expects_array else "request"
        return json.dumps(
            {
                "success": False,
                "error": f"Invalid {label} JSON: {e.msg}",
            }
        )

    # 4. Build metadata from target headers
    metadata = [(k, v) for k, v in ctx.target_headers.items()] if ctx.target_headers else None

    # 5. Execute RPC (dispatch by kind)
    common_kwargs: dict[str, Any] = {
        "target_url": ctx.target_url,
        "method_path": method_info.full_method_path,
        "pool": schema.pool,
        "input_type_name": method_info.input_type,
        "output_type_name": method_info.output_type,
        "metadata": metadata,
    }
    if rpc_kind == "unary":
        result = await execute_unary_rpc(request_json=parsed, **common_kwargs)
    elif rpc_kind == "server_stream":
        assert max_messages is not None  # always provided by wrapper
        result = await execute_server_streaming_rpc(
            request_json=parsed, max_messages=max_messages, **common_kwargs
        )
    elif rpc_kind == "client_stream":
        result = await execute_client_streaming_rpc(requests_json=parsed, **common_kwargs)
    else:
        assert max_messages is not None  # always provided by wrapper
        result = await execute_bidi_streaming_rpc(
            requests_json=parsed, max_messages=max_messages, **common_kwargs
        )

    # 6. Track call in ContextVar
    tracking: dict[str, Any] = {
        "method": method_info.full_method_path,
        "requests" if expects_array else "request": payload_str,
        "name": name,
        "success": bool(result.get("success")),
    }
    if rpc_kind == "server_stream":
        tracking["streaming"] = True
    elif rpc_kind == "client_stream":
        tracking["streaming"] = "client"
    elif rpc_kind == "bidi_stream":
        tracking["streaming"] = "bidi"
    safe_append_contextvar_list(_rpc_calls, tracking)

    # 7. Store result for sql_query
    uses_list_data = rpc_kind in ("server_stream", "bidi_stream")
    stored_data, schema_info = None, None
    if result.get("success"):
        data = result.get("data", [] if uses_list_data else {})
        stored_data, schema_info = store_result(_ctx_vars, data, name)

    # 8. Recipe step tracking (unary only)
    if rpc_kind == "unary" and result.get("success"):
        safe_append_contextvar_list(
            _recipe_steps,
            {
                "kind": "grpc",
                "method": method_info.full_method_path,
                "request": payload_str,
                "name": name,
            },
        )

    # 9. Log
    if uses_list_data:
        msg_count = result.get("message_count", 0)
        _log(f"{_LOG_PREFIX[rpc_kind]} {method_info.full_method_path} -> {msg_count} messages")
    else:
        _log(
            f"{_LOG_PREFIX[rpc_kind]} {method_info.full_method_path} -> {json.dumps(result)[:200]}"
        )

    # 10. Handle return_directly (unary + client_stream only)
    if return_directly and result.get("success") and rpc_kind in ("unary", "client_stream"):
        _set_return_directly()

    # 11. Format response
    if uses_list_data:
        # Server/bidi stream: custom response with message_count
        msg_count = result.get("message_count", 0)
        if result.get("success") and stored_data and isinstance(stored_data, list):
            return json.dumps(
                {
                    "success": True,
                    "message_count": msg_count,
                    **await truncate_for_context_async(stored_data, name),
                },
                indent=2,
            )
        return json.dumps(result, indent=2)

    # Unary / client_stream: standard format
    return await format_tool_response(stored_data, schema_info, name, result)


# ---------------------------------------------------------------------------
# gRPC-specific: thin tool factory wrappers
# ---------------------------------------------------------------------------


def _create_grpc_call_tool(ctx: RequestContext, schema: GrpcSchema) -> Any:
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
        return await _execute_grpc_common(
            ctx, schema, method, request, name, "unary", return_directly=return_directly
        )

    return tool(grpc_call)


def _create_grpc_stream_tool(ctx: RequestContext, schema: GrpcSchema) -> Any:
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
        return await _execute_grpc_common(
            ctx, schema, method, request, name, "server_stream", max_messages=max_messages
        )

    return tool(grpc_stream)


def _create_grpc_client_stream_tool(ctx: RequestContext, schema: GrpcSchema) -> Any:
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
        return await _execute_grpc_common(
            ctx, schema, method, requests, name, "client_stream", return_directly=return_directly
        )

    return tool(grpc_client_stream)


def _create_grpc_bidi_stream_tool(ctx: RequestContext, schema: GrpcSchema) -> Any:
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
        return await _execute_grpc_common(
            ctx, schema, method, requests, name, "bidi_stream", max_messages=max_messages
        )

    return tool(grpc_bidi_stream)


# Create search_schema tool bound to gRPC schema context var
search_schema = create_search_schema_tool(_raw_schema)

# Create sql_query tool via shared factory
sql_query_tool = create_sql_query_tool(_ctx_vars, _log, "Call grpc_call first.")


# ---------------------------------------------------------------------------
# gRPC-specific: recipe step executor
# ---------------------------------------------------------------------------


def _make_grpc_step_executor_factory(ctx: RequestContext, schema: GrpcSchema):
    """Build a gRPC step executor factory for recipe tools.

    Returns a factory: (recipe_id) -> async step_executor
    """

    def factory(recipe_id: str):
        async def grpc_step_executor(step_idx, step, params, results):
            if not isinstance(step, dict) or step.get("kind") != "grpc":
                return (
                    False,
                    None,
                    json.dumps({"success": False, "error": "invalid recipe step"}, indent=2),
                    None,
                )

            method_path = step.get("method", "")
            name = str(step.get("name") or "data")

            # Render request template with params
            tmpl = step.get("request_template", "{}")
            if not isinstance(tmpl, str):
                tmpl = json.dumps(tmpl) if tmpl else "{}"
            request_str = render_text_template(tmpl, params)

            try:
                request_json = json.loads(request_str)
            except json.JSONDecodeError:
                return (
                    False,
                    None,
                    json.dumps(
                        {
                            "success": False,
                            "error": f"Invalid request JSON after template rendering: {request_str[:100]}",
                        },
                        indent=2,
                    ),
                    None,
                )

            method_info = _find_method(schema, method_path)
            if not method_info:
                return (
                    False,
                    None,
                    json.dumps(
                        {"success": False, "error": f"Method not found: {method_path}"},
                        indent=2,
                    ),
                    None,
                )

            # Mutation safety check (same as tool functions)
            if not _is_grpc_method_safe(method_info.full_method_path, ctx.grpc_allow_unsafe_rpcs):
                return (
                    False,
                    None,
                    _blocked_method_response(method_path),
                    None,
                )

            metadata = (
                [(k, v) for k, v in ctx.target_headers.items()] if ctx.target_headers else None
            )

            res = await execute_unary_rpc(
                target_url=ctx.target_url,
                method_path=method_info.full_method_path,
                request_json=request_json,
                pool=schema.pool,
                input_type_name=method_info.input_type,
                output_type_name=method_info.output_type,
                metadata=metadata,
            )
            if not res.get("success"):
                return (
                    False,
                    None,
                    json.dumps(
                        {"success": False, "error": res.get("error", "RPC failed")},
                        indent=2,
                    ),
                    None,
                )

            data = res.get("data", {})
            tables, _ = extract_tables_from_response(data, name)
            results.update(tables)
            _query_results.set(results)

            call_rec = {
                "method": method_info.full_method_path,
                "request": request_str,
                "name": name,
                "success": True,
            }
            safe_append_contextvar_list(_rpc_calls, call_rec)
            return True, tables.get(name), "", call_rec

        return grpc_step_executor

    return factory


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def process_grpc_query(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Process natural language query against gRPC API.

    Args:
        question: Natural language question
        ctx: Request context with target_url (grpc:// or grpcs://) and target_headers
    """
    t0 = time.monotonic()
    status = "ok"
    try:
        # Fetch schema via reflection
        t_schema = time.monotonic()
        try:
            with trace_span("schema.fetch", {"protocol": "grpc"}):
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
                "error": f"Failed to connect to gRPC server: {sanitize_error(e)}",
            }
        record_schema_fetch((time.monotonic() - t_schema) * 1000, "grpc")

        if not schema.services:
            return {
                "ok": False,
                "data": None,
                "rpc_calls": [],
                "error": "No services found via reflection. The server may not expose any services.",
            }

        # Apply endpoint allowlist filtering
        config_pats = parse_config_allowlist(settings.ALLOW_ENDPOINTS_GRPC)
        # Empty tuple (from X-Allow-Endpoints: []) treated as "no constraint" — not "block all"
        header_pats = ctx.allow_endpoints or None
        if config_pats is not None or header_pats is not None:
            total_methods = sum(len(s.methods) for s in schema.services)
            filtered_services = filter_grpc_services(schema.services, config_pats, header_pats)
            allowed_methods = sum(len(s.methods) for s in filtered_services)
            logger.info(
                "endpoint_allowlist_applied",
                allowed=allowed_methods,
                total=total_methods,
                protocol="grpc",
            )
            if not filtered_services:
                return {
                    "ok": False,
                    "data": None,
                    "rpc_calls": [],
                    "error": "No gRPC methods match the configured endpoint allowlist. "
                    "Check ALLOW_ENDPOINTS_GRPC config and X-Allow-Endpoints header.",
                }
            filtered_text = build_schema_text(filtered_services, schema.pool)
            schema = GrpcSchema(
                services=filtered_services,
                pool=schema.pool,
                raw_schema_text=filtered_text,
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

        # Package config and run orchestration
        config = ProtocolConfig(
            agent_type="grpc",
            log_prefix="[gRPC]",
            call_key="rpc_calls",
            ctx_vars=_ctx_vars,
            unreduced_schema_text=schema.raw_schema_text,
            raw_schema=schema.raw_schema_text,
            provider=provider,
            tools=tools,
            instructions=instructions,
            api_id=build_api_id(ctx, "grpc"),
            recipe_step_executor_factory=_make_grpc_step_executor_factory(ctx, schema),
            recipe_item_key="executed_calls",
        )

        result = await run_agent_orchestration(question, config)

        # Recipe extraction (stays here to preserve monkeypatch targets).
        # Uses values captured from OrchestrationResult since orchestration
        # runs in an isolated context copy.
        if result.should_extract_recipe:
            await maybe_extract_and_save_recipe(
                api_type="grpc",
                api_id=build_api_id(ctx, "grpc"),
                question=question,
                steps=result.recipe_steps,
                sql_steps=result.sql_steps,
                raw_schema=result.raw_schema_value,
            )

        return result.result_dict

    except Exception as e:
        status = "error"
        logger.exception("grpc_agent_error")
        return {
            "ok": False,
            "data": None,
            "rpc_calls": [],
            "error": sanitize_error(e),
        }
    finally:
        record_request("grpc", status, (time.monotonic() - t0) * 1000)
