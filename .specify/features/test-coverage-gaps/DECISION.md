# Decision: Close Test Coverage Gaps for Open-Source Readiness

**Version:** 1.0
**Date:** 2026-02-25
**Status:** Proposed

## Use Case

API Agent (Ratatoskr) is being prepared for open-source release. The existing test suite
(363 tests) is well-written where it exists and covers leaf-level functions thoroughly --
schema parsing, DuckDB executor, REST safety guards, recipe store, LLM tool loop, and
format methods. However, a reverse-TDD analysis reveals that **zero tests exist for the
orchestration layer** -- the code that actually wires everything together and constitutes
the primary entry points for both API types.

The test pipeline currently covers this shape:

```
MCP tool handler --> [ agent orchestrator ] --> LLM loop --> [ tool functions ] --> HTTP clients
                      ^^^ UNTESTED ^^^                                             ^^^ UNTESTED
```

Both ends of the pipeline -- the entry points that callers invoke and the HTTP clients that
talk to real services -- have no test coverage. The interior (bracketed) components are
well-tested. This means the system's integration seams are invisible to the test suite:
a refactor to `process_query()` or `process_rest_query()` could silently break the entire
product without any test failure.

## Business Value

### Open-source credibility
Contributors evaluating whether to adopt or contribute to this project will look at the
test suite to gauge project health. A suite that covers helpers but skips the main entry
points signals "hobby project," not "production-ready tool." Closing these gaps is table
stakes for credible open-source release.

### Safe refactoring
The orchestration layer (`process_query`, `process_rest_query`) is the most likely target
for future changes -- adding new LLM providers, changing the recipe pipeline, modifying
context management, adjusting error handling. Without tests, every such change requires
manual end-to-end verification against live APIs with live LLM calls.

### PR validation for maintainers
When external contributors submit PRs, maintainers need CI to catch regressions. The
current suite is approximately 60% effective at catching real regressions. The untested
orchestration layer is where most regressions will originate because it is where all the
wiring decisions live.

### Cost avoidance
Bugs in the orchestration layer (e.g., incorrect ContextVar initialization, wrong schema
hash comparison, missing recipe extraction) manifest as silent data corruption or dropped
results -- the hardest class of bugs to diagnose in production.

## Scope

### In Scope (Must Fix) -- Zero Coverage

These modules have **zero test coverage** and represent the highest-risk gaps:

| # | Module | Entry Point / Function | Lines | Risk |
|---|--------|------------------------|-------|------|
| 1 | `api_agent/agent/graphql_agent.py` | `process_query()` | 707 | THE main GraphQL entry point. Orchestrates schema fetch, recipe lookup, agent loop, ContextVar initialization, result assembly, error handling, partial results on MaxTurnsExceeded. |
| 2 | `api_agent/agent/rest_agent.py` | `process_rest_query()` | 823 | THE main REST entry point. Same orchestration pattern plus base URL resolution, polling tool creation, and skip-polling-recipes logic. |
| 3 | `api_agent/graphql/client.py` | `execute_query()` | 63 | GraphQL HTTP client. Mutation blocking regex (`_MUTATION_PATTERN`) is untested. Response parsing (errors vs. data) is untested. HTTP error handling is untested. |
| 4 | `api_agent/tools/execute.py` | `execute()` (via `register_execute_tool`) | 108 | The `_execute` MCP tool endpoint. Zero tests for either GraphQL or REST paths, including base URL fallback, truncation, and error propagation. |
| 5 | `api_agent/config.py` | `Settings` class | 72 | Configuration via pydantic-settings. Env var alias resolution (`AliasChoices`), computed fields (`MCP_SLUG`), and default values are all unverified. A renamed env var or changed default would go undetected. |

### In Scope (Should Fix) -- Weak or Over-Mocked Coverage

These modules have tests, but the tests mock away the logic they should be verifying:

| # | Module | Issue |
|---|--------|-------|
| 6 | `api_agent/recipe/runner.py` | `execute_recipe_tool()` tested with monkeypatched `execute_recipe_steps` that bypasses all real step execution logic. The actual GraphQL/REST step executors inside the function are never invoked. |
| 7 | `api_agent/tools/query.py` | `_build_response()` helper is tested, but the `query()` MCP handler itself -- which routes GraphQL vs REST, handles `MissingHeaderError`, triggers recipe change notifications, and decides CSV vs dict response -- is untested. |
| 8 | LLM provider `complete()` methods | All 3 providers (`openai_provider.py`, `anthropic_provider.py`, `openai_compat.py`) have `format_*` method tests but zero tests for `complete()` response parsing. The OpenAI-compat retry-without-tools fallback is also untested. |
| 9 | `api_agent/middleware.py` | `on_call_tool` recipe path is well-tested. The non-recipe path (tool name validation, `_suffix` -> `internal_name` transformation, modified context forwarding) has zero tests. |

### Out of Scope (Acceptable As-Is)

These modules are explicitly excluded from this effort:

| Module | Reason |
|--------|--------|
| `agent/prompts.py` | String constants only. No logic to test. |
| `agent/progress.py` | 6 lines, trivial counter. |
| `agent/contextvar_utils.py` | 8 lines, 2 helper functions. Indirectly tested through callers. |
| `tracing.py` | Optional/conditional OpenTelemetry setup. Testing adds complexity without regression value. |
| `__main__.py` | Server bootstrap (FastMCP app creation, middleware registration). Hard to unit test; covered by manual smoke testing and Docker integration. |

## Key Decisions

### 1. Integration tests for orchestrators, not end-to-end tests

**Decision:** Test `process_query()` and `process_rest_query()` as integration tests with
mocked HTTP transport (httpx) and mocked LLM responses -- but with real ContextVar
initialization, real recipe store, real DuckDB executor, and real result assembly.

**Rationale:** End-to-end tests against live APIs are flaky, expensive (LLM tokens), and
slow. Mocking only the I/O boundary (HTTP + LLM) preserves the integration value while
keeping tests deterministic and fast. The goal is to test the *wiring*, not the LLM's
ability to generate queries.

**What this catches:** ContextVar initialization bugs, recipe lookup/extraction flow,
partial result handling on MaxTurnsExceeded, error propagation, schema truncation thresholds,
return_directly flag behavior.

### 2. Unit tests for the GraphQL client mutation blocker

**Decision:** Write focused unit tests for `_MUTATION_PATTERN` regex matching and
`execute_query()` response parsing. Mock only httpx.

**Rationale:** The mutation blocker is a security boundary. If the regex fails to match a
mutation variant (e.g., leading whitespace, mixed case, multiline), the system silently
allows writes to target APIs. This is a safety-critical path that must have explicit tests
for known edge cases.

### 3. Behavioral tests for recipe runner, not mock-heavy tests

**Decision:** Replace the current over-mocked `test_recipe_runner.py` tests with behavioral
tests that use a real `RecipeStore`, real `execute_recipe_steps`, and mocked HTTP only.

**Rationale:** The current tests monkeypatch `execute_recipe_steps` itself, which means
they verify that `execute_recipe_tool` *calls* the step executor but not that the step
executor *works correctly* with real recipe structures. The function is 182 lines with
two distinct code paths (GraphQL and REST) and its own ContextVar management -- all of
which are invisible to the current tests.

### 4. Config tests as snapshot/contract tests

**Decision:** Write tests that assert specific env var names resolve to expected defaults,
that `AliasChoices` priority works correctly, and that `MCP_SLUG` computation is stable.

**Rationale:** Configuration is the most common source of "works on my machine" bugs in
open-source projects. A contributor who renames an env var or changes a default should see
a test failure, not a silent behavior change discovered in production.

### 5. LLM provider complete() tests with recorded fixtures

**Decision:** Test `complete()` methods using httpx mock responses that replay recorded
API response shapes (not live calls). Cover the happy path, error responses, and
malformed tool call arguments.

**Rationale:** The `complete()` methods parse SDK-specific response objects into the
internal `LLMResponse` type. This parsing logic is provider-specific and non-trivial
(Anthropic uses content blocks, OpenAI uses choice messages, OpenAI-compat has the
retry-without-tools fallback). A provider SDK upgrade that changes response shapes would
be caught by these tests.

## Estimated Complexity

| Work Item | Points | Notes |
|-----------|--------|-------|
| Orchestrator integration tests (GraphQL + REST) | 8 | Largest item. Requires careful mock setup for LLM tool loop. |
| GraphQL client unit tests | 2 | Small module, focused scope. |
| Execute tool tests | 3 | Two code paths (GraphQL/REST), base URL fallback logic. |
| Config contract tests | 2 | Straightforward assertions. |
| Recipe runner behavioral tests | 5 | Replace existing mocks, two code paths, ContextVar setup. |
| Query tool handler tests | 3 | GraphQL/REST routing, CSV detection, recipe notification. |
| LLM provider complete() tests | 5 | Three providers, fixture setup for each SDK. |
| Middleware non-recipe path tests | 3 | Tool name validation, internal name mapping. |
| **Total** | **31** | Recommend splitting into 2-3 PRs by risk tier. |

## Recommended PR Strategy

1. **PR 1 -- Critical gaps (Must Fix #1-5):** Orchestrators, GraphQL client, execute tool,
   config. This PR alone raises effective regression coverage from ~60% to ~85%.

2. **PR 2 -- Weak coverage (Should Fix #6-9):** Recipe runner, query handler, LLM providers,
   middleware non-recipe path. Brings coverage to ~95% effective.

3. **PR 3 -- Cleanup:** Remove or refactor any tests that became redundant after the
   behavioral tests replaced mock-heavy tests.

## References

- Test analysis conducted: 2026-02-25 (reverse-TDD review of all `api_agent/` modules against `tests/`)
- Existing test count: 363 tests across 16 test files
- Lines of untested code: ~2,652 lines across 11 modules
- Framework: pytest + pytest-asyncio, httpx mocking via `pytest-httpx` or `respx`
