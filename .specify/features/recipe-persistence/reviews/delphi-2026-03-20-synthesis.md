# Delphi Panel Synthesis — Recipe Persistence

**Date:** 2026-03-20
**Panelists:** Systems Architect, Skeptic, Pragmatist, Security Engineer, SRE
**Full reviews:** [delphi-2026-03-20.md](delphi-2026-03-20.md)

---

## Consensus Decisions

### 1. SQLite as default backend — APPROVED (4-1)

**For:** Architect, Pragmatist, Security, SRE
**Against:** Skeptic (prefers JSON-lines, cites container WAL gotchas)

**Why SQLite wins:**
- Concurrent writes safe via WAL mode (agents complete in parallel)
- Crash-safe — half-written JSON = cold start; SQLite WAL protects existing data
- No path traversal risk (single file vs per-recipe filenames)
- stdlib `sqlite3` = genuinely zero-dep
- Ops debuggable: `sqlite3 recipes.db "SELECT ..."`

**Implementation detail (Architect):** Use connection-per-operation inside `asyncio.to_thread()`. Negligible overhead at 64 slots, sidesteps thread-safety entirely.

**Path resolution (Security):** Fallback chain: `$RECIPE_SQLITE_PATH` → `$XDG_CACHE_HOME/ratatoskr/` → `$HOME/.cache/ratatoskr/` → tempdir with warning. Validate writable at startup.

### 2. Eager startup load — APPROVED (unanimous)

**Critical correctness point (Pragmatist/SRE):** Recipes must be registered as MCP tools via `send_tool_list_changed()` BEFORE clients connect. Lazy loading = first request to a previously-known recipe tool 404s. Eager load is a **correctness requirement**, not just performance.

**Safety valve:** `MAX_STARTUP_LOAD = 256` cap. Log warning if load takes >100ms.

### 3. Drop Redis from v1 — APPROVED (unanimous)

**The Redis backend is the over-engineering risk.** No user has asked for it. Adds optional dependency, connection management, TTL logic, error handling, and a second code path that bitrot without users. The ABC makes it trivially extensible later.

**Action:** Ship SQLite only. Add `# TODO: RedisBackend` with one-line description.

### 4. Interface tweaks

- Add `load_all()` method (bulk startup load)
- Don't design for semantic search or MemRL weighting — YAGNI, wrong layer
- `save()` should be fire-and-forget (background task, logged but not blocking)
- Persist full recipe record (including original question text, not just the dict)

### 5. Security — recipe deserialization

**Flagged by Security and Skeptic:** Recipes from SQLite contain user-influenced data (API responses, SQL templates). Use parameterized queries for all SQLite ops. Validate recipe structure on load (schema check). Don't use `pickle` or `eval` — JSON only.

---

## Revised Scope

| Component | Points | Status |
|-----------|--------|--------|
| `RecipeBackend` ABC + `MemoryBackend` | 1 | Ship |
| `SQLiteBackend` | 3 | Ship |
| ~~`RedisBackend`~~ | ~~3~~ | Deferred |
| `RecipeStore` integration (write-through + eager startup) | 2 | Ship |
| Config settings (3 env vars) | 1 | Ship |
| Tests | 3 | Ship |
| **Total** | **10** → **~8** | Single PR |

---

## Design Doc Updates Needed

1. Remove RedisBackend from v1 scope (defer section)
2. Add connection-per-operation pattern for SQLite
3. Add path resolution fallback chain
4. Add `load_all()` to ABC
5. Note eager load as correctness requirement (MCP tool registration)
6. Add recipe validation on deserialization
7. Document WAL mode container considerations (Skeptic's concern)
