"""Behavioral tests for api_agent.recipe.runner (T014, T015, T016).

Mock strategy: use REAL RecipeStore, REAL execute_recipe_steps, REAL ContextVar.
Mock ONLY the network layer (graphql execute_query, rest execute_request,
schema fetching).
"""

import json

import pytest

from api_agent.context import RequestContext
from api_agent.recipe.common import build_api_id
from api_agent.recipe.runner import execute_recipe_tool
from api_agent.recipe.store import RECIPE_STORE, sha256_hex

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_recipe_store():
    """Ensure the global recipe store is empty before and after each test."""
    RECIPE_STORE._records.clear()
    RECIPE_STORE._by_key.clear()
    RECIPE_STORE._lru.clear()
    yield
    RECIPE_STORE._records.clear()
    RECIPE_STORE._by_key.clear()
    RECIPE_STORE._lru.clear()


def _graphql_ctx(url: str = "https://example.com/graphql") -> RequestContext:
    return RequestContext(
        target_url=url,
        target_headers={},
        api_type="graphql",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


def _rest_ctx(
    url: str = "https://example.com/api",
    base_url: str = "https://example.com",
) -> RequestContext:
    return RequestContext(
        target_url=url,
        target_headers={},
        api_type="rest",
        base_url=base_url,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


# ---------------------------------------------------------------------------
# T014: GraphQL recipe behavioral test
# ---------------------------------------------------------------------------


class TestGraphqlRecipeExecution:
    """Execute a real GraphQL recipe through the full runner path."""

    @pytest.mark.asyncio
    async def test_graphql_recipe_executes_query_with_bound_params(self, monkeypatch):
        """Store a recipe, execute it, and verify the GraphQL query has substituted params."""
        ctx = _graphql_ctx()
        raw_schema = '{"queryType": {"fields": []}}'
        schema_hash = sha256_hex(raw_schema)
        api_id = build_api_id(ctx, "graphql", "")

        recipe = {
            "params": {"user_id": {"type": "str", "description": "User ID"}},
            "steps": [
                {
                    "kind": "graphql",
                    "query_template": '{ user(id: "{{user_id}}") { id name } }',
                    "name": "data",
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="Get user by ID",
            recipe=recipe,
            tool_name="get_user",
        )

        # Mock schema loading
        async def mock_fetch_schema_raw(*args, **kwargs):
            return raw_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        # Mock GraphQL execution -- capture the actual query sent
        captured_queries: list[str] = []

        async def mock_graphql_execute(query, variables, endpoint, headers):
            captured_queries.append(query)
            return {
                "success": True,
                "data": {"user": {"id": "42", "name": "Alice"}},
            }

        monkeypatch.setattr("api_agent.recipe.runner.graphql_execute", mock_graphql_execute)

        result = await execute_recipe_tool(
            ctx, recipe_id, params={"user_id": "42"}, return_directly=False
        )

        # The query was sent with the parameter substituted
        assert len(captured_queries) == 1
        assert "42" in captured_queries[0]
        assert "{{user_id}}" not in captured_queries[0]

        # Result is valid JSON with success
        parsed = json.loads(result)
        assert parsed["success"] is True

    @pytest.mark.asyncio
    async def test_graphql_recipe_return_directly_gives_csv(self, monkeypatch):
        """return_directly=True should produce CSV output."""
        ctx = _graphql_ctx()
        raw_schema = '{"queryType": {"fields": []}}'
        schema_hash = sha256_hex(raw_schema)
        api_id = build_api_id(ctx, "graphql", "")

        recipe = {
            "params": {},
            "steps": [
                {
                    "kind": "graphql",
                    "query_template": "{ users { id name } }",
                    "name": "data",
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="List users",
            recipe=recipe,
            tool_name="list_users",
        )

        async def mock_fetch_schema_raw(*args, **kwargs):
            return raw_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        async def mock_graphql_execute(query, variables, endpoint, headers):
            return {
                "success": True,
                "data": {
                    "users": [
                        {"id": "1", "name": "Alice"},
                        {"id": "2", "name": "Bob"},
                    ]
                },
            }

        monkeypatch.setattr("api_agent.recipe.runner.graphql_execute", mock_graphql_execute)

        result = await execute_recipe_tool(ctx, recipe_id, params={}, return_directly=True)

        # CSV output should contain the column headers and data
        assert "id" in result
        assert "name" in result
        assert "Alice" in result
        assert "Bob" in result
        # Should have multiple lines (header + rows)
        lines = result.strip().splitlines()
        assert len(lines) >= 3  # header + 2 data rows

    @pytest.mark.asyncio
    async def test_graphql_recipe_missing_param_returns_error(self, monkeypatch):
        """Omitting a required param should return a clear error."""
        ctx = _graphql_ctx()
        raw_schema = '{"queryType": {"fields": []}}'
        schema_hash = sha256_hex(raw_schema)
        api_id = build_api_id(ctx, "graphql", "")

        recipe = {
            "params": {"user_id": {"type": "str", "description": "User ID"}},
            "steps": [
                {
                    "kind": "graphql",
                    "query_template": '{ user(id: "{{user_id}}") { id } }',
                    "name": "data",
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="Get user",
            recipe=recipe,
            tool_name="get_user",
        )

        async def mock_fetch_schema_raw(*args, **kwargs):
            return raw_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        result = await execute_recipe_tool(ctx, recipe_id, params={}, return_directly=False)

        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "missing required param" in parsed["error"]


# ---------------------------------------------------------------------------
# T015: REST recipe behavioral test
# ---------------------------------------------------------------------------


class TestRestRecipeExecution:
    """Execute a real REST recipe through the full runner path."""

    @pytest.mark.asyncio
    async def test_rest_recipe_executes_request_with_bound_params(self, monkeypatch):
        """Store a REST recipe, execute it, and verify the request has substituted params."""
        ctx = _rest_ctx()
        raw_schema = '{"openapi": "3.0.0", "paths": {}}'
        schema_hash = sha256_hex(raw_schema)
        base_url = "https://example.com"
        api_id = build_api_id(ctx, "rest", base_url)

        recipe = {
            "params": {"user_id": {"type": "str", "description": "User ID"}},
            "steps": [
                {
                    "kind": "rest",
                    "method": "GET",
                    "path": "/users/{user_id}",
                    "name": "data",
                    "path_params": {"user_id": {"$param": "user_id"}},
                    "query_params": {},
                    "body": {},
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="Get user by ID",
            recipe=recipe,
            tool_name="get_user",
        )

        # Mock schema loading
        async def mock_fetch_schema_context(*args, **kwargs):
            return ("schema_context_str", base_url, raw_schema)

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_schema_context", mock_fetch_schema_context
        )

        # Mock REST execution -- capture the call
        captured_calls: list[dict] = []

        async def mock_execute_request(method, path, path_params, query_params, body, **kwargs):
            captured_calls.append(
                {
                    "method": method,
                    "path": path,
                    "path_params": path_params,
                    "query_params": query_params,
                    "body": body,
                    "kwargs": kwargs,
                }
            )
            return {
                "success": True,
                "data": {"id": "99", "name": "Charlie"},
            }

        monkeypatch.setattr("api_agent.recipe.runner.execute_request", mock_execute_request)

        result = await execute_recipe_tool(
            ctx, recipe_id, params={"user_id": "99"}, return_directly=False
        )

        # The request was made with the parameter substituted
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["method"] == "GET"
        assert call["path"] == "/users/{user_id}"
        assert call["path_params"] == {"user_id": "99"}

        # Result is valid JSON with success
        parsed = json.loads(result)
        assert parsed["success"] is True

    @pytest.mark.asyncio
    async def test_rest_recipe_return_directly_gives_csv(self, monkeypatch):
        """return_directly=True should produce CSV output for REST too."""
        ctx = _rest_ctx()
        raw_schema = '{"openapi": "3.0.0", "paths": {}}'
        schema_hash = sha256_hex(raw_schema)
        base_url = "https://example.com"
        api_id = build_api_id(ctx, "rest", base_url)

        recipe = {
            "params": {},
            "steps": [
                {
                    "kind": "rest",
                    "method": "GET",
                    "path": "/users",
                    "name": "data",
                    "path_params": {},
                    "query_params": {},
                    "body": {},
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="List users",
            recipe=recipe,
            tool_name="list_users",
        )

        async def mock_fetch_schema_context(*args, **kwargs):
            return ("schema_context_str", base_url, raw_schema)

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_schema_context", mock_fetch_schema_context
        )

        async def mock_execute_request(method, path, path_params, query_params, body, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": "1", "name": "Alice"},
                    {"id": "2", "name": "Bob"},
                ],
            }

        monkeypatch.setattr("api_agent.recipe.runner.execute_request", mock_execute_request)

        result = await execute_recipe_tool(ctx, recipe_id, params={}, return_directly=True)

        # CSV output should contain column headers and data
        assert "id" in result
        assert "name" in result
        assert "Alice" in result
        assert "Bob" in result
        lines = result.strip().splitlines()
        assert len(lines) >= 3

    @pytest.mark.asyncio
    async def test_rest_recipe_no_base_url_returns_error(self, monkeypatch):
        """REST recipe without base_url should fail gracefully."""
        ctx = RequestContext(
            target_url="https://example.com/api",
            target_headers={},
            api_type="rest",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
        )
        raw_schema = '{"openapi": "3.0.0", "paths": {}}'
        schema_hash = sha256_hex(raw_schema)
        # base_url is empty string, matching how load_schema_and_base_url would return
        api_id = build_api_id(ctx, "rest", "")

        recipe = {
            "params": {},
            "steps": [
                {
                    "kind": "rest",
                    "method": "GET",
                    "path": "/users",
                    "name": "data",
                    "path_params": {},
                    "query_params": {},
                    "body": {},
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="List users",
            recipe=recipe,
            tool_name="list_users",
        )

        # Schema loader returns empty base_url
        async def mock_fetch_schema_context(*args, **kwargs):
            return ("schema_context_str", "", raw_schema)

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_schema_context", mock_fetch_schema_context
        )

        result = await execute_recipe_tool(ctx, recipe_id, params={}, return_directly=False)

        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "base URL" in parsed["error"]


# ---------------------------------------------------------------------------
# T016: return_directly CSV + stale schema rejection
# ---------------------------------------------------------------------------


class TestReturnDirectlyAndSchemaValidation:
    """Test CSV formatting and schema staleness checks."""

    @pytest.mark.asyncio
    async def test_stale_schema_rejected(self, monkeypatch):
        """A recipe stored against one schema hash should be rejected when the live schema changes."""
        ctx = _graphql_ctx()
        original_schema = '{"queryType": {"fields": ["old_field"]}}'
        original_hash = sha256_hex(original_schema)
        api_id = build_api_id(ctx, "graphql", "")

        recipe = {
            "params": {},
            "steps": [
                {
                    "kind": "graphql",
                    "query_template": "{ users { id } }",
                    "name": "data",
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=original_hash,
            question="List users",
            recipe=recipe,
            tool_name="list_users",
        )

        # The live schema has changed
        new_schema = '{"queryType": {"fields": ["new_field"]}}'
        assert sha256_hex(new_schema) != original_hash  # Sanity check

        async def mock_fetch_schema_raw(*args, **kwargs):
            return new_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        result = await execute_recipe_tool(ctx, recipe_id, params={}, return_directly=False)

        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "schema" in parsed["error"].lower() or "match" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_recipe_not_found(self, monkeypatch):
        """Requesting a non-existent recipe_id should return a clear error."""
        ctx = _graphql_ctx()
        raw_schema = '{"queryType": {"fields": []}}'

        async def mock_fetch_schema_raw(*args, **kwargs):
            return raw_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        result = await execute_recipe_tool(ctx, "r_nonexistent", params={}, return_directly=False)

        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "not found" in parsed["error"]

    @pytest.mark.asyncio
    async def test_schema_not_loaded_returns_error(self, monkeypatch):
        """If schema loading returns empty, return an error."""
        ctx = _graphql_ctx()

        async def mock_fetch_schema_raw(*args, **kwargs):
            return ""

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        result = await execute_recipe_tool(ctx, "r_any", params={}, return_directly=False)

        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "schema" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_graphql_query_failure_propagates(self, monkeypatch):
        """If the GraphQL query returns success=False, the recipe returns an error."""
        ctx = _graphql_ctx()
        raw_schema = '{"queryType": {"fields": []}}'
        schema_hash = sha256_hex(raw_schema)
        api_id = build_api_id(ctx, "graphql", "")

        recipe = {
            "params": {},
            "steps": [
                {
                    "kind": "graphql",
                    "query_template": "{ broken_query }",
                    "name": "data",
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="Broken query",
            recipe=recipe,
            tool_name="broken_query",
        )

        async def mock_fetch_schema_raw(*args, **kwargs):
            return raw_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        async def mock_graphql_execute(query, variables, endpoint, headers):
            return {"success": False, "error": "Field 'broken_query' not found"}

        monkeypatch.setattr("api_agent.recipe.runner.graphql_execute", mock_graphql_execute)

        result = await execute_recipe_tool(ctx, recipe_id, params={}, return_directly=False)

        parsed = json.loads(result)
        assert parsed["success"] is False

    @pytest.mark.asyncio
    async def test_api_id_mismatch_rejected(self, monkeypatch):
        """Recipe stored for one API ID should not work for a different target URL."""
        original_ctx = _graphql_ctx("https://original.com/graphql")
        raw_schema = '{"queryType": {"fields": []}}'
        schema_hash = sha256_hex(raw_schema)
        api_id = build_api_id(original_ctx, "graphql", "")

        recipe = {
            "params": {},
            "steps": [
                {
                    "kind": "graphql",
                    "query_template": "{ users { id } }",
                    "name": "data",
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(
            api_id=api_id,
            schema_hash=schema_hash,
            question="List users",
            recipe=recipe,
            tool_name="list_users",
        )

        # Use a different target URL
        different_ctx = _graphql_ctx("https://different.com/graphql")

        async def mock_fetch_schema_raw(*args, **kwargs):
            return raw_schema

        monkeypatch.setattr(
            "api_agent.recipe.runner.fetch_graphql_schema_raw", mock_fetch_schema_raw
        )

        result = await execute_recipe_tool(
            different_ctx, recipe_id, params={}, return_directly=False
        )

        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "match" in parsed["error"].lower()


class TestGrpcRecipeNoOp:
    """Test that gRPC sessions get no recipes (deferred to v2)."""

    @pytest.mark.asyncio
    async def test_load_schema_grpc_returns_empty(self):
        """gRPC api_type returns empty schema and base_url."""
        from api_agent.recipe.runner import load_schema_and_base_url

        grpc_ctx = RequestContext(
            target_url="grpc://localhost:50051",
            api_type="grpc",
            target_headers={},
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
        )

        raw_schema, base_url = await load_schema_and_base_url(grpc_ctx)
        assert raw_schema == ""
        assert base_url == ""
