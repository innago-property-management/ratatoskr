"""TDD tests for SCHEMA_STORE wiring into REST and GraphQL schema loaders.

Tests that load_openapi_spec and GraphQL _fetch_schema_context check
SCHEMA_STORE before network fetch, and work correctly with pre-loaded specs.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from api_agent.schema.spec_cache import SCHEMA_STORE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0"},
    "paths": {
        "/users": {
            "get": {
                "summary": "List users",
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "servers": [{"url": "https://api.example.com"}],
}


@pytest.fixture(autouse=True)
def _clean_schema_store():
    """Reset SCHEMA_STORE between tests."""
    SCHEMA_STORE._schemas.clear()
    yield
    SCHEMA_STORE._schemas.clear()


# ---------------------------------------------------------------------------
# load_openapi_spec — SCHEMA_STORE integration
# ---------------------------------------------------------------------------


class TestLoadOpenapiSpecUsesSchemaStore:
    """load_openapi_spec checks SCHEMA_STORE before doing HTTP fetch."""

    @pytest.mark.asyncio
    async def test_returns_preloaded_spec_no_http(self, monkeypatch) -> None:
        """When SCHEMA_STORE has the spec, return it without HTTP fetch."""
        from api_agent.rest.schema_loader import load_openapi_spec

        url = "https://api.example.com/openapi.json"
        SCHEMA_STORE._schemas[url] = MINIMAL_OPENAPI_SPEC

        # Mock the HTTP pool to verify it's NOT called
        mock_client = AsyncMock()
        monkeypatch.setattr("api_agent.rest.schema_loader.pool.get_http_client", mock_client)

        result = await load_openapi_spec(url)

        assert result == MINIMAL_OPENAPI_SPEC
        mock_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_http_when_not_preloaded(self, monkeypatch) -> None:
        """When SCHEMA_STORE has nothing, fetch via HTTP as before."""
        from api_agent.rest.schema_loader import load_openapi_spec

        url = "https://api.example.com/openapi.json"
        # SCHEMA_STORE is empty — should fall back to HTTP

        mock_response = AsyncMock()
        mock_response.text = json.dumps(MINIMAL_OPENAPI_SPEC)
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        async def mock_get_client(url):
            return mock_client

        monkeypatch.setattr("api_agent.rest.schema_loader.pool.get_http_client", mock_get_client)

        result = await load_openapi_spec(url)

        assert result == MINIMAL_OPENAPI_SPEC
        mock_client.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_preloaded_spec_skips_version_validation(self, monkeypatch) -> None:
        """Pre-loaded specs are trusted — no re-validation of OpenAPI version.

        This tests that even a non-3.x spec from the store is returned as-is,
        because validation happened at load time.
        """
        from api_agent.rest.schema_loader import load_openapi_spec

        url = "https://api.example.com/spec"
        weird_spec = {"openapi": "2.0", "info": {"title": "Old"}, "paths": {}}
        SCHEMA_STORE._schemas[url] = weird_spec

        mock_client = AsyncMock()
        monkeypatch.setattr("api_agent.rest.schema_loader.pool.get_http_client", mock_client)

        result = await load_openapi_spec(url)
        assert result == weird_spec
        mock_client.assert_not_awaited()


# ---------------------------------------------------------------------------
# fetch_schema_context — works with pre-loaded specs
# ---------------------------------------------------------------------------


class TestFetchSchemaContextWithStore:
    """fetch_schema_context correctly uses pre-loaded specs from SCHEMA_STORE."""

    @pytest.mark.asyncio
    async def test_builds_context_from_preloaded_spec(self, monkeypatch) -> None:
        """Full pipeline: store has spec → fetch_schema_context returns DSL + base_url."""
        from api_agent.rest.schema_loader import fetch_schema_context

        url = "https://api.example.com/openapi.json"
        SCHEMA_STORE._schemas[url] = MINIMAL_OPENAPI_SPEC

        mock_client = AsyncMock()
        monkeypatch.setattr("api_agent.rest.schema_loader.pool.get_http_client", mock_client)

        schema_ctx, base_url, raw_json = await fetch_schema_context(url)

        assert "GET /users" in schema_ctx
        assert base_url == "https://api.example.com"
        assert "/users" in raw_json
        mock_client.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spec_filter_applied_to_preloaded(self, monkeypatch) -> None:
        """spec_filter (endpoint allowlist) still applies to pre-loaded specs."""
        from api_agent.rest.schema_loader import fetch_schema_context

        url = "https://api.example.com/openapi.json"
        spec_with_two_paths = {
            **MINIMAL_OPENAPI_SPEC,
            "paths": {
                "/users": {"get": {"summary": "List users", "responses": {"200": {"description": "OK"}}}},
                "/admin": {"get": {"summary": "Admin panel", "responses": {"200": {"description": "OK"}}}},
            },
        }
        SCHEMA_STORE._schemas[url] = spec_with_two_paths

        mock_client = AsyncMock()
        monkeypatch.setattr("api_agent.rest.schema_loader.pool.get_http_client", mock_client)

        # Filter that removes /admin
        def only_users(spec: dict) -> dict:
            filtered = dict(spec)
            filtered["paths"] = {k: v for k, v in spec["paths"].items() if k == "/users"}
            return filtered

        schema_ctx, base_url, raw_json = await fetch_schema_context(url, spec_filter=only_users)

        assert "GET /users" in schema_ctx
        assert "/admin" not in schema_ctx
        assert "/admin" not in raw_json

    @pytest.mark.asyncio
    async def test_empty_url_still_returns_empty(self, monkeypatch) -> None:
        """Empty URL returns empty tuple regardless of store state."""
        from api_agent.rest.schema_loader import fetch_schema_context

        schema_ctx, base_url, raw_json = await fetch_schema_context("")
        assert schema_ctx == ""
        assert base_url == ""
        assert raw_json == ""


# ---------------------------------------------------------------------------
# Helpers — GraphQL introspection schema
# ---------------------------------------------------------------------------

MINIMAL_GRAPHQL_SCHEMA: dict[str, Any] = {
    "queryType": {
        "fields": [
            {
                "name": "users",
                "description": "Get all users",
                "args": [],
                "type": {"kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "User"}},
            }
        ]
    },
    "mutationType": None,
    "subscriptionType": None,
    "types": [
        {
            "name": "User",
            "kind": "OBJECT",
            "fields": [
                {
                    "name": "id",
                    "type": {"kind": "SCALAR", "name": "ID", "ofType": None},
                    "args": [],
                },
                {
                    "name": "name",
                    "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                    "args": [],
                },
            ],
        },
        {
            "name": "Query",
            "kind": "OBJECT",
            "fields": [
                {
                    "name": "users",
                    "description": "Get all users",
                    "args": [],
                    "type": {"kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "User"}},
                }
            ],
        },
    ],
    "directives": [],
}


# ---------------------------------------------------------------------------
# GraphQL _fetch_schema_context — SCHEMA_STORE integration
# ---------------------------------------------------------------------------


class TestGraphqlFetchSchemaContextUsesSchemaStore:
    """GraphQL _fetch_schema_context checks SCHEMA_STORE before introspection."""

    @pytest.mark.asyncio
    async def test_returns_preloaded_schema_no_introspection(self, monkeypatch) -> None:
        """When SCHEMA_STORE has the schema, skip introspection fetch entirely."""
        from api_agent.agent.graphql_agent import _fetch_schema_context

        endpoint = "https://api.example.com/graphql"
        SCHEMA_STORE._schemas[endpoint] = MINIMAL_GRAPHQL_SCHEMA

        # Mock graphql_fetch to verify it's NOT called
        mock_fetch = AsyncMock()
        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        result = await _fetch_schema_context(endpoint, headers=None)

        assert result.schema_context != ""
        assert "users" in result.schema_context
        assert result.raw_schema_json != ""
        mock_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_introspection_when_not_preloaded(self, monkeypatch) -> None:
        """When SCHEMA_STORE is empty, do normal introspection."""
        from api_agent.agent.graphql_agent import _fetch_schema_context

        endpoint = "https://api.example.com/graphql"
        # SCHEMA_STORE is empty

        async def mock_fetch(query, variables, ep, headers):
            return {"success": True, "data": {"__schema": MINIMAL_GRAPHQL_SCHEMA}}

        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        result = await _fetch_schema_context(endpoint, headers=None)

        assert "users" in result.schema_context
        assert result.raw_schema_json != ""

    @pytest.mark.asyncio
    async def test_preloaded_schema_still_applies_allowlist(self, monkeypatch) -> None:
        """Endpoint allowlist filtering works on pre-loaded schemas too."""
        from api_agent.agent.graphql_agent import _fetch_schema_context

        endpoint = "https://api.example.com/graphql"
        schema_with_two_fields = {
            **MINIMAL_GRAPHQL_SCHEMA,
            "queryType": {
                "fields": [
                    {
                        "name": "users",
                        "description": "Get all users",
                        "args": [],
                        "type": {"kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "User"}},
                    },
                    {
                        "name": "adminSettings",
                        "description": "Admin only",
                        "args": [],
                        "type": {"kind": "OBJECT", "name": "Settings", "ofType": None},
                    },
                ]
            },
        }
        SCHEMA_STORE._schemas[endpoint] = schema_with_two_fields

        mock_fetch = AsyncMock()
        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        # Allowlist: only "Query.users" pattern (match target is "Query.fieldName")
        result = await _fetch_schema_context(
            endpoint, headers=None, config_patterns=("Query.users",)
        )

        assert result.allowed_field_count == 1
        assert "users" in result.schema_context
        mock_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_preloaded_string_schema_ignored(self, monkeypatch) -> None:
        """If store has a string (e.g., SDL from file), it's not a valid introspection dict.
        Should fall back to introspection."""
        from api_agent.agent.graphql_agent import _fetch_schema_context

        endpoint = "https://api.example.com/graphql"
        SCHEMA_STORE._schemas[endpoint] = "type Query { users: [User] }"

        async def mock_fetch(query, variables, ep, headers):
            return {"success": True, "data": {"__schema": MINIMAL_GRAPHQL_SCHEMA}}

        monkeypatch.setattr("api_agent.agent.graphql_agent.graphql_fetch", mock_fetch)

        result = await _fetch_schema_context(endpoint, headers=None)
        assert "users" in result.schema_context
