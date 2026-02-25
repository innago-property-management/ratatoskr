# Implementation Plan: Close Structural Test Coverage Gaps

**Branch**: `002-test-coverage-gaps` | **Date**: 2026-02-25 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/002-test-coverage-gaps/spec.md`

## Summary

Close the structural test coverage gaps in api-agent's test suite by adding integration tests for the two main orchestrator entry points (`process_query`, `process_rest_query`), unit tests for untested safety boundaries (GraphQL mutation blocker, execute tool), behavioral tests replacing over-mocked recipe runner tests, config contract tests, and LLM provider `complete()` tests with recorded fixtures. Includes a prerequisite code fix for the GraphQL client's error handling (discards `data` when `errors` present).

## Technical Context

**Language/Version**: Python 3.11, 3.12
**Primary Dependencies**: pytest, pytest-asyncio, httpx (mocking target), DuckDB (real in tests)
**Storage**: N/A (tests only)
**Testing**: pytest + pytest-asyncio (existing infrastructure)
**Target Platform**: CI (ubuntu-latest, Python 3.11/3.12 matrix)
**Project Type**: Test suite expansion for existing MCP server
**Performance Goals**: All new tests complete within existing CI time budget (< 60s total)
**Constraints**: Deterministic (no live APIs/LLM), no new test dependencies unless justified
**Scale/Scope**: ~9 modules, estimated 31 complexity points across 2 PRs

## Constitution Check

*GATE: No project constitution established. No violations to check.*

## Project Structure

### Documentation (this feature)

```text
specs/002-test-coverage-gaps/
├── plan.md              # This file
├── spec.md              # Feature specification (complete)
├── research.md          # Phase 0 (consolidated from reverse-TDD analysis)
├── checklists/
│   └── requirements.md  # Spec quality checklist (complete)
└── tasks.md             # Phase 2 output (via /speckit.tasks)
```

### Source Code (repository root)

```text
# Existing structure — new test files only
tests/
├── test_graphql_agent.py          # NEW: process_query() integration tests
├── test_rest_agent.py             # NEW: process_rest_query() integration tests
├── test_graphql_client.py         # NEW: mutation blocker + response parsing
├── test_execute_tool.py           # NEW: _execute MCP tool
├── test_config.py                 # NEW: config contract/snapshot tests
├── test_query_tool.py             # NEW: query() MCP handler routing
├── test_middleware_routing.py     # NEW: on_call_tool non-recipe path
├── test_recipe_runner.py          # REPLACE: behavioral tests (delete over-mocked)
├── test_llm/
│   ├── test_openai_complete.py    # NEW: complete() with recorded fixtures
│   ├── test_anthropic_complete.py # NEW: complete() with recorded fixtures
│   └── test_compat_complete.py    # NEW: complete() + retry-without-tools
└── fixtures/                      # NEW: recorded LLM response fixtures
    ├── openai_text_response.json
    ├── openai_tool_call_response.json
    ├── anthropic_text_response.json
    ├── anthropic_tool_call_response.json
    └── graphql_schemas/
        └── sample_schema.graphql

# Code fix (prerequisite)
api_agent/graphql/client.py        # FIX: return data alongside errors (partial success)
```

**Structure Decision**: New test files follow existing convention (one test file per source module, `test_` prefix). Fixtures in a shared `tests/fixtures/` directory. LLM provider complete() tests co-located in existing `tests/test_llm/` directory.

## Mock Boundaries

The critical design decision for these tests. Mock only the I/O boundary:

| Component | In Tests | Rationale |
|-----------|----------|-----------|
| httpx (HTTP transport) | **Mocked** | Deterministic, no network |
| LLM provider responses | **Mocked** | No token cost, deterministic |
| `api_agent.agent.model._provider` | **Monkeypatched** with FakeLLMProvider | Prevents lazy singleton from creating real clients |
| ContextVar | **Real** | Tests must verify initialization/propagation |
| RecipeStore | **Real** | Tests must verify recipe extraction flow |
| DuckDB executor | **Real** | Tests must verify SQL post-processing |
| Result assembly | **Real** | Tests must verify output structure |

### FakeLLMProvider Pattern

```python
class FakeLLMProvider(LLMProvider):
    """Test double that returns canned responses without HTTP calls."""
    def __init__(self, responses: list[LLMResponse]):
        self._responses = iter(responses)

    async def complete(self, messages, tools=None, **kwargs):
        return next(self._responses)
```

Monkeypatch before each test:
```python
@pytest.fixture(autouse=True)
def fake_provider(monkeypatch, canned_responses):
    monkeypatch.setattr("api_agent.agent.model._provider", FakeLLMProvider(canned_responses))
```

## PR Strategy

### PR 1: Critical Gaps (Must Fix) — ~18 points

1. **Code fix**: `graphql/client.py` — return `data` alongside `errors`
2. **test_graphql_client.py** — mutation blocker (5+ variants), response parsing (success, error, partial), no-endpoint error
3. **test_execute_tool.py** — GraphQL path, REST path, base URL resolution, truncation
4. **test_config.py** — all env var names, defaults, alias priority, MCP_SLUG computed field
5. **test_graphql_agent.py** — process_query() integration: success, MaxTurnsExceeded partial results, schema fetch failure (upstream), recipe extraction
6. **test_rest_agent.py** — process_rest_query() integration: success, polling tool creation, upstream error handling

### PR 2: Weak Coverage + Cleanup — ~13 points

7. **test_recipe_runner.py** — REPLACE existing file. Behavioral tests with real RecipeStore, real step execution, mocked HTTP only. GraphQL recipe path, REST recipe path, return_directly CSV, stale schema rejection
8. **test_query_tool.py** — query() handler routing (GraphQL vs REST), missing header errors, recipe change notification
9. **test_middleware_routing.py** — on_call_tool non-recipe path, tool name transformation, context forwarding
10. **test_llm/test_*_complete.py** — Each provider's complete() with recorded fixtures. Focus on openai-compat retry-without-tools path.

## Complexity Tracking

No constitution violations to justify.
