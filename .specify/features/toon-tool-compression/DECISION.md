# TOON Tool Result Compression — Decision

**Version:** 1.0
**Date:** 2026-03-16
**Status:** Approved for implementation

## Use Case

Ratatoskr feeds API responses back to the LLM as tool results. These are typically
JSON-serialized lists of records — exactly the shape where TOON achieves 30-60%
token reduction. Reducing token volume directly lowers cost and latency for every
agent turn that processes data.

## Business Value

- **30-60% token reduction** on tool results (list payloads from API calls and SQL)
- **Immediate savings** — the hot path hits `format_tool_response()` on every data-returning call
- **Zero degradation risk** — graceful fallback to JSON if TOON is unavailable or larger
- **No LLM prompt changes needed** — LLMs understand TOON natively (it's a readable format)

## Scope

### In scope
- TOON-encode list payloads in `format_tool_response()` (orchestrator)
- TOON-encode SQL results in `create_sql_query_tool()` (orchestrator)
- `api_agent/llm/toon_encoder.py` — reusable encoder wrapper with graceful degradation
- Config gate: `TOON_TOOL_RESULTS_ENABLED: bool = True`
- Observability: log compression ratio per call

### Out of scope
- Schema TOON (already handled by ToonLayer in reducer.py — inert for DSL text by design)
- MCP output format header (deferred — needs MCP client negotiation)
- DuckDB output formatting changes (DuckDB returns list[dict] — already handled by tool result path)
- Per-turn fallback (if LLM signals confusion) — deferred; monitor in production first

## Technology Decision

**toon_format** library — already installed as optional dependency (`[toon]` group in pyproject.toml).

The encoder wrapper (`api_agent/llm/toon_encoder.py`) isolates the dependency:
- ImportError → log warning, return JSON (no crash)
- Encoding failure → log warning, return JSON (no crash)
- TOON output larger than JSON → return JSON (no overhead)

## Key Insight (from Cygnus prior art)

> "Convert at middleware layer, not agent layer"

The right integration point is `format_tool_response()` — the single choke point where
all 4 protocol agents (GraphQL, REST, gRPC, MCP) serialize tool results. This is
already centralized in orchestrator.py, making the integration clean and minimal.
