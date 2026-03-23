"""Tests for SQLiteBackend (T005)."""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from api_agent.recipe.backend import MemoryBackend, RecipeBackend, create_backend_from_config


class TestSQLiteBackendRoundTrip:
    """Save + load_all round-trip verifies all fields survive."""

    @pytest.mark.asyncio
    async def test_save_and_load_roundtrip(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        recipe = {"steps": [{"type": "graphql", "query": "{ users }"}], "params": {}}
        await backend.save("api1", "hash1", "r_abc123", recipe, 1000.0, 2000.0)

        rows = await backend.load_all()
        assert len(rows) == 1
        api_id, schema_hash, recipe_id, recipe_dict, created_at, last_used_at = rows[0]
        assert api_id == "api1"
        assert schema_hash == "hash1"
        assert recipe_id == "r_abc123"
        assert recipe_dict == recipe
        assert created_at == 1000.0
        assert last_used_at == 2000.0

    @pytest.mark.asyncio
    async def test_save_multiple_and_load(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        await backend.save("api1", "hash1", "r_001", {"steps": []}, 1.0, 1.0)
        await backend.save("api1", "hash1", "r_002", {"steps": [1]}, 2.0, 2.0)
        await backend.save("api2", "hash2", "r_003", {"steps": [2]}, 3.0, 3.0)

        rows = await backend.load_all()
        assert len(rows) == 3
        ids = {r[2] for r in rows}
        assert ids == {"r_001", "r_002", "r_003"}

    @pytest.mark.asyncio
    async def test_upsert_replaces_existing(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        await backend.save("api1", "hash1", "r_001", {"v": 1}, 1.0, 1.0)
        await backend.save("api1", "hash1", "r_001", {"v": 2}, 1.0, 5.0)

        rows = await backend.load_all()
        assert len(rows) == 1
        assert rows[0][3] == {"v": 2}
        assert rows[0][5] == 5.0


class TestSQLiteBackendDelete:
    """delete_by_schema removes correct entries only."""

    @pytest.mark.asyncio
    async def test_delete_removes_matching_entries(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        await backend.save("api1", "hash1", "r_001", {}, 1.0, 1.0)
        await backend.save("api1", "hash1", "r_002", {}, 2.0, 2.0)
        await backend.save("api2", "hash2", "r_003", {}, 3.0, 3.0)

        count = await backend.delete_by_schema("api1", "hash1")
        assert count == 2

        rows = await backend.load_all()
        assert len(rows) == 1
        assert rows[0][2] == "r_003"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_zero(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        count = await backend.delete_by_schema("nope", "nada")
        assert count == 0


class TestSQLiteBackendCorruptedJSON:
    """Handles corrupted/malformed JSON in DB gracefully."""

    @pytest.mark.asyncio
    async def test_skips_corrupted_json(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        # Save a valid recipe
        await backend.save("api1", "hash1", "r_good", {"valid": True}, 1.0, 1.0)

        # Manually insert corrupted JSON
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO recipes (api_id, schema_hash, recipe_id, recipe_json, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("api1", "hash1", "r_bad", "{not valid json!!!", 2.0, 2.0),
        )
        conn.commit()
        conn.close()

        rows = await backend.load_all()
        # Should skip the corrupted one, load the valid one
        assert len(rows) == 1
        assert rows[0][2] == "r_good"


class TestSQLiteBackendWALMode:
    """WAL mode is enabled."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        SQLiteBackend(db_path=db_path)

        # After init, verify WAL mode
        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"


class TestSQLiteBackendConcurrentWrites:
    """Concurrent writes via asyncio.gather are safe."""

    @pytest.mark.asyncio
    async def test_concurrent_saves(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path)

        tasks = [
            backend.save("api1", "hash1", f"r_{i:03d}", {"i": i}, float(i), float(i))
            for i in range(20)
        ]
        await asyncio.gather(*tasks)

        rows = await backend.load_all()
        assert len(rows) == 20


class TestSQLiteBackendMaxRecipes:
    """load_all respects max_recipes cap."""

    @pytest.mark.asyncio
    async def test_caps_at_max_recipes(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        db_path = str(tmp_path / "test.db")
        backend = SQLiteBackend(db_path=db_path, max_recipes=5)

        for i in range(10):
            await backend.save("api1", "hash1", f"r_{i:03d}", {"i": i}, float(i), float(i))

        rows = await backend.load_all()
        assert len(rows) == 5
        # Should be ordered by last_used_at DESC, so most recent first
        last_used_values = [r[5] for r in rows]
        assert last_used_values == sorted(last_used_values, reverse=True)


class TestSQLiteBackendPathResolution:
    """Path resolution and directory creation."""

    @pytest.mark.asyncio
    async def test_creates_parent_directory(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        nested = tmp_path / "deep" / "nested" / "dir"
        db_path = str(nested / "recipes.db")
        backend = SQLiteBackend(db_path=db_path)

        # Should have created the directory
        assert nested.exists()
        # Should work
        await backend.save("api1", "hash1", "r_001", {}, 1.0, 1.0)
        rows = await backend.load_all()
        assert len(rows) == 1

    def test_resolve_path_explicit(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        explicit = str(tmp_path / "my.db")
        assert SQLiteBackend.resolve_path(explicit) == explicit

    def test_resolve_path_xdg_cache(self, monkeypatch, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        xdg = str(tmp_path / "xdg_cache")
        monkeypatch.setenv("XDG_CACHE_HOME", xdg)
        monkeypatch.delenv("API_AGENT_RECIPE_SQLITE_PATH", raising=False)

        result = SQLiteBackend.resolve_path("")
        assert result == os.path.join(xdg, "ratatoskr", "recipes.db")

    def test_resolve_path_home_fallback(self, monkeypatch, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        home = str(tmp_path / "fakehome")
        os.makedirs(home, exist_ok=True)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("API_AGENT_RECIPE_SQLITE_PATH", raising=False)

        result = SQLiteBackend.resolve_path("")
        assert result == os.path.join(home, ".cache", "ratatoskr", "recipes.db")


class TestSQLiteBackendUnwritable:
    """Graceful handling of unwritable path."""

    def test_unwritable_path_raises(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        # Create a file where we'd want a directory
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a directory")
        bad_path = str(blocker / "subdir" / "recipes.db")

        with pytest.raises(Exception):
            SQLiteBackend(db_path=bad_path)


class TestSQLiteBackendIsSubclass:
    """SQLiteBackend is a RecipeBackend."""

    def test_is_subclass(self):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        assert issubclass(SQLiteBackend, RecipeBackend)


class TestCreateBackendFromConfig:
    """Factory function creates the right backend."""

    def test_memory_config_returns_memory_backend(self):
        class FakeSettings:
            RECIPE_PERSISTENCE = "memory"
            RECIPE_SQLITE_PATH = ""
            RECIPE_CACHE_SIZE = 64

        backend = create_backend_from_config(FakeSettings())
        assert isinstance(backend, MemoryBackend)

    def test_sqlite_config_returns_sqlite_backend(self, tmp_path):
        from api_agent.recipe.sqlite_backend import SQLiteBackend

        class FakeSettings:
            RECIPE_PERSISTENCE = "sqlite"
            RECIPE_SQLITE_PATH = str(tmp_path / "test.db")
            RECIPE_CACHE_SIZE = 64

        backend = create_backend_from_config(FakeSettings())
        assert isinstance(backend, SQLiteBackend)

    def test_sqlite_failure_falls_back_to_memory(self, tmp_path):
        """If SQLiteBackend init fails, falls back to MemoryBackend."""

        blocker = tmp_path / "blocker"
        blocker.write_text("file")

        class FakeSettings:
            RECIPE_PERSISTENCE = "sqlite"
            RECIPE_SQLITE_PATH = str(blocker / "sub" / "recipes.db")
            RECIPE_CACHE_SIZE = 64

        backend = create_backend_from_config(FakeSettings())
        assert isinstance(backend, MemoryBackend)
