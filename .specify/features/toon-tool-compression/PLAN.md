# TOON Tool Result Compression — Implementation Plan

**Version:** 1.0
**Date:** 2026-03-16
**Changes:** Initial plan

## Pre-Implementation Checklist

- [x] DECISION.md complete
- [x] DESIGN.md complete
- [x] toon_format installed (optional dep, pyproject.toml [toon] group)
- [ ] Feature branch created: `feature/toon-tool-compression`
- [ ] `uv run pytest tests/ -v` passes (1219 green on feature branch)

## Task Breakdown

### T1: Config Setting (5 min)

**File:** `api_agent/config.py`

Add under schema reduction settings:
```python
# TOON tool result compression
TOON_TOOL_RESULTS_ENABLED: bool = True
```

**Tests first** (`tests/test_config.py`):
- `test_toon_tool_results_enabled_default_true`
- `test_toon_tool_results_env_override`

### T2: ToolResultEncoder (TDD) (20 min)

**Write tests first** — `tests/test_toon_encoder.py`:
```
TestToolResultEncoder:
  test_encodes_homogeneous_list_smaller
  test_returns_json_when_toon_larger
  test_degrades_on_import_error
  test_degrades_on_encoding_exception
  test_empty_list_returns_json
  test_single_item_list
  test_returns_tuple_of_str_and_bool
```

**Then implement** — `api_agent/llm/toon_encoder.py`:
- Lazy `from toon_format import encode` inside method
- JSON-serialize for baseline comparison
- Return `(toon_str, True)` or `(json_str, False)`
- Never raises

### T3: Wire into format_tool_response() (TDD) (20 min)

**Write tests first** — `tests/test_toon_encoder.py` (additional class):
```
TestFormatToolResponseWithToon:
  test_toon_applied_when_list_fits_budget
  test_falls_back_to_truncated_json_when_toon_oversized
  test_toon_disabled_returns_json
  test_schema_info_path_unaffected_by_toon
  test_failed_result_unaffected_by_toon
```

**Then wire** — `api_agent/agent/orchestrator.py`:
- Import `ToolResultEncoder` and `settings`
- In `format_tool_response()`, check `settings.TOON_TOOL_RESULTS_ENABLED` before calling encoder
- If TOON fits in `MAX_TOOL_RESPONSE_CHARS` → return raw TOON string
- Otherwise fall through to existing truncated JSON path

### T4: Wire into SQL query tool (TDD) (15 min)

**Write test first**:
```
TestSqlQueryToolWithToon:
  test_sql_result_toon_encoded_when_enabled
  test_sql_result_json_when_toon_disabled
```

**Then wire** — `create_sql_query_tool()` in orchestrator.py:
- Same pattern as format_tool_response()
- Applied to `rows` list from SQL execution

### T5: Observability (10 min)

Add structlog event in ToolResultEncoder:
```python
logger.info(
    "toon_tool_result",
    was_applied=toon_applied,
    table=table_name,  # passed as optional context
    original_chars=original_len,
    toon_chars=toon_len,
    reduction_pct=...,
)
```

**Test:** log output is present with correct fields on successful compression.

### T6: Linting + CI (5 min)

1. `uv run ruff check api_agent/`
2. `uv run ruff format api_agent/`
3. `uv run ty check`
4. `uv run pytest tests/ -v` — verify all green + new tests present

## Execution Order

Tasks T1 and T2 are independent — run in parallel.
T3 depends on T2 (needs ToolResultEncoder).
T4 depends on T3 (same pattern).
T5 co-develops with T2-T4.
T6 is final gate.

```
T1 (config) ─────────────────────────────────┐
T2 (encoder, TDD) → T3 (format_tool_response) → T4 (sql tool) → T5 (obs) → T6 (CI)
```

## Estimated Test Delta

+14 new tests:
- `tests/test_toon_encoder.py`: ~12 tests
- `tests/test_config.py`: ~2 tests

New total: 1219 + 14 = ~1233 tests

## Complexity

5 points:
- New module with graceful degradation: 2
- Integration into orchestrator hot path: 2
- Config + observability: 1
