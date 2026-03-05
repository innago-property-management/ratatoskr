"""GraphQL agent using declarative queries (GraphQL + DuckDB SQL)."""

import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from ..config import settings
from ..context import RequestContext
from ..filtering import filter_graphql_schema, parse_config_allowlist
from ..graphql import execute_query as graphql_fetch
from ..metrics import record_request, record_schema_fetch
from ..recipe import (
    _set_return_directly,
    build_api_id,
    maybe_extract_and_save_recipe,
    render_text_template,
)
from ..sanitize import sanitize_error, sanitize_schema_text
from ..schema.reducer import reduce_schema
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
    GRAPHQL_SCHEMA_NOTATION,
    OPTIONAL_PARAMS_SPEC,
    PERSISTENCE_SPEC,
    SEARCH_TOOL_DESC,
    SQL_RULES,
    SQL_TOOL_DESC,
    TOOL_USAGE_RULES,
    UNCERTAINTY_SPEC,
)
from .schema_search import create_search_schema_tool

logger = structlog.get_logger(__name__)

_log = make_logger("[GQL]")

# Context-local storage (isolated per async request)
# NOTE: Use mutable containers for values that need to be modified by tool functions,
# because ContextVar.set() in child tasks (task groups) doesn't propagate to parent.
_graphql_queries: ContextVar[list[str]] = ContextVar("graphql_queries")
_recipe_steps: ContextVar[list[dict[str, Any]]] = ContextVar("recipe_steps")
_query_results: ContextVar[dict[str, Any]] = ContextVar("query_results")
_last_result: ContextVar[list] = ContextVar("last_result")  # Mutable container: [result_value]
_raw_schema: ContextVar[str] = ContextVar("raw_schema")  # Raw introspection JSON for search
_sql_steps: ContextVar[list[str]] = ContextVar("sql_steps")

# Bundle for orchestrator
_ctx_vars = AgentContextVars(
    api_calls=_graphql_queries,
    recipe_steps=_recipe_steps,
    query_results=_query_results,
    last_result=_last_result,
    raw_schema=_raw_schema,
    sql_steps=_sql_steps,
)


# ---------------------------------------------------------------------------
# GraphQL-specific: schema introspection + formatting
# ---------------------------------------------------------------------------


def _format_type(t: dict | None) -> str:
    """Convert introspection type to compact notation: [User!]!"""
    if not t:
        return "?"
    kind = t.get("kind")
    name = t.get("name")
    inner = t.get("ofType")

    if kind == "NON_NULL":
        return f"{_format_type(inner)}!"
    if kind == "LIST":
        return f"[{_format_type(inner)}]"
    return name or "?"


_INTROSPECTION_QUERY = """{
  __schema {
    queryType {
      fields { name description args { name type { ...TypeRef } defaultValue } type { ...TypeRef } }
    }
    types {
      name kind description
      fields { name description args { name type { ...TypeRef } defaultValue } type { ...TypeRef } }
      enumValues { name description }
      inputFields { name type { ...TypeRef } defaultValue }
      interfaces { name }
      possibleTypes { name }
    }
  }
}
fragment TypeRef on __Type {
  name kind ofType { name kind ofType { name kind ofType { name } } }
}"""

# Shallow introspection for APIs with strict depth limits
_INTROSPECTION_QUERY_SHALLOW = """{
  __schema {
    queryType { fields { name args { name } type { name kind } } }
    types {
      name kind
      fields { name type { name kind } }
      inputFields { name }
      enumValues { name }
    }
  }
}"""


def _is_required(type_def: dict | None) -> bool:
    """Check if GraphQL type is required (NON_NULL wrapper)."""
    return type_def.get("kind") == "NON_NULL" if type_def else False


def _format_arg(a: dict) -> str:
    """Format argument with optional default value."""
    type_str = _format_type(a["type"])
    default = a.get("defaultValue")
    if default is not None:
        return f"{a['name']}: {type_str} = {default}"
    return f"{a['name']}: {type_str}"


def _filter_required_args(args: list[dict]) -> list[dict]:
    """Filter to only required arguments (NON_NULL type)."""
    return [a for a in args if _is_required(a.get("type"))]


def _format_field(fld: dict) -> str:
    """Format a field with optional args."""
    args = fld.get("args", [])
    if args:
        arg_str = "(" + ", ".join(_format_arg(a) for a in args) + ")"
    else:
        arg_str = ""
    raw_desc = fld.get("description")
    desc = f" # {sanitize_schema_text(raw_desc)}" if raw_desc else ""
    return f"  {fld['name']}{arg_str}: {_format_type(fld['type'])}{desc}"


def _build_schema_context(schema: dict) -> str:
    """Build compact SDL context from introspection schema."""
    queries = schema.get("queryType", {}).get("fields", [])
    all_types = [t for t in schema.get("types", []) if not t["name"].startswith("__")]

    objects = [
        t
        for t in all_types
        if t["kind"] == "OBJECT" and t["name"] not in ("Query", "Mutation", "Subscription")
    ]
    enums = [t for t in all_types if t["kind"] == "ENUM"]
    inputs = [t for t in all_types if t["kind"] == "INPUT_OBJECT"]
    interfaces = [t for t in all_types if t["kind"] == "INTERFACE"]
    unions = [t for t in all_types if t["kind"] == "UNION"]

    lines = ["<queries>"]
    for f in queries:
        raw_desc = f.get("description")
        desc = f" # {sanitize_schema_text(raw_desc)}" if raw_desc else ""
        # Only show required args
        required_args = _filter_required_args(f.get("args", []))
        args = ", ".join(_format_arg(a) for a in required_args)
        lines.append(f"{f['name']}({args}) -> {_format_type(f['type'])}{desc}")

    if interfaces:
        lines.append("\n<interfaces>")
        for t in interfaces:
            impl = [p["name"] for p in t.get("possibleTypes", []) or []]
            impl_str = f" # implemented by: {', '.join(impl)}" if impl else ""
            fields = [_format_field(fld) for fld in t.get("fields", []) or []]
            lines.append(f"{t['name']} {{{impl_str}\n" + "\n".join(fields) + "\n}")

    if unions:
        lines.append("\n<unions>")
        for t in unions:
            types = [p["name"] for p in t.get("possibleTypes", []) or []]
            lines.append(f"{t['name']}: {' | '.join(types)}")

    lines.append("\n<types>")
    for t in objects:
        impl = [i["name"] for i in t.get("interfaces", []) or []]
        impl_str = f" implements {', '.join(impl)}" if impl else ""
        fields = [_format_field(fld) for fld in t.get("fields", []) or []]
        lines.append(f"{t['name']}{impl_str} {{\n" + "\n".join(fields) + "\n}")

    lines.append("\n<enums>")
    for e in enums:
        vals = " | ".join(v["name"] for v in e.get("enumValues", []))
        lines.append(f"{e['name']}: {vals}")

    lines.append("\n<inputs>")
    for inp in inputs:
        # Only show required input fields
        required_fields = [
            f for f in (inp.get("inputFields", []) or []) if _is_required(f.get("type"))
        ]
        fields = ", ".join(f"{f['name']}: {_format_type(f['type'])}" for f in required_fields)
        lines.append(f"{inp['name']} {{ {fields} }}")

    return "\n".join(lines)


def _strip_descriptions(context: str) -> str:
    """Strip # comments from SDL context."""
    return re.sub(r" #[^\n]*", "", context)


def _is_depth_limit_error(result: dict) -> bool:
    """Check if error is due to query depth limit (413)."""
    error = result.get("error", "")
    if isinstance(error, str):
        return "413" in error or "depth" in error.lower()
    if isinstance(error, list):
        return any("depth" in str(e).lower() for e in error)
    return False


@dataclass
class SchemaFetchResult:
    """Typed result from ``_fetch_schema_context``.

    Replaces a bare ``tuple[str, int | None, str]`` for clarity at call sites.
    """

    schema_context: str
    """Compact SDL context for the LLM prompt (may be reduced/truncated)."""

    allowed_field_count: int | None
    """Number of query fields after allowlist filtering.  ``None`` when no allowlist is active."""

    raw_schema_json: str
    """Full introspection JSON (filtered if allowlist active) for recipe matching."""


async def _fetch_schema_context(
    endpoint: str,
    headers: dict[str, str] | None,
    config_patterns: tuple[str, ...] | None = None,
    header_patterns: tuple[str, ...] | None = None,
    question: str = "",
) -> SchemaFetchResult:
    """Fetch schema in compact SDL format. Falls back to shallow query on depth limit.

    Args:
        endpoint: GraphQL endpoint URL
        headers: Optional auth headers
        config_patterns: Config-level allowlist patterns (fnmatch)
        header_patterns: Header-level allowlist patterns (fnmatch)
        question: User's NL question (guides smart schema reduction)

    Returns:
        SchemaFetchResult with schema_context, allowed_field_count, and raw_schema_json.
        allowed_field_count is None when no allowlist is active; 0+ when filtering applied.
        raw_schema_json is the full introspection JSON (filtered if allowlist active).
    """
    result = await graphql_fetch(_INTROSPECTION_QUERY, None, endpoint, headers)

    # Retry with shallow introspection if depth limit exceeded
    if not result.get("success") and _is_depth_limit_error(result):
        logger.info("graphql_introspection_retry", reason="depth_limit")
        result = await graphql_fetch(_INTROSPECTION_QUERY_SHALLOW, None, endpoint, headers)

    if not result.get("success") or not result.get("data"):
        return SchemaFetchResult(schema_context="", allowed_field_count=None, raw_schema_json="")

    schema = result["data"]["__schema"]
    allowed_count: int | None = None

    # Apply endpoint allowlist filter (if configured)
    if config_patterns is not None or header_patterns is not None:
        qt = schema.get("queryType") or {}
        total = len(qt.get("fields") or [])
        schema = filter_graphql_schema(schema, config_patterns, header_patterns)
        qt_filtered = schema.get("queryType") or {}
        allowed_count = len(qt_filtered.get("fields") or [])
        logger.info(
            "endpoint_allowlist_applied", allowed=allowed_count, total=total, protocol="graphql"
        )

    # Raw introspection JSON (filtered if allowlist active) — returned to caller,
    # NOT stored in ContextVar (avoids cross-request race conditions)
    raw_schema_json = json.dumps(schema, indent=2)

    # Build DSL for LLM context (from FILTERED schema)
    context = _build_schema_context(schema)

    # Pre-pass: strip descriptions if oversized (cheap, before TOON/Haiku)
    if len(context) > settings.MAX_SCHEMA_CHARS:
        context = _strip_descriptions(context)

    # Smart schema reduction (TOON + Haiku + hard truncation fallback)
    result = await reduce_schema(
        schema_text=context,
        question=question,
        threshold=settings.MAX_SCHEMA_CHARS,
        api_key=settings.SCHEMA_REDUCTION_API_KEY,
        model=settings.SCHEMA_REDUCTION_MODEL,
        timeout_ms=settings.SCHEMA_REDUCTION_TIMEOUT_MS,
        enabled=settings.SCHEMA_REDUCTION_ENABLED,
        max_input_chars=settings.SCHEMA_REDUCTION_MAX_INPUT_CHARS,
        max_output_tokens=settings.SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS,
    )
    context = result.schema_text

    return SchemaFetchResult(
        schema_context=context,
        allowed_field_count=allowed_count,
        raw_schema_json=raw_schema_json,
    )


async def fetch_graphql_schema_raw(endpoint: str, headers: dict[str, str] | None) -> str:
    """Fetch raw GraphQL schema JSON for matching and validation."""
    result = await graphql_fetch(_INTROSPECTION_QUERY, None, endpoint, headers)

    if not result.get("success") and _is_depth_limit_error(result):
        logger.info("graphql_introspection_retry", reason="depth_limit")
        result = await graphql_fetch(_INTROSPECTION_QUERY_SHALLOW, None, endpoint, headers)

    if not result.get("success") or not result.get("data"):
        return ""

    schema = result["data"]["__schema"]
    return json.dumps(schema, indent=2)


# ---------------------------------------------------------------------------
# GraphQL-specific: system prompt
# ---------------------------------------------------------------------------


def _build_system_prompt(recipe_context: str = "") -> str:
    """Build system prompt for GraphQL agent."""
    current_date = datetime.now().strftime("%Y-%m-%d")

    workflow_start = "1"

    return f"""You are a GraphQL API agent that answers questions by querying APIs and returning data.

{SQL_RULES}

## GraphQL-Specific
- Use inline values, never $variables

<tools>
graphql_query(query, name?, return_directly?)
  Execute GraphQL query. Result stored as DuckDB table.
  - return_directly: Skip LLM analysis, return raw data directly to user

{SQL_TOOL_DESC}

{SEARCH_TOOL_DESC}
</tools>
<workflow>
{workflow_start}. Read <queries> and <types> provided below
{int(workflow_start) + 1}. Execute graphql_query with needed fields
{int(workflow_start) + 2}. If user needs filtering/aggregation → sql_query, else return data
</workflow>

{CONTEXT_SECTION.format(current_date=current_date, max_turns=settings.MAX_AGENT_TURNS)}

{recipe_context}

{DECISION_GUIDANCE}

{GRAPHQL_SCHEMA_NOTATION}

{UNCERTAINTY_SPEC}

{OPTIONAL_PARAMS_SPEC}

{PERSISTENCE_SPEC.format(max_turns=settings.MAX_AGENT_TURNS)}

{EFFECTIVE_PATTERNS}

{TOOL_USAGE_RULES}

<examples>
Simple: graphql_query('{{ users(limit: 10) {{ id name }} }}')
Aggregation: graphql_query('{{ posts {{ authorId views }} }}'); sql_query('SELECT authorId, SUM(views) as total FROM data GROUP BY authorId')
Join: graphql_query('{{ users {{ id name }} }}', name='u'); graphql_query('{{ posts {{ authorId title }} }}', name='p'); sql_query('SELECT u.name, p.title FROM u JOIN p ON u.id = p.authorId')
</examples>
"""


# ---------------------------------------------------------------------------
# GraphQL-specific: API call tool
# ---------------------------------------------------------------------------


def _create_graphql_query_tool(ctx: RequestContext) -> Any:
    """Create graphql_query tool with bound context."""

    async def graphql_query(query: str, name: str = "data", return_directly: bool = False) -> str:
        """Execute GraphQL query and store result for sql_query.

        Args:
            query: GraphQL query string
            name: Table name for sql_query (default: "data")
            return_directly: Skip LLM processing, return data directly to client.
                            Only applies on success. Errors still processed by LLM.

        Returns:
            JSON string with query results
        """
        result = await graphql_fetch(query, None, ctx.target_url, ctx.target_headers)

        stored_data, schema_info = None, None
        if result.get("success"):
            data = result.get("data", {})
            stored_data, schema_info = store_result(_ctx_vars, data, name)

            # Track successful step for recipe extraction
            safe_append_contextvar_list(
                _recipe_steps, {"kind": "graphql", "query": query, "name": name}
            )

        safe_append_contextvar_list(_graphql_queries, query)

        _log(f"RESULT {json.dumps(result)[:200]}")

        if return_directly and result.get("success"):
            _set_return_directly()

        return await format_tool_response(stored_data, schema_info, name, result)

    from ..llm.tools import tool

    return tool(graphql_query)


# Create search_schema tool bound to GraphQL schema context var
search_schema = create_search_schema_tool(_raw_schema)

# Create sql_query tool via shared factory
sql_query_tool = create_sql_query_tool(_ctx_vars, _log, "Call graphql_query first.")


# ---------------------------------------------------------------------------
# GraphQL-specific: recipe step executor
# ---------------------------------------------------------------------------


def _make_graphql_step_executor_factory(ctx: RequestContext):
    """Build a GraphQL step executor factory for recipe tools.

    Returns a factory: (recipe_id) -> async step_executor
    Captures ctx at factory-creation time to avoid race conditions.
    """

    def factory(recipe_id: str):
        async def graphql_step_executor(step_idx, step, params, results):
            if not isinstance(step, dict) or step.get("kind") != "graphql":
                return (
                    False,
                    None,
                    json.dumps({"success": False, "error": "invalid recipe step"}, indent=2),
                    None,
                )

            name = step.get("name") or "data"
            tmpl = step.get("query_template")
            if not isinstance(tmpl, str):
                return (
                    False,
                    None,
                    json.dumps({"success": False, "error": "missing query_template"}, indent=2),
                    None,
                )

            query = render_text_template(tmpl, params)
            res = await graphql_fetch(query, None, ctx.target_url, ctx.target_headers)
            if not res.get("success"):
                return (
                    False,
                    None,
                    json.dumps(
                        {"success": False, "error": res.get("error", "query failed")}, indent=2
                    ),
                    None,
                )

            data = res.get("data", {})
            from ..executor import extract_tables_from_response

            tables, _ = extract_tables_from_response(data, str(name))
            results.update(tables)
            _query_results.set(results)
            safe_append_contextvar_list(_graphql_queries, query)
            return True, tables.get(str(name)), "", query

        return graphql_step_executor

    return factory


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def process_query(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Process natural language query against GraphQL API.

    Args:
        question: Natural language question
        ctx: Request context with target_url and target_headers
    """
    t0 = time.monotonic()
    status = "ok"
    try:
        return await _process_query_inner(question, ctx)
    except Exception as e:
        status = "error"
        logger.exception("GraphQL Agent error")
        return {
            "ok": False,
            "data": None,
            "queries": [],
            "error": sanitize_error(e),
        }
    finally:
        record_request("graphql", status, (time.monotonic() - t0) * 1000)


async def _process_query_inner(question: str, ctx: RequestContext) -> dict[str, Any]:
    """Inner implementation of process_query (wrapped by outer error guard)."""
    # Fetch schema (protocol-specific) — with endpoint allowlist filtering
    config_pats = parse_config_allowlist(settings.ALLOW_ENDPOINTS_GRAPHQL)
    # Empty tuple (from X-Allow-Endpoints: []) treated as "no constraint" — not "block all"
    header_pats = ctx.allow_endpoints or None
    t_schema = time.monotonic()
    with trace_span("schema.fetch", {"protocol": "graphql"}):
        schema_result = await _fetch_schema_context(
            ctx.target_url, ctx.target_headers, config_pats, header_pats, question=question
        )
    record_schema_fetch((time.monotonic() - t_schema) * 1000, "graphql")

    # Check if allowlist filtered out all query fields (uses count from _fetch_schema_context)
    if schema_result.allowed_field_count is not None and schema_result.allowed_field_count == 0:
        return {
            "ok": False,
            "data": None,
            "queries": [],
            "error": "No GraphQL query fields match the configured endpoint allowlist. "
            "Check ALLOW_ENDPOINTS_GRAPHQL config and X-Allow-Endpoints header.",
        }

    # Create protocol-specific tools
    gql_tool = _create_graphql_query_tool(ctx)
    tools = [gql_tool, sql_query_tool, search_schema]

    # Build system prompt
    instructions = _build_system_prompt()

    # Package config and run orchestration
    config = ProtocolConfig(
        agent_type="graphql",
        log_prefix="[GQL]",
        call_key="queries",
        ctx_vars=_ctx_vars,
        schema_text=schema_result.schema_context,
        raw_schema=schema_result.raw_schema_json,
        provider=provider,
        tools=tools,
        instructions=instructions,
        api_id=build_api_id(ctx, "graphql"),
        recipe_step_executor_factory=_make_graphql_step_executor_factory(ctx),
        recipe_item_key="executed_queries",
    )

    result = await run_agent_orchestration(question, config)

    # Recipe extraction (stays here to preserve monkeypatch targets).
    # Uses values captured from OrchestrationResult since orchestration
    # runs in an isolated context copy.
    if result.should_extract_recipe:
        await maybe_extract_and_save_recipe(
            api_type="graphql",
            api_id=build_api_id(ctx, "graphql"),
            question=question,
            steps=result.recipe_steps,
            sql_steps=result.sql_steps,
            raw_schema=result.raw_schema_value,
        )

    return result.result_dict
