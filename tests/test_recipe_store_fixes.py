"""Tests for recipe store fixes: async lock, deduplicate guard, docstrings."""

import asyncio

import pytest

from api_agent.recipe.common import deduplicate_tool_name
from api_agent.recipe.store import RecipeStore, render_sql_safe


# --- asyncio.Lock tests ---


class TestRecipeStoreAsyncLock:
    """RecipeStore should use asyncio.Lock, not threading.Lock."""

    def test_lock_is_asyncio(self):
        """RecipeStore._lock should be an asyncio.Lock instance."""
        store = RecipeStore(max_size=10)
        assert isinstance(store._lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_save_recipe_is_async(self):
        """save_recipe should be awaitable."""
        store = RecipeStore(max_size=10)
        recipe_id = await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe={"params": {}, "steps": [], "sql_steps": []},
            tool_name="test",
        )
        assert recipe_id.startswith("r_")

    @pytest.mark.asyncio
    async def test_get_recipe_is_async(self):
        """get_recipe should be awaitable."""
        store = RecipeStore(max_size=10)
        recipe_id = await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe={"params": {}, "steps": [], "sql_steps": []},
            tool_name="test",
        )
        result = await store.get_recipe(recipe_id)
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_recipe_meta_is_async(self):
        """get_recipe_meta should be awaitable."""
        store = RecipeStore(max_size=10)
        recipe_id = await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe={"params": {}, "steps": [], "sql_steps": []},
            tool_name="test",
        )
        meta = await store.get_recipe_meta(recipe_id)
        assert meta is not None
        assert meta["api_id"] == "test"

    @pytest.mark.asyncio
    async def test_suggest_recipes_is_async(self):
        """suggest_recipes should be awaitable."""
        store = RecipeStore(max_size=10)
        await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="find hotels",
            recipe={"params": {}, "steps": [], "sql_steps": []},
            tool_name="find_hotels",
        )
        suggestions = await store.suggest_recipes(
            api_id="test", schema_hash="s", question="find hotels", k=3
        )
        assert len(suggestions) > 0

    @pytest.mark.asyncio
    async def test_list_recipes_is_async(self):
        """list_recipes should be awaitable."""
        store = RecipeStore(max_size=10)
        await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe={"params": {}, "steps": [], "sql_steps": []},
            tool_name="test",
        )
        recipes = await store.list_recipes(api_id="test", schema_hash="s")
        assert len(recipes) == 1

    @pytest.mark.asyncio
    async def test_find_recipe_by_tool_slug_is_async(self):
        """find_recipe_by_tool_slug should be awaitable."""
        store = RecipeStore(max_size=10)
        await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe={"params": {}, "steps": [], "sql_steps": []},
            tool_name="my_tool",
        )
        result = await store.find_recipe_by_tool_slug(
            api_id="test", schema_hash="s", tool_slug="my_tool"
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_concurrent_access_safe(self):
        """Multiple concurrent async operations should not corrupt state."""
        store = RecipeStore(max_size=100)

        async def save_and_get(i: int):
            rid = await store.save_recipe(
                api_id="test",
                schema_hash="s",
                question=f"query {i}",
                recipe={"params": {}, "steps": [], "sql_steps": []},
                tool_name=f"tool_{i}",
            )
            result = await store.get_recipe(rid)
            assert result is not None
            return rid

        ids = await asyncio.gather(*[save_and_get(i) for i in range(20)])
        assert len(set(ids)) == 20


# --- deduplicate_tool_name max-attempts guard ---


class TestDeduplicateToolNameMaxAttempts:
    """deduplicate_tool_name should have a max-attempts guard."""

    def test_raises_on_exhausted_names(self):
        """Should raise ValueError when all candidate names are taken."""
        # Fill seen_names with base + all suffixes up to max
        seen = {"recipe"} | {f"recipe_{i}" for i in range(2, 102)}
        with pytest.raises(ValueError, match="max attempts"):
            deduplicate_tool_name("recipe", seen, max_len=40)

    def test_succeeds_within_limit(self):
        """Should succeed when a free name exists within max attempts."""
        seen = {"recipe"} | {f"recipe_{i}" for i in range(2, 50)}
        name = deduplicate_tool_name("recipe", seen, max_len=40)
        assert name == "recipe_50"

    def test_original_behavior_preserved(self):
        """Normal deduplication still works as before."""
        seen: set[str] = set()
        assert deduplicate_tool_name("get_users", seen) == "get_users"
        assert deduplicate_tool_name("get_users", seen) == "get_users_2"
        assert deduplicate_tool_name("get_users", seen) == "get_users_3"


# --- render_sql_safe docstring ---


class TestRenderSqlSafeDocumentation:
    """render_sql_safe should have clear documentation of its limitations."""

    def test_has_limitations_docstring(self):
        """render_sql_safe docstring should document known limitations."""
        doc = render_sql_safe.__doc__
        assert doc is not None
        assert "limitation" in doc.lower() or "not" in doc.lower()

    def test_escapes_single_quotes(self):
        """Basic SQL injection via single quotes is prevented."""
        result = render_sql_safe("WHERE name = '{{n}}'", {"n": "O'Brien"})
        assert "''" in result
        assert "O''Brien" in result

    def test_strips_semicolons(self):
        """Multi-statement injection via semicolons is prevented."""
        result = render_sql_safe("WHERE id = {{x}}", {"x": "1; DROP TABLE users"})
        assert ";" not in result

    def test_strips_comment_markers(self):
        """SQL comment injection markers are stripped."""
        result = render_sql_safe("WHERE id = {{x}}", {"x": "1 -- comment"})
        assert "--" not in result
        result2 = render_sql_safe("WHERE id = {{x}}", {"x": "1 /* block */"})
        assert "/*" not in result2
        assert "*/" not in result2


# --- RecipeStore single-tenant docstring ---


class TestRecipeStoreSingleTenantDoc:
    """RecipeStore should document its single-tenant assumption."""

    def test_has_single_tenant_docstring(self):
        """RecipeStore class docstring should document single-tenant design."""
        doc = RecipeStore.__doc__
        assert doc is not None
        assert "single-tenant" in doc.lower() or "single tenant" in doc.lower()
