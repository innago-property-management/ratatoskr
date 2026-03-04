"""Unit tests for recipe store utilities."""

import pytest

from api_agent.recipe import (
    RECIPE_STORE,
    RecipeStore,
    build_recipe_context,
    get_example_values,
    render_param_refs,
    render_text_template,
)
from api_agent.recipe.extractor import _find_used_params, _validate_equivalence
from api_agent.recipe.store import sha256_hex


def test_render_text_template_basic():
    assert render_text_template("limit {{n}}", {"n": 10}) == "limit 10"
    assert render_text_template("active={{flag}}", {"flag": True}) == "active=true"
    assert render_text_template("v={{x}}", {"x": None}) == "v=null"


def test_sha256_hex_normalizes_json():
    a = '{"b": 1, "a": 2}'
    b = '{"a": 2, "b": 1}'
    assert sha256_hex(a) == sha256_hex(b)


def test_render_param_refs_nested():
    obj = {"a": {"$param": "x"}, "b": [{"$param": "y"}], "c": 3}
    out = render_param_refs(obj, {"x": 1, "y": "foo"})
    assert out == {"a": 1, "b": ["foo"], "c": 3}


def test_get_example_values():
    spec = {"limit": {"type": "int", "default": 10}, "q": {"type": "str"}}
    params = get_example_values(spec, {"q": "abc"})
    assert params == {"limit": 10, "q": "abc"}


def test_get_example_values_none_value():
    """None defaults are included (not skipped)."""
    spec = {"id": {"type": "int", "default": None}, "limit": {"type": "int", "default": 10}}
    params = get_example_values(spec, {})
    assert params == {"id": None, "limit": 10}
    # Provided value overrides None default
    params2 = get_example_values(spec, {"id": 42})
    assert params2 == {"id": 42, "limit": 10}


@pytest.mark.asyncio
async def test_recipe_store_preserves_defaults():
    """Defaults are preserved as-is (no sensitivity filtering)."""
    store = RecipeStore(max_size=10)
    recipe = {
        "params": {
            "user_id": {"type": "str", "default": "123e4567-e89b-12d3-a456-426614174000"},
            "limit": {"type": "int", "default": 10},
        },
        "steps": [],
        "sql_steps": [],
    }
    recipe_id = await store.save_recipe(
        api_id="rest:https://spec|https://api",
        schema_hash="s",
        question="q",
        recipe=recipe,
        tool_name="test_recipe",
    )
    saved = await store.get_recipe(recipe_id)
    assert saved is not None
    # Defaults preserved exactly as provided
    assert saved["params"]["user_id"]["default"] == "123e4567-e89b-12d3-a456-426614174000"
    assert saved["params"]["limit"]["default"] == 10


@pytest.mark.asyncio
async def test_recipe_store_scoring_prefers_closer_match():
    store = RecipeStore(max_size=10)
    r1 = {"params": {}, "steps": [], "sql_steps": []}
    r2 = {"params": {}, "steps": [], "sql_steps": []}
    id1 = await store.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="top hotels by rating",
        recipe=r1,
        tool_name="top_hotels",
    )
    id2 = await store.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="list users by age",
        recipe=r2,
        tool_name="list_users",
    )
    assert id1 and id2
    suggestions = await store.suggest_recipes(
        api_id="rest:a|b", schema_hash="s", question="best hotels", k=2
    )
    assert suggestions
    assert suggestions[0]["recipe_id"] == id1


@pytest.mark.asyncio
async def test_recipe_store_scoring_handles_token_order():
    store = RecipeStore(max_size=10)
    r1 = {"params": {}, "steps": [], "sql_steps": []}
    r2 = {"params": {}, "steps": [], "sql_steps": []}
    id1 = await store.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="find hotels in nyc",
        recipe=r1,
        tool_name="find_hotels_in_nyc",
    )
    _id2 = await store.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="find users in nyc",
        recipe=r2,
        tool_name="find_users_in_nyc",
    )
    suggestions = await store.suggest_recipes(
        api_id="rest:a|b", schema_hash="s", question="nyc hotels find", k=2
    )
    assert suggestions
    assert suggestions[0]["recipe_id"] == id1


def test_render_text_template_missing_param_raises():
    import pytest

    with pytest.raises(KeyError):
        render_text_template("limit {{n}}", {})


def test_validate_recipe_params_requires_all_params():
    """All declared params are required (defaults are examples, not fallbacks)."""
    from api_agent.recipe.common import validate_recipe_params

    params_spec = {
        "manager_name": {"type": "str", "default": "Alice Smith"},
        "manager_email": {"type": "str", "default": "alice.smith@example.com"},
    }
    params, error = validate_recipe_params(params_spec, {"manager_name": "Bob Jones"})
    assert params is None
    assert "missing required param: manager_email" in error


def test_validate_recipe_params_rejects_extra():
    """Extra params are rejected even when spec is non-empty."""
    from api_agent.recipe.common import validate_recipe_params

    params_spec = {"limit": {"type": "int", "default": 10}}
    params, error = validate_recipe_params(params_spec, {"limit": 5, "extra": "bad"})
    assert params is None
    assert "unexpected params: extra" in error


def test_global_recipe_store_available():
    # Basic smoke test to ensure singleton is constructed
    assert RECIPE_STORE is not None


# --- Extractor tests ---


def test_find_used_params_graphql():
    recipe = {
        "params": {"limit": {"default": 10}},
        "steps": [{"kind": "graphql", "query_template": "{ users(limit: {{limit}}) { id } }"}],
        "sql_steps": [],
    }
    assert _find_used_params(recipe, "graphql") == {"limit"}


def test_find_used_params_sql():
    recipe = {
        "params": {"prefix": {"default": "A"}},
        "steps": [],
        "sql_steps": ["SELECT * FROM t WHERE name ILIKE '{{prefix}}%'"],
    }
    assert _find_used_params(recipe, "graphql") == {"prefix"}


def test_find_used_params_rest_param_refs():
    recipe = {
        "params": {"id": {"default": "123"}},
        "steps": [
            {
                "kind": "rest",
                "method": "GET",
                "path": "/users/{id}",
                "path_params": {"id": {"$param": "id"}},
                "query_params": {},
                "body": {},
            }
        ],
        "sql_steps": [],
    }
    assert _find_used_params(recipe, "rest") == {"id"}


def test_find_used_params_multiple():
    recipe = {
        "params": {"limit": {}, "prefix": {}},
        "steps": [{"kind": "graphql", "query_template": "{ users(limit: {{limit}}) }"}],
        "sql_steps": ["SELECT * WHERE name ILIKE '{{prefix}}%'"],
    }
    assert _find_used_params(recipe, "graphql") == {"limit", "prefix"}


def test_find_used_params_none_used():
    recipe = {
        "params": {"unused": {"default": "x"}},
        "steps": [{"kind": "graphql", "query_template": "{ users { id } }"}],
        "sql_steps": ["SELECT * FROM t"],
    }
    assert _find_used_params(recipe, "graphql") == set()


def test_validate_equivalence_graphql_valid():
    original_steps = [{"kind": "graphql", "query": "{ users(limit: 10) { id } }", "name": "data"}]
    original_sql = ["SELECT * FROM data WHERE active = true"]
    recipe = {
        "params": {"limit": {"type": "int", "default": 10}},
        "steps": [
            {
                "kind": "graphql",
                "query_template": "{ users(limit: {{limit}}) { id } }",
                "name": "data",
            }
        ],
        "sql_steps": ["SELECT * FROM data WHERE active = true"],
    }
    assert _validate_equivalence(
        api_type="graphql", original_steps=original_steps, original_sql=original_sql, recipe=recipe
    )


def test_validate_equivalence_graphql_mismatch():
    original_steps = [{"kind": "graphql", "query": "{ users(limit: 10) { id } }", "name": "data"}]
    original_sql = []
    recipe = {
        "params": {"limit": {"type": "int", "default": 5}},  # Wrong default
        "steps": [
            {
                "kind": "graphql",
                "query_template": "{ users(limit: {{limit}}) { id } }",
                "name": "data",
            }
        ],
        "sql_steps": [],
    }
    assert not _validate_equivalence(
        api_type="graphql", original_steps=original_steps, original_sql=original_sql, recipe=recipe
    )


def test_validate_equivalence_sql_parameterized():
    original_steps = [{"kind": "graphql", "query": "{ teams { name } }", "name": "data"}]
    original_sql = ["SELECT * FROM data WHERE name ILIKE 'A%'"]
    recipe = {
        "params": {"prefix": {"type": "str", "default": "A"}},
        "steps": [{"kind": "graphql", "query_template": "{ teams { name } }", "name": "data"}],
        "sql_steps": ["SELECT * FROM data WHERE name ILIKE '{{prefix}}%'"],
    }
    assert _validate_equivalence(
        api_type="graphql", original_steps=original_steps, original_sql=original_sql, recipe=recipe
    )


def test_validate_equivalence_rest_valid():
    original_steps = [
        {
            "kind": "rest",
            "method": "GET",
            "path": "/users",
            "name": "data",
            "path_params": {},
            "query_params": {"limit": 10},
            "body": {},
        }
    ]
    original_sql = []
    recipe = {
        "params": {"limit": {"type": "int", "default": 10}},
        "steps": [
            {
                "kind": "rest",
                "method": "GET",
                "path": "/users",
                "name": "data",
                "path_params": {},
                "query_params": {"limit": {"$param": "limit"}},
                "body": {},
            }
        ],
        "sql_steps": [],
    }
    assert _validate_equivalence(
        api_type="rest", original_steps=original_steps, original_sql=original_sql, recipe=recipe
    )


def test_validate_equivalence_length_mismatch():
    original_steps = [{"kind": "graphql", "query": "{ a }", "name": "a"}]
    original_sql = []
    recipe = {
        "params": {},
        "steps": [],  # Empty - length mismatch
        "sql_steps": [],
    }
    assert not _validate_equivalence(
        api_type="graphql", original_steps=original_steps, original_sql=original_sql, recipe=recipe
    )


@pytest.mark.asyncio
async def test_build_recipe_context_empty():
    """Empty suggestions returns empty string."""
    assert await build_recipe_context([]) == ""


@pytest.mark.asyncio
async def test_build_recipe_context_with_suggestions():
    """Suggestions are formatted correctly for prompt injection."""
    r1 = {"params": {"prefix": {"type": "str", "default": "A"}}, "steps": [], "sql_steps": []}
    r2 = {"params": {}, "steps": [], "sql_steps": []}

    rid1 = await RECIPE_STORE.save_recipe(
        api_id="rest:test|test",
        schema_hash="s",
        question="get users starting with A",
        recipe=r1,
        tool_name="get_users_starting_with_a",
    )
    rid2 = await RECIPE_STORE.save_recipe(
        api_id="rest:test|test",
        schema_hash="s",
        question="list all users",
        recipe=r2,
        tool_name="list_all_users",
    )

    suggestions = [
        {
            "recipe_id": rid1,
            "score": 0.85,
            "question": "get users starting with A",
            "params": {"prefix": {"type": "str", "default": "A"}},
            "tool_name": "get_users_starting_with_a",
        },
        {
            "recipe_id": rid2,
            "score": 0.72,
            "question": "list all users",
            "params": {},
            "tool_name": "list_all_users",
        },
    ]
    result = await build_recipe_context(suggestions)

    assert "<recipes>" in result
    assert "</recipes>" in result
    assert "Score: 0.85" in result
    assert "get users starting with A" in result
    assert "prefix: str = A" in result
    assert "Score: 0.72" in result
    assert "list all users" in result


@pytest.mark.asyncio
async def test_build_recipe_context_no_params():
    """Recipes without params show empty param list."""
    r = {"params": {}, "steps": [], "sql_steps": []}
    rid = await RECIPE_STORE.save_recipe(
        api_id="rest:test|test",
        schema_hash="s",
        question="simple query",
        recipe=r,
        tool_name="simple_query",
    )

    suggestions = [
        {"recipe_id": rid, "score": 0.90, "question": "simple query", "params": {}},
    ]
    result = await build_recipe_context(suggestions)
    # Tool name with no params should have empty signature
    assert "simple_query()" in result or "simple_query\n" in result


@pytest.mark.asyncio
async def test_build_recipe_context_enhanced_format():
    """Enhanced context shows tool names, score hints, step summaries."""
    # Create mock recipe in store
    # Using global RECIPE_STORE
    recipe = {
        "params": {"user_id": {"type": "int", "default": 123}},
        "steps": [{"kind": "rest", "method": "GET", "path": "/users"}],
        "sql_steps": ["SELECT * FROM data WHERE active = true"],
    }
    recipe_id = await RECIPE_STORE.save_recipe(
        api_id="rest:test|test",
        schema_hash="test_hash",
        question="Get user's recent posts",
        recipe=recipe,
        tool_name="get_users_recent_posts",
    )

    suggestions = [
        {
            "recipe_id": recipe_id,
            "score": 0.85,
            "question": "Get user's recent posts",
            "params": {"user_id": {"type": "int", "default": 123}},
        }
    ]

    result = await build_recipe_context(suggestions)

    # Check new format elements
    assert "Available recipe tools" in result
    assert "get_users_recent_posts" in result  # Sanitized tool name
    assert "user_id: int = 123" in result  # Typed param signature
    assert "Score: 0.85" in result
    assert "STRONG MATCH" in result  # Score >= 0.8
    assert "1 API call + 1 SQL step" in result  # Step summary


@pytest.mark.asyncio
async def test_build_recipe_context_score_hints():
    """Test different score interpretation hints."""
    # Using global RECIPE_STORE
    recipe = {"params": {}, "steps": [], "sql_steps": []}

    # High score
    rid1 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="high score query",
        recipe=recipe,
        tool_name="high_score_query",
    )
    # Medium score
    rid2 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="medium score query",
        recipe=recipe,
        tool_name="medium_score_query",
    )
    # Low score
    rid3 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="low score query",
        recipe=recipe,
        tool_name="low_score_query",
    )

    suggestions = [
        {"recipe_id": rid1, "score": 0.92, "question": "high score query", "params": {}},
        {"recipe_id": rid2, "score": 0.68, "question": "medium score query", "params": {}},
        {"recipe_id": rid3, "score": 0.45, "question": "low score query", "params": {}},
    ]

    result = await build_recipe_context(suggestions)

    assert "STRONG MATCH - highly recommended" in result
    assert "Good match - verify params" in result
    assert "Possible match - check alignment" in result


@pytest.mark.asyncio
async def test_build_recipe_context_step_summaries():
    """Test step summary formatting."""
    # Using global RECIPE_STORE

    # API only
    r1 = {"params": {}, "steps": [{"kind": "rest"}], "sql_steps": []}
    # SQL only
    r2 = {"params": {}, "steps": [], "sql_steps": ["SELECT * FROM t"]}
    # Both
    r3 = {
        "params": {},
        "steps": [{"kind": "rest"}, {"kind": "rest"}],
        "sql_steps": ["SQL1", "SQL2"],
    }
    # Neither
    r4 = {"params": {}, "steps": [], "sql_steps": []}

    rid1 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="api only",
        recipe=r1,
        tool_name="api_only",
    )
    rid2 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="sql only",
        recipe=r2,
        tool_name="sql_only",
    )
    rid3 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="both",
        recipe=r3,
        tool_name="both",
    )
    rid4 = await RECIPE_STORE.save_recipe(
        api_id="rest:a|b",
        schema_hash="s",
        question="neither",
        recipe=r4,
        tool_name="neither",
    )

    suggestions = [
        {"recipe_id": rid1, "score": 0.7, "question": "api only", "params": {}},
        {"recipe_id": rid2, "score": 0.7, "question": "sql only", "params": {}},
        {"recipe_id": rid3, "score": 0.7, "question": "both", "params": {}},
        {"recipe_id": rid4, "score": 0.7, "question": "neither", "params": {}},
    ]

    result = await build_recipe_context(suggestions)

    assert "1 API call" in result
    assert "1 SQL step" in result
    assert "2 API calls + 2 SQL steps" in result
    assert "no steps" in result


@pytest.mark.asyncio
async def test_validate_and_prepare_recipe_success():
    """validate_and_prepare_recipe returns recipe and params."""
    from contextvars import ContextVar

    from api_agent.recipe import RECIPE_STORE, validate_and_prepare_recipe

    schema_var: ContextVar[str] = ContextVar("schema")
    schema_var.set('{"type": "test"}')

    recipe = {
        "params": {"limit": {"type": "int", "default": 10}},
        "steps": [{"kind": "graphql", "query_template": "{ users }"}],
        "sql_steps": [],
    }
    rid = await RECIPE_STORE.save_recipe(
        api_id="graphql:test",
        schema_hash="abc",
        question="get users",
        recipe=recipe,
        tool_name="get_users",
    )

    result, params, error = await validate_and_prepare_recipe(rid, '{"limit": 5}', schema_var)
    assert error == ""
    assert result is not None
    assert params == {"limit": 5}


@pytest.mark.asyncio
async def test_validate_and_prepare_recipe_not_found():
    """validate_and_prepare_recipe returns error for missing recipe."""
    from contextvars import ContextVar

    from api_agent.recipe import validate_and_prepare_recipe

    schema_var: ContextVar[str] = ContextVar("schema")
    schema_var.set('{"type": "test"}')

    result, params, error = await validate_and_prepare_recipe("nonexistent", "{}", schema_var)
    assert result is None
    assert params is None
    assert "not found" in error


@pytest.mark.asyncio
async def test_validate_and_prepare_recipe_no_schema():
    """validate_and_prepare_recipe returns error when schema not loaded."""
    from contextvars import ContextVar

    from api_agent.recipe import validate_and_prepare_recipe

    schema_var: ContextVar[str] = ContextVar("schema")  # Not set

    result, params, error = await validate_and_prepare_recipe("r_123", "{}", schema_var)
    assert result is None
    assert "schema not loaded" in error


@pytest.mark.asyncio
async def test_execute_recipe_steps_returns_executed_sql():
    """execute_recipe_steps returns executed SQL list."""
    from contextvars import ContextVar

    from api_agent.recipe.common import execute_recipe_steps

    query_results: ContextVar[dict] = ContextVar("qr")
    last_result: ContextVar[list] = ContextVar("lr")
    query_results.set({"data": [{"id": 1, "name": "test"}]})
    last_result.set([None])

    recipe = {
        "steps": [],
        "sql_steps": ["SELECT * FROM data", "SELECT id FROM data WHERE id = 1"],
    }

    executed_items: list = []

    async def mock_executor(idx, step, params, results):
        return True, {"mock": "data"}, "", {"call": idx}

    success, last_data, executed_sql, error = await execute_recipe_steps(
        recipe,
        {},
        query_results,
        last_result,
        mock_executor,
        executed_items,
    )

    assert success is True
    assert error == ""
    assert len(executed_sql) == 2
    assert executed_sql[0] == "SELECT * FROM data"
    assert executed_sql[1] == "SELECT id FROM data WHERE id = 1"


@pytest.mark.asyncio
async def test_execute_recipe_steps_with_api_and_sql():
    """execute_recipe_steps executes both API and SQL steps."""
    from contextvars import ContextVar

    from api_agent.recipe.common import execute_recipe_steps

    query_results: ContextVar[dict] = ContextVar("qr")
    last_result: ContextVar[list] = ContextVar("lr")
    query_results.set({})
    last_result.set([None])

    recipe = {
        "steps": [{"kind": "test", "name": "step1"}],
        "sql_steps": ["SELECT * FROM step1"],
    }

    executed_items: list = []
    executor_calls: list = []

    async def mock_executor(idx, step, params, results):
        executor_calls.append(step)
        results["step1"] = [{"id": 1}, {"id": 2}]
        return True, [{"id": 1}, {"id": 2}], "", {"call": "step1"}

    success, last_data, executed_sql, error = await execute_recipe_steps(
        recipe,
        {},
        query_results,
        last_result,
        mock_executor,
        executed_items,
    )

    assert success is True
    assert len(executor_calls) == 1
    assert len(executed_items) == 1
    assert len(executed_sql) == 1
    assert last_data == [{"id": 1}, {"id": 2}]  # SQL result


@pytest.mark.asyncio
async def test_execute_recipe_steps_api_failure():
    """execute_recipe_steps returns empty sql on API failure."""
    from contextvars import ContextVar

    from api_agent.recipe.common import execute_recipe_steps

    query_results: ContextVar[dict] = ContextVar("qr")
    last_result: ContextVar[list] = ContextVar("lr")
    query_results.set({})
    last_result.set([None])

    recipe = {
        "steps": [{"kind": "test"}],
        "sql_steps": ["SELECT * FROM data"],
    }

    async def failing_executor(idx, step, params, results):
        return False, None, '{"error": "api failed"}', None

    success, last_data, executed_sql, error = await execute_recipe_steps(
        recipe,
        {},
        query_results,
        last_result,
        failing_executor,
        [],
    )

    assert success is False
    assert executed_sql == []
    assert "api failed" in error


# --- sanitize_tool_name tests ---


class TestSanitizeToolName:
    def test_basic(self):
        from api_agent.recipe.naming import sanitize_tool_name

        assert sanitize_tool_name("get_users") == "get_users"

    def test_special_chars_stripped(self):
        from api_agent.recipe.naming import sanitize_tool_name

        assert sanitize_tool_name("get-users!@#v2") == "getusersv2"

    def test_spaces_to_underscores(self):
        from api_agent.recipe.naming import sanitize_tool_name

        assert sanitize_tool_name("get users by name") == "get_users_by_name"

    def test_empty_returns_recipe(self):
        from api_agent.recipe.naming import sanitize_tool_name

        assert sanitize_tool_name("") == "recipe"
        assert sanitize_tool_name(None) == "recipe"

    def test_uppercase_lowered(self):
        from api_agent.recipe.naming import sanitize_tool_name

        assert sanitize_tool_name("GetUsers") == "getusers"

    def test_leading_trailing_underscores_stripped(self):
        from api_agent.recipe.naming import sanitize_tool_name

        assert sanitize_tool_name("  _hello_  ") == "hello"


# --- _recipes_equivalent tests ---


class TestRecipesEquivalent:
    def test_identical_graphql(self):
        from api_agent.recipe.common import _recipes_equivalent

        r = {
            "params": {"limit": {"type": "int"}},
            "steps": [{"kind": "graphql", "name": "users", "query_template": "{ users }"}],
            "sql_steps": ["SELECT * FROM users"],
        }
        assert _recipes_equivalent(r, dict(r), "graphql") is True

    def test_whitespace_normalized_graphql(self):
        from api_agent.recipe.common import _recipes_equivalent

        a = {
            "params": {},
            "steps": [{"kind": "graphql", "name": "d", "query_template": "{  users\n{ id } }"}],
            "sql_steps": [],
        }
        b = {
            "params": {},
            "steps": [{"kind": "graphql", "name": "d", "query_template": "{ users { id } }"}],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "graphql") is True

    def test_different_params(self):
        from api_agent.recipe.common import _recipes_equivalent

        a = {"params": {"x": {"type": "int"}}, "steps": [], "sql_steps": []}
        b = {"params": {"y": {"type": "int"}}, "steps": [], "sql_steps": []}
        assert _recipes_equivalent(a, b, "graphql") is False

    def test_different_step_count(self):
        from api_agent.recipe.common import _recipes_equivalent

        a = {
            "params": {},
            "steps": [{"kind": "graphql", "name": "a", "query_template": "q"}],
            "sql_steps": [],
        }
        b = {"params": {}, "steps": [], "sql_steps": []}
        assert _recipes_equivalent(a, b, "graphql") is False

    def test_rest_step_fields(self):
        from api_agent.recipe.common import _recipes_equivalent

        base = {
            "kind": "rest",
            "name": "d",
            "method": "GET",
            "path": "/a",
            "path_params": {},
            "query_params": {},
            "body": {},
        }
        a = {"params": {}, "steps": [base], "sql_steps": []}
        b_step = {**base, "path": "/b"}
        b = {"params": {}, "steps": [b_step], "sql_steps": []}
        assert _recipes_equivalent(a, b, "rest") is False

    def test_sql_whitespace_normalized(self):
        from api_agent.recipe.common import _recipes_equivalent

        a = {"params": {}, "steps": [], "sql_steps": ["SELECT *  FROM   t"]}
        b = {"params": {}, "steps": [], "sql_steps": ["SELECT * FROM t"]}
        assert _recipes_equivalent(a, b, "graphql") is True


# --- recipe change tracking tests ---


class TestRecipeChangeTracking:
    def test_consume_empty(self):
        from api_agent.recipe.common import consume_recipe_changes, reset_recipe_change_flag

        reset_recipe_change_flag()
        assert consume_recipe_changes() == []

    def test_mark_and_consume(self):
        from api_agent.recipe.common import (
            consume_recipe_changes,
            mark_recipe_changed,
            reset_recipe_change_flag,
        )

        reset_recipe_change_flag()
        mark_recipe_changed("r_1")
        mark_recipe_changed("r_2")
        changes = consume_recipe_changes()
        assert changes == ["r_1", "r_2"]
        # second consume should be empty
        assert consume_recipe_changes() == []

    def test_consume_without_reset(self):
        """consume_recipe_changes works even without prior reset."""
        import contextvars

        from api_agent.recipe.common import consume_recipe_changes, mark_recipe_changed

        # Force fresh ContextVar state by operating in a new context

        ctx = contextvars.copy_context()

        def _inner():
            mark_recipe_changed("r_x")
            return consume_recipe_changes()

        result = ctx.run(_inner)
        assert result == ["r_x"]
