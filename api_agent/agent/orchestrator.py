"""Shared orchestration for protocol agents (GraphQL, REST, gRPC).

Extracts the common skeleton from process_query / process_rest_query / process_grpc_query
into composable utilities and a shared run loop. Each agent file supplies protocol-specific
logic via ProtocolConfig; the orchestrator owns ContextVar lifecycle, recipe pre-flight,
tool loop invocation, and result building.
"""

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..llm.provider import LLMProvider

from ..config import settings
from ..executor import execute_sql, extract_tables_from_response, truncate_for_context
from ..llm.provider import MaxTurnsExceeded
from ..llm.tools import tool
from ..recipe import (
    RECIPE_STORE,
    _return_directly_flag,
    _set_return_directly,
    build_partial_result,
    build_recipe_docstring,
    create_params_model,
    deduplicate_tool_name,
    execute_recipe_steps,
    format_recipe_response,
    search_recipes,
    validate_and_prepare_recipe,
    validate_recipe_params,
)
from ..tracing import trace_metadata
from .contextvar_utils import safe_append_contextvar_list, safe_get_contextvar
from .model import get_inject_instructions
from .progress import get_turn_context, reset_progress

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentContextVars:
    """Per-protocol context variable bundle.

    Each protocol creates its own instance with unique ContextVar names.
    The orchestrator operates on the generic bundle.
    """

    api_calls: ContextVar[list]
    recipe_steps: ContextVar[list]
    query_results: ContextVar[dict]
    last_result: ContextVar[list]
    raw_schema: ContextVar[str]
    sql_steps: ContextVar[list[str]]


@dataclass
class ProtocolConfig:
    """Everything the orchestrator needs to run a protocol agent."""

    # Identity
    agent_type: str  # "graphql" | "rest" | "grpc"
    log_prefix: str  # "[GQL]" | "[REST]" | "[gRPC]"
    call_key: str  # "queries" | "api_calls" | "rpc_calls"

    # Context vars
    ctx_vars: AgentContextVars

    # Schema (already fetched by protocol)
    schema_text: str  # Truncated schema for LLM context
    raw_schema: str  # Full schema for recipe matching

    # Provider — passed from agent module to preserve monkeypatch targets in tests.
    # Typed as LLMProvider for IDE support; Any at runtime for monkeypatch compatibility.
    provider: "LLMProvider"

    # Tools & prompt
    tools: list  # Protocol tools (without recipe tools — those get added by orchestrator)
    instructions: str  # System prompt

    # Recipe
    api_id: str  # For recipe store matching
    recipe_step_executor_factory: Callable | None = None  # Builds step executor for recipes
    recipe_item_key: str = "executed_calls"  # Key for recipe response formatting


@dataclass
class OrchestrationResult:
    """Intermediate result from orchestration."""

    result_dict: dict[str, Any]
    should_extract_recipe: bool
    api_calls: list = field(default_factory=list)
    agent_output: str | None = None


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def make_logger(prefix: str) -> Callable[[str], None]:
    """Return a debug-only logger with the given prefix."""
    _logger = logging.getLogger(f"api_agent.agent.{prefix.strip('[]').lower()}")

    def _log(msg: str) -> None:
        if settings.DEBUG:
            _logger.info(f"{prefix} {msg}")

    return _log


def reset_context_vars(ctx_vars: AgentContextVars) -> None:
    """Reset all context vars for a new request."""
    ctx_vars.api_calls.set([])
    ctx_vars.recipe_steps.set([])
    ctx_vars.query_results.set({})
    ctx_vars.last_result.set([None])
    ctx_vars.raw_schema.set("")
    ctx_vars.sql_steps.set([])
    _return_directly_flag.set([])
    reset_progress()


def store_result(
    ctx_vars: AgentContextVars,
    data: Any,
    name: str,
) -> tuple[Any, dict | None]:
    """Extract tables from API response and store in context vars.

    Returns (stored_data, schema_info). Both may be None on failure.
    """
    try:
        results = ctx_vars.query_results.get()
        tables, schema_info = extract_tables_from_response(data, name)
        results.update(tables)
        ctx_vars.query_results.set(results)
        stored_data = tables.get(name)
        if stored_data is not None:
            ctx_vars.last_result.get()[0] = stored_data
        return stored_data, schema_info
    except LookupError:
        return None, None


def format_tool_response(
    stored_data: Any,
    schema_info: dict | None,
    name: str,
    result: dict,
) -> str:
    """Format API tool response with smart truncation.

    - Wrapped dict (single object) → schema summary
    - List → char-based truncation
    - Fallback → full result JSON
    """
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


# ---------------------------------------------------------------------------
# SQL query tool factory
# ---------------------------------------------------------------------------


def create_sql_query_tool(
    ctx_vars: AgentContextVars,
    log: Callable[[str], None],
    no_data_hint: str,
) -> Any:
    """Create a sql_query tool bound to the given context vars.

    Args:
        ctx_vars: Protocol's context variable bundle
        log: Logger function (e.g., from make_logger)
        no_data_hint: Error message when no data is loaded (e.g., "Call graphql_query first.")
    """

    def sql_query(sql: str, return_directly: bool = False) -> str:
        """Run DuckDB SQL on stored API results.

        Args:
            sql: DuckDB SQL query
            return_directly: Skip LLM processing, return results directly to client

        Returns:
            JSON string with query results
        """
        try:
            data = ctx_vars.query_results.get()
        except LookupError:
            return json.dumps({"success": False, "error": f"No data. {no_data_hint}"})

        if not data:
            return json.dumps({"success": False, "error": f"No data. {no_data_hint}"})

        result = execute_sql(data, sql)

        log(f"SQL {json.dumps(result, default=str)[:200]}")

        if result.get("success"):
            rows = result.get("result", [])
            try:
                ctx_vars.last_result.get()[0] = rows
            except LookupError:
                pass

            safe_append_contextvar_list(ctx_vars.sql_steps, sql)

            if return_directly:
                _set_return_directly()

            if isinstance(rows, list):
                return json.dumps(
                    {"success": True, **truncate_for_context(rows, "sql_result")},
                    indent=2,
                )

        return json.dumps(result, indent=2)

    return tool(sql_query)


# ---------------------------------------------------------------------------
# Recipe tool factory
# ---------------------------------------------------------------------------


def create_recipe_tools(
    ctx_vars: AgentContextVars,
    suggestions: list[dict[str, Any]],
    api_type: str,
    step_executor_factory: Callable,
    recipe_item_key: str = "executed_calls",
) -> list:
    """Generate one ToolDefinition per recipe suggestion.

    Args:
        ctx_vars: Protocol's context variable bundle
        suggestions: Recipe suggestions from search_recipes()
        api_type: "graphql", "rest", or "grpc"
        step_executor_factory: Callable that returns a protocol-specific step executor.
            Signature: (recipe_id, raw_schema_var) -> async (step_idx, step, params, results) -> (success, data, error, call_rec)
        recipe_item_key: Key for executed items in response (e.g., "executed_queries", "executed_calls")
    """
    tools = []
    seen_names: set[str] = set()

    for s in suggestions:
        recipe = RECIPE_STORE.get_recipe(s["recipe_id"])
        if not recipe:
            continue

        tool_name = deduplicate_tool_name(s.get("tool_name", "unknown_recipe"), seen_names)
        params_spec = recipe.get("params", {})
        docstring = build_recipe_docstring(
            s["question"],
            recipe.get("steps", []),
            recipe.get("sql_steps", []),
            api_type,
            params_spec=params_spec,
        )

        def make_tool(rid: str, pspec: dict[str, Any], doc: str, tname: str):
            ParamsModel = create_params_model(pspec, tname)

            async def dynamic_recipe_tool(
                params: ParamsModel,
                return_directly: bool = True,
            ) -> str:
                kwargs = params.model_dump()
                validated_params, error = validate_recipe_params(pspec, kwargs)
                if error:
                    return error

                recipe, validated_params, error = validate_and_prepare_recipe(
                    rid, json.dumps(kwargs), ctx_vars.raw_schema
                )
                if error:
                    return error

                executor = step_executor_factory(rid)

                executed_items: list[Any] = []
                if recipe is None or validated_params is None:
                    return json.dumps({"success": False, "error": "recipe validation failed"})
                success, last_data, executed_sql, error = await execute_recipe_steps(
                    recipe,
                    validated_params,
                    ctx_vars.query_results,
                    ctx_vars.last_result,
                    executor,
                    executed_items,
                )
                if not success:
                    return error

                for sql in executed_sql:
                    safe_append_contextvar_list(ctx_vars.sql_steps, sql)

                if return_directly:
                    _set_return_directly()

                return format_recipe_response(
                    ctx_vars.last_result,
                    executed_items,
                    executed_sql,
                    recipe_item_key,
                )

            dynamic_recipe_tool.__name__ = tname
            dynamic_recipe_tool.__doc__ = doc
            return tool(dynamic_recipe_tool)

        tools.append(make_tool(s["recipe_id"], params_spec, docstring, tool_name))

    return tools


# ---------------------------------------------------------------------------
# Main orchestration loop
# ---------------------------------------------------------------------------


async def run_agent_orchestration(
    question: str,
    config: ProtocolConfig,
) -> OrchestrationResult:
    """Run the shared agent orchestration loop.

    Each protocol agent calls this after fetching schema and creating tools.
    Recipe extraction is NOT done here — the caller handles it to preserve
    monkeypatch targets in tests.
    """
    log = make_logger(config.log_prefix)
    ctx_vars = config.ctx_vars

    try:
        log(f"QUERY {question[:80]}")

        # Reset per-request storage
        reset_context_vars(ctx_vars)

        # Store raw schema
        ctx_vars.raw_schema.set(config.raw_schema)

        # Pre-flight recipe search
        suggestions, recipe_context = [], ""
        if settings.ENABLE_RECIPES:
            raw_schema = safe_get_contextvar(ctx_vars.raw_schema, "")
            suggestions, recipe_context = search_recipes(config.api_id, raw_schema, question)
            if suggestions:
                log(
                    f"PRE-FLIGHT found={len(suggestions)} "
                    f"ids={[s['recipe_id'] for s in suggestions]}"
                )
            elif raw_schema:
                log(f"PRE-FLIGHT no matches for api_id={config.api_id[:50]}")

        # Build tool list: recipe tools (if any) + protocol tools
        tools = list(config.tools)
        if suggestions and config.recipe_step_executor_factory:
            recipe_tools = create_recipe_tools(
                ctx_vars,
                suggestions,
                config.agent_type,
                config.recipe_step_executor_factory,
                config.recipe_item_key,
            )
            tools = [*recipe_tools, *tools]

        # Inject recipe context into prompt
        instructions = config.instructions
        if recipe_context:
            instructions += recipe_context

        # Build augmented query with schema context
        augmented_query = (
            f"{config.schema_text}\n\nQuestion: {question}" if config.schema_text else question
        )

        def _should_stop(results):
            try:
                return bool(_return_directly_flag.get())
            except LookupError:
                return False

        # Run tool-calling loop
        api_calls: list = []
        last_data = None
        turn_info = ""
        try:
            with trace_metadata({"mcp_name": settings.MCP_SLUG, "agent_type": config.agent_type}):
                result = await config.provider.run_tool_loop(
                    instructions=instructions,
                    user_message=augmented_query,
                    tool_defs=tools,
                    max_turns=settings.MAX_AGENT_TURNS,
                    should_stop=_should_stop,
                    inject_instructions=get_inject_instructions(),
                )

            api_calls = ctx_vars.api_calls.get()
            last_data = ctx_vars.last_result.get()[0]
            turn_info = get_turn_context(settings.MAX_AGENT_TURNS)

        except MaxTurnsExceeded:
            api_calls = ctx_vars.api_calls.get()
            last_data = ctx_vars.last_result.get()[0]
            turn_info = get_turn_context(settings.MAX_AGENT_TURNS)
            return OrchestrationResult(
                result_dict=build_partial_result(last_data, api_calls, turn_info, config.call_key),
                should_extract_recipe=False,
                api_calls=api_calls,
            )

        # Check if tool requested direct return
        is_direct_return = False
        try:
            is_direct_return = result.final_output == "__DIRECT_RETURN__" or bool(
                _return_directly_flag.get()
            )
        except LookupError:
            pass

        # Handle empty output
        if not result.final_output and not is_direct_return:
            if last_data:
                return OrchestrationResult(
                    result_dict={
                        "ok": True,
                        "data": f"[Partial - {turn_info}] Data retrieved but agent didn't complete.",
                        "result": last_data,
                        config.call_key: api_calls,
                        "error": None,
                    },
                    should_extract_recipe=False,
                    api_calls=api_calls,
                )
            return OrchestrationResult(
                result_dict={
                    "ok": False,
                    "data": None,
                    "result": None,
                    config.call_key: api_calls,
                    "error": f"No output ({turn_info})",
                },
                should_extract_recipe=False,
                api_calls=api_calls,
            )

        # Build success result
        if is_direct_return:
            agent_output = None
        else:
            agent_output = str(result.final_output)
            log(f"DONE calls={len(api_calls)} output={agent_output[:100]}")

        return OrchestrationResult(
            result_dict={
                "ok": True,
                "data": agent_output,
                "result": last_data,
                config.call_key: api_calls,
                "error": None,
            },
            should_extract_recipe=True,
            api_calls=api_calls,
            agent_output=agent_output,
        )

    except Exception as e:
        logger.exception(f"{config.log_prefix} Agent error")
        return OrchestrationResult(
            result_dict={
                "ok": False,
                "data": None,
                config.call_key: [],
                "error": str(e),
            },
            should_extract_recipe=False,
        )
