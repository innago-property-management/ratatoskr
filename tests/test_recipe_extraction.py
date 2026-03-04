"""Tests for recipe extraction and deduplication."""

import pytest

from api_agent.recipe.common import maybe_extract_and_save_recipe
from api_agent.recipe.store import RecipeStore, sha256_hex


@pytest.mark.asyncio
async def test_skip_duplicate_recipe(monkeypatch):
    store = RecipeStore(max_size=10)
    monkeypatch.setattr("api_agent.recipe.common.RECIPE_STORE", store)
    monkeypatch.setattr("api_agent.recipe.common.settings.ENABLE_RECIPES", True)

    raw_schema = '{"schema":"ok"}'
    api_id = "graphql:https://api.example.com/graphql"
    schema_hash = sha256_hex(raw_schema)
    recipe = {
        "tool_name": "list_users",
        "params": {},
        "steps": [
            {
                "kind": "graphql",
                "name": "users",
                "query_template": "{ users { id } }",
            }
        ],
        "sql_steps": [],
    }
    await store.save_recipe(
        api_id=api_id,
        schema_hash=schema_hash,
        question="List users",
        recipe=recipe,
        tool_name=recipe["tool_name"],
    )

    async def fake_extract_recipe(**_kwargs):
        return dict(recipe)

    monkeypatch.setattr("api_agent.recipe.common.extract_recipe", fake_extract_recipe)

    called = False
    original_save = store.save_recipe

    def save_wrapper(*args, **kwargs):
        nonlocal called
        called = True
        return original_save(*args, **kwargs)

    monkeypatch.setattr(store, "save_recipe", save_wrapper)

    await maybe_extract_and_save_recipe(
        api_type="graphql",
        api_id=api_id,
        question="List users",
        steps=[{"kind": "graphql", "name": "users", "query": "{ users { id } }"}],
        sql_steps=[],
        raw_schema=raw_schema,
    )

    assert called is False
    assert len(await store.list_recipes(api_id=api_id, schema_hash=schema_hash)) == 1


@pytest.mark.asyncio
async def test_deduplicate_tool_name_on_collision(monkeypatch):
    store = RecipeStore(max_size=10)
    monkeypatch.setattr("api_agent.recipe.common.RECIPE_STORE", store)
    monkeypatch.setattr("api_agent.recipe.common.settings.ENABLE_RECIPES", True)

    raw_schema = '{"schema":"ok"}'
    api_id = "graphql:https://api.example.com/graphql"
    schema_hash = sha256_hex(raw_schema)

    recipe_existing = {
        "tool_name": "list_users",
        "params": {},
        "steps": [
            {
                "kind": "graphql",
                "name": "users",
                "query_template": "{ users { id } }",
            }
        ],
        "sql_steps": [],
    }
    await store.save_recipe(
        api_id=api_id,
        schema_hash=schema_hash,
        question="List users",
        recipe=recipe_existing,
        tool_name=recipe_existing["tool_name"],
    )

    recipe_new = {
        "tool_name": "list_users",
        "params": {},
        "steps": [
            {
                "kind": "graphql",
                "name": "users",
                "query_template": "{ users { name } }",
            }
        ],
        "sql_steps": [],
    }

    async def fake_extract_recipe(**_kwargs):
        return dict(recipe_new)

    monkeypatch.setattr("api_agent.recipe.common.extract_recipe", fake_extract_recipe)

    await maybe_extract_and_save_recipe(
        api_type="graphql",
        api_id=api_id,
        question="List users by name",
        steps=[{"kind": "graphql", "name": "users", "query": "{ users { name } }"}],
        sql_steps=[],
        raw_schema=raw_schema,
    )

    tool_names = {
        r["tool_name"] for r in await store.list_recipes(api_id=api_id, schema_hash=schema_hash)
    }
    assert "list_users" in tool_names
    assert "list_users_2" in tool_names
