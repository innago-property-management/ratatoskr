# Ratatoskr Code Review

**Date:** 2026-02-27
**Reviewer:** Claude Opus 4.6
**Scope:** Full codebase review -- architecture, error handling, concurrency, DRY, testing, tech debt
**Commit:** `4165b9a` (feat/grpc-recipes branch, 673 tests)

---

## Executive Summary

Ratatoskr is a well-structured polyglot MCP server with clean module boundaries, consistent error handling patterns, and solid test coverage. The codebase has grown organically through three protocol agents (GraphQL, REST, gRPC) and a recipe caching system. The main areas for improvement are:

1. **Significant code duplication** across the three protocol agents (~60% structural overlap)
2. **SQL injection surface** in DuckDB table name handling
3. **Inconsistent `_return_directly_flag` usage** between gRPC and GraphQL/REST agents
4. **Missing recipe support** for gRPC recipes in the middleware layer
5. **Resource management** concerns in DuckDB and gRPC channel usage

Priority legend: **P0** = security/correctness, **P1** = architectural, **P2** = maintainability, **P3** = minor/style

---

## 1. Architecture

### 1.1 [P1] Massive DRY violation across three protocol agents

The three agents (`graphql_agent.py`, `rest_agent.py`, `grpc_agent.py`) share approximately 60% identical structural code. The `process_query` / `process_rest_query` / `process_grpc_query` functions follow the exact same skeleton:

1. Reset ContextVars
2. Fetch schema
3. Search recipes
4. Create tools
5. Build prompt
6. Run tool loop
7. Handle MaxTurnsExceeded
8. Check direct return
9. Handle empty output
10. Extract recipe
11. Build result dict

**Duplicated patterns:**

- **ContextVar declarations**: Each agent declares its own `_query_results`, `_last_result`, `_raw_schema`, `_sql_steps` ContextVars with identical semantics
  - `api_agent/agent/graphql_agent.py:69-74`
  - `api_agent/agent/rest_agent.py:71-76`
  - `api_agent/agent/grpc_agent.py:53-57`

- **`sql_query` function**: Three nearly identical implementations
  - `api_agent/agent/graphql_agent.py:402-448`
  - `api_agent/agent/rest_agent.py:474-521`
  - `api_agent/agent/grpc_agent.py:715-752`

- **Result storage pattern** (store data in ContextVar after API call): Copy-pasted with minor variations across `_create_graphql_query_tool`, `_create_rest_call_tool`, `_create_grpc_call_tool`, `_create_grpc_stream_tool`, `_create_grpc_client_stream_tool`, `_create_grpc_bidi_stream_tool`.

- **`_log` function**: Three identical copies
  - `api_agent/agent/graphql_agent.py:60-63`
  - `api_agent/agent/rest_agent.py:62-65`
  - `api_agent/agent/grpc_agent.py:47-49`

- **Post-loop result handling**: The "check direct return, handle empty output, build result dict" block is copy-pasted across all three `process_*` functions.

**Recommendation:** Extract a shared `AgentBase` or `AgentOrchestrator` class/function that encapsulates the common skeleton. Each protocol agent would supply:
- Schema fetcher
- Tool factory
- System prompt builder
- Step executor (for recipes)
- Result key name ("queries", "api_calls", "rpc_calls")

This would eliminate ~300 lines of duplicated code and make it much easier to add a fourth protocol (e.g., SOAP, WebSocket).

### 1.2 [P1] Recipe step executors are duplicated in three places

Each protocol has a recipe step executor defined in *two* separate locations:
1. Inside `_create_individual_recipe_tools` in the agent module (for agent-context recipes)
2. Inside `runner.py` (for MCP-level recipe execution)

For example, the GraphQL step executor is at:
- `api_agent/agent/graphql_agent.py:489-530` (agent context)
- `api_agent/recipe/runner.py:89-107` (runner context)

And the REST step executor is at:
- `api_agent/agent/rest_agent.py:563-618` (agent context)
- `api_agent/recipe/runner.py:233-279` (runner context)

And gRPC:
- `api_agent/recipe/runner.py:137-213` (runner context, but no agent-level recipe tools for gRPC yet)

**Recommendation:** Extract protocol-specific step executors into shared factories that accept context parameters (target_url, headers, etc.) and return a step executor function. Both the agent recipe tools and the runner would use the same factory.

### 1.3 [P2] Module-level singleton with `_ProviderProxy` magic

`api_agent/agent/model.py:57-64` uses a `_ProviderProxy` class with `__getattr__` to create a lazy singleton:

```python
class _ProviderProxy:
    def __getattr__(self, name: str):
        return getattr(get_provider(), name)

provider: LLMProvider = _ProviderProxy()  # type: ignore[assignment]
```

This works but has subtle issues:
- `isinstance(provider, LLMProvider)` returns `False`
- Type checkers cannot verify any attribute access
- Monkeypatching in tests requires patching both `_provider` and `provider` (documented in CLAUDE.md but still fragile)

**Recommendation:** Consider using a module-level `get_provider()` call everywhere, or use a proper dependency injection pattern. The current approach forces every test to monkeypatch two symbols.

### 1.4 [P2] `recipe/__init__.py` re-exports private symbols

`api_agent/recipe/__init__.py` re-exports `_return_directly_flag` and `_set_return_directly` (underscore-prefixed "private" names) as public API:

```python
from .common import (
    _return_directly_flag,
    _set_return_directly,
    ...
)
```

These are used by all three agents and the middleware. They are not truly private -- they are cross-module shared state.

**Recommendation:** Rename to `return_directly_flag` and `set_return_directly` (drop the underscore) since they are part of the public recipe API.

---

## 2. Error Handling

### 2.1 [P0] SQL injection via unsanitized table names in DuckDB

`api_agent/executor.py:162`:
```python
conn.execute(f"CREATE TABLE {key} AS SELECT * FROM read_json_auto('{f.name}')")
```

The `key` variable comes from the top-level keys of API response data (e.g., `data.keys()`). While the LLM typically generates benign names, a malicious API could return response keys like `data; DROP TABLE data--` that would be interpolated directly into the SQL string.

Similarly at `api_agent/executor.py:67`:
```python
conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM read_json_auto('{temp_file}')")
```

The `table_name` parameter flows from the LLM's `name` argument in `graphql_query(name="data")`, which the LLM controls.

**Impact:** Since DuckDB is an in-process ephemeral database per request, the blast radius is limited to data corruption within a single request. However, this is still a correctness issue and a defense-in-depth gap.

**Recommendation:** Sanitize table names to `[a-zA-Z_][a-zA-Z0-9_]*` before interpolation, or use DuckDB's identifier quoting:
```python
safe_name = re.sub(r'[^a-zA-Z0-9_]', '', key)
conn.execute(f'CREATE TABLE "{safe_name}" AS SELECT * FROM read_json_auto(?)', [f.name])
```

### 2.2 [P1] `except LookupError: pass` pattern masks real bugs

Throughout the agents, `LookupError` is caught and silently ignored in many places:

- `api_agent/agent/graphql_agent.py:362` -- `_last_result.get()[0]` failure silenced
- `api_agent/agent/rest_agent.py:298-299` -- entire result storage silenced
- `api_agent/agent/grpc_agent.py:288-289` -- result storage silenced
- `api_agent/agent/grpc_agent.py:294-297` -- `_return_directly_flag` access silenced
- `api_agent/agent/grpc_agent.py:738-741` -- sql_query return_directly silenced

In the `process_*` functions, the ContextVars are explicitly set at the top (e.g., `_query_results.set({})`), so a `LookupError` during tool execution indicates a real bug -- the ContextVar was somehow unset, which means the request isolation failed. Silently ignoring this hides concurrency bugs.

**Recommendation:** Distinguish between expected cases (ContextVar not initialized because we are in a recipe runner context outside the agent) and unexpected cases (ContextVar should have been set). Use `safe_get_contextvar` (already exists in `contextvar_utils.py`) instead of bare try/except, or at minimum log a warning.

### 2.3 [P2] Broad `except Exception` in `process_*` functions

All three agents catch `Exception` at the outermost level:

```python
except Exception as e:
    logger.exception("Agent error")
    return {"ok": False, ...}
```

- `api_agent/agent/graphql_agent.py:700-707`
- `api_agent/agent/rest_agent.py:816-823`
- `api_agent/agent/grpc_agent.py:932-939`

This is reasonable for a server that should never crash, but it catches `KeyboardInterrupt` (via `BaseException` -> no, `Exception` does not catch that, good) and `SystemExit` (also not caught, good). However, it does catch `asyncio.CancelledError` in Python 3.9+ where it inherits from `BaseException` -- actually, in Python 3.9+, `CancelledError` inherits from `BaseException`, so this is fine.

The real concern is that `logger.exception()` logs the full stack trace but the error message returned to the client is just `str(e)`, which may be uninformative. Consider adding a request ID or correlation token.

### 2.4 [P2] JSON parse errors silently return empty dicts in `context.py`

`api_agent/context.py:71-84`:
```python
try:
    target_headers = json.loads(target_headers_raw)
except json.JSONDecodeError:
    target_headers = {}
```

If a user sends malformed JSON in `X-Target-Headers`, the error is silently swallowed and the request proceeds with no headers. This could cause confusing auth failures downstream. At minimum, log a warning.

**Recommendation:** Log a warning on parse failure so users can diagnose header issues:
```python
except json.JSONDecodeError:
    logger.warning("Malformed X-Target-Headers JSON, using empty headers")
    target_headers = {}
```

---

## 3. Concurrency

### 3.1 [P1] Module-level ContextVars shared across agents

Each agent declares its own set of ContextVars, but the names in the `ContextVar()` constructor are not unique across agents. For example:

- `graphql_agent.py`: `ContextVar("query_results")`
- `rest_agent.py`: `ContextVar("query_results")`
- `grpc_agent.py`: `ContextVar("grpc_query_results")`

The gRPC agent correctly uses unique names (`"grpc_query_results"`, `"grpc_last_result"`, etc.), but GraphQL and REST use generic names (`"query_results"`, `"last_result"`).

While ContextVar identity is based on the Python object (not the name string), the duplicate names make debugging confusing in logs and tracebacks. More importantly, the `_return_directly_flag` ContextVar is shared across all agents via `recipe.common`, which is intentional but worth documenting.

**Recommendation:** Use unique name strings for all ContextVars (the gRPC pattern is correct). This is a debugging quality-of-life improvement, not a correctness issue.

### 3.2 [P2] `_ProviderProxy` is not thread-safe for lazy init

`api_agent/agent/model.py:15-53`:
```python
_provider: LLMProvider | None = None

def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        # ... create provider
        _provider = create_provider(...)
    return _provider
```

If two requests arrive simultaneously before the provider is initialized, both threads/tasks will create a provider. Since FastMCP uses asyncio (single-threaded), this is not a practical issue, but if the server ever uses thread pools or multiple workers, this would cause a race condition.

**Recommendation:** Use `threading.Lock` or `asyncio.Lock` for the lazy init, or initialize eagerly at startup.

### 3.3 [P2] `RecipeStore._lock` is a threading lock used with async code

`api_agent/recipe/store.py:159`:
```python
self._lock = threading.Lock()
```

`threading.Lock` blocks the event loop when acquired in async code. Since the critical sections are fast (dict operations), this is unlikely to cause observable latency, but it is technically incorrect for an async server.

**Recommendation:** Since the store is also accessed from sync contexts (e.g., `sql_query` tool functions are sync), a `threading.Lock` is actually the safest choice here. Document that the lock is intentionally a threading lock because the store is accessed from both sync and async contexts.

### 3.4 [P1] gRPC channels not reused across calls

`api_agent/grpc/client.py:84,185,308,418`: Every RPC call creates a new channel and closes it in `finally`:

```python
channel = _create_channel(target, tls)
try:
    ...
finally:
    await channel.close()
```

gRPC channels are expensive to create (TCP connection + TLS handshake + HTTP/2 negotiation). For a server that may process many requests against the same gRPC target, this is a significant performance concern.

**Recommendation:** Implement a channel pool or cache keyed by `(target, tls)` with TTL-based eviction. The `GrpcSchema.pool` (DescriptorPool) is already carried across the agent's lifetime, so a similar pattern for channels would be natural.

---

## 4. Code Duplication (DRY)

### 4.1 [P1] Three copies of `sql_query` tool

As noted in 1.1, the `sql_query` function is copied three times with the only difference being:
- The error message ("Call graphql_query first" vs "Call rest_call first" vs "Call grpc_call first")
- The log prefix ("[GQL]" vs "[REST]" vs "[gRPC]")
- The ContextVars used

All three follow the exact same logic:
1. Get `_query_results` from ContextVar
2. Call `execute_sql(data, sql)`
3. Store result in `_last_result`
4. Track SQL in `_sql_steps`
5. Handle `return_directly`
6. Truncate for context

**Lines of duplicated code:** ~40 lines x 3 = ~120 lines

**Recommendation:** Extract a `create_sql_query_tool(query_results_var, last_result_var, sql_steps_var, log_prefix, error_hint)` factory function.

### 4.2 [P1] Result storage pattern duplicated 6+ times

The pattern of storing API results into ContextVars after a successful call:

```python
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
```

This block appears in:
- `graphql_agent.py:350-363`
- `rest_agent.py:272-299`
- `grpc_agent.py:278-289` (grpc_call)
- `grpc_agent.py:421-432` (grpc_stream)
- `grpc_agent.py:553-564` (grpc_client_stream)
- `grpc_agent.py:682-694` (grpc_bidi_stream)

**Recommendation:** Extract a `store_api_result(result, name, query_results_var, last_result_var)` helper.

### 4.3 [P2] `_find_method` duplicated between grpc_agent and execute tool

`api_agent/agent/grpc_agent.py:135-148` defines `_find_method(schema, method_path)`.
`api_agent/tools/execute.py:27-36` defines `_find_grpc_method(schema, method_path)`.

These are nearly identical implementations.

**Recommendation:** Move `_find_method` to `api_agent/grpc/reflection.py` as a method on `GrpcSchema` or as a module-level utility.

### 4.4 [P2] Metadata construction repeated

The pattern `[(k, v) for k, v in ctx.target_headers.items()] if ctx.target_headers else None` appears at:
- `grpc_agent.py:247-250`
- `grpc_agent.py:389-392`
- `grpc_agent.py:521-524`
- `grpc_agent.py:651-654`
- `grpc_agent.py:781-783`
- `recipe/runner.py:40,128`
- `tools/execute.py:133-134`

**Recommendation:** Add a `metadata` property or method to `RequestContext`:
```python
@property
def grpc_metadata(self) -> list[tuple[str, str]] | None:
    return [(k, v) for k, v in self.target_headers.items()] if self.target_headers else None
```

---

## 5. Testing Gaps

### 5.1 [P1] No integration tests for the middleware + agent round-trip

The middleware (`DynamicToolNamingMiddleware`) is tested in `test_middleware_routing.py` (8 tests), and each agent is tested independently. However, there are no tests that exercise the full path: `middleware.on_call_tool` -> tool name transformation -> agent `process_query`. This means regressions in the name transformation logic could break real requests without being caught.

**Recommendation:** Add at least 2-3 integration tests that mock the HTTP headers and LLM provider but exercise the full middleware -> tool -> agent path.

### 5.2 [P1] `executor.py` SQL injection not tested

There are no tests that verify table name sanitization in `execute_sql` or `_extract_schema`. A test with a malicious table name like `"data; DROP TABLE data--"` would document and enforce the expected behavior.

### 5.3 [P2] `OpenAICompatProvider` retry-without-tools path undertested

`api_agent/llm/openai_compat.py:52-57` has a fallback that retries without tools when the endpoint doesn't support tool calling. The test file `test_compat_complete.py` has 9 tests but should verify:
- The specific error messages that trigger the retry ("tool" or "function" in error string)
- That the retry actually succeeds and returns a valid response
- That non-tool errors are not retried

### 5.4 [P2] Recipe extraction relies on real LLM in tests

`api_agent/recipe/extractor.py:252` calls `provider.complete()` directly:
```python
response = await provider.complete(messages, temperature=0.0, max_tokens=4096)
```

Tests for recipe extraction need to monkeypatch the provider to return canned extraction responses. This is done in `test_grpc_recipe.py` (20 tests), but the REST and GraphQL recipe extraction paths have thinner coverage.

### 5.5 [P2] No tests for `to_csv` with edge cases

`api_agent/utils/csv.py` has no dedicated test file. Edge cases to test:
- Empty list input
- Non-list input (single dict)
- Nested objects (how does DuckDB flatten them?)
- Very large data (does it handle memory gracefully?)

### 5.6 [P3] `tracing.py` global state not tested

`api_agent/tracing.py` uses module-level globals (`_tracer_ready`, `_using_metadata_fn`) that are modified by `init_tracing()`. No tests verify the tracing setup/teardown or the no-op behavior when tracing is disabled.

---

## 6. Technical Debt

### 6.1 [P0] DuckDB temp file path injection

`api_agent/executor.py:162` and `api_agent/utils/csv.py:29`:
```python
conn.execute(f"CREATE TABLE data AS SELECT * FROM read_json_auto('{f.name}')")
```

The temp file path is interpolated as a string literal. On systems where `tempfile.NamedTemporaryFile` returns paths containing single quotes (unlikely but possible with unusual `TMPDIR` settings), this would break.

**Recommendation:** Use parameterized queries where DuckDB supports them, or at minimum escape the path.

### 6.2 [P1] `_return_directly_flag` inconsistency between agents

In the GraphQL and REST agents, `_set_return_directly()` is used (from `recipe.common`):
```python
# graphql_agent.py:374-375
if return_directly and result.get("success"):
    _set_return_directly()
```

But in the gRPC agent, the flag is manipulated directly:
```python
# grpc_agent.py:294-297
if return_directly and result.get("success"):
    try:
        _return_directly_flag.get().append(True)
    except LookupError:
        pass
```

This is at `grpc_agent.py:294-297`, `grpc_agent.py:569-572`, and `grpc_agent.py:737-740`.

The `_set_return_directly()` helper handles the LookupError internally and is the canonical way to set this flag. The gRPC agent bypasses it, creating an inconsistency.

**Recommendation:** Replace all direct `_return_directly_flag.get().append(True)` calls in `grpc_agent.py` with `_set_return_directly()`.

### 6.3 [P1] gRPC agent missing recipe tools in agent context

The GraphQL and REST agents create individual recipe tools and add them to the tool list:
- `graphql_agent.py:609-611`
- `rest_agent.py:720-722`

The gRPC agent searches for recipes at `grpc_agent.py:818-820` and appends the context string to the prompt, but does **not** create individual recipe tools:
```python
# grpc_agent.py:846-847
if recipe_context:
    instructions += recipe_context
```

Compare to GraphQL:
```python
# graphql_agent.py:609-611
if suggestions:
    recipe_tools = _create_individual_recipe_tools(ctx, suggestions)
    tools = [*recipe_tools, *tools]
```

This means gRPC recipes are mentioned in the system prompt but the agent has no tool to execute them. The agent would need to manually replicate the recipe's steps using `grpc_call` etc., which defeats the purpose of recipes.

**Recommendation:** Implement `_create_individual_recipe_tools` for gRPC, or better yet, extract the recipe tool creation into a shared function parameterized by protocol.

### 6.4 [P2] `OpenAICompatProvider.format_assistant_tool_calls` redundant import

`api_agent/llm/openai_compat.py:109`:
```python
def format_assistant_tool_calls(self, response, messages):
    import json as _json
```

The `json` module is already imported at the top of the file (line 6). This inner import is unnecessary.

### 6.5 [P2] `executor.py` creates a new DuckDB connection per query

`api_agent/executor.py:166`:
```python
conn = duckdb.connect()
```

Every `execute_sql` call creates a fresh DuckDB connection and writes data to temp files. For multi-step recipes that execute several SQL queries against the same data, this means re-serializing and re-loading the same data multiple times. DuckDB connections are lightweight, but the temp file I/O is not.

**Recommendation:** Consider caching the DuckDB connection and loaded tables within a request scope (via ContextVar). This would eliminate redundant temp file writes for multi-step SQL pipelines.

### 6.6 [P2] `executor.py:206` unused wrapper function

```python
async def execute_graphql(query, variables=None):
    """Execute GraphQL query (wrapper around graphql client)."""
    return await graphql_execute(query, variables)
```

This async wrapper adds no value -- it delegates directly to the graphql client. No callers appear to use it (the agents call `graphql_fetch` directly).

**Recommendation:** Remove `execute_graphql` from `executor.py`. Verify no external callers depend on it.

### 6.7 [P2] `rest_agent.py` does not use `_recipe_steps` ContextVar

`api_agent/agent/rest_agent.py:72`:
```python
_recipe_steps: ContextVar[list[dict[str, Any]]] = ContextVar("recipe_steps")
```

This is initialized at line 673 (`_recipe_steps.set([])`) and tracked at line 286-297, but the recipe steps are actually tracked in `_rest_calls` for the REST agent. The `_recipe_steps` variable is only used for recipe extraction in `process_rest_query` at line 803:
```python
steps=safe_get_contextvar(_recipe_steps, []),
```

However, the steps are appended at line 286-297 inside `rest_call`, so this does work. But it means there are two parallel tracking mechanisms: `_rest_calls` (for the response) and `_recipe_steps` (for extraction). This is confusing.

### 6.8 [P3] `get_table_schema_summary` marked deprecated but not removed

`api_agent/executor.py:135-137`:
```python
def get_table_schema_summary(data, table_name):
    """Get DuckDB schema summary (deprecated, use extract_tables_from_response)."""
    return _extract_schema(data, table_name)
```

**Recommendation:** Check if any callers remain and remove if unused.

### 6.9 [P3] `Settings` creates a module-level singleton

`api_agent/config.py:72`:
```python
settings = Settings()
```

But `api_agent/agent/model.py:30` creates a *new* `Settings()` instance to pick up CLI overrides:
```python
s = Settings()
```

This means there are two Settings objects in play. The module-level `settings` is used everywhere for limits and flags, while the `model.py` version is used once for provider creation. If CLI overrides modify env vars after import, the module-level `settings` will have stale values.

**Recommendation:** Document this behavior, or reload the module-level settings in `get_provider()`:
```python
from ..config import settings as _settings
_settings.__init__()  # Reload from env
```

---

## 7. Positive Observations

These aspects of the codebase are well-done and worth preserving:

1. **Clean LLM provider abstraction** (`api_agent/llm/`): The `LLMProvider` ABC with `complete()` / `format_tools()` / `format_tool_results()` is well-designed. Adding a new provider requires only implementing 4 methods. The shared `run_tool_loop()` in the base class eliminates duplication across providers.

2. **ContextVar documentation**: The mutable-container pattern for ContextVars is well-documented with inline comments explaining *why* lists are used instead of `ContextVar.set()`.

3. **Defensive schema handling**: Both the GraphQL and REST schema loaders handle malformed responses gracefully (depth limit fallback in GraphQL, boolean schemas in OpenAPI 3.1, etc.).

4. **Recipe validation**: The `_validate_equivalence` function in `extractor.py` is a robust round-trip check that ensures extracted recipes render back to the original execution.

5. **Safety defaults**: Mutations blocked in GraphQL, unsafe HTTP methods blocked in REST, with explicit opt-in via headers. Good defense-in-depth.

6. **Test infrastructure**: The `FakeLLMProvider` and `fake_provider_factory` pattern is clean and reusable. The `make_text_response` / `make_tool_call_response` helpers make tests readable.

7. **`@tool` decorator** (`api_agent/llm/tools.py`): Clean replacement for the Agents SDK dependency. Extracts schema from type hints and docstrings automatically.

8. **Prompt engineering**: The shared prompt fragments in `prompts.py` are well-organized and DRY within their own module.

---

## 8. Prioritized Action Items

| # | Priority | Finding | Effort | Impact |
|---|----------|---------|--------|--------|
| 1 | P0 | SQL injection in DuckDB table names (6.1, 2.1) | Small | Security |
| 2 | P1 | gRPC `_return_directly_flag` inconsistency (6.2) | Trivial | Correctness |
| 3 | P1 | gRPC agent missing recipe tools (6.3) | Medium | Feature gap |
| 4 | P1 | Extract shared agent orchestration skeleton (1.1) | Large | Maintainability |
| 5 | P1 | Extract shared `sql_query` tool factory (4.1) | Small | DRY |
| 6 | P1 | Extract shared result storage helper (4.2) | Small | DRY |
| 7 | P1 | gRPC channel pooling (3.4) | Medium | Performance |
| 8 | P1 | Integration tests for middleware->agent path (5.1) | Medium | Test coverage |
| 9 | P2 | Log warnings on malformed header JSON (2.4) | Trivial | Debuggability |
| 10 | P2 | Move `_find_method` to shared location (4.3) | Trivial | DRY |
| 11 | P2 | Add `grpc_metadata` property to RequestContext (4.4) | Trivial | DRY |
| 12 | P2 | Remove redundant import in openai_compat (6.4) | Trivial | Cleanup |
| 13 | P2 | Test `to_csv` edge cases (5.5) | Small | Test coverage |
| 14 | P2 | Remove deprecated `get_table_schema_summary` (6.8) | Trivial | Cleanup |
| 15 | P2 | Remove unused `execute_graphql` wrapper (6.6) | Trivial | Cleanup |
| 16 | P3 | Document Settings dual-instance behavior (6.9) | Trivial | Documentation |

---

## 9. Summary Metrics

| Metric | Value |
|--------|-------|
| Total source files reviewed | 28 |
| Total findings | 27 |
| P0 (security/correctness) | 2 |
| P1 (architectural) | 10 |
| P2 (maintainability) | 11 |
| P3 (minor) | 4 |
| Estimated refactoring effort for P0+P1 | 3-5 days |
| Test count at review time | 673 |

The codebase is in good shape for its maturity. The most impactful improvements would be addressing the DuckDB injection surface (P0, quick fix), the gRPC `_return_directly_flag` inconsistency (P1, trivial fix), and beginning the shared agent skeleton extraction (P1, high-value refactor that pays dividends as more protocols are added).
