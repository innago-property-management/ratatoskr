# Ratatoskr — Python Best Practices Review

**Date:** 2026-03-04
**Scope:** Full codebase review for open-source release readiness
**Codebase:** 848 tests, ~5,500 LOC across 30+ modules

---

## Executive Summary

Ratatoskr is a well-architected polyglot-LLM MCP server with clean separation of concerns, a solid orchestrator pattern, and good test coverage. The codebase is generally production-ready with several areas needing attention before a broader open-source release.

**Finding Counts:**

| Severity | Count | Action Required |
|----------|-------|----------------|
| CRITICAL | 5 | Must fix before release |
| HIGH | 16 | Should fix before release |
| MEDIUM | 32 | Fix in near-term |
| LOW | 24 | Nice to have |
| INFO | 19 | Good patterns noted |

---

## Table of Contents

1. [Critical Findings](#critical-findings)
2. [High Findings](#high-findings)
3. [Medium Findings](#medium-findings)
4. [Low Findings](#low-findings)
5. [Good Patterns (INFO)](#good-patterns)
6. [Recommended Priority Order](#recommended-priority-order)

---

## Critical Findings

### C-1. Git dependency blocks PyPI installs
**File:** `pyproject.toml:34`
**Category:** Packaging

`toon_format` is pinned to a git commit hash. Users running `pip install api-agent-ratatoskr` without git installed (or behind firewalls) will fail. The `allow-direct-references = true` masks the problem.

**Fix:** Vendor the dependency, publish it to PyPI, or move to `[optional-dependencies]`.

---

### C-2. New httpx/gRPC client created per request — no connection pooling
**Files:** `api_agent/graphql/client.py:54`, `api_agent/rest/client.py:126`, `api_agent/rest/schema_loader.py:36`, `api_agent/grpc/client.py` (all RPC functions)

Every HTTP and gRPC call creates and tears down a fresh client/channel. This eliminates HTTP/2 multiplexing and connection reuse, causing socket exhaustion under load and high latency from repeated TLS handshakes.

**Fix:** Use a shared `httpx.AsyncClient` per target host (or a module-level pool). For gRPC, cache channels keyed by target URL.

---

### C-3. No resource cleanup for LLM SDK clients
**Files:** `api_agent/llm/openai_provider.py:19`, `api_agent/llm/anthropic_provider.py:23`, `api_agent/llm/openai_compat.py:30`

All three providers create `AsyncOpenAI` / `AsyncAnthropic` clients in `__init__` but never close them. No `close()`, no `__aenter__`/`__aexit__`, no shutdown hook. This prevents clean graceful shutdown and can cause "Event loop is closed" warnings.

**Fix:** Add `async def close()` to the ABC. Implement in all providers. Wire into application shutdown.

---

### C-4. User-provided SQL executed directly in DuckDB
**File:** `api_agent/executor.py:217`

`conn.execute(query)` runs raw SQL from the agent. The `_sandbox()` call mitigates external access, but DuckDB SQL still allows `CREATE TABLE`, `DROP TABLE`, `INSERT`, and other DDL/DML. This could corrupt the in-session state.

**Fix:** Restrict to `SELECT`-only queries (prefix check or DuckDB read-only mode).

---

### C-5. `deduplicate_tool_name` has unbounded `while True` loop
**File:** `api_agent/recipe/common.py:244-252`

If `seen_names` is adversarially populated or a degenerate collision occurs, this loops forever. No upper-bound guard. Potential DoS vector in long-running processes.

**Fix:** Add a `max_attempts` guard (e.g., 1000) and raise on exhaustion.

---

## High Findings

### H-1. Stale settings singleton
**File:** `api_agent/__main__.py:147-151`

`apply_cli_overrides` sets env vars, then creates a *new* `Settings()` locally. But the module-level `settings` singleton was already constructed at import time. Any module that cached a settings value at import time uses stale values.

---

### H-2. `MissingHeaderError` misused for validation errors
**File:** `api_agent/context.py:47-48, 64, 76`

`validate_target_url` raises `MissingHeaderError` for scheme violations, blocked hosts, and private IPs. These are *validation* errors, not missing headers. Callers cannot distinguish "no header" from "dangerous header value."

**Fix:** Create `TargetUrlValidationError` (or a generic `ValidationError` subclass).

---

### H-3. Schema fetched on every `on_list_tools` call
**File:** `api_agent/middleware.py:150`

`load_schema_and_base_url(req_ctx)` makes a network round-trip every time a client lists tools. For large OpenAPI specs or gRPC reflection, this is expensive. No caching at this layer.

---

### H-4. `__DIRECT_RETURN__` magic string sentinel
**File:** `api_agent/llm/provider.py:134`

A stringly-typed sentinel used as cross-layer signaling. If the LLM ever generates this exact string, it would be misinterpreted.

**Fix:** Use a proper sentinel object (`DIRECT_RETURN = object()`) or an enum variant on `RunResult`.

---

### H-5. Broad exception catch leaks info to LLM context
**File:** `api_agent/llm/provider.py:184-186`

`str(e)` on arbitrary exceptions can expose file paths, connection strings, or stack frames to the LLM context.

**Fix:** Sanitize error messages before sending to LLM (e.g., generic "Internal tool error" with details logged server-side).

---

### H-6. Mutable list mutation + return dual contract in providers
**Files:** All three LLM providers' `format_tool_results()` and `format_assistant_tool_calls()`

Methods mutate `messages` in-place AND return it. The caller reassigns the return value (redundant). This hybrid is a mutation hazard if anyone reuses the original list reference.

**Fix:** Either mutate in place and return `None`, or return a new list.

---

### H-7. Monolithic `execute()` function — 190+ lines
**File:** `api_agent/tools/execute.py:64-285`

Handles GraphQL, gRPC (4 streaming modes), and REST in one function. Violates Single Responsibility.

**Fix:** Extract to `_execute_graphql()`, `_execute_grpc()`, `_execute_rest()`.

---

### H-8. Inconsistent error return patterns across modules
**Files:** `query.py`, `execute.py`, `middleware.py`, all agents

Some errors return `{"ok": False, "error": ...}`, others raise `ToolError`/`MissingHeaderError`/`RuntimeError`. MCP clients see different shapes depending on failure location.

**Fix:** Define a unified error contract (e.g., always `{"ok": False, "error": str}` at the tool boundary).

---

### H-9. `RecipeStore` uses `threading.Lock` in async context
**File:** `api_agent/recipe/store.py`

`threading.Lock` blocks the event loop under contention. `suggest_recipes` does scoring under the lock; `find_recipe_by_tool_slug` calls `sanitize_tool_name()` for every record while holding the lock.

**Fix:** Use `asyncio.Lock` or ensure lock hold times are trivially short (copy data out, release, process).

---

### H-10. `render_sql_safe` uses blocklist instead of parameterized queries
**File:** `api_agent/recipe/store.py:69-105`

Strips `;`, `--`, `/*`, `*/` from values but doesn't use parameterized queries. DuckDB supports `$$` dollar-quoting and other syntaxes that bypass this.

**Mitigated by:** DuckDB sandbox disabling external access. Blast radius limited to in-memory data.

---

### H-11. No `pytest-asyncio` mode configured
**File:** `pyproject.toml` (missing `[tool.pytest.ini_options]`)

Without explicit `asyncio_mode`, pytest-asyncio defaults may change across versions. ContextVar bleed between tests is possible.

**Fix:** Add `asyncio_mode = "auto"` to pyproject.toml.

---

### H-12. No upper bounds on major dependencies
**File:** `pyproject.toml`

`fastmcp>=3.0.0`, `openai>=1.0.0`, etc. have no upper bounds. A breaking 4.0 release would silently break installs.

**Fix:** Use `>=3.0.0,<4` style constraints for core deps.

---

### H-13. No security scanning in CI
**Files:** `.github/workflows/`

No CodeQL, Snyk, Trivy, or `pip-audit`. For a project that handles API keys and makes network calls, this is a notable gap.

---

### H-14. Duplicate `FakeLLMProvider` definitions in tests
**Files:** `tests/conftest.py:18-64`, `tests/test_graphql_agent.py:73-107`

Two slightly different `FakeLLMProvider` implementations. The graphql_agent copy has different `format_assistant_tool_calls` behavior. Maintenance hazard.

---

### H-15. Duplicate fixtures shadow conftest
**Files:** `tests/conftest.py:89`, `tests/test_graphql_agent.py:122`

`graphql_ctx` defined in both with different URLs. The conftest version is silently shadowed.

---

### H-16. No ContextVar cleanup in agent tests
**Files:** `test_graphql_agent.py`, `test_rest_agent.py`

Agent tests set ContextVars but never explicitly reset them. Relies on pytest-asyncio creating new event loops per test (not guaranteed without explicit config).

---

## Medium Findings

### Code Quality

| # | Finding | File(s) |
|---|---------|---------|
| M-1 | `PROVIDER` config is unvalidated `str`, should be `Literal["openai", "anthropic", "openai-compat"]` | `config.py:25` |
| M-2 | `TRANSPORT` is unvalidated `str`, should be `Literal["http", "streamable-http", "sse"]` | `config.py:62` |
| M-3 | `ProtocolConfig.agent_type` is `str` not `Literal["graphql", "rest", "grpc"]` | `orchestrator.py:70` |
| M-4 | `ProtocolConfig.tools` typed as bare `list` not `list[ToolDefinition]` | `orchestrator.py:86` |
| M-5 | OpenAI and OpenAI-compat providers are ~90% identical code (DRY violation) | `openai_provider.py`, `openai_compat.py` |
| M-6 | `tool` decorator only maps 4 primitive types, unknown types silently fall back to `"string"` | `llm/tools.py:16-21` |
| M-7 | No retry/backoff on LLM API calls | All providers |
| M-8 | No wall-clock timeout on `run_tool_loop` — slow LLM/API hangs indefinitely | `orchestrator.py:414` |
| M-9 | `runner.py` has 274-line function with deeply nested closures | `recipe/runner.py` |
| M-10 | `_list_recipe_tools` parameter `req_ctx` untyped | `middleware.py:77` |

### Type Safety

| # | Finding | File(s) |
|---|---------|---------|
| M-11 | `target_headers` typed as bare `dict` not `dict[str, str]` | `context.py:100` |
| M-12 | `ToolCall.arguments` typed as bare `dict` not `dict[str, Any]` | `llm/types.py:17` |
| M-13 | `build_api_id` has untyped `ctx` parameter | `recipe/common.py:329` |
| M-14 | `execute_recipe_steps` has untyped `api_step_executor` and `executed_items_list` | `recipe/common.py:508-515` |
| M-15 | `execute()` return type is bare `dict` | `tools/execute.py:95` |
| M-16 | No `__all__` exports in any module | All modules |
| M-17 | No `py.typed` marker for downstream type checking | Package root |

### DRY Violations

| # | Finding | File(s) |
|---|---------|---------|
| M-18 | Repeated JSON parsing boilerplate (5 identical `try/except` blocks) | `context.py:162-193` |
| M-19 | Duplicated truncation logic (3 identical blocks) | `tools/execute.py:115, 242, 279` |
| M-20 | Duplicate `_PLACEHOLDER_RE` regex compiled in two files | `recipe/store.py:42`, `recipe/extractor.py:96` |
| M-21 | Two separate tool-name sanitization functions with subtly different behavior | `recipe/naming.py`, `recipe/common.py` |

### Testing

| # | Finding | File(s) |
|---|---------|---------|
| M-22 | Very low `@pytest.mark.parametrize` usage (3 times across 848 tests) | All test files |
| M-23 | Weak assertion: `assert result["ok"] is True or result["error"] is None or ...` (always passes) | `test_rest_agent.py:196` |
| M-24 | No coverage configuration or enforcement (`--cov`, `.coveragerc`, threshold) | `pyproject.toml` |
| M-25 | `fake_provider_factory` accepts args in either order via isinstance check | `tests/conftest.py:131-149` |

### Documentation & Packaging

| # | Finding | File(s) |
|---|---------|---------|
| M-26 | f-strings in logger calls (9+ instances) — should use lazy `%s` formatting | Multiple files |
| M-27 | No CHANGELOG.md | Project root |
| M-28 | CONTRIBUTING.md missing: pre-commit hooks, type checking step, formatting step | `CONTRIBUTING.md` |
| M-29 | Missing PyPI classifiers (`Typing :: Typed`, topic classifiers) | `pyproject.toml` |
| M-30 | No optional dependency extras for protocol-specific deps (grpc, anthropic, openai) | `pyproject.toml` |
| M-31 | No coverage reporting in CI | `.github/workflows/test.yml` |
| M-32 | `__init__.py` exports underscore-prefixed names in `__all__` | `recipe/__init__.py:37-38` |

---

## Low Findings

| # | Finding | File(s) |
|---|---------|---------|
| L-1 | `create_app()` missing return type annotation | `__main__.py:107` |
| L-2 | `main()` missing return type annotation | `__main__.py:141` |
| L-3 | `health` endpoint parameter `request` untyped | `__main__.py:134` |
| L-4 | `parse_args` not testable (reads `sys.argv` directly) | `__main__.py:28` |
| L-5 | CORS `allow_headers=["*"]` — needs comment or config for production | `__main__.py:119` |
| L-6 | `_PRIVATE_NETWORKS` could be `frozenset` for clarity | `context.py:13-22` |
| L-7 | Silently swallows malformed JSON in `X-Target-Headers` | `context.py:164-165` |
| L-8 | `MAX_TOOL_NAME_LEN = 60` — no docstring explaining why 60 | `middleware.py:25` |
| L-9 | Broad `except Exception` in middleware JSON parsing | `middleware.py:234` |
| L-10 | `init_tracing()` defined but never called from entry point | `tracing.py` |
| L-11 | `get_table_schema_summary` deprecated but no `warnings.warn()` | `executor.py:172-174` |
| L-12 | `execute_graphql` trivial one-line wrapper adds no value | `executor.py:241-254` |
| L-13 | `response.choices[0]` with no bounds check | `openai_provider.py:41`, `openai_compat.py:60` |
| L-14 | Default model duplicated in provider class and factory | `anthropic_provider.py:16`, `factory.py:12` |
| L-15 | `_ProviderProxy` loses type safety (`type: ignore[assignment]`) | `agent/model.py:64` |
| L-16 | Sequential tool execution — could use `asyncio.gather()` for parallel calls | `llm/provider.py:164-188` |
| L-17 | REST client logs header keys at INFO level for every request (noisy) | `rest/client.py:117-124` |
| L-18 | `naming.py` is 10-line module for a single function | `recipe/naming.py` |
| L-19 | YAML detection by `startswith("{")` is fragile (BOM edge case) | `rest/schema_loader.py:42` |
| L-20 | `RecipeRecord` not frozen — shallow copy of `recipe` dict could leak mutations | `recipe/store.py:178` |
| L-21 | Inconsistent error return formats across protocol clients | All clients |
| L-22 | No Python 3.12+ `match` statements used (natural for protocol routing) | `execute.py`, `query.py` |
| L-23 | Docker base image pinned to tag not digest | `Dockerfile` |
| L-24 | Release workflow has no test gate | `.github/workflows/release.yml` |

---

## Good Patterns

These are patterns worth preserving and highlighting in contributor docs:

| # | Pattern | Where |
|---|---------|-------|
| I-1 | Frozen dataclasses for `RequestContext`, `AgentContextVars`, `ProtocolConfig` | `context.py`, `orchestrator.py` |
| I-2 | Template Method pattern in `LLMProvider` ABC | `llm/provider.py` |
| I-3 | Composition-over-inheritance orchestrator (ProtocolConfig dataclass) | `agent/orchestrator.py` |
| I-4 | Graceful degradation — tracing is no-op when not configured | `tracing.py` |
| I-5 | Lazy imports in provider factory | `llm/factory.py` |
| I-6 | Correct mutable-container pattern for ContextVars | All agents |
| I-7 | Step executor factories capture `ctx` at creation time (race condition prevention) | `graphql_agent.py:437` |
| I-8 | DuckDB `_sandbox()` disabling external access | `executor.py` |
| I-9 | `_safe_table_name()` for SQL injection mitigation | `executor.py` |
| I-10 | Multi-stage Docker build with non-root user, layer optimization, HEALTHCHECK | `Dockerfile` |
| I-11 | OIDC trusted publishing for PyPI | `.github/workflows/release.yml` |
| I-12 | SHA-pinned GitHub Actions (in test.yml and release.yml) | `.github/workflows/` |
| I-13 | `filtering.py` — clean, immutable, well-tested (72 tests) | `api_agent/filtering.py` |
| I-14 | Consistent `{"success": bool, "data"?, "error"?}` pattern in protocol clients | All clients |
| I-15 | Test IDs (T005, T006, etc.) mapped to test plan | Test files |
| I-16 | `removeprefix()` usage (Python 3.9+) | `middleware.py` |
| I-17 | PEP 621 compliant `[project]` table | `pyproject.toml` |
| I-18 | Contributor Covenant v2.1 with enforcement contact | `CODE_OF_CONDUCT.md` |
| I-19 | Annotated + Field for MCP tool parameter descriptions | `tools/query.py`, `tools/execute.py` |

---

## Recommended Priority Order

### Phase 1 — Must Fix (Critical)
1. **C-1:** Remove git dependency from core deps (publish toon_format to PyPI or vendor it)
2. **C-2:** Add httpx/gRPC connection pooling (shared client per target host)
3. **C-4:** Restrict DuckDB to SELECT-only queries
4. **C-5:** Add max_attempts guard to `deduplicate_tool_name`
5. **C-3:** Add `close()` to LLM provider ABC, wire into app shutdown

### Phase 2 — Should Fix (High Impact)
6. **H-11:** Add `asyncio_mode = "auto"` to pyproject.toml
7. **H-13:** Add security scanning to CI (CodeQL or pip-audit)
8. **H-8:** Unify error return contract at tool boundary
9. **H-5:** Sanitize exception messages before sending to LLM
10. **H-14/H-15:** Deduplicate test doubles, remove shadowed fixtures
11. **H-12:** Add upper bounds to major dependency versions

### Phase 3 — Quality Improvements (Medium)
12. **M-1/M-2/M-3:** Add `Literal` types for config enums
13. **M-5:** Extract OpenAI-compat as subclass of OpenAI provider
14. **M-17:** Add `py.typed` marker
15. **M-24/M-31:** Add coverage configuration and CI reporting
16. **M-26:** Fix f-string logging to use `%s` lazy formatting
17. **M-18/M-19/M-20:** DRY up duplicated code

### Phase 4 — Polish
18. **M-27:** Add CHANGELOG.md
19. **M-30:** Add optional dependency extras (`[grpc]`, `[anthropic]`, `[openai]`)
20. **L-22:** Consider `match` statements for protocol routing
21. **L-16:** Parallel tool execution with `asyncio.gather()`

---

*Review conducted by Claude Code against the full ratatoskr codebase. 96 findings across 30+ modules.*
