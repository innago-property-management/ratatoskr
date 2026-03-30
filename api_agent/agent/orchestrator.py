"""Shared orchestration for protocol agents (GraphQL, REST, gRPC).

Extracts the common skeleton from process_query / process_rest_query / process_grpc_query
into composable utilities and a shared run loop. Each agent file supplies protocol-specific
logic via ProtocolConfig; the orchestrator owns ContextVar lifecycle, recipe pre-flight,
tool loop invocation, and result building.
"""

import asyncio
import contextvars
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import structlog

if TYPE_CHECKING:
    from ..llm.provider import LLMProvider

from ..config import settings
from ..executor import (
    execute_sql,
    extract_tables_from_response,
    truncate_for_context_async,
)
from ..llm.factory import create_schema_reduction_provider
from ..llm.provider import DIRECT_RETURN, LLMProvider, MaxTurnsExceeded
from ..llm.tools import tool
from ..llm.toon_encoder import ToolResultEncoder
from ..metrics import record_agent_turns, record_token_usage
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
from ..sanitize import sanitize_error
from ..schema.reducer import reduce_schema
from ..tracing import trace_metadata, trace_span
from .contextvar_utils import safe_append_contextvar_list, safe_get_contextvar
from .model import get_inject_instructions
from .progress import get_turn_context, reset_progress

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Agent concurrency limiter
# ---------------------------------------------------------------------------

_agent_semaphore: asyncio.Semaphore | None = None


def _get_agent_semaphore() -> asyncio.Semaphore:
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_AGENTS)
    return _agent_semaphore


# ---------------------------------------------------------------------------
# Schema reduction provider (lazy singleton)
# ---------------------------------------------------------------------------

_schema_reduction_provider: LLMProvider | None = None
_schema_reduction_provider_initialized = False


def _get_schema_reduction_provider() -> LLMProvider | None:
    global _schema_reduction_provider, _schema_reduction_provider_initialized
    if not _schema_reduction_provider_initialized:
        _schema_reduction_provider = create_schema_reduction_provider(settings)
        _schema_reduction_provider_initialized = True
    return _schema_reduction_provider


def _reset_schema_reduction_provider() -> None:
    """Reset for testing — forces re-evaluation on next call."""
    global _schema_reduction_provider, _schema_reduction_provider_initialized
    _schema_reduction_provider = None
    _schema_reduction_provider_initialized = False


def set_agent_semaphore(limit: int) -> None:
    """Reset the agent concurrency semaphore (for testing or reconfiguration)."""
    global _agent_semaphore
    _agent_semaphore = asyncio.Semaphore(limit)


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

    # Schema (fetched by protocol, reduced by orchestrator)
    unreduced_schema_text: str  # Schema text BEFORE reduction (orchestrator will reduce)
    raw_schema: str  # Full schema for recipe matching

    # Provider — passed from agent module to preserve monkeypatch targets in tests.
    # Typed as LLMProvider for IDE support; Any at runtime for monkeypatch compatibility.
    provider: "LLMProvider"

    # Tools & prompt
    tools: list  # Protocol tools (without recipe tools — those get added by orchestrator)
    instructions: str  # System prompt

    # Recipe
    api_id: str  # For recipe store matching

    # Fields with defaults must come after fields without defaults
    schema_pre_hook: Callable[[str], str] | None = (
        None  # schema-text → schema-text transform before reduction (receives text only, not question)
    )
    recipe_step_executor_factory: Callable | None = None  # Builds step executor for recipes
    recipe_item_key: str = "executed_calls"  # Key for recipe response formatting


@dataclass
class OrchestrationResult:
    """Intermediate result from orchestration.

    Values captured from ContextVars are returned here because orchestration
    runs in an isolated context copy (via copy_context()) — the caller cannot
    read them from ContextVars after orchestration completes.
    """

    result_dict: dict[str, Any]
    should_extract_recipe: bool
    api_calls: list = field(default_factory=list)
    agent_output: str | None = None
    # Captured from ContextVars at end of orchestration
    recipe_steps: list = field(default_factory=list)
    sql_steps: list[str] = field(default_factory=list)
    raw_schema_value: str = ""

    @classmethod
    def from_contextvars(
        cls,
        ctx_vars: "AgentContextVars",
        *,
        result_dict: dict[str, Any],
        should_extract_recipe: bool,
        api_calls: list | None = None,
        agent_output: str | None = None,
    ) -> "OrchestrationResult":
        """Build an OrchestrationResult, snapshotting ContextVar values.

        Must be called from within the isolated context copy (before the
        task completes), because ContextVar values are not accessible from
        the parent context after the copy exits.
        """
        return cls(
            result_dict=result_dict,
            should_extract_recipe=should_extract_recipe,
            api_calls=api_calls if api_calls is not None else [],
            agent_output=agent_output,
            recipe_steps=list(safe_get_contextvar(ctx_vars.recipe_steps, [])),
            sql_steps=list(safe_get_contextvar(ctx_vars.sql_steps, [])),
            raw_schema_value=safe_get_contextvar(ctx_vars.raw_schema, ""),
        )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def make_logger(prefix: str) -> Callable[[str], None]:
    """Return a debug-only logger with the given prefix."""
    _logger = structlog.get_logger(f"api_agent.agent.{prefix.strip('[]').lower()}")

    def _log(msg: str) -> None:
        if settings.DEBUG:
            _logger.debug("agent_trace", prefix=prefix, detail=msg)

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


async def format_tool_response(
    stored_data: Any,
    schema_info: dict | None,
    name: str,
    result: dict,
) -> str:
    """Format API tool response with smart truncation and optional TOON compression.

    - Wrapped dict (single object) → schema summary (no TOON)
    - List + TOON enabled → TOON-encoded string if it fits within budget
    - List + TOON disabled or TOON oversized → char-based truncated JSON
    - Fallback → full result JSON
    """
    if result.get("success") and stored_data:
        if schema_info:
            return json.dumps(
                {"success": True, "table": name, **schema_info},
                indent=2,
            )
        if isinstance(stored_data, list):
            if settings.TOON_TOOL_RESULTS_ENABLED:
                encoder = ToolResultEncoder()
                toon_str, toon_applied = encoder.encode(stored_data)
                if toon_applied and len(toon_str) <= settings.MAX_TOOL_RESPONSE_CHARS:
                    return toon_str
            return json.dumps(
                {"success": True, **await truncate_for_context_async(stored_data, name)},
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

    async def sql_query(sql: str, return_directly: bool = False) -> str:
        """Run DuckDB SQL on stored API results.

        Args:
            sql: DuckDB SQL query
            return_directly: Skip LLM processing, return results directly to client

        Returns:
            JSON or TOON-formatted string with query results
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
                if settings.TOON_TOOL_RESULTS_ENABLED:
                    encoder = ToolResultEncoder()
                    toon_str, toon_applied = encoder.encode(rows)
                    if toon_applied and len(toon_str) <= settings.MAX_TOOL_RESPONSE_CHARS:
                        return toon_str
                return json.dumps(
                    {"success": True, **await truncate_for_context_async(rows, "sql_result")},
                    indent=2,
                )

        return json.dumps(result, indent=2)

    return tool(sql_query)


# ---------------------------------------------------------------------------
# Recipe tool factory
# ---------------------------------------------------------------------------


async def create_recipe_tools(
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
        recipe = await RECIPE_STORE.get_recipe(s["recipe_id"])
        if not recipe:
            continue

        try:
            tool_name = deduplicate_tool_name(s.get("tool_name", "unknown_recipe"), seen_names)
        except ValueError:
            logger.warning("recipe_tool_name_exhausted", recipe_id=s["recipe_id"])
            continue
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

                recipe, validated_params, error = await validate_and_prepare_recipe(
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

                return await format_recipe_response(
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
    """Run the shared agent orchestration loop in an isolated context.

    Each concurrent request gets its own ContextVar snapshot via
    ``contextvars.copy_context()``, preventing cross-request data leaks.

    Each protocol agent calls this after fetching schema and creating tools.
    Recipe extraction is NOT done here — the caller handles it to preserve
    monkeypatch targets in tests.  ContextVar values needed for recipe
    extraction are captured in OrchestrationResult before leaving the
    isolated context.

    Note on ``create_task(context=)``:
        ``copy_context().run()`` only accepts *sync* callables.  For async
        coroutines the correct Python 3.11+ pattern is
        ``asyncio.create_task(coro, context=copy_context())``, which
        schedules the coroutine in a copied context.  The immediate
        ``await`` is intentional — the task exists solely for context
        isolation, not parallelism.
    """
    async with _get_agent_semaphore():
        ctx = contextvars.copy_context()
        task = asyncio.create_task(
            _run_agent_orchestration_impl(question, config),
            context=ctx,
        )
        return await task


async def _run_agent_orchestration_impl(
    question: str,
    config: ProtocolConfig,
) -> OrchestrationResult:
    """Inner orchestration logic — runs inside an isolated context copy."""
    log = make_logger(config.log_prefix)
    ctx_vars = config.ctx_vars

    try:
        log(f"QUERY {question[:80]}")

        # Initialize per-request storage (safe — runs in isolated context copy)
        reset_context_vars(ctx_vars)

        # Store raw schema
        ctx_vars.raw_schema.set(config.raw_schema)

        # Schema reduction (centralized — replaces per-agent reduce_schema calls)
        schema_for_reduction = config.unreduced_schema_text
        if schema_for_reduction:
            if config.schema_pre_hook:
                schema_for_reduction = config.schema_pre_hook(schema_for_reduction)
            reduction = await reduce_schema(
                schema_text=schema_for_reduction,
                question=question,
                threshold=settings.MAX_SCHEMA_CHARS,
                provider=(
                    _get_schema_reduction_provider() if settings.SCHEMA_REDUCTION_ENABLED else None
                ),
                enabled=settings.SCHEMA_REDUCTION_ENABLED,
                max_input_chars=settings.SCHEMA_REDUCTION_MAX_INPUT_CHARS,
                max_output_tokens=settings.SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS,
                ai_reduction_threshold=settings.SCHEMA_AI_REDUCTION_THRESHOLD,
            )
            schema_text = reduction.schema_text
        else:
            schema_text = ""

        # Pre-flight recipe search
        suggestions, recipe_context = [], ""
        if settings.ENABLE_RECIPES:
            raw_schema = safe_get_contextvar(ctx_vars.raw_schema, "")
            suggestions, recipe_context = await search_recipes(config.api_id, raw_schema, question)
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
            recipe_tools = await create_recipe_tools(
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
        augmented_query = f"{schema_text}\n\nQuestion: {question}" if schema_text else question

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
                with trace_span("agent.tool_loop", {"agent_type": config.agent_type}):
                    result = await config.provider.run_tool_loop(
                        instructions=instructions,
                        user_message=augmented_query,
                        tool_defs=tools,
                        max_turns=settings.MAX_AGENT_TURNS,
                        should_stop=_should_stop,
                        inject_instructions=get_inject_instructions(),
                    )

            # Record metrics from the completed tool loop
            record_agent_turns(result.turns_used, config.agent_type)
            record_token_usage(
                result.prompt_tokens,
                result.completion_tokens,
                config.provider.provider_name,
            )

            api_calls = ctx_vars.api_calls.get()
            last_data = ctx_vars.last_result.get()[0]
            turn_info = get_turn_context(settings.MAX_AGENT_TURNS)

        except MaxTurnsExceeded as exc:
            # Still record metrics from the partial run
            record_agent_turns(exc.last_result.turns_used, config.agent_type)
            record_token_usage(
                exc.last_result.prompt_tokens,
                exc.last_result.completion_tokens,
                config.provider.provider_name,
            )
            api_calls = ctx_vars.api_calls.get()
            last_data = ctx_vars.last_result.get()[0]
            turn_info = get_turn_context(settings.MAX_AGENT_TURNS)
            return OrchestrationResult.from_contextvars(
                ctx_vars,
                result_dict=build_partial_result(last_data, api_calls, turn_info, config.call_key),
                should_extract_recipe=False,
                api_calls=api_calls,
            )

        # Check if tool requested direct return
        is_direct_return = False
        try:
            is_direct_return = result.final_output is DIRECT_RETURN or bool(
                _return_directly_flag.get()
            )
        except LookupError:
            pass

        # Handle empty output
        if not result.final_output and not is_direct_return:
            if last_data:
                return OrchestrationResult.from_contextvars(
                    ctx_vars,
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
            return OrchestrationResult.from_contextvars(
                ctx_vars,
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

        return OrchestrationResult.from_contextvars(
            ctx_vars,
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
        logger.exception("agent_error", agent_type=config.agent_type)
        return OrchestrationResult(
            result_dict={
                "ok": False,
                "data": None,
                "result": None,
                config.call_key: [],
                "error": sanitize_error(e),
            },
            should_extract_recipe=False,
        )
