# Smart Schema Reduction — Implementation Plan

**Version:** 1.0 (Pre-Delphi)
**Date:** 2026-03-03
**Changes:** Initial plan, pending Delphi sign-off

## Pre-Implementation Checklist

- [ ] Delphi panel review complete (reviews/delphi-YYYY-MM-DD.md exists)
- [ ] DESIGN.md open questions resolved
- [ ] Feature branch created from main: `feature/smart-schema-reduction`
- [ ] `uv run pytest tests/ -v` passes (848 tests green on feature branch)

## Implementation Phases

### Phase 1: Dependency + Module Scaffold

**Goal:** `toon_format` installed, `reducer.py` skeleton compiles, no tests yet.

Tasks:
1. Add `toon_format` to `pyproject.toml` (install from GitHub until PyPI stable release):
   ```toml
   "toon_format @ git+https://github.com/toon-format/toon-python.git"
   ```
2. Run `uv sync --group dev` to install
3. Create `api_agent/schema/__init__.py` (empty)
4. Create `api_agent/schema/reducer.py` with:
   - `ReductionResult` dataclass
   - `ToonLayer` class (stub)
   - `HaikuLayer` class (stub)
   - `reduce_schema()` async function (returns original unchanged as stub)
5. Add new settings to `api_agent/config.py`:
   - `SCHEMA_REDUCTION_ENABLED: bool = True`
   - `SCHEMA_REDUCTION_MODEL: str = "claude-haiku-4-5"`
   - `SCHEMA_REDUCTION_TIMEOUT_MS: int = 30_000`
   - `SCHEMA_REDUCTION_API_KEY: str = ""`
6. Verify `uv run ruff check api_agent/` passes
7. Verify `uv run ty check` passes

### Phase 2: ToonLayer (TDD)

**Goal:** TOON encoding layer is correct and gracefully degrades.

Tests first (`tests/test_schema_reducer.py`):
```python
async def test_toon_reduces_homogeneous_array(): ...
async def test_toon_no_gain_returns_original(): ...
async def test_toon_import_error_degrades_gracefully(): ...
async def test_toon_with_non_json_input_degrades(): ...
```

Implementation:
1. Implement `ToonLayer.encode()`:
   - Try `from toon_format import encode`; catch `ImportError` → log warning, return (original, False)
   - Call `toon_format.encode(json.loads(text))` — or for DSL text (not JSON): call on raw text
   - If TOON output >= original length → return (original, False)
   - Return (toon_text, True)
2. Note: DSL schema text is NOT always valid JSON (REST DSL is human-readable text, not
   JSON). TOON encodes Python objects, not text strings. For non-JSON schema text:
   - Option A: Pass `schema_text` as a plain string to `toon_format.encode()` → may help, may not
   - Option B: Only TOON-encode schemas that are JSON-serializable (GraphQL introspection)
   - **Decision (to validate in Delphi)**: Attempt JSON parse; if succeeds, encode the parsed
     object; if not (REST DSL text), skip TOON layer and go straight to Haiku if needed.

### Phase 3: HaikuLayer (TDD)

**Goal:** Haiku reduction layer works with mocked Anthropic client.

Tests first:
```python
async def test_haiku_reduces_oversized_schema(): ...
async def test_haiku_timeout_degrades_gracefully(): ...
async def test_haiku_auth_error_degrades_gracefully(): ...
async def test_haiku_empty_response_degrades(): ...
async def test_haiku_strips_markdown_fences(): ...
```

Implementation:
1. Implement `HaikuLayer.__init__()`: creates `anthropic.AsyncAnthropic(api_key=..., timeout=...)`
2. Implement `HaikuLayer.reduce()`:
   - Build prompt (see DESIGN.md prompt template)
   - Call `self.client.messages.create(model=..., max_tokens=4096, messages=[...])`
   - Extract text content block
   - Strip markdown fences (normalizeOutput equivalent)
   - Return (reduced_text, True) or (original, False) on any error
3. Implement `_get_api_key()` helper in `reducer.py`:
   - If `settings.SCHEMA_REDUCTION_API_KEY` is non-empty → use it
   - Elif `settings.PROVIDER == "anthropic"` → use `settings.API_KEY`
   - Else → return `""` (Haiku layer disabled)

### Phase 4: `reduce_schema()` Orchestration (TDD)

**Goal:** Pipeline logic correct end-to-end.

Tests first:
```python
async def test_reduce_schema_disabled_returns_original(): ...
async def test_reduce_schema_under_threshold_no_ai_call(): ...
async def test_reduce_schema_toon_brings_under_threshold(): ...
async def test_reduce_schema_over_threshold_no_api_key_truncates(): ...
async def test_reduce_schema_over_threshold_haiku_invoked(): ...
async def test_reduce_schema_haiku_fails_falls_back_to_toon(): ...
async def test_reduce_schema_preserves_old_truncation_as_last_resort(): ...
```

Implementation of `reduce_schema()`:
```python
async def reduce_schema(schema_text, question, threshold, api_key, model, timeout_ms, enabled):
    if not enabled or not schema_text:
        return ReductionResult(schema_text=schema_text, ...)

    original_chars = len(schema_text)

    # Layer 1: TOON
    toon_layer = ToonLayer()
    current_text, toon_applied = toon_layer.encode(schema_text)

    if len(current_text) <= threshold:
        return ReductionResult(schema_text=current_text, was_toon_applied=toon_applied, ...)

    # Layer 2: Haiku (only if still over threshold)
    resolved_key = _get_api_key(api_key)
    if resolved_key:
        haiku_layer = HaikuLayer(resolved_key, model, timeout_ms)
        reduced_text, ai_applied = await haiku_layer.reduce(current_text, question)
        if ai_applied:
            current_text = reduced_text

    # Final fallback: hard truncation with marker
    if len(current_text) > threshold:
        current_text = (
            current_text[:threshold]
            + "\n[SCHEMA TRUNCATED - use search_schema() to explore]"
        )

    return ReductionResult(
        schema_text=current_text,
        was_toon_applied=toon_applied,
        was_ai_applied=ai_applied,
        original_chars=original_chars,
        final_chars=len(current_text),
    )
```

### Phase 5: Integration into Protocol Agents

**Goal:** All 3 truncation points use `reduce_schema()`.

For each of the 3 integration points:
1. Add `question: str` parameter to the schema-loading function signature (if not present)
2. Replace truncation block with `await reduce_schema(...)` call
3. Pass through `question` from the caller (`process_rest_query`, `process_graphql_query`, `process_grpc_query`)

Integration order:
1. `api_agent/rest/schema_loader.py` — `load_openapi_spec(spec_url, headers, question="", spec_filter=None)` → `reduce_schema(dsl_context, question, ...)`
2. `api_agent/agent/graphql_agent.py` — `_build_schema_context()` call site → keep `_strip_descriptions()` pre-pass, then `reduce_schema(context, question, ...)`
3. `api_agent/agent/grpc_agent.py` — gRPC truncation block → `reduce_schema(schema_text, question, ...)`

### Phase 6: Config Tests

Add to `tests/test_config.py`:
- `test_schema_reduction_defaults()` — verify all 4 new settings have correct defaults
- `test_schema_reduction_env_override()` — verify env var override works

### Phase 7: Linting, Type Checking, CI

1. `uv run ruff check api_agent/` — fix any issues
2. `uv run ruff format api_agent/`
3. `uv run ty check` — fix any type errors
4. `uv run pytest tests/ -v` — all tests pass
5. Update test count in README.md and CLAUDE.md

## File Change Summary

| File | Change |
|------|--------|
| `api_agent/schema/__init__.py` | New (empty) |
| `api_agent/schema/reducer.py` | New |
| `api_agent/config.py` | Add 4 new settings |
| `api_agent/rest/schema_loader.py` | Replace truncation, add `question` param |
| `api_agent/agent/graphql_agent.py` | Replace truncation, pass `question` |
| `api_agent/agent/grpc_agent.py` | Replace truncation, pass `question` |
| `pyproject.toml` | Add `toon_format` dependency |
| `tests/test_schema_reducer.py` | New (~20 tests) |
| `tests/test_config.py` | Add ~4 settings tests |
| `README.md` | Update test count |
| `CLAUDE.md` | Update test count |

## Estimated Complexity

8 points:
- New module with graceful degradation logic: 3
- External beta dependency: 1 (mitigated by degradation contract)
- 3 protocol agent integrations: 2 (question param threading)
- Haiku async + mock testing: 2

## Test Count Estimate

+24 new tests:
- `test_schema_reducer.py`: ~20 tests
- `test_config.py`: ~4 tests

New total: 848 + 24 = ~872 tests
