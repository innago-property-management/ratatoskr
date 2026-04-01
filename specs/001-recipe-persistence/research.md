# Research: Recipe Persistence

**Phase**: 0 — Outline & Research
**Date**: 2026-03-20
**Status**: Complete (all unknowns resolved via Delphi panel)

## R1: Storage Backend Choice

**Decision**: SQLite via stdlib `sqlite3`
**Rationale**: Crash-safe concurrent writes (WAL mode), atomic operations, no path traversal risk (single file), stdlib zero-dep, ops-debuggable via CLI. Delphi panel approved 4-1.
**Alternatives considered**:
- JSON-lines files: Simpler but unsafe under concurrent writes, requires fsync+rename dance, filename derivation from user data creates path traversal risk
- Redis: Requires external infrastructure, deferred until demand (YAGNI)
- shelve/dbm: Less portable, no WAL-equivalent, harder to debug

## R2: Thread Safety Pattern

**Decision**: Connection-per-operation inside `asyncio.to_thread()`
**Rationale**: `sqlite3` connections are not thread-safe by default. Creating a new connection per operation inside `to_thread()` is simplest and avoids all thread-safety concerns. Overhead is negligible at 64-slot cache scale.
**Alternatives considered**:
- Dedicated writer thread with queue: Cleanest but over-engineered for this scale
- `check_same_thread=False` + external mutex: Works but adds complexity
- Single connection with lock: Risk of blocking the event loop

## R3: Startup Load Strategy

**Decision**: Eager load all recipes on startup
**Rationale**: Correctness requirement, not just performance. Recipes must register as MCP tools via `send_tool_list_changed()` before clients connect. Lazy loading would cause 404s on previously-known recipe tools.
**Alternatives considered**:
- Lazy load per (api_id, schema_hash): Simpler but breaks MCP tool registration — clients would see missing tools until first query triggers load
- Background load with eventual consistency: Still races with first client connection

## R4: Path Resolution

**Decision**: Fallback chain — env var → XDG → HOME → tempdir
**Rationale**: Security review recommendation. Handles containerized deployments where HOME may be unset. Validates writable at startup, not on first save.
**Resolution chain**:
1. `API_AGENT_RECIPE_SQLITE_PATH` (explicit override)
2. `$XDG_CACHE_HOME/ratatoskr/recipes.db`
3. `$HOME/.cache/ratatoskr/recipes.db`
4. `tempfile.gettempdir()/ratatoskr/recipes.db` (with warning log)

## R5: Recipe Serialization Safety

**Decision**: JSON only, validate structure on load
**Rationale**: Recipes contain user-influenced data (API responses, SQL templates). JSON is safe (no code execution). Parameterized SQL queries for all SQLite operations prevent injection. Malformed entries skipped with warning — one bad recipe doesn't block loading others.
**Alternatives considered**:
- pickle: Unsafe (arbitrary code execution on deserialization)
- msgpack: Faster but adds dependency
- Protocol buffers: Overkill for this scale

## R6: Existing RecipeStore Integration Points

**Research**: Read `api_agent/recipe/store.py` to identify integration seams.
**Findings**:
- `RecipeStore` has `save_recipe_if_unique()` — add backend.save() call here (fire-and-forget)
- `RecipeStore` keyed by `(api_id, schema_hash)` — matches backend interface
- Schema invalidation already removes recipes from LRU — extend to call backend.delete_by_schema()
- `send_tool_list_changed()` already called after recipe mutations — no new notification needed
- Constructor accepts `max_size` — add optional `backend` parameter

## R7: Container/WAL Considerations

**Decision**: Document WAL limitations, don't work around them
**Rationale**: SQLite WAL mode requires `flock()` support. NFS and some overlay filesystems don't support this. Rather than adding complexity to detect/fallback, document the requirement: mount a local volume for the cache directory in containerized deployments.
**Mitigation**: If WAL init fails, catch the error and fall back to MemoryBackend with a warning log.
