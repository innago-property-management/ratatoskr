# Tasks: Recipe Persistence

**Input**: Design documents from `/specs/001-recipe-persistence/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md
**Tests**: TDD approach — tests written first per CLAUDE.md conventions

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

## Phase 1: Setup

**Purpose**: Configuration and interface scaffolding

- [ ] T001 Add `RECIPE_PERSISTENCE` and `RECIPE_SQLITE_PATH` settings to `api_agent/config.py`
- [ ] T002 [P] Create `RecipeBackend` ABC with `load_all()`, `save()`, `delete_by_schema()` in `api_agent/recipe/backend.py`
- [ ] T003 [P] Create `MemoryBackend` (no-op implementation) in `api_agent/recipe/backend.py`

---

## Phase 2: Foundational — SQLiteBackend

**Purpose**: Core persistence implementation that MUST be complete before user story integration

**⚠️ CRITICAL**: RecipeStore integration (Phase 3) depends on this phase

### Tests (TDD — write first, verify RED)

- [ ] T004 [P] Write tests for `RecipeBackend` ABC and `MemoryBackend` in `tests/test_recipe_backend.py`: load_all returns empty list, save is no-op, delete_by_schema is no-op
- [ ] T005 [P] Write tests for `SQLiteBackend` in `tests/test_sqlite_backend.py`: save and load_all round-trip, delete_by_schema removes correct entries, handles corrupted JSON gracefully, handles missing/unwritable DB path, WAL mode enabled, concurrent writes safe, respects cache size limit on load
- [ ] T006 [P] Write tests for config settings in `tests/test_config.py`: RECIPE_PERSISTENCE default, RECIPE_SQLITE_PATH default, env var overrides

### Implementation

- [ ] T007 Implement `SQLiteBackend` in `api_agent/recipe/sqlite_backend.py`: path resolution fallback chain (env → XDG → HOME → tempdir), WAL mode init, connection-per-operation via `asyncio.to_thread()`, parameterized queries, JSON validation on load, LRU cap on load_all, graceful fallback on init failure
- [ ] T008 Verify all Phase 2 tests pass (GREEN)

**Checkpoint**: Both backends work independently. SQLiteBackend can save/load/delete recipes to/from SQLite.

---

## Phase 3: User Story 1 — Recipes Survive Restarts (Priority: P1) 🎯 MVP

**Goal**: Recipes persist across server restarts. On startup, all persisted recipes load into the LRU and register as MCP tools before clients connect.

**Independent Test**: Start server, extract a recipe via query, restart, verify recipe tool is available without re-querying.

### Tests (TDD — write first, verify RED)

- [ ] T009 [P] [US1] Write integration tests in `tests/test_recipe_store_persist.py`: RecipeStore with SQLiteBackend saves on `save_recipe_if_unique()`, RecipeStore with SQLiteBackend loads all on init, loaded recipes register as MCP tools, fire-and-forget write failure doesn't block save_recipe_if_unique

### Implementation

- [ ] T010 [US1] Add optional `backend` parameter to `RecipeStore.__init__()` in `api_agent/recipe/store.py`
- [ ] T011 [US1] Add `async def load_from_backend()` to `RecipeStore` — calls `backend.load_all()`, populates LRU, returns count loaded. Cap at `MAX_STARTUP_LOAD=256` with warning
- [ ] T012 [US1] Add write-through call in `save_recipe_if_unique()` — calls `backend.save()` as fire-and-forget background task via `asyncio.create_task()`
- [ ] T013 [US1] Wire backend creation in server startup (`api_agent/__main__.py` or app factory) — create backend from config, pass to RecipeStore, call `load_from_backend()` before accepting connections
- [ ] T014 [US1] Verify all US1 tests pass (GREEN)

**Checkpoint**: Recipes survive restarts. MCP tools registered on startup.

---

## Phase 4: User Story 2 — Zero-Config Default (Priority: P1)

**Goal**: Fresh `pip install` + `uv run api-agent` persists recipes automatically with no env vars or config.

**Independent Test**: Run server with zero config, generate recipe, restart, verify persisted.

### Tests (TDD — write first, verify RED)

- [ ] T015 [P] [US2] Write tests in `tests/test_sqlite_backend.py`: default path resolution creates `~/.cache/ratatoskr/recipes.db`, directory auto-created if missing, unwritable path falls back to MemoryBackend with warning log

### Implementation

- [ ] T016 [US2] Implement `_resolve_sqlite_path()` helper in `api_agent/recipe/sqlite_backend.py` — env var → XDG_CACHE_HOME → HOME/.cache → tempdir fallback chain, auto-create parent directory
- [ ] T017 [US2] Implement `create_backend_from_config()` factory function in `api_agent/recipe/backend.py` — reads `RECIPE_PERSISTENCE` setting, returns appropriate backend, catches SQLiteBackend init failure and falls back to MemoryBackend with warning
- [ ] T018 [US2] Verify all US2 tests pass (GREEN)

**Checkpoint**: Zero-config works. Server auto-persists to sensible default location.

---

## Phase 5: User Story 3 — Schema Change Invalidation (Priority: P2)

**Goal**: When an API's schema changes, stale recipes are purged from both memory and persistent storage.

**Independent Test**: Persist recipe, simulate schema hash change, verify recipe purged.

### Tests (TDD — write first, verify RED)

- [ ] T019 [P] [US3] Write tests in `tests/test_recipe_store_persist.py`: schema change triggers `backend.delete_by_schema()`, only affected API's recipes are deleted, other API's recipes remain

### Implementation

- [ ] T020 [US3] Add `backend.delete_by_schema()` call to RecipeStore's existing schema invalidation path in `api_agent/recipe/store.py`
- [ ] T021 [US3] Verify all US3 tests pass (GREEN)

**Checkpoint**: Schema changes automatically invalidate stale persisted recipes.

---

## Phase 6: User Story 4 — Opt-Out to Memory-Only (Priority: P3)

**Goal**: Operators can explicitly disable persistence via config.

**Independent Test**: Set `RECIPE_PERSISTENCE=memory`, verify no files created.

### Tests (TDD — write first, verify RED)

- [ ] T022 [P] [US4] Write test in `tests/test_recipe_store_persist.py`: RECIPE_PERSISTENCE=memory creates MemoryBackend, no disk files created

### Implementation

- [ ] T023 [US4] Verify `create_backend_from_config()` returns MemoryBackend when `RECIPE_PERSISTENCE=memory` (may already work from T017)
- [ ] T024 [US4] Verify all US4 tests pass (GREEN)

**Checkpoint**: Opt-out works. Memory-only mode matches current behavior exactly.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, regression check, cleanup

- [ ] T025 Run full test suite (`uv run pytest tests/ -v`) — verify zero regressions, all existing 1286 tests pass
- [ ] T026 [P] Run linter (`uv run ruff check api_agent/`) — verify clean
- [ ] T027 [P] Update CLAUDE.md test coverage table with new test files
- [ ] T028 [P] Add container WAL considerations to `deploy/` docs or README
- [ ] T029 Validate quickstart.md scenarios manually

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 — core integration
- **Phase 4 (US2)**: Depends on Phase 2 — can run parallel with US1
- **Phase 5 (US3)**: Depends on Phase 3 (needs RecipeStore integration)
- **Phase 6 (US4)**: Depends on Phase 4 (needs factory function)
- **Phase 7 (Polish)**: Depends on all user stories complete

### User Story Dependencies

- **US1 (Restart survival)**: Depends on Foundational only — MVP
- **US2 (Zero-config)**: Depends on Foundational only — can parallel with US1
- **US3 (Schema invalidation)**: Depends on US1 (needs write-through integration)
- **US4 (Opt-out)**: Depends on US2 (needs factory function)

### Parallel Opportunities

```
Phase 1: T002 ‖ T003 (different files)
Phase 2: T004 ‖ T005 ‖ T006 (different test files)
Phase 3+4: US1 ‖ US2 (independent after foundational)
Phase 7: T026 ‖ T027 ‖ T028 (different files)
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T003)
2. Complete Phase 2: Foundational SQLiteBackend (T004-T008)
3. Complete Phase 3: US1 Restart Survival (T009-T014)
4. **STOP and VALIDATE**: Restart server, verify recipes survive
5. This is a shippable increment — recipes persist with default config

### Incremental Delivery

1. Setup + Foundational → backends work independently
2. US1 (restart survival) → MVP, shippable
3. US2 (zero-config) → improved DX, shippable
4. US3 (schema invalidation) → correctness, shippable
5. US4 (opt-out) → operator control, shippable
6. Polish → docs, regression check

---

## Notes

- TDD mandatory per CLAUDE.md — write tests first, verify RED, then implement
- Fire-and-forget writes: use `asyncio.create_task()`, log errors but never raise
- Connection-per-operation: new `sqlite3.connect()` per `to_thread()` call
- JSON serialization only — no pickle, no eval (security constraint)
- Existing RecipeStore locking (TOCTOU-safe) remains unchanged
- All new files: `backend.py`, `sqlite_backend.py`, `test_recipe_backend.py`, `test_sqlite_backend.py`, `test_recipe_store_persist.py`
