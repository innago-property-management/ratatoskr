"""Integration tests for RecipeStore with persistence backends (T009, T019, T022)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from api_agent.recipe.backend import MemoryBackend
from api_agent.recipe.store import RecipeStore


async def _drain_background_tasks(store: RecipeStore) -> None:
    """Await all pending fire-and-forget persistence tasks deterministically."""
    if store._background_tasks:
        await asyncio.gather(*store._background_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# US1: Recipes survive restarts
# ---------------------------------------------------------------------------


class TestRecipeStoreSaveWritesThrough:
    """save_recipe_if_unique fires off backend.save."""

    @pytest.mark.asyncio
    async def test_save_writes_to_backend(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)
        store = RecipeStore(max_size=10, backend=backend)

        recipe_id = await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="list users",
            recipe={"steps": [{"type": "graphql", "query": "{ users }"}]},
            tool_name="list_users",
        )
        assert recipe_id is not None
        await _drain_background_tasks(store)

        # Verify it was persisted to SQLite
        rows = await backend.load_all()
        assert len(rows) == 1
        assert rows[0][2] == recipe_id
        assert rows[0][3]["steps"][0]["query"] == "{ users }"

    @pytest.mark.asyncio
    async def test_save_recipe_also_persists(self, tmp_path):
        """save_recipe (not just save_recipe_if_unique) also writes through."""
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)
        store = RecipeStore(max_size=10, backend=backend)

        recipe_id = await store.save_recipe(
            api_id="api1",
            schema_hash="hash1",
            question="list users",
            recipe={"steps": []},
            tool_name="list_users",
        )
        await _drain_background_tasks(store)

        rows = await backend.load_all()
        assert len(rows) == 1
        assert rows[0][2] == recipe_id

    @pytest.mark.asyncio
    async def test_tool_name_survives_round_trip(self, tmp_path):
        """tool_name is persisted and restored correctly, not lost to recipe_id fallback."""
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)
        store = RecipeStore(max_size=10, backend=backend)

        await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="list users",
            recipe={"steps": []},
            tool_name="list_users",
        )
        await _drain_background_tasks(store)

        # Create a fresh store and load from backend
        store2 = RecipeStore(max_size=10, backend=backend)
        count = await store2.load_from_backend()
        assert count == 1

        # The loaded recipe should have the original tool_name, not the recipe_id
        recipes = await store2.list_recipes(api_id="api1", schema_hash="hash1")
        assert len(recipes) == 1
        assert recipes[0]["tool_name"] == "list_users"


class TestRecipeStoreLoadFromBackend:
    """load_from_backend restores recipes into LRU."""

    @pytest.mark.asyncio
    async def test_load_restores_recipes(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        # Pre-populate backend directly
        recipe = {"steps": [], "params": {}, "question": "get users", "tool_name": "get_users"}
        await backend.save("api1", "hash1", "r_preload1", recipe, 100.0, 200.0)

        # Create new store and load
        store = RecipeStore(max_size=10, backend=backend)
        count = await store.load_from_backend()
        assert count == 1

    @pytest.mark.asyncio
    async def test_loaded_recipes_retrievable(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        recipe = {
            "steps": [{"type": "rest"}],
            "params": {"id": {"type": "string"}},
            "tool_name": "get_thing",
        }
        await backend.save("api1", "hash1", "r_load01", recipe, 100.0, 200.0)

        store = RecipeStore(max_size=10, backend=backend)
        await store.load_from_backend()

        result = await store.get_recipe("r_load01")
        assert result is not None
        assert result["steps"][0]["type"] == "rest"

    @pytest.mark.asyncio
    async def test_load_caps_at_max_size(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path, max_recipes=100)

        # Save more recipes than store max_size
        for i in range(20):
            await backend.save("api1", "hash1", f"r_{i:03d}", {"i": i}, float(i), float(i))

        store = RecipeStore(max_size=5, backend=backend)
        count = await store.load_from_backend()
        # Should cap at store's max_size
        assert count <= 5

    @pytest.mark.asyncio
    async def test_load_with_no_backend_returns_zero(self):
        store = RecipeStore(max_size=10)
        count = await store.load_from_backend()
        assert count == 0


class TestRecipeStoreFireAndForget:
    """Fire-and-forget: backend.save failure doesn't break save_recipe_if_unique."""

    @pytest.mark.asyncio
    async def test_backend_save_failure_doesnt_break_store(self):
        backend = AsyncMock(spec=MemoryBackend)
        backend.save.side_effect = RuntimeError("disk full")

        store = RecipeStore(max_size=10, backend=backend)

        recipe_id = await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="get things",
            recipe={"steps": []},
            tool_name="get_things",
        )
        # Should still return the recipe_id (in-memory save succeeded)
        assert recipe_id is not None

        # Drain tasks — the failure should be caught and logged
        await _drain_background_tasks(store)

        # Recipe should still be retrievable from in-memory store
        result = await store.get_recipe(recipe_id)
        assert result is not None


# ---------------------------------------------------------------------------
# US3: Schema change invalidation
# ---------------------------------------------------------------------------


class TestRecipeStoreSchemaInvalidation:
    """invalidate_schema removes from memory and backend."""

    @pytest.mark.asyncio
    async def test_invalidate_removes_matching_recipes(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)
        store = RecipeStore(max_size=10, backend=backend)

        # Save two recipes for same schema
        await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="q1",
            recipe={"steps": []},
            tool_name="t1",
        )
        await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="q2",
            recipe={"steps": []},
            tool_name="t2",
        )
        # Save one for different schema
        await store.save_recipe_if_unique(
            api_id="api2",
            schema_hash="hash2",
            question="q3",
            recipe={"steps": []},
            tool_name="t3",
        )
        await _drain_background_tasks(store)

        count = await store.invalidate_schema("api1", "hash1")
        assert count == 2

        # Verify gone from memory
        recipes = await store.list_recipes(api_id="api1", schema_hash="hash1")
        assert len(recipes) == 0

        # Verify other schema untouched
        recipes = await store.list_recipes(api_id="api2", schema_hash="hash2")
        assert len(recipes) == 1

    @pytest.mark.asyncio
    async def test_invalidate_removes_from_backend(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)
        store = RecipeStore(max_size=10, backend=backend)

        await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="q1",
            recipe={"steps": []},
            tool_name="t1",
        )
        await _drain_background_tasks(store)

        await store.invalidate_schema("api1", "hash1")

        # Backend should also be empty
        rows = await backend.load_all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_returns_zero(self):
        store = RecipeStore(max_size=10, backend=MemoryBackend())
        count = await store.invalidate_schema("nope", "nada")
        assert count == 0


# ---------------------------------------------------------------------------
# US4: Opt-out to memory-only
# ---------------------------------------------------------------------------


class TestRecipeStoreOptOut:
    """RECIPE_PERSISTENCE=memory creates MemoryBackend, no disk files."""

    @pytest.mark.asyncio
    async def test_memory_backend_no_disk(self, tmp_path):
        """With MemoryBackend, no files are created."""
        backend = MemoryBackend()
        store = RecipeStore(max_size=10, backend=backend)

        await store.save_recipe_if_unique(
            api_id="api1",
            schema_hash="hash1",
            question="test",
            recipe={"steps": []},
            tool_name="test_tool",
        )
        await _drain_background_tasks(store)

        # tmp_path should have no new files
        files = list(tmp_path.iterdir())
        assert len(files) == 0

    def test_factory_returns_memory_for_memory_config(self):
        from api_agent.recipe.backend import create_backend_from_config

        class FakeSettings:
            RECIPE_PERSISTENCE = "memory"
            RECIPE_SQLITE_PATH = ""
            RECIPE_CACHE_SIZE = 64

        backend = create_backend_from_config(FakeSettings())
        assert isinstance(backend, MemoryBackend)
