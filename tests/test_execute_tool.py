"""Tests for the _execute MCP tool in api_agent.tools.execute.

T011: Direct API execution — GraphQL path, REST path, gRPC path, truncation,
missing params, missing headers.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastmcp import FastMCP

from api_agent.context import MissingHeaderError, RequestContext
from api_agent.grpc.reflection import GrpcSchema, MethodInfo, ServiceInfo
from api_agent.tools.execute import _find_grpc_method, register_execute_tool


@pytest_asyncio.fixture
async def execute_tool():
    """Register the _execute tool on a test FastMCP instance and return the inner function."""
    mcp = FastMCP("test")
    register_execute_tool(mcp)
    tool = await mcp.get_tool("_execute")
    assert tool is not None
    return tool.fn  # type: ignore[unresolved-attribute]


@pytest.fixture
def graphql_ctx():
    """RequestContext configured for GraphQL."""
    return RequestContext(
        target_url="https://api.example.com/graphql",
        api_type="graphql",
        target_headers={"Authorization": "Bearer test-token"},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
    )


@pytest.fixture
def rest_ctx_for_execute():
    """RequestContext configured for REST with a base_url."""
    return RequestContext(
        target_url="https://api.example.com/openapi.json",
        api_type="rest",
        target_headers={"Authorization": "Bearer test-token"},
        allow_unsafe_paths=(),
        base_url="https://api.example.com/v1",
        include_result=False,
        poll_paths=(),
    )


@pytest.fixture
def rest_ctx_no_base_url():
    """RequestContext configured for REST without base_url (needs spec lookup)."""
    return RequestContext(
        target_url="https://api.example.com/openapi.json",
        api_type="rest",
        target_headers={},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
    )


class TestExecuteGraphQL:
    """T011: GraphQL execution path."""

    @pytest.mark.asyncio
    async def test_graphql_success(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL query returns ok=True with data."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        mock_execute_query = AsyncMock(
            return_value={
                "success": True,
                "data": {"users": [{"id": 1, "name": "Alice"}]},
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        result = await execute_tool(query="{ users { id name } }")

        assert result["ok"] is True
        assert result["data"]["users"][0]["name"] == "Alice"
        mock_execute_query.assert_awaited_once_with(
            "{ users { id name } }",
            None,
            "https://api.example.com/graphql",
            {"Authorization": "Bearer test-token"},
        )

    @pytest.mark.asyncio
    async def test_graphql_with_variables(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL query with variables passes them through."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        mock_execute_query = AsyncMock(
            return_value={
                "success": True,
                "data": {"user": {"id": 1, "name": "Alice"}},
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        result = await execute_tool(
            query="query($id: ID!) { user(id: $id) { id name } }",
            variables={"id": "1"},
        )

        assert result["ok"] is True
        assert result["data"]["user"]["name"] == "Alice"
        mock_execute_query.assert_awaited_once_with(
            "query($id: ID!) { user(id: $id) { id name } }",
            {"id": "1"},
            "https://api.example.com/graphql",
            {"Authorization": "Bearer test-token"},
        )

    @pytest.mark.asyncio
    async def test_graphql_query_failure(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL query that returns success=False propagates error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        mock_execute_query = AsyncMock(
            return_value={"success": False, "error": "Syntax error in query"}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        result = await execute_tool(query="{ invalid }")

        assert result["ok"] is False
        assert "Syntax error" in result["error"]

    @pytest.mark.asyncio
    async def test_graphql_missing_query(self, execute_tool, graphql_ctx, monkeypatch):
        """GraphQL without query param returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        result = await execute_tool()

        assert result["ok"] is False
        assert "query" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_graphql_truncation(self, execute_tool, graphql_ctx, monkeypatch):
        """Large GraphQL response is truncated."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: graphql_ctx
        )

        # Create data larger than MAX_RESPONSE_CHARS
        large_data = {"items": [{"value": "x" * 1000} for _ in range(100)]}
        mock_execute_query = AsyncMock(
            return_value={"success": True, "data": large_data}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_query", mock_execute_query
        )

        # Set a small max for testing
        monkeypatch.setattr("api_agent.tools.execute.settings.MAX_RESPONSE_CHARS", 500)

        result = await execute_tool(query="{ items { value } }")

        assert result["ok"] is True
        assert isinstance(result["data"], str)
        assert "[TRUNCATED" in result["data"]


class TestExecuteREST:
    """T011: REST execution path."""

    @pytest.mark.asyncio
    async def test_rest_success_with_base_url(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """REST call with base_url in context succeeds."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        mock_execute_request = AsyncMock(
            return_value={
                "success": True,
                "data": [{"id": 1, "name": "Alice"}],
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(method="GET", path="/users")

        assert result["ok"] is True
        assert result["data"][0]["name"] == "Alice"
        mock_execute_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rest_with_all_params(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """REST call with path_params, query_params, and body."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        mock_execute_request = AsyncMock(
            return_value={"success": True, "data": {"updated": True}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(
            method="GET",
            path="/users/{id}",
            path_params={"id": "123"},
            query_params={"fields": "name,email"},
            body=None,
        )

        assert result["ok"] is True
        call_args = mock_execute_request.call_args
        assert call_args[0][0] == "GET"  # method
        assert call_args[0][1] == "/users/{id}"  # path
        assert call_args[0][2] == {"id": "123"}  # path_params

    @pytest.mark.asyncio
    async def test_rest_missing_method(self, execute_tool, rest_ctx_for_execute, monkeypatch):
        """REST call without method returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        result = await execute_tool(path="/users")

        assert result["ok"] is False
        assert "method" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rest_missing_path(self, execute_tool, rest_ctx_for_execute, monkeypatch):
        """REST call without path returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        result = await execute_tool(method="GET")

        assert result["ok"] is False
        assert "path" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rest_no_base_url_falls_back_to_schema(
        self, execute_tool, rest_ctx_no_base_url, monkeypatch
    ):
        """When ctx.base_url is None, fetches base URL from schema."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_no_base_url,
        )

        mock_fetch_schema = AsyncMock(
            return_value=(
                "endpoints text",
                "https://api.example.com/v2",
                '{"paths": {}}',
            )
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_schema_context", mock_fetch_schema
        )

        mock_execute_request = AsyncMock(
            return_value={"success": True, "data": {"status": "ok"}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(method="GET", path="/health")

        assert result["ok"] is True
        # Verify base_url came from schema fetch
        call_kwargs = mock_execute_request.call_args[1]
        assert call_kwargs["base_url"] == "https://api.example.com/v2"

    @pytest.mark.asyncio
    async def test_rest_no_base_url_anywhere(
        self, execute_tool, rest_ctx_no_base_url, monkeypatch
    ):
        """When neither ctx nor schema provides base URL, return error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_no_base_url,
        )

        mock_fetch_schema = AsyncMock(return_value=("", "", ""))
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_schema_context", mock_fetch_schema
        )

        result = await execute_tool(method="GET", path="/users")

        assert result["ok"] is False
        assert "base url" in result["error"].lower() or "base_url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rest_request_failure(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """REST request that fails propagates error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        mock_execute_request = AsyncMock(
            return_value={"success": False, "error": "HTTP 500: Internal Server Error"}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        result = await execute_tool(method="GET", path="/broken")

        assert result["ok"] is False
        assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_rest_truncation(
        self, execute_tool, rest_ctx_for_execute, monkeypatch
    ):
        """Large REST response is truncated."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: rest_ctx_for_execute,
        )

        large_data = [{"value": "y" * 1000} for _ in range(100)]
        mock_execute_request = AsyncMock(
            return_value={"success": True, "data": large_data}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_request", mock_execute_request
        )

        monkeypatch.setattr("api_agent.tools.execute.settings.MAX_RESPONSE_CHARS", 500)

        result = await execute_tool(method="GET", path="/large")

        assert result["ok"] is True
        assert isinstance(result["data"], str)
        assert "[TRUNCATED" in result["data"]


class TestExecuteMissingHeader:
    """T011: Missing header error path."""

    @pytest.mark.asyncio
    async def test_missing_header_error(self, execute_tool, monkeypatch):
        """When get_request_context raises MissingHeaderError, return error dict."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context",
            lambda: (_ for _ in ()).throw(
                MissingHeaderError("X-Target-URL header required")
            ),
        )

        result = await execute_tool(query="{ users { id } }")

        assert result["ok"] is False
        assert "X-Target-URL" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_api_type_header(self, execute_tool, monkeypatch):
        """When X-API-Type header is missing, return error."""

        def raise_missing():
            raise MissingHeaderError("X-API-Type header required (graphql|rest)")

        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", raise_missing
        )

        result = await execute_tool(method="GET", path="/users")

        assert result["ok"] is False
        assert "X-API-Type" in result["error"]


# ---------------------------------------------------------------------------
# gRPC execute helpers
# ---------------------------------------------------------------------------


def _make_grpc_schema() -> GrpcSchema:
    """Build a minimal GrpcSchema for execute tool tests with all streaming types."""
    pool = MagicMock()
    services = [
        ServiceInfo(
            full_name="helloworld.Greeter",
            methods=[
                MethodInfo(
                    name="SayHello",
                    full_method_path="/helloworld.Greeter/SayHello",
                    input_type="helloworld.HelloRequest",
                    output_type="helloworld.HelloReply",
                ),
                MethodInfo(
                    name="StreamGreetings",
                    full_method_path="/helloworld.Greeter/StreamGreetings",
                    input_type="helloworld.HelloRequest",
                    output_type="helloworld.HelloReply",
                    server_streaming=True,
                ),
                MethodInfo(
                    name="UploadNames",
                    full_method_path="/helloworld.Greeter/UploadNames",
                    input_type="helloworld.HelloRequest",
                    output_type="helloworld.HelloReply",
                    client_streaming=True,
                ),
                MethodInfo(
                    name="Chat",
                    full_method_path="/helloworld.Greeter/Chat",
                    input_type="helloworld.HelloRequest",
                    output_type="helloworld.HelloReply",
                    client_streaming=True,
                    server_streaming=True,
                ),
            ],
        )
    ]
    raw_text = (
        "<services>\nhelloworld.Greeter\n"
        "  SayHello(helloworld.HelloRequest) -> helloworld.HelloReply\n"
    )
    return GrpcSchema(services=services, pool=pool, raw_schema_text=raw_text)


@pytest.fixture
def grpc_ctx():
    """RequestContext configured for gRPC."""
    return RequestContext(
        target_url="grpc://localhost:50051",
        api_type="grpc",
        target_headers={"authorization": "Bearer grpc-token"},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
    )


class TestExecuteGRPC:
    """T011: gRPC execution path."""

    @pytest.mark.asyncio
    async def test_grpc_success(self, execute_tool, grpc_ctx, monkeypatch):
        """gRPC execute with valid method and request returns ok=True with data."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_execute_rpc = AsyncMock(
            return_value={"success": True, "data": {"message": "Hello, world!"}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_unary_rpc", mock_execute_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
            grpc_request='{"name": "world"}',
        )

        assert result["ok"] is True
        assert result["data"]["message"] == "Hello, world!"

        # Verify fetch_schema called with target URL and metadata
        mock_fetch_schema.assert_awaited_once_with(
            "grpc://localhost:50051",
            metadata=[("authorization", "Bearer grpc-token")],
        )

        # Verify execute_unary_rpc called correctly
        mock_execute_rpc.assert_awaited_once()
        call_kwargs = mock_execute_rpc.call_args
        assert call_kwargs[1]["method_path"] == "/helloworld.Greeter/SayHello"
        assert call_kwargs[1]["request_json"] == {"name": "world"}
        assert call_kwargs[1]["input_type_name"] == "helloworld.HelloRequest"
        assert call_kwargs[1]["output_type_name"] == "helloworld.HelloReply"

    @pytest.mark.asyncio
    async def test_grpc_missing_method(self, execute_tool, grpc_ctx, monkeypatch):
        """gRPC execute without grpc_method returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        result = await execute_tool()

        assert result["ok"] is False
        assert "grpc_method" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_grpc_method_not_found(self, execute_tool, grpc_ctx, monkeypatch):
        """gRPC execute with unknown method returns method-not-found error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        result = await execute_tool(
            grpc_method="nonexistent.Service/DoThing",
            grpc_request="{}",
        )

        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_grpc_rpc_error(self, execute_tool, grpc_ctx, monkeypatch):
        """gRPC execute handles RPC failures from execute_unary_rpc."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_execute_rpc = AsyncMock(
            return_value={
                "success": False,
                "error": "gRPC error [UNAVAILABLE]: Connection refused",
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_unary_rpc", mock_execute_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
            grpc_request='{"name": "world"}',
        )

        assert result["ok"] is False
        assert "UNAVAILABLE" in result["error"]

    @pytest.mark.asyncio
    async def test_grpc_reflection_failure(self, execute_tool, grpc_ctx, monkeypatch):
        """gRPC execute handles reflection failure gracefully."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        mock_fetch_schema = AsyncMock(
            side_effect=Exception("UNIMPLEMENTED: reflection not enabled")
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
            grpc_request='{"name": "world"}',
        )

        assert result["ok"] is False
        assert "reflection" in result["error"].lower() or "schema" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_grpc_truncation(self, execute_tool, grpc_ctx, monkeypatch):
        """Large gRPC response is truncated."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        large_data = {"items": [{"value": "z" * 1000} for _ in range(100)]}
        mock_execute_rpc = AsyncMock(
            return_value={"success": True, "data": large_data}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_unary_rpc", mock_execute_rpc
        )

        monkeypatch.setattr("api_agent.tools.execute.settings.MAX_RESPONSE_CHARS", 500)

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
            grpc_request="{}",
        )

        assert result["ok"] is True
        assert isinstance(result["data"], str)
        assert "[TRUNCATED" in result["data"]

    @pytest.mark.asyncio
    async def test_grpc_invalid_json_request(self, execute_tool, grpc_ctx, monkeypatch):
        """gRPC execute with invalid JSON in grpc_request returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
            grpc_request="{bad json",
        )

        assert result["ok"] is False
        assert "json" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_grpc_server_streaming_via_execute(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Server-streaming method routes to execute_server_streaming_rpc."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_stream_rpc = AsyncMock(
            return_value={
                "success": True,
                "data": [{"message": "Hello 1"}, {"message": "Hello 2"}],
                "message_count": 2,
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_server_streaming_rpc", mock_stream_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/StreamGreetings",
            grpc_request='{"name": "world"}',
        )

        assert result["ok"] is True
        assert len(result["data"]) == 2
        mock_stream_rpc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_grpc_client_streaming_via_execute(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Client-streaming method routes to execute_client_streaming_rpc."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_client_rpc = AsyncMock(
            return_value={
                "success": True,
                "data": {"summary": "got 2 names"},
                "messages_sent": 2,
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_client_streaming_rpc", mock_client_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/UploadNames",
            grpc_requests='[{"name": "Alice"}, {"name": "Bob"}]',
        )

        assert result["ok"] is True
        assert result["data"]["summary"] == "got 2 names"
        call_kwargs = mock_client_rpc.call_args[1]
        assert call_kwargs["requests_json"] == [{"name": "Alice"}, {"name": "Bob"}]

    @pytest.mark.asyncio
    async def test_grpc_bidi_streaming_via_execute(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Bidi-streaming method routes to execute_bidi_streaming_rpc."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_bidi_rpc = AsyncMock(
            return_value={
                "success": True,
                "data": [{"reply": "Hey Alice"}, {"reply": "Hey Bob"}],
                "messages_sent": 2,
                "message_count": 2,
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_bidi_streaming_rpc", mock_bidi_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/Chat",
            grpc_requests='[{"name": "Alice"}, {"name": "Bob"}]',
        )

        assert result["ok"] is True
        assert len(result["data"]) == 2
        mock_bidi_rpc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_grpc_client_streaming_missing_requests(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Client-streaming without grpc_requests returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/UploadNames",
        )

        assert result["ok"] is False
        assert "grpc_requests" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_grpc_client_streaming_bad_json(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Client-streaming with invalid grpc_requests JSON returns error."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/UploadNames",
            grpc_requests="[{bad json}]",
        )

        assert result["ok"] is False
        assert "json" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_grpc_client_streaming_fallback_to_grpc_request(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Client-streaming wraps grpc_request as single-element array when grpc_requests missing."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_client_rpc = AsyncMock(
            return_value={
                "success": True,
                "data": {"summary": "got 1 name"},
                "messages_sent": 1,
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_client_streaming_rpc", mock_client_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/UploadNames",
            grpc_request='{"name": "Alice"}',
        )

        assert result["ok"] is True
        call_kwargs = mock_client_rpc.call_args[1]
        assert call_kwargs["requests_json"] == [{"name": "Alice"}]

    @pytest.mark.asyncio
    async def test_grpc_server_streaming_truncation(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Large server-streaming response is truncated."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        large_data = [{"value": "z" * 1000} for _ in range(100)]
        mock_stream_rpc = AsyncMock(
            return_value={
                "success": True,
                "data": large_data,
                "message_count": 100,
            }
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_server_streaming_rpc", mock_stream_rpc
        )

        monkeypatch.setattr("api_agent.tools.execute.settings.MAX_RESPONSE_CHARS", 500)

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/StreamGreetings",
            grpc_request="{}",
        )

        assert result["ok"] is True
        assert isinstance(result["data"], str)
        assert "[TRUNCATED" in result["data"]

    @pytest.mark.asyncio
    async def test_grpc_method_not_found_lists_all_methods(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """Unknown method error lists all available methods (not just unary)."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        result = await execute_tool(
            grpc_method="nonexistent.Service/DoThing",
            grpc_request="{}",
        )

        assert result["ok"] is False
        # Should list all methods, including streaming ones
        assert "SayHello" in result["error"]
        assert "StreamGreetings" in result["error"]

    @pytest.mark.asyncio
    async def test_grpc_metadata_forwarded(self, execute_tool, grpc_ctx, monkeypatch):
        """Target headers are forwarded as gRPC metadata to execute_unary_rpc."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_execute_rpc = AsyncMock(
            return_value={"success": True, "data": {"message": "ok"}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_unary_rpc", mock_execute_rpc
        )

        await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
            grpc_request='{"name": "world"}',
        )

        call_kwargs = mock_execute_rpc.call_args[1]
        assert call_kwargs["metadata"] == [("authorization", "Bearer grpc-token")]

    @pytest.mark.asyncio
    async def test_grpc_default_empty_request(
        self, execute_tool, grpc_ctx, monkeypatch
    ):
        """gRPC execute with no grpc_request defaults to empty dict."""
        monkeypatch.setattr(
            "api_agent.tools.execute.get_request_context", lambda: grpc_ctx
        )

        schema = _make_grpc_schema()
        mock_fetch_schema = AsyncMock(return_value=schema)
        monkeypatch.setattr(
            "api_agent.tools.execute.fetch_grpc_schema", mock_fetch_schema
        )

        mock_execute_rpc = AsyncMock(
            return_value={"success": True, "data": {"message": "Hello!"}}
        )
        monkeypatch.setattr(
            "api_agent.tools.execute.execute_unary_rpc", mock_execute_rpc
        )

        result = await execute_tool(
            grpc_method="/helloworld.Greeter/SayHello",
        )

        assert result["ok"] is True
        # Request JSON should be empty dict
        call_kwargs = mock_execute_rpc.call_args[1]
        assert call_kwargs["request_json"] == {}
