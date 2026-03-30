# PLAN.md — provider-generalization

**Feature:** Provider Generalization (Schema Reduction LLM + PROFILE=local Preset)
**Version:** 1.0
**Date:** 2026-03-30
**Spec:** [SPEC.md](SPEC.md) v1.1 | **Design:** [DESIGN.md](DESIGN.md) v1.0
**Approach:** TDD — tests first, then implementation, per step

---

## Step Overview

| Step | Description | Complexity | Depends On | AC Coverage |
|------|-------------|-----------|------------|-------------|
| 1 | Config: PROFILE + new fields + validator | 5 | — | AC-1..4, AC-7, AC-18 |
| 2 | Provider factory with timeout injection | 5 | Step 1 | AC-5, AC-6, AC-13 |
| 3 | AIReductionLayer (replace HaikuLayer) | 5 | — | AC-8..10, AC-12, AC-14, AC-15 |
| 4 | reduce_schema() signature + orchestrator wiring | 3 | Steps 2, 3 | AC-10, AC-11 |
| 5 | CLI --profile flag + startup logging | 2 | Step 1 | AC-16, AC-17 |
| 6 | Integration verification + cleanup | 2 | Steps 1..5 | AC-11 |
| **Total** | | **22** | | |

---

## Step 1 — Config: PROFILE + New Fields + Validator

**Complexity:** 5
**Depends on:** Nothing
**Files changed:** `api_agent/config.py`, `tests/test_config.py`
**AC coverage:** AC-1, AC-2, AC-3, AC-4, AC-7, AC-18

### 1a. Write tests (TDD red phase)

Add to `tests/test_config.py`:

```
test_profile_local_sets_block_private_ips_false        → AC-1
test_profile_local_sets_log_format_console             → AC-1
test_profile_local_sets_schema_reduction_disabled       → AC-3
test_profile_local_explicit_override_wins               → AC-2
test_profile_empty_no_change                            → AC-4
test_profile_invalid_raises_value_error                 → AC-18
test_profile_invalid_error_message_is_human_readable    → AC-18
test_schema_reduction_model_default_empty               → AC-7
test_schema_reduction_api_key_no_anthropic_alias        → AC-6 (partial)
test_schema_reduction_provider_field_exists              → AC-5 (partial)
test_schema_reduction_base_url_field_exists              → AC-5 (partial)
```

### 1b. Implement (TDD green phase)

1. Add `PROFILE` field with `Literal["local", ""]` default `""`
2. Add `SCHEMA_REDUCTION_PROVIDER: str = ""`
3. Add `SCHEMA_REDUCTION_BASE_URL: str = ""`
4. Change `SCHEMA_REDUCTION_MODEL` default from `"claude-haiku-4-5-20251001"` to `""`
5. Change `SCHEMA_REDUCTION_API_KEY` alias — remove `ANTHROPIC_API_KEY` from `AliasChoices`
6. Add `@model_validator(mode="before")` for `apply_profile_defaults`:
   - Custom error for invalid profile (U-3)
   - Inject local defaults for absent keys
   - Record applied overrides in module-level `_profile_overrides` list
7. Verify all new tests pass
8. Verify all 1382 existing tests still pass

### Risks
- `model_validator(mode="before")` receives raw dict from pydantic-settings — env vars
  may appear with or without `API_AGENT_` prefix. Must check both.
- Removing `ANTHROPIC_API_KEY` alias is a breaking change for a narrow case — documented
  in SPEC.md migration note.

---

## Step 2 — Provider Factory with Timeout Injection

**Complexity:** 5
**Depends on:** Step 1 (needs new config fields)
**Files changed:** `api_agent/llm/factory.py` (new), `api_agent/llm/openai_provider.py`,
  `api_agent/llm/anthropic_provider.py`, `api_agent/llm/openai_compat.py`,
  `tests/test_schema/test_provider_factory.py` (new)
**AC coverage:** AC-5, AC-6, AC-13

### 2a. Write tests (TDD red phase)

Create `tests/test_schema/test_provider_factory.py`:

```
test_factory_returns_anthropic_when_provider_is_anthropic
test_factory_returns_openai_when_provider_is_openai
test_factory_returns_compat_when_provider_is_openai_compat
test_factory_inherits_main_provider_when_override_empty       → AC-5
test_factory_inherits_main_api_key_when_override_empty        → AC-6
test_factory_returns_none_for_cloud_provider_without_key
test_factory_returns_provider_for_compat_without_key          (Ollama)
test_factory_injects_timeout_ms                                → AC-13 (S-1)
test_factory_uses_reduction_base_url_when_set
test_factory_falls_back_to_main_base_url
test_factory_uses_reduction_model_when_set
test_factory_returns_none_for_unknown_provider
```

### 2b. Implement (TDD green phase)

1. Create `api_agent/llm/factory.py` with `create_schema_reduction_provider()`
2. Add optional `timeout: httpx.Timeout | None = None` parameter to each provider's
   `__init__()`:
   - `AnthropicProvider`: pass to `AsyncAnthropic(timeout=...)`
   - `OpenAIProvider`: pass to `AsyncOpenAI(timeout=...)`
   - `OpenAICompatProvider`: pass to `AsyncOpenAI(timeout=...)`
3. Default timeout in each provider remains their current value (no behavior change
   for non-factory construction)
4. Verify all new + existing tests pass

### Risks
- Provider constructors may have different timeout semantics (httpx.Timeout vs float).
  Use `httpx.Timeout` consistently.
- Must not break existing provider construction in `api_agent/agent/model.py`.

---

## Step 3 — AIReductionLayer (Replace HaikuLayer)

**Complexity:** 5
**Depends on:** Nothing (uses `LLMProvider` interface, not factory)
**Files changed:** `api_agent/schema/reducer.py`, `tests/test_schema/test_reducer.py`
**AC coverage:** AC-8, AC-9, AC-10, AC-12, AC-14, AC-15

> **Can be implemented in parallel with Step 2.**

### 3a. Write tests (TDD red phase)

Add/modify in `tests/test_schema/test_reducer.py`:

```
test_ai_reduction_layer_produces_reduced_output              → AC-8
test_ai_reduction_layer_empty_response_returns_original      → AC-9
test_ai_reduction_layer_too_short_returns_original
test_ai_reduction_layer_longer_than_input_returns_original
test_ai_reduction_layer_injection_detected_returns_original
test_ai_reduction_layer_exception_returns_original           (never raises)
test_ai_reduction_layer_passes_max_tokens                    → AC-14 (S-2)
test_ai_reduction_layer_strips_markdown_fences               → AC-15 (S-3)
test_reduce_schema_provider_none_skips_ai_layer              → AC-10
test_reduce_schema_with_fake_provider_runs_ai_layer
```

### 3b. Implement (TDD green phase)

1. Rename `HaikuLayer` → `AIReductionLayer`
2. Replace constructor: accept `LLMProvider` + `max_output_tokens`
3. Update `reduce()`:
   - Build `messages` list for `provider.complete()`
   - Pass `max_tokens=self.max_output_tokens` **(S-2)**
   - Read `response.content` (str) instead of iterating content blocks
   - Preserve all safety checks unchanged
4. Remove `import anthropic`, `import httpx`
5. Remove `_get_haiku_layer()` LRU cache
6. Remove `_get_api_key()` helper
7. Update `reduce_schema()` signature: replace `api_key`/`model`/`timeout_ms` with
   `provider: LLMProvider | None`
8. Update Layer 2 logic: construct `AIReductionLayer(provider, max_output_tokens)`
   instead of `_get_haiku_layer(...)`
9. Verify all new + existing reducer tests pass

### Risks
- `LLMResponse.content` may be `None` for tool-call-only responses. Since we pass
  `tools=None`, the response will always have text content. Add a guard anyway.
- Existing tests that mock `HaikuLayer` directly will need updating. Check for any
  imports of `HaikuLayer` in the test suite.

---

## Step 4 — Orchestrator Wiring

**Complexity:** 3
**Depends on:** Steps 2 + 3
**Files changed:** `api_agent/agent/orchestrator.py`
**AC coverage:** AC-10, AC-11

### 4a. Write tests (TDD red phase)

Existing orchestrator tests in `test_graphql_agent.py` and `test_rest_agent.py` use
`FakeLLMProvider` and monkeypatch settings. These should continue passing without
modification **(AC-11)**. Add:

```
test_orchestrator_passes_provider_to_reduce_schema
test_orchestrator_passes_none_when_reduction_disabled
```

### 4b. Implement (TDD green phase)

1. Add import: `from ..llm.factory import create_schema_reduction_provider`
2. Add lazy singleton: `_get_schema_reduction_provider()`
3. Update `reduce_schema()` call site:
   - Remove `api_key`, `model`, `timeout_ms` kwargs
   - Add `provider=_get_schema_reduction_provider() if settings.SCHEMA_REDUCTION_ENABLED else None`
4. Run full test suite — all 1382+ tests must pass

### Risks
- Monkeypatched settings in tests may not trigger factory re-evaluation. The lazy
  singleton must be resettable (add `_reset_schema_reduction_provider()` for tests,
  same pattern as `set_agent_semaphore()`).

---

## Step 5 — CLI Flag + Startup Logging

**Complexity:** 2
**Depends on:** Step 1 (needs `PROFILE` field and `_profile_overrides`)
**Files changed:** `api_agent/__main__.py`, tests for CLI
**AC coverage:** AC-16, AC-17

> **Can be implemented in parallel with Steps 2-4** (only depends on Step 1).

### 5a. Write tests (TDD red phase)

```
test_parse_args_profile_flag                                  → AC-17
test_apply_cli_overrides_sets_profile_env_var                  → AC-17
test_startup_logs_profile_overrides                            → AC-16
```

### 5b. Implement (TDD green phase)

1. Add `--profile` to `parse_args()` with `choices=("local",)`
2. Add `if args.profile:` block to `apply_cli_overrides()`
3. Add startup log block in `main()` after settings reload:
   ```python
   from .config import _profile_overrides
   if _profile_overrides:
       logger.info("profile_applied", profile=reloaded.PROFILE,
                   overrides=",".join(_profile_overrides))
   ```
4. Verify tests pass

---

## Step 6 — Integration Verification + Cleanup

**Complexity:** 2
**Depends on:** Steps 1-5
**Files changed:** None (verification only) or minor cleanup
**AC coverage:** AC-11

### Tasks

1. Run full test suite: `uv run pytest tests/ -v` — all tests must pass
2. Run linter: `uv run ruff check api_agent/`
3. Run formatter: `uv run ruff format api_agent/`
4. Run type checker: `uv run ty check`
5. Verify no `import anthropic` in `reducer.py` **(AC-12)**
6. Verify `HaikuLayer` is fully removed (no references in codebase)
7. Manual smoke test: `PROFILE=local uv run api-agent --provider openai-compat --base-url http://localhost:11434/v1`
   - Confirm startup log shows profile overrides
   - Confirm `BLOCK_PRIVATE_IPS=false` in effect
8. Update log message references if any mention "Haiku" → "AI reduction"

---

## Parallelization Opportunities

```
         Step 1 (config)
        /       |       \
   Step 2    Step 3    Step 5
  (factory)  (layer)   (CLI)
        \       /
         Step 4
      (orchestrator)
            |
         Step 6
      (verification)
```

- **Steps 2 + 3 + 5** can all run in parallel after Step 1 completes
- **Step 4** requires both 2 and 3
- **Step 6** is final verification after everything

---

## PR Strategy

**Option A — Single PR (recommended for this size):**
All 5 implementation steps in one PR. The feature is cohesive and the diff is moderate
(~300-400 lines of implementation + ~400 lines of tests). Splitting would create
intermediate states where `reduce_schema()` has an incompatible signature.

**Option B — Two PRs:**
- PR 1: Steps 1 + 5 (config + CLI) — no behavior change, purely additive
- PR 2: Steps 2 + 3 + 4 (factory + layer + wiring) — the actual provider swap

Option A is preferred unless review bandwidth requires splitting.
