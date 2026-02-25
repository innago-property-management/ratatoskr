# Tasks: Close Structural Test Coverage Gaps

**Input**: Design documents from `/specs/002-test-coverage-gaps/`
**Prerequisites**: plan.md, spec.md, research.md

**Tests**: This feature IS tests — every task produces test files. TDD approach: write tests that assert expected behavior, verify they fail against current code, then fix code where needed.

**Organization**: Tasks grouped by user story (US1-US6 from spec.md). US1+US2 = PR1 (critical gaps). US3-US6 = PR2 (weak coverage).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create test fixtures, FakeLLMProvider, and shared helpers

- [X] T001 Create `tests/fixtures/` directory and sample GraphQL schema fixture at `tests/fixtures/graphql_schemas/sample_schema.graphql`
- [X] T002 Create recorded LLM response fixtures at `tests/fixtures/openai_text_response.json`, `tests/fixtures/openai_tool_call_response.json`, `tests/fixtures/anthropic_text_response.json`, `tests/fixtures/anthropic_tool_call_response.json`
- [X] T003 Create `FakeLLMProvider` test helper and shared fixtures in `tests/conftest.py` (or `tests/helpers.py`) — implements `LLMProvider` interface, returns canned `LLMResponse` objects, supports multi-turn tool call sequences

**Checkpoint**: Test infrastructure ready — all story phases can begin

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Code fix that must land before tests can assert correct behavior

**CRITICAL**: This fix changes behavior that US1 and US2 tests depend on

- [X] T004 Fix `api_agent/graphql/client.py` line 56-57 — return `data` alongside `errors` when both present (per GraphQL spec). Change: `if "errors" in result: return {"success": False, "error": result["errors"]}` → return both `data` and `errors` fields

**Checkpoint**: Code fix in place — test phases can begin

---

## Phase 3: User Story 1 — Maintainer Validates Orchestration Regressions (P1) MVP

**Goal**: Integration tests for `process_query()` and `process_rest_query()` — the two main entry points

**Independent Test**: Run `pytest tests/test_graphql_agent.py tests/test_rest_agent.py -v` — all pass, and modifying any orchestration wiring line causes at least one failure

- [X] T005 [P] [US1] Create `tests/test_graphql_agent.py` — test `process_query()` success path: mock httpx for schema introspection + query execution, use FakeLLMProvider for agent loop, assert response contains `ok=True`, executed queries, formatted data. Use real ContextVar + DuckDB.
- [X] T006 [P] [US1] Create `tests/test_rest_agent.py` — test `process_rest_query()` success path: mock httpx for OpenAPI fetch + REST calls, use FakeLLMProvider, assert response structure. Include polling tool creation test with `X-Poll-Paths` header.
- [X] T007 [US1] Add MaxTurnsExceeded partial results test to `tests/test_graphql_agent.py` — FakeLLMProvider returns tool calls exceeding max turns, assert partial results returned with collected data
- [X] T008 [US1] Add upstream error tests to both `tests/test_graphql_agent.py` and `tests/test_rest_agent.py` — mock httpx to return 500/timeout for schema fetch, assert error surfaced in response without crash (graceful degradation)
- [X] T009 [US1] Add recipe extraction test to `tests/test_graphql_agent.py` — after successful query with recipes enabled, assert `RECIPE_STORE` contains extracted recipe (use real RecipeStore)

**Checkpoint**: Both orchestrator entry points have integration tests. ~60% → ~80% effective coverage.

---

## Phase 4: User Story 2 — Contributor Verifies Safety Boundaries (P1)

**Goal**: Unit tests for GraphQL mutation blocker, response parsing, and execute tool

**Independent Test**: Run `pytest tests/test_graphql_client.py tests/test_execute_tool.py -v`

- [X] T010 [P] [US2] Create `tests/test_graphql_client.py` — mutation blocker tests: standard mutation, mixed case (`MuTaTiOn`), leading whitespace, multiline, inline fragment. Assert all blocked before HTTP call.
- [X] T011 [P] [US2] Create `tests/test_execute_tool.py` — test `register_execute_tool` for GraphQL path (query + variables + endpoint) and REST path (method + path + base URL resolution). Mock httpx. Assert response truncation for large results.
- [X] T012 [US2] Add response parsing tests to `tests/test_graphql_client.py` — success response (data only), error response (errors only), partial success (both data and errors per fixed code from T004), HTTP error, no endpoint error
- [X] T013 [US2] Add valid query execution test to `tests/test_graphql_client.py` — non-mutation query passes through, httpx called with correct payload/headers, response parsed correctly

**Checkpoint**: Safety boundaries and execute tool fully tested. PR1 scope complete (~85% effective).

---

## Phase 5: User Story 3 — Maintainer Trusts Recipe Pipeline (P2)

**Goal**: Replace over-mocked recipe runner tests with behavioral tests

**Independent Test**: Run `pytest tests/test_recipe_runner.py -v`

- [X] T014 [US3] Delete existing `tests/test_recipe_runner.py` content and rewrite with behavioral tests — create real `RecipeStore` with known recipes, execute `execute_recipe_tool()` with mocked httpx only, assert GraphQL recipe path sends correct query with bound parameters
- [X] T015 [US3] Add REST recipe behavioral test to `tests/test_recipe_runner.py` — REST recipe with parameterized API call, assert correct URL + method + substituted parameters
- [X] T016 [US3] Add `return_directly=True` CSV test and stale schema hash rejection test to `tests/test_recipe_runner.py`

**Checkpoint**: Recipe runner has real behavioral coverage.

---

## Phase 6: User Story 4 — Configuration Contract (P2)

**Goal**: Snapshot/contract tests for Settings class

**Independent Test**: Run `pytest tests/test_config.py -v`

- [X] T017 [US4] Create `tests/test_config.py` — assert all env var names resolve to documented defaults when no env vars set. Assert `API_AGENT_API_KEY` takes precedence. Assert `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` alias chain works correctly. Assert `MCP_SLUG` computed field is stable.

**Checkpoint**: Config contract locked down.

---

## Phase 7: User Story 5 — LLM Provider SDK Upgrade Safety (P3)

**Goal**: Test `complete()` methods with recorded response fixtures

**Independent Test**: Run `pytest tests/test_llm/test_openai_complete.py tests/test_llm/test_anthropic_complete.py tests/test_llm/test_compat_complete.py -v`

- [X] T018 [P] [US5] Create `tests/test_llm/test_openai_complete.py` — test `complete()` with recorded text response fixture and tool call response fixture. Mock httpx at SDK level. Assert `LLMResponse` structure.
- [X] T019 [P] [US5] Create `tests/test_llm/test_anthropic_complete.py` — test `complete()` with recorded content block fixtures. Assert `LLMResponse` maps correctly.
- [X] T020 [P] [US5] Create `tests/test_llm/test_compat_complete.py` — test `complete()` success path + retry-without-tools fallback at `api_agent/llm/openai_compat.py` lines 51-58. Assert graceful degradation.

**Checkpoint**: Provider SDK upgrade safety net in place.

---

## Phase 8: User Story 6 — MCP Tool Routing (P3)

**Goal**: Test query handler routing and middleware non-recipe path

**Independent Test**: Run `pytest tests/test_query_tool.py tests/test_middleware_routing.py -v`

- [X] T021 [P] [US6] Create `tests/test_query_tool.py` — test `query()` handler routes to GraphQL agent when `X-API-Type: graphql`, REST agent when `X-API-Type: rest`. Test missing `X-Target-URL` returns clear error. Test recipe change notification on success.
- [X] T022 [P] [US6] Create `tests/test_middleware_routing.py` — test `on_call_tool` non-recipe path: tool name transformation from session prefix to internal name, context forwarding to underlying handler.

**Checkpoint**: All user stories complete. PR2 scope done (~95% effective).

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T023 Run full test suite `uv run pytest tests/ -v` — verify all new + existing 363 tests pass (511 passing)
- [X] T024 Run `uv run ruff check api_agent/ tests/` — lint new test files (all checks passed)
- [ ] T025 Second reverse-TDD review — repeat module-by-module analysis, re-score effective regression detection rate, document in review artifact

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: T004 depends on T001 (needs fixture for test validation)
- **US1 (Phase 3)**: Depends on T003 (FakeLLMProvider) and T004 (code fix)
- **US2 (Phase 4)**: Depends on T004 (code fix for response parsing tests)
- **US3-US6 (Phases 5-8)**: Depend on Phase 1 only (independent of US1/US2)
- **Polish (Phase 9)**: Depends on all desired story phases

### User Story Dependencies

- **US1 (P1)**: Needs FakeLLMProvider (T003) + code fix (T004). Heaviest setup.
- **US2 (P1)**: Needs code fix (T004) only. Can parallel with US1.
- **US3 (P2)**: Independent — only needs mocked httpx. Can parallel with US1/US2.
- **US4 (P2)**: Fully independent — no mocking needed. Can start anytime after Phase 1.
- **US5 (P3)**: Independent — uses recorded fixtures from T002. Can parallel with anything.
- **US6 (P3)**: Independent — needs middleware/handler access only. Can parallel.

### Parallel Opportunities

- **T005 + T006**: Both orchestrator tests can be written simultaneously (different files)
- **T010 + T011**: GraphQL client tests + execute tool tests (different files)
- **T014 + T017 + T018-T020 + T021-T022**: US3, US4, US5, US6 are all fully independent
- **T018 + T019 + T020**: All three provider tests can run in parallel

---

## Parallel Example: PR1 Critical Gaps

```text
# After Phase 1+2 complete, launch in parallel:
Agent 1: T005 + T007 + T008 + T009 (tests/test_graphql_agent.py)
Agent 2: T006 (tests/test_rest_agent.py)
Agent 3: T010 + T012 + T013 (tests/test_graphql_client.py)
Agent 4: T011 (tests/test_execute_tool.py)
```

## Parallel Example: PR2 Weak Coverage

```text
# All independent, launch simultaneously:
Agent 1: T014 + T015 + T016 (tests/test_recipe_runner.py)
Agent 2: T017 (tests/test_config.py)
Agent 3: T018 + T019 + T020 (tests/test_llm/test_*_complete.py)
Agent 4: T021 + T022 (tests/test_query_tool.py + test_middleware_routing.py)
```

---

## Implementation Strategy

### MVP First (PR1: US1 + US2 = ~18 points)

1. Complete Phase 1: Setup (T001-T003)
2. Complete Phase 2: Code fix (T004)
3. Complete Phase 3: US1 orchestrator tests (T005-T009)
4. Complete Phase 4: US2 safety boundary tests (T010-T013)
5. **STOP and VALIDATE**: Run full suite, verify ~85% effective coverage
6. Create PR1, merge

### Incremental Delivery (PR2: US3-US6 = ~13 points)

7. Phase 5: US3 recipe runner (T014-T016)
8. Phase 6: US4 config contract (T017)
9. Phase 7: US5 LLM providers (T018-T020)
10. Phase 8: US6 MCP routing (T021-T022)
11. Phase 9: Polish + reverse-TDD review (T023-T025)
12. Create PR2, merge

---

## Notes

- Total tasks: 25
- PR1 tasks: 13 (Phases 1-4)
- PR2 tasks: 12 (Phases 5-9)
- Parallel agent opportunities: 4 agents for PR1, 4 agents for PR2
- Every test file is independently runnable
- FakeLLMProvider is the key shared infrastructure — get it right in T003
