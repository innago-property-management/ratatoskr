# SPEC.md — provider-generalization

**Feature:** Provider Generalization (Schema Reduction LLM + PROFILE=local Preset)
**Date:** 2026-03-30
**Status:** Approved
**Version:** 1.1 (Post-Delphi Round 1)
**Changes:** Folded 6 Delphi findings (S-1, S-2, S-3, U-1, U-2, U-3) into acceptance criteria and functional spec

---

## Overview

Two user-facing improvements delivered as a single feature:

1. **Generalize schema reduction to use any configured LLM provider** — not just Anthropic.
2. **Add `PROFILE=local` preset** — one env var that applies sensible local-dev defaults.

---

## User Stories

### Story 1 — Ollama user gets AI schema reduction for free

**As a** developer running Ratatoskr with `--provider openai-compat` pointing at Ollama,
**I want** schema reduction to use my already-configured Ollama instance,
**So that** I don't need a separate Anthropic API key just to get AI schema reduction.

**Acceptance Criteria:**

1. When `PROVIDER=openai-compat` (or `openai` or `anthropic`) and no
   `SCHEMA_REDUCTION_PROVIDER` override is set, the AI reduction layer uses the same
   provider/key/model as the main agent.
2. When `SCHEMA_REDUCTION_PROVIDER` is set to a different value (e.g., `anthropic` while
   main provider is `openai-compat`), that override is used for reduction only.
3. When neither the main provider key nor a reduction-specific key is available/valid,
   the AI reduction layer is skipped silently (same graceful degradation as today).
4. Log messages at the start of the reduction layer identify which provider is being used
   (e.g., `schema_ai_reduction_provider=openai-compat model=qwen2.5`).

### Story 2 — User overrides the schema reduction provider independently

**As an** operator deploying Ratatoskr with a cheap/fast main LLM (e.g., GPT-4o mini)
but wanting a separate model for schema reduction,
**I want** to specify `SCHEMA_REDUCTION_PROVIDER`, `SCHEMA_REDUCTION_API_KEY`,
`SCHEMA_REDUCTION_MODEL`, and `SCHEMA_REDUCTION_BASE_URL` independently,
**So that** I can use a different model (or provider) just for schema reduction without
changing the main agent configuration.

**Acceptance Criteria:**

1. `SCHEMA_REDUCTION_PROVIDER` accepts `openai`, `anthropic`, or `openai-compat`.
   Defaults to the value of `PROVIDER`.
2. `SCHEMA_REDUCTION_API_KEY` defaults to the value of `API_KEY` (not `ANTHROPIC_API_KEY`
   as it does today).
3. `SCHEMA_REDUCTION_MODEL` defaults to `""` (empty = use provider's built-in default).
   Today's default of `claude-haiku-4-5-20251001` is removed.
4. `SCHEMA_REDUCTION_BASE_URL` defaults to the value of `BASE_URL`.
5. All four settings accept the `API_AGENT_` prefix and `.env` file values.

### Story 3 — Local dev with one env var

**As a** developer running Ratatoskr locally against a localhost API target,
**I want** to set `PROFILE=local` and have it just work,
**So that** I don't have to remember to set `BLOCK_PRIVATE_IPS=false` and other flags.

**Acceptance Criteria:**

1. Setting `PROFILE=local` enables these defaults (before individual env var overrides):
   - `BLOCK_PRIVATE_IPS=false`
   - `LOG_FORMAT=console`
   - `SCHEMA_REDUCTION_ENABLED=false`
2. Any individual env var set explicitly still wins over the profile default.
   Example: `PROFILE=local BLOCK_PRIVATE_IPS=true` → `BLOCK_PRIVATE_IPS` is `true`.
3. `PROFILE` unset (default `""`) produces zero behavior change — all existing defaults
   remain identical.
4. `PROFILE=local` is documented with a note explaining the SSRF / Istio trade-off
   (why we don't auto-detect).
5. Invalid `PROFILE` values produce a clear, custom validation error at startup
   (not pydantic's default Literal error message). **(U-3)**
6. When `PROFILE=local`, log at INFO level the effective overridden settings so the user
   can see what the profile changed. **(U-1)**
7. `--profile` CLI flag added to `__main__.py` (same pattern as `--provider`),
   sets `API_AGENT_PROFILE` env var. **(U-2)**

### Story 4 — Schema reduction disabled when no key is available (PROFILE=local + no cloud key)

**As a** local developer running Ratatoskr with `PROFILE=local` and no cloud LLM API key
(using `openai-compat` → Ollama for the main agent),
**I want** schema reduction to be off by default in local profile,
**So that** I don't get noisy log warnings about missing API keys during local development.

**Acceptance Criteria:**

1. When `PROFILE=local`, `SCHEMA_REDUCTION_ENABLED` defaults to `false`.
2. If the user explicitly sets `SCHEMA_REDUCTION_PROVIDER` alongside `PROFILE=local`,
   they can opt back in by also setting `SCHEMA_REDUCTION_ENABLED=true`.
3. The log at startup reflects the effective `SCHEMA_REDUCTION_ENABLED` value.

---

## Non-Functional Requirements

- **No breaking changes.** All 1382 existing tests must pass unchanged.
- **Zero new mandatory dependencies.** The `anthropic` package remains (used by
  `anthropic_provider.py`); `reducer.py` simply stops importing it directly.
- **Never raises.** `AIReductionLayer` (successor to `HaikuLayer`) must preserve the
  "never raises — returns (original, False) on ANY error" contract.
- **Startup performance.** Provider construction in `create_schema_reduction_provider()`
  must be synchronous and fast (no network calls).
- **Test coverage.** New behavior covered by unit tests. `FakeLLMProvider` used for
  `AIReductionLayer` tests (no live API calls).

---

## Functional Specification

### 1. New / Changed Config Fields

| Field | Type | Default | Change |
|-------|------|---------|--------|
| `PROFILE` | `Literal["local", ""]` | `""` | New |
| `SCHEMA_REDUCTION_PROVIDER` | `str` | `""` (= inherit from `PROVIDER`) | New |
| `SCHEMA_REDUCTION_API_KEY` | `str` | `""` (= inherit from `API_KEY`) | Changed fallback (was `ANTHROPIC_API_KEY`) |
| `SCHEMA_REDUCTION_MODEL` | `str` | `""` (= provider default) | Changed default (was `claude-haiku-4-5-20251001`) |
| `SCHEMA_REDUCTION_BASE_URL` | `str` | `""` (= inherit from `BASE_URL`) | New |

The existing `API_AGENT_SCHEMA_REDUCTION_API_KEY` / `ANTHROPIC_API_KEY` alias on
`SCHEMA_REDUCTION_API_KEY` is removed. Users who relied on `ANTHROPIC_API_KEY`
automatically wiring to schema reduction should set `SCHEMA_REDUCTION_PROVIDER=anthropic`
and `SCHEMA_REDUCTION_API_KEY` explicitly, or set the main `PROVIDER=anthropic` (which
picks up `ANTHROPIC_API_KEY` via the existing alias on `API_KEY`).

> **Migration note (BREAKING for a narrow case):** Users who set `ANTHROPIC_API_KEY`
> but use a non-Anthropic main provider, relying on the implicit fallback for schema
> reduction, must now set `SCHEMA_REDUCTION_PROVIDER=anthropic` explicitly. This
> narrow case is worth the clean-up. The change is documented in the changelog.

### 2. `PROFILE=local` Behavior

Implemented as a `@model_validator(mode="before")` on `Settings`. When `PROFILE==local`
(case-insensitive, strip whitespace), inject these keys into the raw dict only if they
are not already present:

```python
{
    "BLOCK_PRIVATE_IPS": False,
    "LOG_FORMAT": "console",
    "SCHEMA_REDUCTION_ENABLED": False,
}
```

The validator runs before field-level validation, so explicit env var values always
override the injected defaults.

**Invalid PROFILE values (U-3):** The `model_validator` checks `PROFILE` before
injecting defaults. If the value is non-empty and not `"local"`, the validator raises
`ValueError` with a clear message: `"Invalid PROFILE '{value}'. Supported profiles:
'local'. Leave empty for default behavior."` This runs before pydantic's `Literal`
validation, giving a human-readable error instead of the raw type mismatch.

**Startup logging (U-1):** When `PROFILE=local`, the validator collects which keys it
injected (i.e., which were not already set by the user). After settings construction
completes, `__main__.py` logs at INFO level:
```
profile_applied profile=local overrides=BLOCK_PRIVATE_IPS=false,LOG_FORMAT=console,SCHEMA_REDUCTION_ENABLED=false
```
Only the keys actually injected (not overridden by the user) appear in the log.

**CLI flag (U-2):** `--profile` added to `parse_args()` in `__main__.py`, same pattern
as `--provider`. Sets `os.environ["API_AGENT_PROFILE"]` in `apply_cli_overrides()`.

### 3. `create_schema_reduction_provider()` Factory

New function in `api_agent/llm/factory.py`:

```python
def create_schema_reduction_provider(settings: Settings) -> LLMProvider | None:
    """
    Build an LLMProvider for schema reduction.

    Returns None if no usable API key is available (skips AI layer).
    """
```

Resolution logic:
1. Determine effective provider type:
   - `settings.SCHEMA_REDUCTION_PROVIDER` if non-empty, else `settings.PROVIDER`
2. Determine effective API key:
   - `settings.SCHEMA_REDUCTION_API_KEY` if non-empty, else `settings.API_KEY`
3. Determine effective model:
   - `settings.SCHEMA_REDUCTION_MODEL` if non-empty, else `""` (provider will use its
     own default)
4. Determine effective base URL:
   - `settings.SCHEMA_REDUCTION_BASE_URL` if non-empty, else `settings.BASE_URL`
5. If effective API key is empty AND effective provider type is `openai` or `anthropic`
   (i.e., cloud providers that require a key), return `None`.
6. Construct and return the appropriate `LLMProvider` subclass.

**Timeout injection (S-1):** The `LLMProvider.complete()` interface does NOT gain a
timeout parameter. Instead, `create_schema_reduction_provider()` bakes
`settings.SCHEMA_REDUCTION_TIMEOUT_MS` into the HTTP client at construction time. Each
provider subclass already accepts timeout configuration at construction:
- `OpenAIProvider` / `OpenAICompatProvider`: `httpx.Timeout` passed to the SDK client
- `AnthropicProvider`: `httpx.Timeout` passed to `AsyncAnthropic(timeout=...)`

The factory constructs the provider with the reduction-specific timeout, keeping the
change isolated to the factory. The `LLMProvider` ABC is unchanged.

### 4. `AIReductionLayer` (replaces `HaikuLayer`)

Rename `HaikuLayer` → `AIReductionLayer`. Replace constructor:

**Before:**
```python
def __init__(self, api_key: str, model: str, timeout_ms: int, max_output_tokens: int):
    self.client = anthropic.AsyncAnthropic(api_key=api_key, ...)
```

**After:**
```python
def __init__(self, provider: LLMProvider, max_output_tokens: int):
    self.provider = provider
```

The `reduce()` method calls `await self.provider.complete(messages, tools=None,
max_tokens=self.max_output_tokens)`. **(S-2)** The `max_tokens` parameter is explicitly
mapped from `self.max_output_tokens` — the naming difference between the layer's config
(`max_output_tokens`) and the provider interface (`max_tokens`) is intentional and must
be explicit in the call.

The response handling uses `LLMResponse.content` (a `str`) instead of iterating
`response.content` blocks (Anthropic-specific).

The fence-stripping, sanity checks, injection detection, and "never raises" contract
are all preserved unchanged.

**Fence-stripping test (S-3):** The existing `_FENCE_RE` regex in `reducer.py` already
handles markdown fence stripping for all providers. A new test case is added that
exercises `AIReductionLayer` with a `FakeLLMProvider` returning markdown-fenced output
(e.g., `` ```json\n{...}\n``` ``) to verify the fence is stripped regardless of
provider. This covers the provider response format variance concern.

### 5. `reduce_schema()` Signature Change

**Before:**
```python
async def reduce_schema(
    schema_text: str,
    question: str,
    threshold: int,
    api_key: str = "",
    model: str = "claude-haiku-4-5-20251001",
    timeout_ms: int = 30_000,
    enabled: bool = True,
    max_input_chars: int = 100_000,
    max_output_tokens: int = 8192,
    ai_reduction_threshold: int = 0,
) -> ReductionResult:
```

**After:**
```python
async def reduce_schema(
    schema_text: str,
    question: str,
    threshold: int,
    provider: LLMProvider | None = None,
    enabled: bool = True,
    max_input_chars: int = 100_000,
    max_output_tokens: int = 8192,
    ai_reduction_threshold: int = 0,
) -> ReductionResult:
```

Removed parameters: `api_key`, `model`, `timeout_ms` (absorbed into the provider).

### 6. Orchestrator Call-Site Update

`api_agent/agent/orchestrator.py` currently passes `api_key=settings.SCHEMA_REDUCTION_API_KEY`
and `model=settings.SCHEMA_REDUCTION_MODEL` to `reduce_schema()`. It will instead:

1. Call `create_schema_reduction_provider(settings)` once (lazily cached or at startup).
2. Pass `provider=<LLMProvider | None>` to `reduce_schema()`.

The `_get_haiku_layer()` LRU cache in `reducer.py` is replaced by the provider singleton
pattern already used in `api_agent/agent/model.py`.

---

## Out-of-Scope Clarifications

- **`PROFILE=production`** — not in scope. Can be added later.
- **Auto-detection of localhost targets** — explicitly rejected (see DECISION.md).
- **Schema reduction streaming** — not in scope.
- **Changing the Haiku/AI prompt** — not in scope.
- **Provider health checks at startup** — not in scope; graceful degradation handles failures.
- **Timeout parameter on `LLMProvider.complete()`** — explicitly rejected (S-1). Timeout
  is baked into the HTTP client at construction time via the factory.

---

## Test Strategy

All tests follow the existing TDD patterns in `tests/`:

| Test File | Coverage |
|-----------|----------|
| `tests/test_config.py` | `PROFILE=local` defaults + override priority; new `SCHEMA_REDUCTION_*` fields; custom PROFILE validation error (U-3) |
| `tests/test_schema/test_reducer.py` | `AIReductionLayer` with `FakeLLMProvider`; `reduce_schema()` with provider; None provider = skip AI layer; fence-stripping with non-Anthropic provider response (S-3) |
| `tests/test_schema/test_provider_factory.py` | `create_schema_reduction_provider()` resolution logic for all provider types; timeout injection (S-1) |
| `tests/test_main.py` (or existing) | `--profile` CLI flag parsing (U-2); startup log with profile overrides (U-1) |

Existing `test_config.py`, `test_graphql_agent.py`, `test_rest_agent.py`, etc. must
pass without modification (backward compatibility).

---

## Acceptance Criteria Summary

| # | Criterion | Verified By | Finding |
|---|-----------|-------------|---------|
| AC-1 | `PROFILE=local` sets `BLOCK_PRIVATE_IPS=false` by default | `test_config.py` | |
| AC-2 | Explicit `BLOCK_PRIVATE_IPS=true` wins over `PROFILE=local` | `test_config.py` | |
| AC-3 | `PROFILE=local` sets `SCHEMA_REDUCTION_ENABLED=false` by default | `test_config.py` | |
| AC-4 | `PROFILE` unset → no change in defaults | `test_config.py` | |
| AC-5 | `SCHEMA_REDUCTION_PROVIDER` defaults to main `PROVIDER` | `test_provider_factory.py` | |
| AC-6 | `SCHEMA_REDUCTION_API_KEY` defaults to main `API_KEY` | `test_provider_factory.py` | |
| AC-7 | `SCHEMA_REDUCTION_MODEL` defaults to `""` | `test_config.py` | |
| AC-8 | `AIReductionLayer` with `FakeLLMProvider` produces reduced output | `test_reducer.py` | |
| AC-9 | `AIReductionLayer` with `FakeLLMProvider` returning empty → (original, False) | `test_reducer.py` | |
| AC-10 | `reduce_schema(provider=None)` skips AI layer | `test_reducer.py` | |
| AC-11 | All 1382 existing tests continue to pass | CI | |
| AC-12 | `import anthropic` removed from `reducer.py` | Code review | |
| AC-13 | Factory bakes `SCHEMA_REDUCTION_TIMEOUT_MS` into provider HTTP client | `test_provider_factory.py` | S-1 |
| AC-14 | `AIReductionLayer.reduce()` passes `max_tokens=self.max_output_tokens` explicitly | `test_reducer.py` | S-2 |
| AC-15 | Fence-stripping works for non-Anthropic provider responses (markdown fences) | `test_reducer.py` | S-3 |
| AC-16 | `PROFILE=local` logs effective overridden settings at INFO | `test_main.py` | U-1 |
| AC-17 | `--profile` CLI flag sets `API_AGENT_PROFILE` env var | `test_main.py` | U-2 |
| AC-18 | Invalid `PROFILE` value produces custom validation error message | `test_config.py` | U-3 |
