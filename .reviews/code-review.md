# Ratatoskr Code Review — Open-Source Readiness

**Reviewer:** Claude Code (Opus 4.6)
**Date:** 2026-03-04
**Scope:** `api_agent/` production code (~4,800 LoC across 48 files)
**Test suite:** 848 passing (not reviewed — production code only)

---

## Executive Summary

The codebase is well-structured with clean module boundaries. The orchestrator pattern (PR #21) successfully DRYs the three protocol agents. Security hardening (SSRF, DuckDB sandbox, mutation blocking) is solid. The main concerns are: (1) module-level ContextVar singletons that prevent safe concurrent multi-tenant use, (2) a new gRPC channel per RPC call, (3) some dead code and minor correctness issues.

Overall: **ready for open-source with the CRITICAL and HIGH items addressed.**

---

## CRITICAL

### C1. Module-level ContextVars create cross-request interference under concurrency

**Files:** `grpc_agent.py:58-73`, `rest_agent.py:57-72`, `graphql_agent.py:54-69`

Each agent declares ContextVars at module level and bundles them into a single `_ctx_vars` instance. `reset_context_vars()` is called at the start of each orchestration run — but ContextVars are inherited by child tasks. If two concurrent requests arrive:

1. Request A resets vars and sets schema
2. Request B resets vars **before** A's tool loop reads the schema
3. A's schema is now empty

This is safe only under single-request-at-a-time semantics (which MCP's streamable-http transport may or may not guarantee). The mutable-container pattern (`_last_result = [None]`) was explicitly chosen to handle child-to-parent propagation, but the reset-race is the real issue.

**Risk:** Data corruption or wrong-schema responses under concurrent load.

**Recommendation:** Create fresh ContextVar tokens per request using `contextvars.copy_context().run()`, or pass state through parameters rather than global ContextVars. At minimum, document the single-request assumption prominently.

### C2. `render_text_template` used for GraphQL/gRPC templates has no escaping

**File:** `recipe/store.py:50-66`

`render_text_template()` does raw string substitution of `{{param}}` placeholders. For GraphQL query templates, a param value like `"}}) { __typename } mutation { deleteAll(confirm: true` could break out of the intended query structure and inject arbitrary GraphQL.

`render_sql_safe()` exists for SQL but there's no equivalent for GraphQL/gRPC. The DuckDB sandbox prevents SQL-side damage, but GraphQL mutations could be injected if the mutation regex check happens before template rendering — and it does: `graphql/client.py:16-19` checks the final query string, so the mutation regex is the only defense. The regex (`^\s*mutation\b`) with comment stripping is decent but could theoretically be bypassed with creative input.

**Risk:** GraphQL injection via recipe parameters allowing mutation execution.

**Recommendation:** For GraphQL templates, validate that rendered queries pass the mutation check before execution. For gRPC, the method path comes from the recipe (not params), so the risk is lower but request body injection is still possible.

---

## HIGH

### H1. New gRPC channel created and closed for every RPC call

**File:** `grpc/client.py:84,114` (and all 4 execute functions)

Every `execute_*_rpc()` call creates a new `grpc.aio.Channel`, performs one RPC, then closes it. gRPC channels are expensive to create (TCP + TLS handshake + HTTP/2 negotiation). Under load, this will cause severe latency and connection churn.

```python
channel = _create_channel(target, tls)
try:
    # ... one RPC ...
finally:
    await channel.close()
```

**Recommendation:** Pool channels by target, or at minimum reuse a channel across the agent's tool loop for a single request (pass channel via ProtocolConfig or context).

### H2. `httpx.AsyncClient` created per REST request — no connection reuse

**File:** `rest/client.py:126`

```python
async with httpx.AsyncClient(timeout=30.0) as client:
```

Same pattern as gRPC: a new HTTP client per `execute_request()` call. httpx's `AsyncClient` manages a connection pool, but creating and destroying it per call prevents connection reuse (no HTTP/2 multiplexing, no keep-alive).

**Recommendation:** Create a shared `httpx.AsyncClient` per request context (or per process with appropriate limits).

### H3. GraphQL client also creates a new `httpx.AsyncClient` per call

**File:** `graphql/client.py:54`

Same issue as H2. Introspection + N query calls = N+1 TCP connections to the same endpoint.

### H4. `_return_directly_flag` ContextVar is shared across all protocols

**File:** `recipe/common.py:163`

`_return_directly_flag` is a single module-level ContextVar but `reset_context_vars()` resets it in the orchestrator. Since it's not namespaced per-protocol (unlike the other vars), if two protocol agents were somehow invoked concurrently in the same context, they'd share this flag. This is an extension of C1 but worth calling out separately.

### H5. `RECIPE_STORE` is a process-global singleton with no tenant isolation

**File:** `recipe/store.py:390`

The store is keyed by `(api_id, schema_hash)` but has no per-session or per-user scoping. In a multi-tenant deployment, User A's recipes are visible to User B if they hit the same API+schema. The CLAUDE.md notes this as "low severity for single-tenant" but it's worth documenting for open-source consumers.

### H6. Blocking DuckDB operations in async context

**File:** `executor.py:177-238`

`execute_sql()` is a synchronous function called from async tool handlers. DuckDB's `execute()` and `fetchall()` block the event loop. For small datasets this is fine, but on large API responses (tables with thousands of rows), this can stall all concurrent requests.

```python
result = conn.execute(query).fetchall()  # Blocks event loop
```

**Recommendation:** Wrap in `asyncio.to_thread()` or use DuckDB's async query interface if available.

### H7. Temp file creation in `execute_sql` and `_extract_schema` — potential disk exhaustion

**Files:** `executor.py:92,197-211`

Temp files are created for DuckDB `read_json_auto()` and cleaned up in `finally` blocks, which is correct. However, there's no limit on temp file size — a malicious API response could produce very large JSON files. The `MAX_TOOL_RESPONSE_CHARS` truncation happens after DuckDB processing, not before.

---

## MEDIUM

### M1. `rest_agent.py:546` redefines `spec_filter` inside conditional, shadowing `None`

**File:** `rest_agent.py:542-566`

```python
spec_filter = None
_filter_stats: dict[str, int] = {}
if config_pats is not None or header_pats is not None:
    def spec_filter(spec: dict) -> dict:  # shadows outer None
```

This works but is fragile — linting tools flag this. Consider a named inner function or conditional assignment.

### M2. `_ProviderProxy` swallows attribute errors from real provider

**File:** `agent/model.py:57-63`

```python
class _ProviderProxy:
    def __getattr__(self, name: str):
        return getattr(get_provider(), name)
```

If `get_provider()` itself raises (bad API key, missing env var), the error surfaces as `AttributeError` on whatever method was called, not as a clear initialization error. Consider catching and re-raising with context.

### M3. Schema reduction prompt vulnerable to indirect injection

**File:** `schema/reducer.py:114-133`

The Haiku prompt wraps the schema in `[BEGIN UNTRUSTED SCHEMA]` markers, which is good. However, `str.replace()` is used instead of proper template rendering:

```python
prompt = _HAIKU_PROMPT.replace("{schema_text}", schema_text).replace("{question}", question)
```

If `schema_text` contains the literal string `{question}`, it won't be replaced (the replacement order is correct). But if `question` contains `{schema_text}`, it could inject arbitrary content. The comment acknowledges this partially but the defense is incomplete.

### M4. `tool()` decorator doesn't handle `Union`, `Optional`, or complex type hints

**File:** `llm/tools.py:70-71`

```python
hint = hints.get(name, str)
json_type = _TYPE_MAP.get(hint, "string")
```

`Optional[str]` becomes `typing.Optional[str]` which maps to `"string"` by default fallback. This works accidentally but `list[str]`, `dict[str, Any]`, Pydantic models, etc. all silently become `"string"`. The recipe tool factory uses Pydantic models via a different path (`create_params_model`), but the `@tool` decorator should handle at least `Optional[T]`.

### M5. `extract_tables_from_response` picks first list value from dict — order-dependent

**File:** `executor.py:72-74`

```python
for value in data.values():
    if isinstance(value, list):
        return {name: value}, None
```

Python dicts maintain insertion order (3.7+), but the first list value may not be the most relevant. A GraphQL response like `{"metadata": [...], "users": [...]}` would return `metadata` instead of `users`.

### M6. `OpenAICompatProvider.format_assistant_tool_calls` imports `json` redundantly

**File:** `llm/openai_compat.py:107`

```python
import json as _json  # json already imported at module level (line 6)
```

Dead import alias. Minor but noisy.

### M7. `get_table_schema_summary` is deprecated but still exported

**File:** `executor.py:172-174`

```python
def get_table_schema_summary(data: list[dict], table_name: str) -> dict[str, Any]:
    """Get DuckDB schema summary (deprecated, use extract_tables_from_response)."""
    return _extract_schema(data, table_name)
```

If no callers remain, remove it before open-source release to avoid confusion.

### M8. `_build_system_prompt` in `rest_agent.py` does string arithmetic on workflow steps

**File:** `rest_agent.py:172-188`

```python
workflow_start = "1"
# ...
{int(workflow_start) + 1}. Check if endpoint is in polling paths
{int(workflow_start) + 2}. Use sql_query to filter/aggregate results
```

This is unnecessarily complex. Just hardcode the step numbers or use `enumerate`.

### M9. `process_query` in `graphql_agent.py` is missing outer try/except

**File:** `graphql_agent.py:490-552`

Unlike `process_grpc_query` and `process_rest_query` which wrap everything in try/except and return error dicts, `process_query` has no outer exception handler. An unexpected error during schema fetch or tool creation will propagate as an unhandled exception instead of the standard `{"ok": False, "error": ...}` format.

### M10. `RecipeStore._similarity` produces score > 0 for any non-empty query

**File:** `recipe/store.py:148-175`

`_similarity()` uses `fuzz.token_set_ratio` which can return non-zero for completely unrelated strings (e.g., common stop words). Combined with `search_recipes` having no minimum score threshold, this means any query returns recipes even if they're irrelevant. The system prompt's "score hint" text helps the LLM, but low-scored suggestions still consume context.

---

## LOW

### L1. `_UNSAFE_PATTERNS` parsed at module load from `settings`

**File:** `grpc_agent.py:81`

```python
_UNSAFE_PATTERNS = [p.strip() for p in settings.GRPC_UNSAFE_METHOD_PATTERNS.split(",") if p.strip()]
```

This runs at import time with the initial settings. If env vars are overridden via CLI (`apply_cli_overrides`), the patterns won't update. This matches the `settings = Settings()` singleton pattern, but could surprise users who override `GRPC_UNSAFE_METHOD_PATTERNS` at runtime.

### L2. `validate_target_url` doesn't resolve DNS — TOCTOU for hostname-based blocks

**File:** `context.py:67-81`

Private IP blocking checks if the hostname parses as an IP literal. Hostnames like `internal.evil.com` that resolve to `10.0.0.1` bypass the check. This is a known limitation of synchronous SSRF checks and is documented, but worth noting.

### L3. `_set_return_directly` silently no-ops on LookupError

**File:** `recipe/common.py:166-171`

If the ContextVar isn't initialized, the signal is silently dropped and the LLM will process the result instead of returning directly. This is safe but could cause unexpected behavior.

### L4. `progress.py` not reviewed but imports suggest simple turn counter

The turn-tracking module appears to be a simple counter. If it uses module-level state (like the ContextVars), it shares the same concurrency concern as C1.

### L5. `yaml.safe_load` in schema_loader could be slow on large YAML specs

**File:** `rest/schema_loader.py:45`

For very large OpenAPI specs served as YAML, parsing could be slow. Consider a size limit before parsing.

### L6. `cors_allowed_origins = "*"` default is permissive

**File:** `config.py:63`

The default CORS origin `"*"` with `allow_credentials=True` is invalid per the CORS spec (browsers reject `*` with credentials). Either default to `"*"` without credentials, or require explicit origins.

### L7. Settings re-instantiation in `main()` and `get_provider()`

**Files:** `__main__.py:149`, `agent/model.py:26`

`Settings()` is re-instantiated to pick up env var overrides. This works but creates multiple Settings objects. Consider a refresh/reload pattern on the singleton instead.

---

## INFO

### I1. Architecture is clean and well-organized

The orchestrator pattern (`orchestrator.py`) successfully extracts shared logic while letting protocol agents own their specifics. The `ProtocolConfig` dataclass is a good composition-over-inheritance choice. Module boundaries are sensible.

### I2. Security layering is well thought out

- SSRF: URL validation with scheme, IP, and host checks
- DuckDB: Sandbox mode after data load, table name sanitization
- Mutations: Blocked by default across all protocols with explicit opt-in
- Endpoint allowlist: Config (ceiling) + header (per-session) with intersection semantics
- Schema: `[BEGIN UNTRUSTED SCHEMA]` markers in Haiku prompts

### I3. Error handling is consistent

All three agents return the same `{"ok": bool, "data": ..., "<calls_key>": [...], "error": ...}` shape. Error messages are descriptive without leaking implementation details.

### I4. Recipe system is clever but complex

The extract-validate-deduplicate-serve pipeline is impressive engineering. The equivalence validation (render template with defaults, compare to original) is a nice correctness check. Consider documenting the recipe lifecycle more prominently for contributors.

### I5. LLM provider abstraction is clean

The `LLMProvider` ABC with a shared `run_tool_loop()` and provider-specific message formatting is well-designed. The `@tool` decorator is minimal but effective for the use case.

### I6. Code duplication in streaming tool handlers

The four gRPC tool factories (`_create_grpc_call_tool`, `_create_grpc_stream_tool`, etc.) share ~70% identical code (JSON parsing, metadata building, safety checks, result storage). This is acceptable given that each has distinct RPC-type validation, but a `_execute_grpc_common()` helper could reduce the file from 1023 to ~600 lines.

### I7. No rate limiting on LLM or external API calls

The tool loop has a `max_turns` limit (default 30) but no rate limiting on actual API calls within a turn. A single turn can trigger unlimited outbound requests.

### I8. `__init__.py` files are empty or minimal (good)

Package `__init__.py` files don't pull in heavy imports. Lazy loading via `_ProviderProxy` and deferred imports are used appropriately.

---

## Prioritized Action Items

| # | Severity | Effort | Item |
|---|----------|--------|------|
| C1 | CRITICAL | Medium | Document or fix ContextVar concurrency assumption |
| C2 | CRITICAL | Low | Add mutation check after GraphQL template rendering |
| H1 | HIGH | Medium | Pool gRPC channels per target |
| H2 | HIGH | Medium | Share httpx client across REST requests |
| H3 | HIGH | Low | Share httpx client for GraphQL |
| H6 | HIGH | Low | Wrap DuckDB ops in `asyncio.to_thread()` |
| M9 | MEDIUM | Low | Add outer try/except to `process_query` |
| L6 | LOW | Low | Fix CORS default to not use `*` with credentials |
| M6 | LOW | Trivial | Remove redundant `import json as _json` |
| M7 | LOW | Trivial | Remove deprecated `get_table_schema_summary` |
