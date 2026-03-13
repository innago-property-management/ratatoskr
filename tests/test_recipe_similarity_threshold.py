"""Tests for RecipeStore minimum similarity threshold (M10)."""

from __future__ import annotations

import pytest

from api_agent.recipe.store import RecipeStore, _similarity


@pytest.fixture
def store() -> RecipeStore:
    return RecipeStore(max_size=64)


async def _add_recipe(store: RecipeStore, question: str, tool_name: str = "t") -> str:
    return await store.save_recipe(
        api_id="test-api",
        schema_hash="abc123",
        question=question,
        recipe={"steps": [], "params": {}},
        tool_name=tool_name,
    )


# -- Threshold filtering --


@pytest.mark.asyncio
async def test_similar_recipe_returned(store: RecipeStore) -> None:
    """Recipes with score >= threshold are returned."""
    await _add_recipe(store, "list all users")
    results = await store.suggest_recipes(
        api_id="test-api", schema_hash="abc123", question="list users"
    )
    assert len(results) == 1
    assert results[0]["score"] > 0


@pytest.mark.asyncio
async def test_unrelated_recipe_filtered(store: RecipeStore) -> None:
    """Completely unrelated queries should be filtered out."""
    await _add_recipe(store, "list all users")
    results = await store.suggest_recipes(
        api_id="test-api", schema_hash="abc123", question="zzz xylophone quantum"
    )
    assert len(results) == 0


@pytest.mark.asyncio
async def test_exact_match_returned(store: RecipeStore) -> None:
    """Exact match always passes threshold."""
    await _add_recipe(store, "get order details")
    results = await store.suggest_recipes(
        api_id="test-api", schema_hash="abc123", question="get order details"
    )
    assert len(results) == 1
    assert results[0]["score"] == 1.0


# -- Configurable threshold --


@pytest.mark.asyncio
async def test_threshold_configurable_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting RECIPE_MIN_SIMILARITY to 100 filters out non-exact matches."""
    from api_agent import config

    monkeypatch.setattr(config.settings, "RECIPE_MIN_SIMILARITY", 100)

    s = RecipeStore(max_size=64)
    await _add_recipe(s, "list all users")

    # Partial match should be filtered at threshold=100
    results = await s.suggest_recipes(
        api_id="test-api", schema_hash="abc123", question="list users"
    )
    assert len(results) == 0

    # Exact match still passes
    results = await s.suggest_recipes(
        api_id="test-api", schema_hash="abc123", question="list all users"
    )
    assert len(results) == 1


@pytest.mark.asyncio
async def test_threshold_zero_returns_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting RECIPE_MIN_SIMILARITY to 0 returns any non-zero match."""
    from api_agent import config

    monkeypatch.setattr(config.settings, "RECIPE_MIN_SIMILARITY", 0)

    s = RecipeStore(max_size=64)
    await _add_recipe(s, "list all users")

    results = await s.suggest_recipes(
        api_id="test-api", schema_hash="abc123", question="list"
    )
    assert len(results) >= 1


# -- _similarity sanity check --


def test_similarity_unrelated_strings_low() -> None:
    """Unrelated strings should have similarity well below default threshold of 0.30."""
    score = _similarity("list all active users", "zzz xylophone quantum")
    assert score < 0.30


def test_similarity_related_strings_above_threshold() -> None:
    """Related strings should score above the default threshold."""
    score = _similarity("list all users", "list users")
    assert score >= 0.30
