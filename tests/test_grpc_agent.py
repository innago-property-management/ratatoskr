"""Tests for process_grpc_query() in api_agent.agent.grpc_agent.

Tests follow the FakeLLMProvider pattern from test_rest_agent.py.
"""

import pytest

from api_agent.agent.grpc_agent import _find_method, process_grpc_query
from api_agent.context import RequestContext
from api_agent.grpc.reflection import GrpcSchema, MethodInfo, ServiceInfo
from tests.conftest import make_text_response, make_tool_call_response


@pytest.fixture
def grpc_ctx() -> RequestContext:
    """Standard gRPC request context for tests."""
    return RequestContext(
        target_url="grpc://localhost:50051",
        target_headers={"authorization": "Bearer test-token"},
        api_type="grpc",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


def _make_test_schema():
    """Build a minimal GrpcSchema for testing."""
    from unittest.mock import MagicMock

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
            ],
        )
    ]
    raw_text = (
        "<services>\nhelloworld.Greeter\n"
        "  SayHello(helloworld.HelloRequest) -> helloworld.HelloReply\n"
        "  StreamGreetings(helloworld.HelloRequest) -> helloworld.HelloReply"
        "  [server-streaming, unsupported-v1]\n"
    )
    return GrpcSchema(services=services, pool=pool, raw_schema_text=raw_text)


class TestFindMethod:
    """Test _find_method helper."""

    def test_find_by_full_path(self):
        schema = _make_test_schema()
        m = _find_method(schema, "/helloworld.Greeter/SayHello")
        assert m is not None
        assert m.name == "SayHello"

    def test_find_by_path_without_slash(self):
        schema = _make_test_schema()
        m = _find_method(schema, "helloworld.Greeter/SayHello")
        assert m is not None
        assert m.name == "SayHello"

    def test_not_found(self):
        schema = _make_test_schema()
        m = _find_method(schema, "nonexistent.Service/Method")
        assert m is None


class TestProcessGrpcQuery:
    """Integration tests for the gRPC agent with FakeLLMProvider."""

    @pytest.mark.asyncio
    async def test_success_path(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """Full success: fetch schema, call grpc_call, LLM summarizes."""
        schema = _make_test_schema()

        # Mock fetch_schema to return test schema
        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        # Mock execute_unary_rpc to return data
        async def mock_execute_rpc(*args, **kwargs):
            return {"success": True, "data": {"message": "Hello, world!"}}

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_unary_rpc", mock_execute_rpc
        )

        # FakeLLMProvider: tool call then summary
        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "request": '{"name": "world"}',
                    },
                    call_id="call_grpc_001",
                ),
                make_text_response("The server replied: Hello, world!"),
            ],
        )

        result = await process_grpc_query("Say hello to the world", grpc_ctx)

        assert result["ok"] is True
        assert result["error"] is None
        assert "Hello" in result["data"]
        assert len(result["rpc_calls"]) >= 1

    @pytest.mark.asyncio
    async def test_reflection_failure(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """When reflection fails, return error without running agent."""
        async def mock_fetch_schema(*args, **kwargs):
            raise Exception("UNIMPLEMENTED: reflection not enabled")

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        # No need for FakeLLMProvider — agent shouldn't run
        fake_provider_factory(monkeypatch, [])

        result = await process_grpc_query("List services", grpc_ctx)

        assert result["ok"] is False
        assert "reflection" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_services_found(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """When reflection returns no services, return error."""
        from unittest.mock import MagicMock

        empty_schema = GrpcSchema(services=[], pool=MagicMock(), raw_schema_text="")

        async def mock_fetch_schema(*args, **kwargs):
            return empty_schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(monkeypatch, [])

        result = await process_grpc_query("List services", grpc_ctx)

        assert result["ok"] is False
        assert "No services found" in result["error"]

    @pytest.mark.asyncio
    async def test_streaming_method_blocked(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """Attempting to call a streaming method returns an error from the tool."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        # LLM tries to call a streaming method, gets error, then gives up
        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {
                        "method": "helloworld.Greeter/StreamGreetings",
                        "request": '{"name": "world"}',
                    },
                    call_id="call_grpc_002",
                ),
                make_text_response("That method uses streaming and is not supported."),
            ],
        )

        result = await process_grpc_query("Stream greetings", grpc_ctx)

        assert result["ok"] is True
        assert "streaming" in result["data"].lower() or "not supported" in result["data"].lower()

    @pytest.mark.asyncio
    async def test_rpc_error_propagated(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """RPC errors from execute_unary_rpc are returned to the agent."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {"success": False, "error": "gRPC error [UNAVAILABLE]: Connection refused"}

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_unary_rpc", mock_execute_rpc
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "request": '{"name": "test"}',
                    },
                    call_id="call_grpc_003",
                ),
                make_text_response("The server is unavailable."),
            ],
        )

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is True
        assert "unavailable" in result["data"].lower()

    @pytest.mark.asyncio
    async def test_connection_failure(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """Non-reflection connection failure returns generic error."""
        async def mock_fetch_schema(*args, **kwargs):
            raise ConnectionError("Connection refused to grpc://localhost:50051")

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(monkeypatch, [])

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is False
        assert "Failed to connect" in result["error"]
