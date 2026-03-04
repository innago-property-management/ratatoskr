"""Tests for recipe store fixes: async lock, deduplicate guard, docstrings."""

import asyncio

import pytest

from api_agent.recipe.common import deduplicate_tool_name
from api_agent.recipe.store import RecipeStore, render_sql_safe

# Default max_attempts for deduplicate_tool_name
_DEFAULT_MAX_ATTEMPTS = 100


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
        """Multiple concurrent async operations should not corrupt state.

        Note: asyncio.gather in a single event loop tests interleaving at
        await points, not true OS-level parallelism. This verifies that
        the asyncio.Lock correctly serializes access.
        """
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


# --- save_recipe_if_unique (atomic check-and-insert) ---


class TestSaveRecipeIfUnique:
    """save_recipe_if_unique should atomically check duplicates and save."""

    @pytest.mark.asyncio
    async def test_saves_when_no_duplicate(self):
        """Should save and return recipe_id when no equivalent recipe exists."""
        store = RecipeStore(max_size=10)
        recipe = {"params": {}, "steps": [{"kind": "graphql", "name": "q1"}], "sql_steps": []}
        rid = await store.save_recipe_if_unique(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe=recipe,
            tool_name="my_tool",
            equivalence_checker=lambda existing, candidate: existing == candidate,
        )
        assert rid is not None
        assert rid.startswith("r_")
        stored = await store.get_recipe(rid)
        assert stored is not None

    @pytest.mark.asyncio
    async def test_rejects_duplicate(self):
        """Should return None when an equivalent recipe already exists."""
        store = RecipeStore(max_size=10)
        recipe = {"params": {}, "steps": [{"kind": "graphql", "name": "q1"}], "sql_steps": []}

        # Save the first one normally
        first_id = await store.save_recipe(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe=recipe,
            tool_name="my_tool",
        )
        assert first_id is not None

        # Try to save duplicate via atomic method
        dup_id = await store.save_recipe_if_unique(
            api_id="test",
            schema_hash="s",
            question="q",
            recipe=recipe,
            tool_name="my_tool_2",
            equivalence_checker=lambda existing, candidate: existing == candidate,
        )
        assert dup_id is None

        # Only one recipe should exist
        recipes = await store.list_recipes(api_id="test", schema_hash="s")
        assert len(recipes) == 1

    @pytest.mark.asyncio
    async def test_saves_when_checker_returns_false(self):
        """Should save when checker says recipes are NOT equivalent."""
        store = RecipeStore(max_size=10)
        recipe_a = {"params": {}, "steps": [{"kind": "graphql", "name": "q1"}], "sql_steps": []}
        recipe_b = {"params": {}, "steps": [{"kind": "graphql", "name": "q2"}], "sql_steps": []}

        await store.save_recipe(
            api_id="test", schema_hash="s", question="q", recipe=recipe_a, tool_name="tool_a"
        )

        rid = await store.save_recipe_if_unique(
            api_id="test",
            schema_hash="s",
            question="q2",
            recipe=recipe_b,
            tool_name="tool_b",
            equivalence_checker=lambda existing, candidate: existing == candidate,
        )
        assert rid is not None

        recipes = await store.list_recipes(api_id="test", schema_hash="s")
        assert len(recipes) == 2

    @pytest.mark.asyncio
    async def test_saves_without_checker(self):
        """Should always save when no equivalence_checker is provided."""
        store = RecipeStore(max_size=10)
        recipe = {"params": {}, "steps": [], "sql_steps": []}

        # Save two identical recipes — no checker means no dedup
        rid1 = await store.save_recipe_if_unique(
            api_id="test", schema_hash="s", question="q", recipe=recipe, tool_name="t1"
        )
        rid2 = await store.save_recipe_if_unique(
            api_id="test", schema_hash="s", question="q", recipe=recipe, tool_name="t2"
        )
        assert rid1 is not None
        assert rid2 is not None
        assert rid1 != rid2

    @pytest.mark.asyncio
    async def test_only_checks_same_api_key(self):
        """Equivalence check only compares recipes under the same api_id+schema_hash."""
        store = RecipeStore(max_size=10)
        recipe = {"params": {}, "steps": [{"kind": "graphql", "name": "q1"}], "sql_steps": []}

        # Save under api_id="a"
        await store.save_recipe(
            api_id="a", schema_hash="s", question="q", recipe=recipe, tool_name="t1"
        )

        # save_recipe_if_unique under api_id="b" — should not see the "a" recipe
        rid = await store.save_recipe_if_unique(
            api_id="b",
            schema_hash="s",
            question="q",
            recipe=recipe,
            tool_name="t2",
            equivalence_checker=lambda existing, candidate: existing == candidate,
        )
        assert rid is not None


# --- deduplicate_tool_name max-attempts guard ---


class TestDeduplicateToolNameMaxAttempts:
    """deduplicate_tool_name should have a max-attempts guard."""

    def test_raises_on_exhausted_names(self):
        """Should raise ValueError when all candidate names are taken."""
        # Fill seen_names with base + all suffixes up to default max
        seen = {"recipe"} | {f"recipe_{i}" for i in range(2, 2 + _DEFAULT_MAX_ATTEMPTS)}
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

    def test_string_values_not_auto_quoted(self):
        """String params are NOT auto-quoted — template must supply surrounding quotes.

        This documents the contract: render_sql_safe escapes the value but
        does not wrap it in SQL quotes. Templates should use '{{param}}' for
        strings (e.g., WHERE name = '{{name}}'), not bare {{param}}.
        """
        result = render_sql_safe("WHERE name = {{n}}", {"n": "Alice"})
        # No surrounding quotes — the value is inserted bare
        assert "WHERE name = Alice" == result


# --- RecipeStore single-tenant docstring ---


class TestRecipeStoreSingleTenantDoc:
    """RecipeStore should document its single-tenant assumption."""

    def test_has_single_tenant_docstring(self):
        """RecipeStore class docstring should document single-tenant design."""
        doc = RecipeStore.__doc__
        assert doc is not None
        assert "single-tenant" in doc.lower() or "single tenant" in doc.lower()
