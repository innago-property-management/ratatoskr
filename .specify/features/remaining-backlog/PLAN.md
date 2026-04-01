# Remaining Backlog Plan

**Version:** 1.0
**Date:** 2026-03-19

## Priority Order

### 1. Haiku Reduction Threshold Tuning (Wave 4.3) — Complexity: 3-5
- The reducer pipeline, injection scanner, and zero-tools safety are built
- Need: configurable threshold for when keyword-ranked schema is still too large, triggering inner Haiku call
- Config knob: `SCHEMA_AI_REDUCTION_THRESHOLD` (char count? token estimate?)
- Key files: `api_agent/schema/reducer.py`, `api_agent/config.py`

### 2. `/ready` Probe Fix for Keyless Providers — Complexity: 2
- Ollama/vLLM deployments get 503 on readiness (checks API_KEY presence)
- Fix: env var bypass or check provider type before requiring key
- Key files: `api_agent/__main__.py` (health endpoints)

### 3. Document Minimum k8s Version — Complexity: 1
- `policy/v1` PDB requires k8s ≥1.21
- One-liner in README or deploy docs

### 4. Automate Image Tag Bump on Release — Complexity: 5
- GitHub Action to update kustomize overlay on PyPI publish
- Key files: `.github/workflows/`, `deploy/overlays/production/`

### 5. Global RECIPE_STORE Cross-Session — Complexity: 8
- Persistence layer for recipes across restarts
- Only matters for multi-tenant or long-lived deployments
- Deferred unless real-world usage demands it

### 6. P2 Schema Context / P3 DuckDB — Parked
- Keyword ranking sufficient, hot path covered
- Revisit only if real-world usage exposes gaps

## Bundling Strategy
- Items 2+3 → single small PR (operational fixes)
- Item 1 → standalone PR (feature work)
- Items 4-6 → future sessions
