# Feature Specification: Close Structural Test Coverage Gaps

**Feature Branch**: `002-test-coverage-gaps`
**Created**: 2026-02-25
**Status**: Draft
**Input**: Close the structural test coverage gaps identified in the reverse-TDD analysis for open-source readiness.

## Clarifications

### Session 2026-02-25

- Q: Should tests distinguish between errors originating from api-agent (our regressions) vs errors from upstream (target API failures, LLM provider errors)? → A: Test both — our regressions (assertions on correct output) AND upstream error handling (assertions on graceful degradation).
- Q: Should existing over-mocked recipe runner tests be kept alongside new behavioral tests, replaced, or refactored in-place? → A: Replace — delete old over-mocked tests; new behavioral tests are the canonical coverage.
- Q: How should SC-008 (90% regression detection target) be verified? → A: Second reverse-TDD review — repeat the module-by-module analysis and re-score after tests are written.

### Delphi Panel Conditions (2026-02-25)

- **Condition 1 (Test Infrastructure):** Resolved. LLM provider is a custom lazy singleton (`api_agent/agent/model.py`), not LangChain/ACP. Tests should monkeypatch `api_agent.agent.model._provider` with a `FakeLLMProvider` before calling orchestrators.
- **Condition 2 (GraphQL error handling):** Resolved — fix the code. `graphql/client.py` line 56-57 discards `data` when `errors` is present, violating the GraphQL spec (partial success). Code fix: return both `data` and `errors` when both are present. Tests should assert the fixed behavior.
- **Condition 3 (Subscription operations):** Resolved — accepted risk, out of scope. Subscriptions require WebSocket transport; this app uses HTTP POST only. A subscription sent over HTTP would fail at the server, not at the mutation blocker. No test needed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Maintainer Validates Orchestration Regressions (Priority: P1)

A project maintainer receives a pull request that refactors the GraphQL or REST agent orchestration logic. The maintainer runs the test suite and expects that any change to the wiring -- schema fetching, recipe lookup, context variable initialization, result assembly, or error handling -- produces a clear test failure explaining what broke.

**Why this priority**: The orchestration entry points (`process_query`, `process_rest_query`) are the highest-risk untested code. They wire together every subsystem and are the most likely targets for future refactoring. Without tests here, the entire CI gate is blind to the most impactful category of regressions.

**Independent Test**: Can be fully tested by running the test suite after modifying any line in `process_query()` or `process_rest_query()`. Delivers confidence that the two primary code paths produce correct, structured output for all supported scenarios (success, partial results, errors).

**Acceptance Scenarios**:

1. **Given** a GraphQL query that succeeds on the first agent turn, **When** the orchestrator processes the query, **Then** the response contains `ok=True`, the executed queries list, and the formatted data.
2. **Given** a GraphQL query where the agent exceeds the maximum turn limit, **When** the orchestrator processes the query, **Then** the response contains partial results with whatever data was collected before the limit.
3. **Given** a REST query targeting an API with a valid OpenAPI spec, **When** the orchestrator processes the query, **Then** the response contains `ok=True`, the API calls made, and the formatted data.
4. **Given** a REST query with polling configuration, **When** the orchestrator processes the query, **Then** a polling tool is available to the agent and polling state is tracked correctly.
5. **Given** any orchestrator run that succeeds, **When** recipe extraction is enabled, **Then** the system attempts to extract and cache a reusable recipe from the successful run.
6. **Given** a query where the schema fetch fails due to an upstream error (network timeout, invalid endpoint, 500 response), **When** the orchestrator processes the query, **Then** the upstream error is surfaced clearly in the response without crashing (graceful degradation).
7. **Given** a query where the orchestrator has a bug (e.g., ContextVar not initialized, missing result assembly), **When** tests run against the broken code, **Then** at least one test fails with a clear assertion error (regression detection).

---

### User Story 2 - Contributor Verifies Safety Boundaries (Priority: P1)

A contributor modifies the GraphQL client or the execute tool. The test suite must verify that safety boundaries remain intact: mutations are blocked in GraphQL, unsafe HTTP methods are blocked in REST (unless explicitly allowed), and response parsing handles both success and error shapes.

**Why this priority**: Safety mechanisms are the most critical behavioral contracts in the system. The mutation blocker is a security boundary that protects target APIs from unintended writes. A regex change or removal must produce an immediate, obvious test failure.

**Independent Test**: Can be tested by attempting to execute known mutation patterns and verifying they are rejected. Delivers assurance that the read-only safety guarantee holds across all query variations.

**Acceptance Scenarios**:

1. **Given** a GraphQL query string containing a mutation keyword, **When** the client attempts to execute it, **Then** the request is rejected before any HTTP call is made.
2. **Given** a GraphQL mutation with unusual formatting (leading whitespace, mixed case, multiline), **When** the client checks it, **Then** the mutation is still detected and blocked.
3. **Given** a valid GraphQL query (not a mutation), **When** the client executes it, **Then** the HTTP request is sent and the response is parsed correctly.
4. **Given** a GraphQL response with an `errors` field, **When** the client parses it, **Then** errors are surfaced in the return value alongside any partial data.
5. **Given** a direct execute call for GraphQL with valid query and variables, **When** the execute tool processes it, **Then** the response is returned with appropriate truncation for large results.
6. **Given** a direct execute call for REST with a path and method, **When** the execute tool processes it, **Then** the request uses the correct base URL (from header or schema) and returns the parsed response.

---

### User Story 3 - Maintainer Trusts Recipe Pipeline End-to-End (Priority: P2)

A maintainer changes how recipes are stored, executed, or exposed as MCP tools. The test suite must verify that the recipe runner actually executes recipe steps against real recipe structures -- not mocked-away step executors that bypass the logic under test.

**Why this priority**: The recipe system is a differentiating feature of the project. Current tests mock away the step execution, meaning the actual GraphQL/REST step handling, context variable management, and error paths within the runner are invisible to CI. This reduces trust in recipe-related PRs.

**Independent Test**: Can be tested by creating a recipe with known steps, executing it through the runner with only HTTP transport mocked, and verifying the output matches expected results.

**Acceptance Scenarios**:

1. **Given** a stored GraphQL recipe with parameterized query and SQL post-processing steps, **When** the recipe runner executes it, **Then** the GraphQL query is sent with bound parameters and the SQL step processes the response data.
2. **Given** a stored REST recipe with a parameterized API call, **When** the recipe runner executes it, **Then** the REST call is made with correctly substituted parameters.
3. **Given** a recipe execution with `return_directly=True`, **When** the runner completes, **Then** the output is formatted as CSV.
4. **Given** a recipe whose schema hash no longer matches the current API schema, **When** the runner attempts execution, **Then** the recipe is rejected or flagged as stale.

---

### User Story 4 - Contributor Changes Configuration Without Breaking Deployments (Priority: P2)

A contributor renames an environment variable, changes a default value, or modifies the alias resolution chain in the settings. The test suite must detect that the configuration contract has changed, preventing silent breakage for existing deployments that depend on specific env var names or defaults.

**Why this priority**: Configuration is the most common source of "works on my machine" bugs in open-source projects. Contributors unfamiliar with the deployment patterns may not realize that renaming `API_AGENT_PROVIDER` or changing its default breaks every existing deployment.

**Independent Test**: Can be tested by asserting that known env var names resolve to expected defaults and that the alias priority chain produces the correct value when multiple vars are set.

**Acceptance Scenarios**:

1. **Given** no environment variables are set, **When** the settings are loaded, **Then** each field has its documented default value.
2. **Given** `API_AGENT_API_KEY` is set, **When** the settings are loaded, **Then** the API key field uses that value.
3. **Given** both `API_AGENT_API_KEY` and `OPENAI_API_KEY` are set, **When** the settings are loaded, **Then** the alias priority chain selects the correct value.
4. **Given** the settings are loaded, **When** the `MCP_SLUG` computed field is accessed, **Then** it produces a stable, predictable value.

---

### User Story 5 - Maintainer Upgrades LLM Provider SDK Without Silent Breakage (Priority: P3)

A maintainer upgrades the OpenAI, Anthropic, or compatible-provider SDK. The test suite must verify that response parsing -- converting provider-specific response shapes into the internal `LLMResponse` type -- still works correctly after the upgrade.

**Why this priority**: Provider SDKs change their response shapes across major versions. The parsing logic is provider-specific and non-trivial (Anthropic content blocks vs. OpenAI choice messages). These tests act as a contract between the internal type system and the external SDK.

**Independent Test**: Can be tested by replaying recorded response fixtures through each provider's `complete()` method and verifying the parsed `LLMResponse` matches expected structure.

**Acceptance Scenarios**:

1. **Given** a recorded OpenAI response with a text completion, **When** the OpenAI provider parses it, **Then** the `LLMResponse` contains the text content.
2. **Given** a recorded OpenAI response with tool calls, **When** the provider parses it, **Then** the `LLMResponse` contains correctly parsed tool call objects.
3. **Given** a recorded Anthropic response with content blocks, **When** the Anthropic provider parses it, **Then** the `LLMResponse` maps blocks to the internal type.
4. **Given** a recorded OpenAI-compatible response where tools are not supported, **When** the provider encounters a tool error, **Then** it retries without tools (graceful degradation).
5. **Given** any provider receives a malformed or error response, **When** it parses the result, **Then** the error is surfaced in the `LLMResponse` without crashing.

---

### User Story 6 - Maintainer Validates MCP Tool Routing (Priority: P3)

A maintainer modifies the query tool handler or middleware routing logic. The test suite must verify that MCP requests are correctly routed to the appropriate agent (GraphQL vs. REST), that missing headers produce clear errors, and that the middleware transforms tool names correctly for non-recipe paths.

**Why this priority**: The MCP tool handler and middleware are the system's public interface. While the recipe path through middleware is tested, the standard (non-recipe) path -- which handles all direct queries -- is not.

**Independent Test**: Can be tested by invoking the query handler with different header configurations and verifying correct routing behavior.

**Acceptance Scenarios**:

1. **Given** an MCP request with `X-API-Type: graphql`, **When** the query tool processes it, **Then** it routes to the GraphQL agent.
2. **Given** an MCP request with `X-API-Type: rest`, **When** the query tool processes it, **Then** it routes to the REST agent.
3. **Given** an MCP request missing the `X-Target-URL` header, **When** the query tool processes it, **Then** a clear error is returned indicating the missing header.
4. **Given** a middleware call for a non-recipe tool, **When** the middleware transforms the tool name, **Then** the internal tool name is correctly resolved from the session-specific prefix.

---

### Edge Cases

- What happens when the GraphQL schema introspection returns an empty or malformed schema?
- How does the orchestrator behave when the LLM returns a tool call for an unknown tool?
- What happens when DuckDB fails during SQL post-processing (e.g., malformed SQL from the agent)?
- How does the recipe runner handle a recipe with zero steps?
- What happens when the config env var alias chain has conflicting values across all alias sources?
- How does the execute tool behave when called with a GraphQL subscription (not query or mutation)?
- What happens when the REST schema loader encounters an OpenAPI spec with no paths defined?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Test suite MUST verify that `process_query()` produces correct structured output for successful single-turn GraphQL queries.
- **FR-002**: Test suite MUST verify that `process_query()` returns partial results when the agent exceeds the maximum turn limit.
- **FR-003**: Test suite MUST verify that `process_rest_query()` produces correct structured output for successful REST queries against an OpenAPI-described API.
- **FR-004**: Test suite MUST verify that `process_rest_query()` creates and configures a polling tool when polling headers are present.
- **FR-005**: Test suite MUST verify that the GraphQL client blocks all mutation variants (standard, mixed case, leading whitespace, multiline).
- **FR-006**: Test suite MUST verify that the GraphQL client correctly parses both success and error response shapes, including partial success (both `data` and `errors` present per GraphQL spec). Prerequisite: fix `graphql/client.py` to return `data` alongside `errors` when both are present.
- **FR-007**: Test suite MUST verify that the `_execute` MCP tool handles both GraphQL and REST paths, including base URL resolution and response truncation.
- **FR-008**: Test suite MUST verify that configuration env var names, alias resolution priority, defaults, and computed fields are stable.
- **FR-009**: Test suite MUST verify that the recipe runner executes recipe steps against real recipe structures with only HTTP transport mocked. Existing over-mocked tests in `test_recipe_runner.py` MUST be replaced (not kept alongside) by the new behavioral tests.
- **FR-010**: Test suite MUST verify that the `query()` MCP handler routes correctly to GraphQL or REST agents based on headers and produces appropriate errors for missing headers.
- **FR-011**: Test suite MUST verify that each LLM provider's `complete()` method correctly parses provider-specific response shapes into the internal `LLMResponse` type.
- **FR-012**: Test suite MUST verify that the middleware `on_call_tool` path for non-recipe tools correctly transforms tool names and forwards context.
- **FR-013**: All new tests MUST be deterministic -- no dependency on live APIs, live LLM calls, or network availability.
- **FR-016**: Tests MUST distinguish between two error categories: (a) regression detection tests that assert correct output and fail when our code breaks, and (b) upstream error handling tests that verify graceful degradation when target APIs or LLM providers return errors.
- **FR-014**: All new tests MUST run within the existing `pytest` and `pytest-asyncio` framework and integrate with existing CI.
- **FR-015**: New tests MUST NOT break or invalidate any of the existing 363 passing tests.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The two main orchestrator entry points (`process_query`, `process_rest_query`) each have at least 4 test scenarios covering success, partial results, error propagation, and recipe extraction.
- **SC-002**: The GraphQL mutation blocker has explicit tests for at least 5 mutation format variants (standard, mixed case, leading whitespace, multiline, inline fragment with mutation).
- **SC-003**: The `_execute` MCP tool has tests covering both GraphQL and REST paths.
- **SC-004**: Configuration contract tests assert every public env var name, every default value, and the alias resolution priority chain.
- **SC-005**: Recipe runner tests execute real recipe step logic (not mocked step executors) for both GraphQL and REST recipe types.
- **SC-006**: Each LLM provider (`openai`, `anthropic`, `openai-compat`) has at least 2 `complete()` tests (success with text, success with tool calls).
- **SC-007**: All new tests pass in CI across the Python 3.11 and 3.12 matrix without flakiness.
- **SC-008**: After all tests are added, a second reverse-TDD review (repeating the same module-by-module analysis methodology used for the initial 60% assessment) confirms the effective regression detection rate is at least 90%.

### Assumptions

- Tests may mock HTTP transport (httpx) and LLM API responses, but must use real instances of ContextVar, RecipeStore, DuckDB executor, and result assembly logic.
- Recorded response fixtures will be used for LLM provider tests rather than live API calls.
- The existing test infrastructure (pytest, pytest-asyncio, monkeypatch) is sufficient; no new test frameworks or dependencies are required unless the implementation phase identifies a specific need.
- The 3-PR strategy from DECISION.md (critical gaps, weak coverage, cleanup) is the recommended delivery approach but not a hard requirement of this specification.
