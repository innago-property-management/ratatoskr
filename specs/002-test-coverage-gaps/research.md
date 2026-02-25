# Research: Close Structural Test Coverage Gaps

**Date**: 2026-02-25
**Status**: Complete (research conducted during reverse-TDD analysis + Delphi panel)

## R1: Mock Boundary for Orchestrator Tests

**Decision**: Mock HTTP transport (httpx) and LLM responses. Use real ContextVar, RecipeStore, DuckDB.

**Rationale**: The orchestrators' value is in their wiring — ContextVar initialization, recipe lookup, result assembly. Mocking these defeats the purpose. HTTP and LLM are pure I/O boundaries that add flakiness without testing our code.

**Alternatives considered**:
- Full end-to-end with live APIs: Flaky, expensive (LLM tokens), slow. Rejected.
- Mock everything including DuckDB: Would miss SQL post-processing bugs. Rejected.

## R2: LLM Provider Test Approach

**Decision**: Monkeypatch `api_agent.agent.model._provider` with a `FakeLLMProvider` that returns canned `LLMResponse` objects.

**Rationale**: The `_ProviderProxy` lazy singleton would attempt to create real OpenAI/Anthropic clients on first access. Monkeypatching `_provider` directly prevents this and gives full control over agent loop behavior.

**Alternatives considered**:
- Mock at httpx level for provider tests: More realistic but requires understanding each SDK's internal HTTP patterns. Use for `complete()` tests only.
- Dependency injection refactor: Would require changing production code. Rejected (monkeypatch is sufficient).

## R3: GraphQL Error Handling Bug

**Decision**: Fix `graphql/client.py` to return `data` alongside `errors` when both are present (per GraphQL spec).

**Rationale**: Current code (line 56-57) discards `data` entirely when `errors` exists. The GraphQL spec allows partial success where some fields resolve and others error. Tests should assert the fixed behavior.

**Alternatives considered**:
- Document as intentional behavior: Would be incorrect per GraphQL spec and confuse contributors. Rejected.

## R4: Existing Over-Mocked Test Disposition

**Decision**: Replace `test_recipe_runner.py` entirely with behavioral tests.

**Rationale**: Existing tests monkeypatch `execute_recipe_steps`, bypassing all real logic. Keeping both creates maintenance burden and false confidence. New behavioral tests are strictly superior.

**Alternatives considered**:
- Keep both: Belt-and-suspenders adds maintenance cost. Rejected.
- Refactor in-place: Same outcome as replace, but more git diff noise. Rejected.

## R5: Subscription Operations

**Decision**: Out of scope. Accepted risk.

**Rationale**: GraphQL subscriptions require WebSocket transport. This app uses HTTP POST only. A subscription sent over HTTP would fail at the server level, not at the mutation blocker. No security risk.

## R6: SC-008 Verification Method

**Decision**: Second reverse-TDD review pass after tests are written.

**Rationale**: The 60% baseline was qualitative (module-by-module analysis). The same methodology applied post-implementation is the most honest verification. Automated proxies (line coverage, mutation testing) measure different things.
