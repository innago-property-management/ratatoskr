"""Tests for RecipeBackend ABC and MemoryBackend (T004)."""

from __future__ import annotations

import pytest

from api_agent.recipe.backend import MemoryBackend, RecipeBackend


class TestMemoryBackend:
    """MemoryBackend is a no-op — all methods return empty/zero."""

    @pytest.mark.asyncio
    async def test_load_all_returns_empty_list(self):
        backend = MemoryBackend()
        result = await backend.load_all()
        assert result == []

    @pytest.mark.asyncio
    async def test_save_is_noop(self):
        backend = MemoryBackend()
        # Should not raise
        await backend.save("api1", "hash1", "r_abc", {"steps": []}, 1.0, 2.0)

    @pytest.mark.asyncio
    async def test_delete_by_schema_returns_zero(self):
        backend = MemoryBackend()
        result = await backend.delete_by_schema("api1", "hash1")
        assert result == 0

    def test_is_subclass_of_recipe_backend(self):
        assert issubclass(MemoryBackend, RecipeBackend)

    def test_instance_check(self):
        backend = MemoryBackend()
        assert isinstance(backend, RecipeBackend)
