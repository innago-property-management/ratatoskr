"""MCP facade agent — NL queries against MCP servers via tool-calling loop."""

import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, AsyncIterator

import structlog

from ..config import settings
from ..context import RequestContext
from ..executor import extract_tables_from_response
from ..filtering import is_endpoint_allowed, parse_config_allowlist
from ..llm.tools import tool
from ..mcp.discovery import resolve_target
from ..metrics import record_request, record_schema_fetch
from ..recipe import (
    _set_return_directly,
    build_api_id,
    maybe_extract_and_save_recipe,
)
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

_log = make_logger("[MCP]")

# Context-local storage (isolated per async request)
_mcp_calls: ContextVar[list[dict[str, Any]]] = ContextVar("mcp_mcp_calls")
_recipe_steps: ContextVar[list[dict[str, Any]]] = ContextVar("mcp_recipe_steps")
_query_results: ContextVar[dict[str, Any]] = ContextVar("mcp_query_results")
_last_result: ContextVar[list] = ContextVar("mcp_last_result")
_raw_schema: ContextVar[str] = ContextVar("mcp_raw_schema")
_sql_steps: ContextVar[list[str]] = ContextVar("mcp_sql_steps")

# Bundle for orchestrator
_ctx_vars = AgentContextVars(
    api_calls=_mcp_calls,
    recipe_steps=_recipe_steps,
    query_results=_query_results,
    last_result=_last_result,
    raw_schema=_raw_schema,
    sql_steps=_sql_steps,
)


# ---------------------------------------------------------------------------
# Schema text builder
# ---------------------------------------------------------------------------


def _build_schema_text(tool_defs: list[Any]) -> str:
    """Render a compact tool list for the LLM context.

    Format per tool:
        tool_name(param!: type, optional?: type) — description (truncated to 120 chars)

    Required params are marked with ``!``, optional with ``?``.
    """
    lines: list[str] = ["<tools>"]
    for td in tool_defs:
        schema = td.inputSchema
        props = getattr(schema, "properties", None) or {}
        required = set(getattr(schema, "required", None) or [])

        param_parts: list[str] = []
        for pname, pdef in props.items():
            ptype = pdef.get("type", "any") if isinstance(pdef, dict) else "any"
            marker = "!" if pname in required else "?"
            param_parts.append(f"{pname}{marker}: {ptype}")

        params_str = ", ".join(param_parts)
        desc = (td.description or "")[:120]
        lines.append(f"  {td.name}({params_str}) — {desc}")

    lines.append("</tools>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _build_system_prompt() -> str:
    """Build system prompt for MCP facade agent."""
    current_date = datetime.now().strftime("%Y-%m-%d")

    return f"""You are an MCP facade agent. You answer questions by calling MCP tools on a remote MCP server and returning their results.

{SQL_RULES}

## MCP-Specific
- Use exact tool names as shown in <tools> below
- Arguments must be provided as a JSON string matching the tool's parameter schema
- Required parameters are marked with !, optional with ?
- Use mcp_call to invoke a single MCP tool

<tools>
mcp_call(tool_name, arguments?, name?, return_directly?)
  Call an MCP tool by name.
  - tool_name: exact tool name from the <tools> list
  - arguments: JSON string of arguments matching the tool's schema (default: "{{}}")
  - name: table name for sql_query (default: "data")
  - return_directly: skip LLM analysis, return raw data directly

{SQL_TOOL_DESC}

{SEARCH_TOOL_DESC}
</tools>

<workflow>
1. Read the available <tools> listed in the schema
2. Identify the correct tool for the question
3. Build arguments JSON matching the tool's schema
4. Execute mcp_call()
5. Use sql_query to filter/aggregate if needed
</workflow>

{CONTEXT_SECTION.format(current_date=current_date, max_turns=settings.MAX_AGENT_TURNS)}

{DECISION_GUIDANCE}

{UNCERTAINTY_SPEC}

{PERSISTENCE_SPEC.format(max_turns=settings.MAX_AGENT_TURNS)}

{EFFECTIVE_PATTERNS}

{TOOL_USAGE_RULES}

<examples>
Simple: mcp_call("list_repos", '{{"owner": "acme"}}')
With SQL: mcp_call("list_issues", '{{"repo": "api"}}', name="issues"); sql_query('SELECT COUNT(*) FROM issues WHERE state=\\'open\\'')
</examples>
"""


# ---------------------------------------------------------------------------
# Shared MCP tool invocation helper
# ---------------------------------------------------------------------------


async def _invoke_mcp_tool(
    session: Any, tool_name: str, args_dict: dict[str, Any]
) -> dict[str, Any]:
    """Call an MCP tool and normalize its output.

    Returns a dict with keys: success, error, data, raw_text.
    """
    try:
        with trace_span("mcp.call_tool", {"tool_name": tool_name}):
            call_result = await session.call_tool(tool_name, args_dict)
    except Exception as e:
        return {
            "success": False,
            "error": f"MCP tool call failed: {sanitize_error(e)}",
            "data": None,
            "raw_text": "",
        }

    # Extract text content from MCP result
    content_parts: list[str] = []
    if hasattr(call_result, "content") and call_result.content:
        for item in call_result.content:
            if getattr(item, "type", None) == "text":
                content_parts.append(getattr(item, "text", ""))
            elif hasattr(item, "text"):
                content_parts.append(item.text)

    raw_text = "\n".join(content_parts) if content_parts else "{}"

    # Normalize data
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        data = {"text": raw_text}

    is_error = bool(getattr(call_result, "isError", False))
    return {
        "success": not is_error,
        "error": raw_text if is_error else None,
        "data": data if not is_error else None,
        "raw_text": raw_text,
    }


# ---------------------------------------------------------------------------
# MCP call tool factory
# ---------------------------------------------------------------------------


def _create_mcp_call_tool(
    session: Any,
    allowed_tool_defs: list[Any],
    ctx_vars: AgentContextVars,
) -> Any:
    """Create mcp_call tool bound to the given session and allowed tools."""

    allowed_names: set[str] = {td.name for td in allowed_tool_defs}

    async def mcp_call(
        tool_name: str,
        arguments: str = "{}",
        name: str = "data",
        return_directly: bool = False,
    ) -> str:
        """Call an MCP tool by name and return the result.

        Args:
            tool_name: Name of the MCP tool to invoke (from schema list)
            arguments: JSON string of tool arguments
            name: Table name for sql_query (default: "data")
            return_directly: Return raw data directly without LLM processing

        Returns:
            JSON string with tool result
        """
        if tool_name not in allowed_names:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Tool '{tool_name}' not found. Available tools: {sorted(allowed_names)}"
                    ),
                }
            )

        try:
            args_dict = json.loads(arguments) if arguments else {}
            if not isinstance(args_dict, dict):
                return json.dumps({"success": False, "error": "arguments must be a JSON object"})
        except json.JSONDecodeError as e:
            return json.dumps({"success": False, "error": f"Invalid arguments JSON: {e.msg}"})

        result = await _invoke_mcp_tool(session, tool_name, args_dict)

        # Track call
        safe_append_contextvar_list(
            _mcp_calls,
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "name": name,
                "success": bool(result["success"]),
            },
        )

        # Store result for sql_query
        stored_data, schema_info = None, None
        if result["success"]:
            stored_data, schema_info = store_result(ctx_vars, result["data"], name)

            # Track for recipe extraction
            safe_append_contextvar_list(
                _recipe_steps,
                {
                    "kind": "mcp",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "name": name,
                },
            )

        _log(f"CALL {tool_name} -> {json.dumps(result)[:200]}")

        if return_directly and result["success"]:
            _set_return_directly()

        return await format_tool_response(stored_data, schema_info, name, result)

    return tool(mcp_call)


# ---------------------------------------------------------------------------
# Session factory (monkeypatch seam)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _open_session(descriptor: Any) -> AsyncIterator[Any]:
    """Open an MCP ClientSession for the given descriptor.

    This thin function is the monkeypatch seam for tests — replace it to
    avoid spawning real MCP processes.

    - transport="stdio" → mcp_stdio_session(StdioServerParameters)
    - transport="sse"   → mcp_http_session(url)
    """
    from ..mcp.client import mcp_http_session, mcp_stdio_session

    if descriptor.transport == "stdio":
        from mcp.client.stdio import StdioServerParameters

        params = StdioServerParameters(
            command=descriptor.command,
            args=list(descriptor.args),
            env=dict(descriptor.env) if descriptor.env else None,
        )
        async with mcp_stdio_session(params) as session:
            yield session
    else:
        async with mcp_http_session(descriptor.url) as session:
            yield session


# Module-level shared tools
search_schema = create_search_schema_tool(_raw_schema)
sql_query_tool = create_sql_query_tool(_ctx_vars, _log, "Call mcp_call first.")


# ---------------------------------------------------------------------------
# Recipe step executor (B2)
# ---------------------------------------------------------------------------


def _make_mcp_step_executor_factory(session: Any):
    """Build an MCP step executor factory for recipe tools.

    Returns a factory: (recipe_id) -> async step_executor
    """
    from ..recipe.store import render_text_template

    def factory(recipe_id: str):
        async def mcp_step_executor(step_idx, step, params, results):
            if not isinstance(step, dict) or step.get("kind") != "mcp":
                return (
                    False,
                    None,
                    json.dumps({"success": False, "error": "invalid recipe step kind"}, indent=2),
                    None,
                )

            tool_name = step.get("tool_name", "")
            name = str(step.get("name") or "data")

            # Render arguments template
            tmpl = step.get("arguments", "{}")
            if not isinstance(tmpl, str):
                tmpl = json.dumps(tmpl) if tmpl else "{}"
            rendered_args = render_text_template(tmpl, params)

            try:
                args_dict = json.loads(rendered_args)
            except json.JSONDecodeError:
                return (
                    False,
                    None,
                    json.dumps(
                        {
                            "success": False,
                            "error": f"Invalid arguments after template rendering: {rendered_args[:100]}",
                        },
                        indent=2,
                    ),
                    None,
                )

            result = await _invoke_mcp_tool(session, tool_name, args_dict)
            if not result["success"]:
                return (
                    False,
                    None,
                    json.dumps(
                        {"success": False, "error": result["error"] or result["raw_text"]},
                        indent=2,
                    ),
                    None,
                )

            tables, _ = extract_tables_from_response(result["data"], name)
            results.update(tables)
            _query_results.set(results)

            call_rec = {
                "tool_name": tool_name,
                "arguments": rendered_args,
                "name": name,
                "success": True,
            }
            safe_append_contextvar_list(_mcp_calls, call_rec)
            return True, tables.get(name), "", call_rec

        return mcp_step_executor

    return factory


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


async def process_mcp_query(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Process a natural language query against an MCP server.

    Args:
        question: Natural language question
        ctx: Request context with target_url (mcp:// or http(s)://) and headers
    """
    t0 = time.monotonic()
    status = "ok"
    try:
        return await _process_mcp_query_inner(question, ctx)
    except Exception as e:
        status = "error"
        logger.exception("mcp_agent_error")
        return {
            "ok": False,
            "data": None,
            "mcp_calls": [],
            "error": sanitize_error(e),
        }
    finally:
        record_request("mcp", status, (time.monotonic() - t0) * 1000)


async def _process_mcp_query_inner(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Inner MCP query handler — resolves target, opens session, runs orchestration."""
    # Resolve descriptor
    try:
        descriptor = await resolve_target(ctx.target_url, settings)
    except Exception as e:
        return {
            "ok": False,
            "data": None,
            "mcp_calls": [],
            "error": f"Failed to resolve MCP target '{ctx.target_url}': {sanitize_error(e)}",
        }

    # Open MCP session
    try:
        async with _open_session(descriptor) as session:
            return await _run_with_session(question, ctx, session)
    except Exception as e:
        return {
            "ok": False,
            "data": None,
            "mcp_calls": [],
            "error": f"Failed to connect to MCP server: {sanitize_error(e)}",
        }


async def _run_with_session(
    question: str,
    ctx: RequestContext,
    session: Any,
) -> dict[str, Any]:
    """Run agent orchestration against an open MCP session."""
    # Fetch tool list (schema)
    t_schema = time.monotonic()
    try:
        with trace_span("schema.fetch", {"protocol": "mcp"}):
            tools_result = await session.list_tools()
        tool_defs = getattr(tools_result, "tools", []) or []
    except Exception as e:
        return {
            "ok": False,
            "data": None,
            "mcp_calls": [],
            "error": f"Failed to list MCP tools: {sanitize_error(e)}",
        }
    record_schema_fetch((time.monotonic() - t_schema) * 1000, "mcp")

    if not tool_defs:
        return {
            "ok": False,
            "data": None,
            "mcp_calls": [],
            "error": "MCP server returned no tools.",
        }

    # Apply endpoint allowlist
    config_pats = parse_config_allowlist(settings.ALLOW_ENDPOINTS_MCP)
    header_pats = ctx.allow_endpoints or None

    if config_pats is not None or header_pats is not None:
        total = len(tool_defs)
        tool_defs = [
            td for td in tool_defs if is_endpoint_allowed(td.name, config_pats, header_pats)
        ]
        logger.info(
            "endpoint_allowlist_applied",
            allowed=len(tool_defs),
            total=total,
            protocol="mcp",
        )
        if not tool_defs:
            return {
                "ok": False,
                "data": None,
                "mcp_calls": [],
                "error": (
                    "No MCP tools match the configured endpoint allowlist filter. "
                    "Check ALLOW_ENDPOINTS_MCP config and X-Allow-Endpoints header."
                ),
            }

    # Build schema text and create tools
    schema_text = _build_schema_text(tool_defs)
    mcp_call_tool = _create_mcp_call_tool(session, tool_defs, _ctx_vars)
    agent_tools = [mcp_call_tool, sql_query_tool, search_schema]

    # Package config and run orchestration
    config = ProtocolConfig(
        agent_type="mcp",
        log_prefix="[MCP]",
        call_key="mcp_calls",
        ctx_vars=_ctx_vars,
        schema_text=schema_text,
        raw_schema=schema_text,
        provider=provider,
        tools=agent_tools,
        instructions=_build_system_prompt(),
        api_id=build_api_id(ctx, "mcp"),
        recipe_step_executor_factory=_make_mcp_step_executor_factory(session),
        recipe_item_key="executed_calls",
    )

    result = await run_agent_orchestration(question, config)

    # Recipe extraction (stays here to preserve monkeypatch targets)
    if result.should_extract_recipe:
        await maybe_extract_and_save_recipe(
            api_type="mcp",
            api_id=build_api_id(ctx, "mcp"),
            question=question,
            steps=result.recipe_steps,
            sql_steps=result.sql_steps,
            raw_schema=result.raw_schema_value,
        )

    return result.result_dict
