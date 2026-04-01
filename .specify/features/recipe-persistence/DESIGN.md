# Recipe Persistence — Design Document

**Version:** 2.0 (Post-Delphi)
**Date:** 2026-03-20
**Status:** Approved — ready for implementation
**Changes:** Incorporated Delphi panel feedback (5 reviewers). Dropped RedisBackend from v1. Added connection-per-op pattern, path fallback chain, eager load correctness rationale, deserialization validation.

## Problem

Ratatoskr's recipe store is in-memory LRU (64 slots). Process restart = all learned recipes lost. The agent must re-discover patterns through LLM-reasoned queries. For single-tenant, short-lived instances this is acceptable. For persistent deployments, cold-start recipe loss becomes painful.

## Prior Art: Cygnus Multi-Tier Memory

The Cygnus supervisor project has a multi-tier memory architecture for a conceptually identical problem — persisting learned execution patterns:

| Tier | Store | Purpose | TTL |
|------|-------|---------|-----|
| Hot | Valkey (Redis) | Working set, fast lookup by fingerprint | Hours–days |
| Triplet | Neo4j | State-action-outcome graphs for RL retrieval | Decay function |
| Pattern | DETS (Elixir) | Codified patterns from successful executions | Permanent |
| Deep | LanceDB (Ashildr) | Semantic retrieval for novel cases | Permanent |

Key patterns from Cygnus:
- **Fire-and-forget writes**: Non-blocking persistence (hooks exit 0 always)
- **Fingerprinting**: Deterministic keys from `(tool, sorted_args)` tuples
- **TTL + decay**: Hot entries expire, deep entries use exponential decay

## Ratatoskr Constraint: Open Source

Ratatoskr is MIT-licensed and published on PyPI. The persistence layer must:

1. **Work out of the box** with zero infrastructure dependencies
2. **Not require** Cygnus, Ashildr, Elixir, or any Innago-specific tooling
3. **Ship as a pluggable backend** behind the existing `RecipeStore` interface
4. **Optionally** support external stores via the ABC (future PRs)

## Architecture

### Interface: `RecipeBackend` (ABC)

```python
class RecipeBackend(ABC):
    """Pluggable persistence backend for the recipe store."""

    @abstractmethod
    async def load_all(self) -> list[tuple[str, str, dict]]:
        """Load all recipes on startup. Returns list of (api_id, schema_hash, recipe_dict)."""

    @abstractmethod
    async def save(self, api_id: str, schema_hash: str, recipe: dict) -> None:
        """Persist a recipe. Fire-and-forget — failures logged, never raised."""

    @abstractmethod
    async def delete_by_schema(self, api_id: str, schema_hash: str) -> int:
        """Invalidate recipes when schema changes. Returns count deleted."""
```

**Delphi decision:** `load_all()` instead of per-key `load()`. Eager startup is a correctness requirement — recipes must register as MCP tools via `send_tool_list_changed()` before clients connect. Lazy loading = 404 on previously-known recipe tools.

### Backend 1: `SQLiteBackend` (default)

**Why SQLite (Delphi 4-1 consensus):**
- Concurrent writes safe via WAL mode (agents complete in parallel)
- Crash-safe — half-written JSON file = cold start; SQLite WAL protects existing data
- No path traversal risk (single file, no filename derivation from user data)
- stdlib `sqlite3` = genuinely zero-dep
- Ops debuggable: `sqlite3 recipes.db "SELECT ..."`

**Implementation details:**
- **Connection-per-operation** inside `asyncio.to_thread()` — sidesteps `sqlite3` thread-safety entirely, negligible overhead at 64 slots
- **WAL mode** enabled on first connection (`PRAGMA journal_mode=WAL`)
- **Parameterized queries only** — recipes contain user-influenced data (API responses, SQL templates)
- **Recipe validation on load** — verify JSON structure before deserializing into recipe objects

**Path resolution fallback chain (Security review):**
1. `API_AGENT_RECIPE_SQLITE_PATH` env var (explicit)
2. `$XDG_CACHE_HOME/ratatoskr/recipes.db`
3. `$HOME/.cache/ratatoskr/recipes.db`
4. Temp directory with warning log

Validate writable at startup, not on first save.

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS recipes (
    api_id TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    PRIMARY KEY (api_id, schema_hash, recipe_id)
);
CREATE INDEX IF NOT EXISTS idx_recipes_schema ON recipes(api_id, schema_hash);
```

**Container considerations (Skeptic's concern):** SQLite WAL mode requires the filesystem to support `flock()`. Document that NFS mounts and some container overlay filesystems may not support this. Recommend mounting a local volume for the cache directory in containerized deployments.

### Backend 2: `MemoryBackend` (tests/ephemeral)

- No-op persistence — `load_all()` returns `[]`, `save()` and `delete_by_schema()` are no-ops
- Current behavior preserved when `RECIPE_PERSISTENCE=memory` (default)
- Used in tests to avoid SQLite file creation

### Configuration

```
API_AGENT_RECIPE_PERSISTENCE=memory    # "memory" (default) or "sqlite"
API_AGENT_RECIPE_SQLITE_PATH=          # empty = use XDG fallback chain
```

### Integration with RecipeStore

`RecipeStore` currently owns the LRU dict. Changes:

1. `RecipeStore.__init__` accepts an optional `RecipeBackend`
2. **On startup:** `await backend.load_all()` pre-populates the in-memory LRU, then `send_tool_list_changed()` registers recipe tools. Safety cap: `MAX_STARTUP_LOAD = 256` with warning log if exceeded.
3. **On `save_recipe_if_unique()`:** also calls `backend.save()` as fire-and-forget background task (logged, not blocking)
4. **On schema invalidation:** also calls `backend.delete_by_schema()`
5. **In-memory LRU remains the hot path** — backend is write-through, read-on-startup

## What We're NOT Building

- **RedisBackend** — deferred until actual demand. The ABC makes it trivially extensible later. No user has asked for multi-instance recipe sharing.
- **Semantic/fuzzy recipe matching** — recipes keyed by exact `(api_id, schema_hash)`. Fuzzy matching is a different layer (future).
- **MemRL weight learning** — no reinforcement signals on recipes yet. Could add `use_count` / `success_rate` as a future enhancement.
- **Recipe versioning/evolution** — recipes are immutable once extracted. Schema hash change = invalidate all.

## Complexity Estimate

| Component | Points |
|-----------|--------|
| `RecipeBackend` ABC + `MemoryBackend` | 1 |
| `SQLiteBackend` (with path resolution, WAL, validation) | 3 |
| `RecipeStore` integration (write-through + eager startup) | 2 |
| Config settings | 1 |
| Tests (both backends + integration) | 3 |
| **Total** | **~8** |

Single PR. RedisBackend deferred to future demand.

## Delphi Review Summary

Reviewed 2026-03-20 by 5-person panel (Systems Architect, Skeptic, Pragmatist, Security Engineer, SRE). Full reviews in `reviews/delphi-2026-03-20.md`.

| Question | Decision |
|----------|----------|
| SQLite vs JSON? | **SQLite.** Crash-safe concurrent writes, stdlib, ops debuggable. (4-1) |
| Eager vs lazy? | **Eager.** Correctness requirement — MCP tool registration. (Unanimous) |
| Interface design? | **`load_all()` over per-key `load()`.** Don't design for semantic/MemRL — YAGNI. |
| Over-engineering? | **Drop Redis from v1.** Ship SQLite only, build Redis on demand. (Unanimous) |
| Security? | **Parameterized queries, validate on load, path fallback chain.** |
