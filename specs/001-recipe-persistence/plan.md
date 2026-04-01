# Implementation Plan: Recipe Persistence

**Branch**: `001-recipe-persistence` | **Date**: 2026-03-20 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-recipe-persistence/spec.md`
**Design**: [DESIGN.md](../../.specify/features/recipe-persistence/DESIGN.md) (v2.0, post-Delphi)

## Summary

Add pluggable recipe persistence to ratatoskr so learned API call + SQL pipeline recipes survive process restarts. Default backend is SQLite (stdlib, zero-dep). In-memory LRU remains the hot path; backend is write-through on save, eager-load on startup. Recipes must register as MCP tools before first client connection (correctness requirement).

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: sqlite3 (stdlib), asyncio, existing RecipeStore
**Storage**: SQLite with WAL mode (default), MemoryBackend (opt-out)
**Testing**: pytest + pytest-asyncio, FakeLLMProvider pattern
**Target Platform**: Linux/macOS server, Docker containers
**Project Type**: Library/MCP server (PyPI package)
**Performance Goals**: Startup load <100ms for 64 recipes, zero latency impact on recipe lookups
**Constraints**: Zero new PyPI dependencies, fire-and-forget writes, crash-safe storage
**Scale/Scope**: 64-slot LRU cache (configurable), single-tenant deployments

## Constitution Check

*GATE: Project constitution is default template (not customized). Using CLAUDE.md conventions instead.*

| Principle | Status | Notes |
|-----------|--------|-------|
| TDD (CLAUDE.md) | PASS | Tests first for all backends and integration |
| Zero new deps | PASS | sqlite3 is stdlib |
| Feature branches | PASS | Working on 001-recipe-persistence |
| ISP (consumer controls interface) | PASS | RecipeStore defines what it needs from RecipeBackend |
| No Console.* | N/A | Python project |
| Pre-commit hooks | PASS | gitleaks + prevent-commits-to-default-branch |

## Project Structure

### Documentation (this feature)

```text
specs/001-recipe-persistence/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 (below)
├── data-model.md        # Phase 1 (below)
└── checklists/
    └── requirements.md  # Quality checklist

.specify/features/recipe-persistence/
├── DESIGN.md            # Technical design (v2.0, post-Delphi)
└── reviews/
    ├── delphi-2026-03-20.md           # Full Delphi panel output
    └── delphi-2026-03-20-synthesis.md # Consolidated decisions
```

### Source Code (repository root)

```text
api_agent/recipe/
├── store.py             # Existing RecipeStore (MODIFY — add backend integration)
├── backend.py           # NEW — RecipeBackend ABC + MemoryBackend
├── sqlite_backend.py    # NEW — SQLiteBackend implementation
└── ...                  # Existing: extractor.py, runner.py, common.py, naming.py

api_agent/config.py      # MODIFY — add RECIPE_PERSISTENCE + RECIPE_SQLITE_PATH settings

tests/
├── test_recipe_backend.py        # NEW — RecipeBackend ABC + MemoryBackend tests
├── test_sqlite_backend.py        # NEW — SQLiteBackend tests
└── test_recipe_store_persist.py  # NEW — RecipeStore + backend integration tests
```

**Structure Decision**: New files in existing `api_agent/recipe/` package. Backend ABC and SQLite impl are separate modules (single responsibility). Tests follow existing pattern of one file per module.
