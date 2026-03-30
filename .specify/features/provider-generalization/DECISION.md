# DECISION.md — provider-generalization

**Feature:** Provider Generalization (Schema Reduction LLM + PROFILE=local Preset)
**Date:** 2026-03-30
**Status:** Accepted

---

## Problem Statement

Two friction points block productive local development and multi-provider deployments:

1. **Schema reduction is hardcoded to Anthropic.** `api_agent/schema/reducer.py` imports
   the `anthropic` SDK directly and instantiates `AsyncAnthropic()` in `HaikuLayer`.
   This means:
   - Users running Ratatoskr with `--provider openai` or `--provider openai-compat`
     (Ollama, LM Studio, vLLM) must *also* supply an Anthropic API key to get AI schema
     reduction, even though their main provider is fully capable of a simple completion.
   - The `SCHEMA_REDUCTION_API_KEY` setting falls back to `ANTHROPIC_API_KEY`, hard-coding
     the assumption that schema reduction == Anthropic.
   - The `SCHEMA_REDUCTION_MODEL` default (`claude-haiku-4-5-20251001`) is
     Anthropic-specific and wrong for non-Anthropic providers.

2. **Local dev requires three manual overrides.** Running Ratatoskr against a localhost
   target (e.g., a local Anamnesis MCP server via mcp-langchain-bridge) requires:
   - `BLOCK_PRIVATE_IPS=false` — otherwise the SSRF guard blocks 127.0.0.1
   - Potentially: awareness that schema reduction will fail silently without an Anthropic key
   - `LOG_FORMAT=console` is nice-to-have for human-readable output

   Auto-detecting localhost targets was considered but rejected: in Kubernetes with an
   Istio sidecar, service-to-service traffic routes through `127.0.0.1` (Envoy proxy),
   so auto-detection would break production deployments. Explicit opt-in is required.

---

## Options Considered

### Schema Reduction Provider (Item 1)

**Option A — Keep Anthropic-only, document it.**
- No code change. Just document that schema reduction requires `ANTHROPIC_API_KEY`.
- Rejected: breaks the "pick any LLM provider" value proposition. Ollama users get silent
  schema degradation with no path forward.

**Option B — Abstract to a new `SchemaReductionProvider` interface.**
- Bespoke interface just for schema reduction. Separate factory, separate config.
- Rejected: duplicates what `LLMProvider` already does. Over-engineering.

**Option C (chosen) — Route through the existing `LLMProvider` abstraction.**
- `reducer.py` accepts an `LLMProvider` instance instead of a raw API key.
- `HaikuLayer` is renamed to `AIReductionLayer` and takes `LLMProvider` instead of
  `anthropic.AsyncAnthropic`.
- A new `create_schema_reduction_provider()` factory in the `llm/` module:
  - Reads `SCHEMA_REDUCTION_PROVIDER` (defaults to main `PROVIDER`)
  - Reads `SCHEMA_REDUCTION_API_KEY` (defaults to main `API_KEY`)
  - Reads `SCHEMA_REDUCTION_MODEL` (defaults to `""` = provider default)
  - Reads `SCHEMA_REDUCTION_BASE_URL` (defaults to main `BASE_URL`)
  - Returns the appropriate `LLMProvider` subclass
- The `reduce_schema()` function signature gains a `provider: LLMProvider | None`
  parameter replacing `api_key` and `model`.
- When `provider is None`, the AI reduction layer is skipped (same behavior as today
  when `api_key` is empty).
- Benefits: zero new abstractions, reuses the existing 3-provider matrix, testable via
  the existing `FakeLLMProvider` pattern.

### Local Dev Preset (Item 2)

**Option A — Document the env vars.**
- No code. README says "set these three vars for local dev."
- Rejected: friction. People hit the private-IP block and file bugs.

**Option B — Auto-detect localhost in SSRF guard.**
- Parse the target URL at request time; if it resolves to a loopback address, bypass
  `BLOCK_PRIVATE_IPS`.
- Rejected: breaks Istio sidecar pattern (k8s services also route via 127.0.0.1).

**Option C (chosen) — `PROFILE=local` preset.**
- A new `PROFILE: Literal["local", ""] = ""` setting in `Settings`.
- When `PROFILE=local`, apply these defaults (user can still override individually):
  - `BLOCK_PRIVATE_IPS=false`
  - `LOG_FORMAT=console`
  - `SCHEMA_REDUCTION_ENABLED=false` (unless `SCHEMA_REDUCTION_PROVIDER` is explicitly
    set — reduces startup friction for users without any cloud LLM key)
- `PROFILE` does NOT override explicitly set values. It sets defaults before pydantic-
  settings reads env vars, so individual overrides always win.
- The implementation uses a pydantic model validator (`@model_validator(mode="before")`)
  to inject profile defaults into the raw field dict before field validation runs.
- Benefits: one env var collapses the common local-dev case; doesn't affect production
  (empty default); individual overrides remain possible.

---

## Scope Decisions

- **In scope:**
  - `reducer.py`: remove `import anthropic`, accept `LLMProvider | None` in place of
    `api_key + model`.
  - `AIReductionLayer`: replaces `HaikuLayer` as provider-agnostic AI reduction layer.
  - `create_schema_reduction_provider()` factory in `api_agent/llm/`.
  - New config fields: `SCHEMA_REDUCTION_PROVIDER`, `SCHEMA_REDUCTION_BASE_URL`.
  - Modified behavior of `SCHEMA_REDUCTION_MODEL` (now defaults to `""` = provider
    default, not `claude-haiku-4-5-20251001`).
  - Modified `SCHEMA_REDUCTION_API_KEY` fallback (defaults to main `API_KEY`, not
    `ANTHROPIC_API_KEY`).
  - `PROFILE: Literal["local", ""] = ""` setting with `model_validator`.
  - All existing tests must pass unchanged. New tests for new behavior.

- **Out of scope:**
  - Changing the schema reduction algorithm itself.
  - Streaming schema reduction responses.
  - `PROFILE=production` or other profiles (can add later).
  - Async provider initialization (providers are lightweight to construct).

- **Backward compatibility guarantees:**
  - Users with `ANTHROPIC_API_KEY` set and no `PROFILE`: schema reduction continues
    to use Anthropic, same model. No observable change.
  - Users with `PROFILE` unset and no `SCHEMA_REDUCTION_*` overrides: all defaults
    identical to today.
  - The `anthropic` package stays as a dependency (used by `anthropic_provider.py`).

---

## Business Value

- **Lowers barrier to entry** for Ollama/local-LLM users: full pipeline (including schema
  reduction) with zero cloud API keys.
- **Simplifies local dev onboarding**: one env var (`PROFILE=local`) instead of three.
- **No breaking changes**: existing deployments see zero impact.
- **Reduces support surface**: fewer "why does schema reduction fail?" issues from users
  who configured a non-Anthropic provider.
