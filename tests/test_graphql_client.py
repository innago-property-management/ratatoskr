"""Tests for api_agent.graphql.client — execute_query (T010, T012, T013)."""

import json

import httpx
import pytest

from api_agent.graphql.client import execute_query


# ---------------------------------------------------------------------------
# T010: Mutation blocker
# ---------------------------------------------------------------------------
class TestMutationBlocker:
    """Mutations must be blocked before any HTTP call is made."""

    @pytest.mark.asyncio
    async def test_standard_mutation_blocked(self):
        result = await execute_query(
            "mutation { createUser(name: \"Alice\") { id } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result["success"] is False
        assert "Mutations are not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_mixed_case_mutation_blocked(self):
        result = await execute_query(
            "MuTaTiOn { createUser(name: \"Bob\") { id } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result["success"] is False
        assert "Mutations are not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_leading_whitespace_mutation_blocked(self):
        result = await execute_query(
            "   mutation { deleteUser(id: 1) { id } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result["success"] is False
        assert "Mutations are not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_multiline_mutation_blocked(self):
        result = await execute_query(
            "\nmutation { updateUser(id: 1) { id } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result["success"] is False
        assert "Mutations are not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_named_mutation_blocked(self):
        result = await execute_query(
            "mutation CreateUser { createUser(name: \"Eve\") { id } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result["success"] is False
        assert "Mutations are not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_regular_query_passes_through(self):
        """A normal query should NOT be blocked (it will fail on network, not on the guard)."""
        result = await execute_query(
            "{ users { id } }",
            endpoint="https://api.example.com/graphql",
        )
        # Should not be the mutation-block error — it should be a network/connection error
        assert result.get("error", "") != "Mutations are not allowed (read-only mode)"

    @pytest.mark.asyncio
    async def test_query_with_mutation_in_field_name_passes_through(self):
        """A query that happens to mention 'mutation' as a field name is NOT a mutation."""
        result = await execute_query(
            "{ mutationLog { id timestamp } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result.get("error", "") != "Mutations are not allowed (read-only mode)"


# ---------------------------------------------------------------------------
# Helpers — mock httpx.AsyncClient at the module level in the client module
# ---------------------------------------------------------------------------
class _MockTransport(httpx.AsyncBaseTransport):
    """Transport that returns canned responses in sequence."""

    def __init__(self, responses: list[tuple[int, dict]]):
        self._responses = iter(responses)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        status, body = next(self._responses)
        return httpx.Response(status, json=body, request=request)


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Transport that captures the request for inspection and returns a canned response."""

    def __init__(self, status: int, body: dict):
        self._status = status
        self._body = body
        self.last_request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(self._status, json=self._body, request=request)


def _make_client_factory(transport):
    """Create a factory function that replaces httpx.AsyncClient with a transport-injected one.

    We capture the real httpx.AsyncClient before the monkeypatch takes effect,
    so we avoid recursion.
    """
    _RealAsyncClient = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    return factory


def _patch_pool_http_client(monkeypatch, transport):
    """Patch pool.get_http_client to return a client with the given mock transport.

    This replaces the old pattern of monkeypatching httpx.AsyncClient directly,
    since clients now come from the connection pool.
    """
    client = httpx.AsyncClient(transport=transport)

    async def _get_http_client(url):
        return client

    monkeypatch.setattr("api_agent.graphql.client.pool.get_http_client", _get_http_client)
    return client


# ---------------------------------------------------------------------------
# T012: Response parsing
# ---------------------------------------------------------------------------
class TestResponseParsing:
    """execute_query must parse various GraphQL response shapes correctly."""

    @pytest.mark.asyncio
    async def test_success_response(self, monkeypatch):
        """Standard success: {data: ...} -> {success: True, data: ...}."""
        transport = _MockTransport([(200, {"data": {"users": [{"id": "1"}]}})])
        _patch_pool_http_client(monkeypatch, transport)

        result = await execute_query("{ users { id } }", endpoint="https://x.test/graphql")

        assert result["success"] is True
        assert result["data"] == {"users": [{"id": "1"}]}

    @pytest.mark.asyncio
    async def test_error_only_response(self, monkeypatch):
        """Error-only response: {errors: [...]} -> {success: False, error: [...]}."""
        body = {"errors": [{"message": "Not found"}]}
        transport = _MockTransport([(200, body)])
        _patch_pool_http_client(monkeypatch, transport)

        result = await execute_query("{ users { id } }", endpoint="https://x.test/graphql")

        assert result["success"] is False
        assert result["error"] == [{"message": "Not found"}]

    @pytest.mark.asyncio
    async def test_partial_success_with_errors(self, monkeypatch):
        """Partial success: {data: ..., errors: [...]} -> returns both per GraphQL spec (T004 fix)."""
        body = {
            "data": {"users": [{"id": "1"}]},
            "errors": [{"message": "Deprecated field"}],
        }
        transport = _MockTransport([(200, body)])
        _patch_pool_http_client(monkeypatch, transport)

        result = await execute_query("{ users { id } }", endpoint="https://x.test/graphql")

        # T004 fix: partial success returns data alongside errors
        assert result["success"] is True
        assert result["data"] == {"users": [{"id": "1"}]}
        assert result["errors"] == [{"message": "Deprecated field"}]

    @pytest.mark.asyncio
    async def test_http_status_error(self, monkeypatch):
        """HTTP 500 -> {success: False, error: 'HTTP 500'}."""
        transport = _MockTransport([(500, {"error": "Internal Server Error"})])
        _patch_pool_http_client(monkeypatch, transport)

        result = await execute_query("{ users { id } }", endpoint="https://x.test/graphql")

        assert result["success"] is False
        assert "HTTP 500" in result["error"]

    @pytest.mark.asyncio
    async def test_no_endpoint_returns_error(self):
        """Missing endpoint -> immediate error, no HTTP call."""
        result = await execute_query("{ users { id } }", endpoint="")

        assert result["success"] is False
        assert result["error"] == "No endpoint provided"


# ---------------------------------------------------------------------------
# T013: Valid query execution — verify request shape
# ---------------------------------------------------------------------------
class TestValidQueryExecution:
    """Verify that execute_query sends the correct HTTP request."""

    @pytest.mark.asyncio
    async def test_sends_correct_payload(self, monkeypatch):
        """POST body must contain query and variables in JSON."""
        transport = _CapturingTransport(200, {"data": {"ok": True}})
        _patch_pool_http_client(monkeypatch, transport)

        query = "{ users(limit: 5) { id name } }"
        variables = {"limit": 5}
        endpoint = "https://api.test/graphql"
        custom_headers = {"Authorization": "Bearer tok123"}

        result = await execute_query(query, variables, endpoint, custom_headers)

        assert result["success"] is True

        req = transport.last_request
        assert req is not None
        assert str(req.url) == endpoint

        body = json.loads(req.content)
        assert body["query"] == query
        assert body["variables"] == variables

    @pytest.mark.asyncio
    async def test_sends_correct_headers(self, monkeypatch):
        """Custom headers must be merged with Content-Type."""
        transport = _CapturingTransport(200, {"data": {"ok": True}})
        _patch_pool_http_client(monkeypatch, transport)

        custom_headers = {"Authorization": "Bearer secret", "X-Custom": "value"}

        await execute_query("{ test }", None, "https://api.test/graphql", custom_headers)

        req = transport.last_request
        assert req is not None
        assert req.headers["content-type"] == "application/json"
        assert req.headers["authorization"] == "Bearer secret"
        assert req.headers["x-custom"] == "value"

    @pytest.mark.asyncio
    async def test_omits_variables_when_none(self, monkeypatch):
        """When variables is None, payload should not contain 'variables' key."""
        transport = _CapturingTransport(200, {"data": {"ok": True}})
        _patch_pool_http_client(monkeypatch, transport)

        await execute_query("{ test }", None, "https://api.test/graphql")

        req = transport.last_request
        assert req is not None
        body = json.loads(req.content)
        assert "query" in body
        assert "variables" not in body
