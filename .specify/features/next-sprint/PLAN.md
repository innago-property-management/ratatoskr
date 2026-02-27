# Ratatoskr Next Sprint Plan

**Version:** 1.0
**Date:** 2026-02-26
**Branch:** main @ 81627e0

## Track A: Bug Fix + Housekeeping (parallel)

### A1: Fix REST `_build_url` bugs (complexity: 3)
- **File:** `api_agent/rest/client.py` lines 24-66
- **Bug 1:** `urljoin` drops base path prefixes when base_url has path (e.g. `/v2`)
- **Bug 2:** Duplicate scheme-check guard (dead code lines 53-55)
- **Bug 3:** Relative server URLs from OpenAPI `servers[0]` produce `https:///v2`
- **Bug 4:** `urlencode` missing `doseq=True` for list-valued query params
- **Fix:** Replace `urljoin` with simple string concat, remove dead code, add `doseq=True`
- **Tests:** TDD — add cases for path prefix, relative URL, multi-value params

### A2: Upstream cherry-pick (complexity: 1)
- **Commit:** `3b597e0` — docs: clarify header escaping with examples
- **Conflict:** Will conflict with our rewritten README; adapt to our style
- **Action:** Cherry-pick, resolve conflict, fold escaping examples into our README

### A3: Cleanup (complexity: 1)
- **Delete:** `Gemini_Generated_Image_*.png` from repo root (already gitignored, just clutter)
- **Update:** MEMORY.md — remove stale notes about test_integration.py/test_providers.py

## Track B: gRPC v2 — Server Streaming + _execute (sequential after A)

### B1: gRPC `_execute` tool (complexity: 3)
- **File:** `tools/execute.py`
- **Add:** `elif ctx.api_type == "grpc":` branch
- **New params:** `grpc_method: str`, `grpc_request: dict`
- **Flow:** fetch_schema → find method → execute_unary_rpc → truncate response

### B2: Server streaming client (complexity: 3)
- **File:** `grpc/client.py`
- **Add:** `execute_server_streaming_rpc()` — async iterator with max_messages cap
- **Key risk:** Channel close ordering — must drain/cancel iterator before close

### B3: Server streaming agent tool (complexity: 3)
- **File:** `agent/grpc_agent.py`, `agent/prompts.py`
- **Add:** `grpc_stream` tool, remove `[unsupported-v1]` tags
- **Depends on:** B2

### B4: Tests (complexity: 3)
- **TDD for B1-B3**, mutation-proof style
- **Files:** `tests/test_grpc_*.py`, `tests/test_execute_tool.py`

## Track C: Stretch Goals (deferred)

### C1: Integration tests against real APIs (complexity: 5)
### C2: Provider token usage comparison (complexity: 3)
### Deferred: Client/bidi streaming, recipe extraction (needs Delphi on DescriptorPool lifecycle)

## Parallelism Map

```
Track A (Cygnus fleet, all parallel):
  Agent 1: A1 — Fix _build_url (TDD)
  Agent 2: A2 — Upstream cherry-pick
  Agent 3: A3 — Cleanup

Track B (after A merges):
  Agent 4: B1 — gRPC _execute (TDD)     } parallel
  Agent 5: B2 — Streaming client (TDD)   }
  Agent 6: B3 — Streaming agent tool (after B2)
```
