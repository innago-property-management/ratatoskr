"""REST agent using declarative queries (REST API + DuckDB SQL)."""

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from ..config import settings
from ..context import RequestContext
from ..executor import extract_tables_from_response, truncate_for_context
from ..filtering import filter_openapi_spec, parse_config_allowlist
from ..llm.tools import tool
from ..recipe import (
    _set_return_directly,
    build_api_id,
    maybe_extract_and_save_recipe,
    render_param_refs,
)
from ..rest.client import execute_request
from ..rest.schema_loader import fetch_schema_context
from .contextvar_utils import safe_append_contextvar_list, safe_get_contextvar
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
    OPTIONAL_PARAMS_SPEC,
    PERSISTENCE_SPEC,
    REST_SCHEMA_NOTATION,
    REST_TOOL_DESC,
    SEARCH_TOOL_DESC,
    SQL_RULES,
    SQL_TOOL_DESC,
    TOOL_USAGE_RULES,
    UNCERTAINTY_SPEC,
)
from .schema_search import create_search_schema_tool

logger = logging.getLogger(__name__)

_log = make_logger("[REST]")

# Context-local storage (isolated per async request)
# NOTE: Use mutable containers for values that need to be modified by tool functions,
# because ContextVar.set() in child tasks (task groups) doesn't propagate to parent.
_rest_calls: ContextVar[list[dict[str, Any]]] = ContextVar("rest_calls")
_recipe_steps: ContextVar[list[dict[str, Any]]] = ContextVar("recipe_steps")
_query_results: ContextVar[dict[str, Any]] = ContextVar("query_results")
_last_result: ContextVar[list] = ContextVar("last_result")  # Mutable container: [result_value]
_raw_schema: ContextVar[str] = ContextVar("raw_schema")  # Raw OpenAPI JSON for search
_sql_steps: ContextVar[list[str]] = ContextVar("sql_steps")

# Bundle for orchestrator
_ctx_vars = AgentContextVars(
    api_calls=_rest_calls,
    recipe_steps=_recipe_steps,
    query_results=_query_results,
    last_result=_last_result,
    raw_schema=_raw_schema,
    sql_steps=_sql_steps,
)


# ---------------------------------------------------------------------------
# REST-specific: polling utilities
# ---------------------------------------------------------------------------


def _get_nested_value(data: dict | None, path: str) -> Any:
    """Extract value from nested dict/list using dot notation.

    Args:
        data: Dictionary to extract from
        path: Dot-separated path (e.g., "polling.completed", "trips.0.isCompleted")

    Returns:
        Value at path or None if not found
    """
    if not data or not path:
        return None
    keys = path.split(".")
    current: Any = data
    for key in keys:
        if not isinstance(current, (dict, list)):
            return None
        if isinstance(current, list) and key.isdigit():
            idx = int(key)
            if 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if current is None:
            return None
    return current


def _set_nested_value(data: dict, path: str, value: Any) -> None:
    """Set value in nested dict using dot notation, creating intermediate dicts.

    Args:
        data: Dictionary to modify
        path: Dot-separated path (e.g., "polling.count")
        value: Value to set
    """
    if not path:
        return
    keys = path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


# ---------------------------------------------------------------------------
# REST-specific: system prompt
# ---------------------------------------------------------------------------


def _build_system_prompt(poll_paths: tuple[str, ...] = ()) -> str:
    """Build system prompt for REST agent.

    Args:
        poll_paths: Paths that require polling (empty = no polling support)
    """
    current_date = datetime.now().strftime("%Y-%m-%d")

    poll_tool_desc = ""
    poll_rules = ""
    if poll_paths:
        paths_str = ", ".join(poll_paths)
        poll_tool_desc = f"""
poll_until_done(method, path, done_field, done_value, body?, name?, delay_ms?)
  Poll async API until done_field equals done_value.
  - done_field: dot-path (e.g., "status", "data.0.complete", "trips.0.isCompleted")
  - done_value: target value as string ("true", "COMPLETED")
  - delay_ms: ms between polls (default: {settings.DEFAULT_POLL_DELAY_MS}ms)
  - Auto-increments polling.count if present in body
  Max {settings.MAX_POLLS} polls. Polling paths: {paths_str}
"""
        poll_rules = f"""
<polling-required>
IMPORTANT: These paths are ASYNC and REQUIRE polling: {paths_str}
- You MUST use poll_until_done (NOT rest_call) for these paths
- rest_call will fail or return incomplete data for polling paths
- Check schema for the completion field (e.g., isCompleted, status, done)
</polling-required>
"""

    # Conditionally add polling example
    poll_example = ""
    if poll_paths:
        poll_example = f"""Polling: poll_until_done("POST", "{poll_paths[0]}", done_field="isCompleted", done_value="true", body='{{...}}')
"""

    workflow_start = "1"

    return f"""You are a REST API agent that answers questions by querying APIs and returning data.

{SQL_RULES}

<tools>
{REST_TOOL_DESC}
{poll_tool_desc}
{SQL_TOOL_DESC}

{SEARCH_TOOL_DESC}
</tools>
<workflow>
{workflow_start}. Read <endpoints> and <schemas> below
{int(workflow_start) + 1}. Check if endpoint is in polling paths - if yes, use poll_until_done; otherwise use rest_call
{int(workflow_start) + 2}. Use sql_query to filter/aggregate results
</workflow>

{CONTEXT_SECTION.format(current_date=current_date, max_turns=settings.MAX_AGENT_TURNS)}

{DECISION_GUIDANCE}

{REST_SCHEMA_NOTATION}
{poll_rules}
{UNCERTAINTY_SPEC}

{OPTIONAL_PARAMS_SPEC}

{PERSISTENCE_SPEC.format(max_turns=settings.MAX_AGENT_TURNS)}

{EFFECTIVE_PATTERNS}

{TOOL_USAGE_RULES}

<examples>
GET: rest_call("GET", "/users", query_params='{{"limit": 10}}')
Path param: rest_call("GET", "/users/{{{{id}}}}", path_params='{{"id": "123"}}')
{poll_example}Join: rest_call("GET", "/users", name="u"); rest_call("GET", "/posts", name="p"); sql_query('SELECT u.name, p.title FROM u JOIN p ON u.id = p.userId')
</examples>
"""


# ---------------------------------------------------------------------------
# REST-specific: API call tools
# ---------------------------------------------------------------------------


def _create_rest_call_tool(ctx: RequestContext, base_url: str) -> Any:
    """Create rest_call tool with bound context."""

    async def rest_call(
        method: str,
        path: str,
        path_params: str = "",
        query_params: str = "",
        body: str = "",
        name: str = "data",
        return_directly: bool = False,
    ) -> str:
        """Execute REST API call and store result for sql_query.

        Args:
            method: HTTP method (GET recommended, others may be blocked)
            path: API path (e.g., /users/{id})
            path_params: JSON string for path values (e.g., '{"id": "123"}')
            query_params: JSON string for query params (e.g., '{"limit": 10}')
            body: JSON string for request body (e.g., '{"name": "John"}')
            name: Table name for sql_query (default: "data")
            return_directly: Skip LLM processing, return data directly to client.
                            Only applies on success. Errors still processed by LLM.

        Returns:
            JSON string with API response
        """
        # Parse JSON params
        pp = json.loads(path_params) if path_params else None
        qp = json.loads(query_params) if query_params else None
        bd = json.loads(body) if body else None

        result = await execute_request(
            method,
            path,
            pp,
            qp,
            bd,
            base_url=base_url,
            headers=ctx.target_headers,
            allow_unsafe_paths=list(ctx.allow_unsafe_paths),
        )

        # Track call
        safe_append_contextvar_list(
            _rest_calls,
            {
                "method": method,
                "path": path,
                "path_params": path_params,
                "query_params": query_params,
                "body": body,
                "name": name,
                "success": bool(result.get("success")),
            },
        )

        # Store result for sql_query
        stored_data, schema_info = None, None
        if result.get("success"):
            data = result.get("data", {})
            stored_data, schema_info = store_result(_ctx_vars, data, name)

            # Track successful step for recipe extraction
            safe_append_contextvar_list(
                _recipe_steps,
                {
                    "kind": "rest",
                    "name": name,
                    "method": method,
                    "path": path,
                    "path_params": pp,
                    "query_params": qp,
                    "body": bd,
                },
            )

        _log(f"RESULT {json.dumps(result)[:200]}")

        if return_directly and result.get("success"):
            _set_return_directly()

        # Add hints on failure to guide agent recovery
        if not result.get("success"):
            status = result.get("status_code", 0)
            if status >= 400:
                result["hint"] = "Use search_schema to find valid enum values or field names"

        return format_tool_response(stored_data, schema_info, name, result)

    return tool(rest_call)


def _create_poll_tool(ctx: RequestContext, base_url: str) -> Any:
    """Create poll_until_done tool with bound context."""

    async def poll_until_done(
        method: str,
        path: str,
        done_field: str,
        done_value: str,
        body: str = "",
        path_params: str = "",
        query_params: str = "",
        name: str = "poll_result",
        delay_ms: int = 0,
    ) -> str:
        """Poll endpoint until done_field equals done_value. Auto-increments polling.count if present.

        Args:
            method: HTTP method (POST typically)
            path: API path
            done_field: Dot-path to check (e.g., "status", "polling.completed", "trips.0.isCompleted")
            done_value: Value indicating done (e.g., "true", "0", "COMPLETED", "100")
            body: JSON string request body
            path_params: JSON string for path values
            query_params: JSON string for query params
            name: Table name for sql_query (default: poll_result)
            delay_ms: Delay between polls in ms (default: 3000ms)

        Returns:
            JSON string with final response or error
        """
        pp = json.loads(path_params) if path_params else None
        qp = json.loads(query_params) if query_params else None
        try:
            body_dict = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid body JSON: {e.msg}",
                }
            )

        # Internal defaults from config
        max_polls = settings.MAX_POLLS
        wait_ms = delay_ms if delay_ms > 0 else settings.DEFAULT_POLL_DELAY_MS
        current = None  # Track last done_field value for error messages

        attempt = 0
        while attempt < max_polls:
            attempt += 1

            result = await execute_request(
                method,
                path,
                pp,
                qp,
                body=body_dict if body_dict else None,
                base_url=base_url,
                headers=ctx.target_headers,
                allow_unsafe_paths=list(ctx.allow_unsafe_paths),
            )

            # Track call
            safe_append_contextvar_list(
                _rest_calls,
                {
                    "method": method,
                    "path": path,
                    "path_params": path_params,
                    "query_params": query_params,
                    "body": json.dumps(body_dict) if body_dict else "",
                    "name": name,
                    "poll_attempt": attempt,
                    "success": bool(result.get("success")),
                },
            )

            if not result.get("success"):
                return json.dumps(
                    {
                        "success": False,
                        "error": result.get("error"),
                        "attempt": attempt,
                    }
                )

            data = result.get("data", {})

            # Validate done_field exists on first response
            current = _get_nested_value(data, done_field)
            if current is None and attempt == 1:
                keys = list(data.keys()) if isinstance(data, dict) else []
                return json.dumps(
                    {
                        "success": False,
                        "error": f"done_field '{done_field}' not found in response. Available keys: {keys}",
                    }
                )

            # Check if done_field value matches done_value (string comparison)
            is_done = str(current).lower() == done_value.lower()

            if is_done:
                # Store result for sql_query
                store_result(_ctx_vars, data, name)

                return json.dumps(
                    {
                        "success": True,
                        **truncate_for_context(data if isinstance(data, list) else [data], name),
                        "attempts": attempt,
                    },
                    indent=2,
                )

            await asyncio.sleep(wait_ms / 1000)

            # Auto-increment polling.count if present in body
            if body_dict.get("polling", {}).get("count") is not None:
                body_dict["polling"]["count"] += 1

        return json.dumps(
            {
                "success": False,
                "error": f"max_polls ({max_polls}) exceeded. Last {done_field} value: {current} (expected: {done_value})",
                "attempts": attempt,
            }
        )

    return tool(poll_until_done)


# Create search_schema tool bound to REST schema context var
search_schema = create_search_schema_tool(_raw_schema)

# Create sql_query tool via shared factory
sql_query_tool = create_sql_query_tool(_ctx_vars, _log, "Call rest_call first.")


# ---------------------------------------------------------------------------
# REST-specific: recipe step executor
# ---------------------------------------------------------------------------


def _make_rest_step_executor_factory(ctx: RequestContext, base_url: str):
    """Build a REST step executor factory for recipe tools.

    Returns a factory: (recipe_id) -> async step_executor
    """

    def factory(recipe_id: str):
        async def rest_step_executor(step_idx, step, params, results):
            if not isinstance(step, dict) or step.get("kind") != "rest":
                return (
                    False,
                    None,
                    json.dumps({"success": False, "error": "invalid recipe step"}, indent=2),
                    None,
                )

            method = str(step.get("method", "GET")).upper()
            path = str(step.get("path", ""))
            name = str(step.get("name") or "data")

            pp = render_param_refs(step.get("path_params") or {}, params)
            qp = render_param_refs(step.get("query_params") or {}, params)
            bd = render_param_refs(step.get("body") or {}, params)

            res = await execute_request(
                method,
                path,
                pp if isinstance(pp, dict) else None,
                qp if isinstance(qp, dict) else None,
                bd if isinstance(bd, dict) and bd else None,
                base_url=base_url,
                headers=ctx.target_headers,
                allow_unsafe_paths=list(ctx.allow_unsafe_paths),
            )
            if not res.get("success"):
                return (
                    False,
                    None,
                    json.dumps(
                        {"success": False, "error": res.get("error", "request failed")},
                        indent=2,
                    ),
                    None,
                )

            data = res.get("data", {})
            tables, _ = extract_tables_from_response(data, name)
            results.update(tables)
            _query_results.set(results)

            call_rec = {
                "method": method,
                "path": path,
                "path_params": json.dumps(pp) if pp else "",
                "query_params": json.dumps(qp) if qp else "",
                "body": json.dumps(bd) if bd else "",
                "name": name,
                "success": True,
            }
            safe_append_contextvar_list(_rest_calls, call_rec)
            return True, tables.get(name), "", call_rec

        return rest_step_executor

    return factory


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def process_rest_query(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Process natural language query against REST API.

    Args:
        question: Natural language question
        ctx: Request context with target_url (OpenAPI spec) and target_headers
    """
    try:
        # Fetch schema context (target_url = OpenAPI spec URL)
        # Build spec filter from config + header allowlist
        config_pats = parse_config_allowlist(settings.ALLOW_ENDPOINTS_REST)
        header_pats = ctx.allow_endpoints or None
        spec_filter = None
        _filter_stats: dict[str, int] = {}  # captures total/allowed from inside closure
        if config_pats is not None or header_pats is not None:

            def spec_filter(spec: dict) -> dict:
                # Count total ops before filtering for logging
                pre_paths = spec.get("paths", {})
                _filter_stats["total"] = sum(
                    1 for p in pre_paths.values() if isinstance(p, dict)
                    for m in ("get", "post", "put", "delete", "patch") if m in p
                )
                filtered = filter_openapi_spec(spec, config_pats, header_pats)
                post_paths = filtered.get("paths", {})
                _filter_stats["allowed"] = sum(
                    1 for p in post_paths.values() if isinstance(p, dict)
                    for m in ("get", "post", "put", "delete", "patch") if m in p
                )
                return filtered

        schema_ctx, spec_base_url, raw_spec_json = await fetch_schema_context(
            ctx.target_url, ctx.target_headers, spec_filter=spec_filter
        )

        # Check if allowlist filtered out all endpoints (log stats + early return)
        if spec_filter is not None and _filter_stats:
            logger.info(
                "Endpoint allowlist: %d/%d REST operations allowed",
                _filter_stats.get("allowed", 0),
                _filter_stats.get("total", 0),
            )
            if _filter_stats.get("allowed", 0) == 0:
                return {
                    "ok": False,
                    "data": None,
                    "api_calls": [],
                    "error": "No REST endpoints match the configured endpoint allowlist. "
                    "Check ALLOW_ENDPOINTS_REST config and X-Allow-Endpoints header.",
                }

        # Use header override or spec-derived base URL
        base_url = ctx.base_url or spec_base_url
        if not base_url:
            return {
                "ok": False,
                "data": None,
                "api_calls": [],
                "error": "Could not determine base URL. Set X-Base-URL header or ensure spec has 'servers' field.",
            }

        # Create protocol-specific tools
        rest_tool = _create_rest_call_tool(ctx, base_url)
        tools = [rest_tool, sql_query_tool, search_schema]

        # Only include poll tool if user specified poll_paths header
        if ctx.poll_paths:
            poll_tool = _create_poll_tool(ctx, base_url)
            tools.insert(1, poll_tool)

        # Build system prompt
        instructions = _build_system_prompt(poll_paths=ctx.poll_paths)

        # Package config and run orchestration
        config = ProtocolConfig(
            agent_type="rest",
            log_prefix="[REST]",
            call_key="api_calls",
            ctx_vars=_ctx_vars,
            schema_text=schema_ctx,
            raw_schema=raw_spec_json,
            provider=provider,
            tools=tools,
            instructions=instructions,
            api_id=build_api_id(ctx, "rest", base_url),
            recipe_step_executor_factory=_make_rest_step_executor_factory(ctx, base_url),
            recipe_item_key="executed_calls",
        )

        result = await run_agent_orchestration(question, config)

        # Recipe extraction (stays here to preserve monkeypatch targets)
        if result.should_extract_recipe:
            skip_polling = any("poll_attempt" in c for c in safe_get_contextvar(_rest_calls, []))
            await maybe_extract_and_save_recipe(
                api_type="rest",
                api_id=build_api_id(ctx, "rest", base_url),
                question=question,
                steps=safe_get_contextvar(_recipe_steps, []),
                sql_steps=safe_get_contextvar(_sql_steps, []),
                raw_schema=safe_get_contextvar(_raw_schema, ""),
                skip_condition=skip_polling,
            )

        return result.result_dict

    except Exception as e:
        logger.exception("REST Agent error")
        return {
            "ok": False,
            "data": None,
            "api_calls": [],
            "error": str(e),
        }
