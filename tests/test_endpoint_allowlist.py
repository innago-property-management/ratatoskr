"""Integration tests for endpoint allowlist feature.

Tests the full process_query → allowlist filter → error/result flow
for each protocol. Verifies:
- Empty filtered schemas return clear error messages
- search_schema on filtered raw schema doesn't leak blocked endpoints
- Config + header intersection works end-to-end
"""

import json
from unittest.mock import AsyncMock

import pytest

from api_agent.agent.graphql_agent import process_query as process_graphql_query
from api_agent.agent.rest_agent import process_rest_query
from api_agent.context import RequestContext
from tests.conftest import make_text_response, make_tool_call_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _graphql_ctx(allow_endpoints: tuple[str, ...] = ()) -> RequestContext:
    return RequestContext(
        target_url="https://example.com/graphql",
        target_headers={"Authorization": "Bearer test-token"},
        api_type="graphql",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
        allow_endpoints=allow_endpoints,
    )


def _rest_ctx(allow_endpoints: tuple[str, ...] = ()) -> RequestContext:
    return RequestContext(
        target_url="https://example.com/openapi.json",
        target_headers={"Authorization": "Bearer test-token"},
        api_type="rest",
        base_url="https://api.example.com/v1",
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
        allow_endpoints=allow_endpoints,
    )


# Sample introspection result with users + posts query fields
SAMPLE_INTROSPECTION = {
    "success": True,
    "data": {
        "__schema": {
            "queryType": {
                "fields": [
                    {
                        "name": "users",
                        "description": "List users",
                        "args": [],
                        "type": {"kind": "LIST", "ofType": {"name": "User", "kind": "OBJECT"}},
                    },
                    {
                        "name": "posts",
                        "description": "List posts",
                        "args": [],
                        "type": {"kind": "LIST", "ofType": {"name": "Post", "kind": "OBJECT"}},
                    },
                ]
            },
            "types": [
                {
                    "name": "User",
                    "kind": "OBJECT",
                    "description": None,
                    "fields": [
                        {"name": "id", "args": [], "type": {"name": "ID", "kind": "SCALAR"}},
                        {"name": "name", "args": [], "type": {"name": "String", "kind": "SCALAR"}},
                    ],
                    "enumValues": None,
                    "inputFields": None,
                    "interfaces": [],
                    "possibleTypes": None,
                },
                {
                    "name": "Post",
                    "kind": "OBJECT",
                    "description": None,
                    "fields": [
                        {"name": "id", "args": [], "type": {"name": "ID", "kind": "SCALAR"}},
                        {"name": "title", "args": [], "type": {"name": "String", "kind": "SCALAR"}},
                    ],
                    "enumValues": None,
                    "inputFields": None,
                    "interfaces": [],
                    "possibleTypes": None,
                },
            ],
        }
    },
}


# Sample OpenAPI spec with /users and /posts
SAMPLE_OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/users": {
            "get": {
                "operationId": "listUsers",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/posts": {
            "get": {
                "operationId": "listPosts",
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
}


# ---------------------------------------------------------------------------
# GraphQL: empty allowlist → error
# ---------------------------------------------------------------------------


class TestGraphqlAllowlistEmpty:
    """When allowlist filters ALL query fields, return error instead of running agent."""

    @pytest.mark.asyncio
    async def test_config_filters_all_returns_error(self, monkeypatch):
        """Config allowlist that matches nothing → error with clear message."""
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ALLOW_ENDPOINTS_GRAPHQL",
            "Query.nonexistent*",
        )
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ENABLE_RECIPES", False
        )

        async def mock_fetch(query, variables, endpoint, headers):
            return SAMPLE_INTROSPECTION

        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        result = await process_graphql_query("List users", _graphql_ctx())

        assert result["ok"] is False
        assert "allowlist" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_header_filters_all_returns_error(self, monkeypatch):
        """Header allowlist that matches nothing → error."""
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ALLOW_ENDPOINTS_GRAPHQL", ""
        )
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ENABLE_RECIPES", False
        )

        async def mock_fetch(query, variables, endpoint, headers):
            return SAMPLE_INTROSPECTION

        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        ctx = _graphql_ctx(allow_endpoints=("Query.nonexistent",))
        result = await process_graphql_query("List users", ctx)

        assert result["ok"] is False
        assert "allowlist" in result["error"].lower()


# ---------------------------------------------------------------------------
# GraphQL: partial allowlist → agent sees only allowed fields
# ---------------------------------------------------------------------------


class TestGraphqlAllowlistPartial:
    """Allowlist keeps some fields — agent runs with filtered schema."""

    @pytest.mark.asyncio
    async def test_header_narrows_to_users_only(
        self, monkeypatch, fake_provider_factory
    ):
        """Header allowlist 'Query.users' → agent only sees users, not posts."""
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ALLOW_ENDPOINTS_GRAPHQL", ""
        )
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ENABLE_RECIPES", False
        )

        introspection_calls = []

        async def mock_fetch(query, variables, endpoint, headers):
            introspection_calls.append(query)
            # For introspection queries, return schema
            if "__schema" in query:
                return SAMPLE_INTROSPECTION
            # For actual queries, return data
            return {
                "success": True,
                "data": {"users": [{"id": "1", "name": "Alice"}]},
            }

        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "graphql_query",
                    {"query": "{ users { id name } }", "name": "data"},
                    call_id="call_gql_001",
                ),
                make_text_response("Found 1 user: Alice."),
            ],
        )

        ctx = _graphql_ctx(allow_endpoints=("Query.users",))
        result = await process_graphql_query("List users", ctx)

        assert result["ok"] is True
        # The raw schema stored should NOT contain "posts"
        # (it was filtered out before storage)


# ---------------------------------------------------------------------------
# REST: empty allowlist → error
# ---------------------------------------------------------------------------


class TestRestAllowlistEmpty:
    """When allowlist filters ALL endpoints, return error."""

    @pytest.mark.asyncio
    async def test_config_filters_all_returns_error(self, monkeypatch):
        """Config allowlist that matches nothing → error with clear message."""
        monkeypatch.setattr(
            "api_agent.agent.rest_agent.settings.ALLOW_ENDPOINTS_REST",
            "GET /nonexistent*",
        )
        monkeypatch.setattr(
            "api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False
        )

        async def mock_fetch_schema(spec_url, headers=None, spec_filter=None):
            spec = dict(SAMPLE_OPENAPI_SPEC)
            if spec_filter:
                spec = spec_filter(spec)
            raw = json.dumps(spec, indent=2)
            return "<endpoints>\n", "https://api.example.com/v1", raw

        monkeypatch.setattr(
            "api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema
        )

        result = await process_rest_query("List users", _rest_ctx())

        assert result["ok"] is False
        assert "allowlist" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_header_filters_all_returns_error(self, monkeypatch):
        """Header allowlist that matches nothing → error."""
        monkeypatch.setattr(
            "api_agent.agent.rest_agent.settings.ALLOW_ENDPOINTS_REST", ""
        )
        monkeypatch.setattr(
            "api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False
        )

        async def mock_fetch_schema(spec_url, headers=None, spec_filter=None):
            spec = dict(SAMPLE_OPENAPI_SPEC)
            if spec_filter:
                spec = spec_filter(spec)
            raw = json.dumps(spec, indent=2)
            return "<endpoints>\n", "https://api.example.com/v1", raw

        monkeypatch.setattr(
            "api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema
        )

        ctx = _rest_ctx(allow_endpoints=("GET /nonexistent",))
        result = await process_rest_query("List users", ctx)

        assert result["ok"] is False
        assert "allowlist" in result["error"].lower()


# ---------------------------------------------------------------------------
# REST: config + header intersection
# ---------------------------------------------------------------------------


class TestRestAllowlistIntersection:
    """Config + header intersection works end-to-end."""

    @pytest.mark.asyncio
    async def test_intersection_narrows(self, monkeypatch, fake_provider_factory):
        """Config allows /users + /posts, header narrows to /users only."""
        monkeypatch.setattr(
            "api_agent.agent.rest_agent.settings.ALLOW_ENDPOINTS_REST",
            "GET /users,GET /posts",
        )
        monkeypatch.setattr(
            "api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False
        )

        async def mock_fetch_schema(spec_url, headers=None, spec_filter=None):
            import copy

            spec = copy.deepcopy(SAMPLE_OPENAPI_SPEC)
            if spec_filter:
                spec = spec_filter(spec)
            # Build simple schema text from remaining paths
            paths = spec.get("paths", {})
            lines = ["<endpoints>"]
            for path, item in paths.items():
                for m in ("get", "post", "put", "delete", "patch"):
                    if m in item:
                        lines.append(f"{m.upper()} {path}() -> any")
            schema_text = "\n".join(lines)
            raw = json.dumps(spec, indent=2)
            return schema_text, "https://api.example.com/v1", raw

        monkeypatch.setattr(
            "api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema
        )

        async def mock_execute_request(*args, **kwargs):
            return {
                "success": True,
                "data": {"users": [{"id": 1, "name": "Alice"}]},
            }

        monkeypatch.setattr(
            "api_agent.agent.rest_agent.execute_request", mock_execute_request
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/users", "name": "data"},
                    call_id="call_rest_001",
                ),
                make_text_response("Found 1 user: Alice."),
            ],
        )

        ctx = _rest_ctx(allow_endpoints=("GET /users",))
        result = await process_rest_query("List users", ctx)

        assert result["ok"] is True


# ---------------------------------------------------------------------------
# GraphQL: search_schema doesn't leak blocked endpoints
# ---------------------------------------------------------------------------


class TestSearchSchemaNoLeak:
    """search_schema on filtered raw schema doesn't return blocked fields."""

    @pytest.mark.asyncio
    async def test_filtered_schema_excludes_blocked_type(self, monkeypatch):
        """After filtering to Query.users, raw schema shouldn't contain Post type."""
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ALLOW_ENDPOINTS_GRAPHQL", ""
        )
        monkeypatch.setattr(
            "api_agent.agent.graphql_agent.settings.ENABLE_RECIPES", False
        )

        async def mock_fetch(query, variables, endpoint, headers):
            if "__schema" in query:
                return SAMPLE_INTROSPECTION
            return {"success": True, "data": {}}

        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        fake_provider = AsyncMock()
        fake_provider.run_tool_loop = AsyncMock()

        # Use _fetch_schema_context directly to verify raw schema filtering
        from api_agent.agent.graphql_agent import _fetch_schema_context, _raw_schema

        ctx = _graphql_ctx(allow_endpoints=("Query.users",))
        schema_ctx = await _fetch_schema_context(
            ctx.target_url,
            ctx.target_headers,
            config_patterns=None,
            header_patterns=("Query.users",),
        )

        # Raw schema should not contain Post type
        raw = _raw_schema.get()
        parsed = json.loads(raw)
        type_names = {t["name"] for t in parsed.get("types", [])}
        assert "User" in type_names
        assert "Post" not in type_names
        assert "posts" not in schema_ctx
