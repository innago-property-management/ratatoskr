# Data Model: Recipe Persistence

**Phase**: 1 — Design & Contracts
**Date**: 2026-03-20

## Entities

### RecipeRecord (persisted)

The unit of persistence. Maps 1:1 with a recipe in the in-memory LRU.

| Field | Type | Description |
|-------|------|-------------|
| api_id | string | Target API identifier (from RequestContext) |
| schema_hash | string | Hash of the API schema at extraction time |
| recipe_id | string | Unique recipe identifier |
| recipe_json | string | Full recipe serialized as JSON |
| created_at | float | Unix timestamp of recipe extraction |
| last_used_at | float | Unix timestamp of last use (for LRU eviction) |

**Primary key**: `(api_id, schema_hash, recipe_id)`
**Index**: `(api_id, schema_hash)` — for bulk load and schema-change invalidation

### Relationships

```
RecipeStore (in-memory LRU)
  └── 1:1 ──→ RecipeBackend (persistence layer)
                 ├── MemoryBackend (no-op, tests/ephemeral)
                 └── SQLiteBackend (default, local file)

RecipeRecord lifecycle:
  Agent extracts recipe
    → RecipeStore.save_recipe_if_unique()
      → LRU cache (hot path)
      → RecipeBackend.save() (fire-and-forget, write-through)

  Server starts
    → RecipeBackend.load_all()
      → RecipeStore populates LRU
      → send_tool_list_changed() registers MCP tools

  Schema changes
    → RecipeStore invalidates LRU entries
    → RecipeBackend.delete_by_schema() purges persistent storage
```

### State Transitions

```
Recipe lifecycle:
  [Extracted] → save() → [Persisted + In-Memory]
  [Persisted] → load_all() on restart → [In-Memory]
  [Persisted + In-Memory] → schema change → [Deleted]
  [In-Memory only] → LRU eviction → [Gone] (not in persistent store)
  [Persisted] → corrupted → skip on load → [In-Memory without this recipe]
```

### Validation Rules

- `recipe_json` must be valid JSON (parse on load, skip with warning if invalid)
- `recipe_json` must contain required fields: `recipe_id`, `api_call` or `graphql_query`, `sql` (optional)
- `api_id` and `schema_hash` must be non-empty strings
- `created_at` and `last_used_at` must be positive floats
- Total persisted recipes capped at `RECIPE_CACHE_SIZE` (default 64) — oldest evicted on load
