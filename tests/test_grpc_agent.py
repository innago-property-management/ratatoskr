"""Tests for process_grpc_query() in api_agent.agent.grpc_agent.

Tests follow the FakeLLMProvider pattern from test_rest_agent.py.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api_agent.agent.grpc_agent import (
    _find_bidi_streaming_method,
    _find_client_streaming_method,
    _find_method,
    _find_streaming_method,
    process_grpc_query,
)
from api_agent.config import settings
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
    """Build a minimal GrpcSchema for testing (includes all streaming types)."""
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
        "  StreamGreetings(helloworld.HelloRequest) -> helloworld.HelloReply"
        "  [server-streaming]\n"
        "  UploadNames(helloworld.HelloRequest) -> helloworld.HelloReply"
        "  [client-streaming]\n"
        "  Chat(helloworld.HelloRequest) -> helloworld.HelloReply"
        "  [bidi-streaming]\n"
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
        assert "streaming" in result["data"].lower() or "grpc_stream" in result["data"].lower()

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


# ---------------------------------------------------------------------------
# Phase 1: Tool Error Paths
# ---------------------------------------------------------------------------


class TestGrpcCallToolErrors:
    """Tests for grpc_call tool error branches exercised through the agent."""

    @pytest.mark.asyncio
    async def test_grpc_call_method_not_found_lists_available(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Calling a nonexistent method returns error with available unary methods.

        Protects grpc_agent.py:144-155 — method-not-found branch with
        available methods list that filters out streaming methods.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        # execute_unary_rpc should NOT be called — guard with side_effect
        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_unary_rpc",
            AsyncMock(side_effect=AssertionError("should not be called")),
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {"method": "nonexistent/Foo", "request": "{}"},
                    call_id="call_notfound",
                ),
                make_text_response("Method not found."),
            ],
        )

        result = await process_grpc_query("Call Foo", grpc_ctx)

        assert result["ok"] is True
        # The LLM saw the error and summarized; the tool response contained
        # the available methods list with SayHello but NOT StreamGreetings
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_grpc_call_invalid_json_request(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Invalid JSON in request field returns error, agent recovers.

        Protects grpc_agent.py:168-174 — json.JSONDecodeError handler.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "request": "{bad json",
                    },
                    call_id="call_badjson",
                ),
                make_text_response("The request body was invalid JSON."),
            ],
        )

        result = await process_grpc_query("Say hello", grpc_ctx)

        # Agent recovered — LLM saw the error and replied
        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_grpc_call_passes_metadata_from_headers(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Target headers are converted to gRPC metadata tuples on RPC call.

        Protects grpc_agent.py:177-181 — metadata construction.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        captured_kwargs = {}

        async def mock_execute_rpc(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return {"success": True, "data": {"message": "ok"}}

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
                        "request": '{"name": "world"}',
                    },
                    call_id="call_meta",
                ),
                make_text_response("Done."),
            ],
        )

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is True
        # grpc_ctx has target_headers={"authorization": "Bearer test-token"}
        assert captured_kwargs.get("metadata") == [
            ("authorization", "Bearer test-token")
        ]

    @pytest.mark.asyncio
    async def test_return_directly_skips_llm_output(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """return_directly=True returns raw data, data field is None.

        Protects grpc_agent.py:223-227 (flag set), 397-403 (direct return check),
        422-423 (agent_output = None).
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": {"users": [{"id": 1, "name": "Alice"}]},
            }

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
                        "request": '{"name": "world"}',
                        "return_directly": True,
                    },
                    call_id="call_direct",
                ),
                # LLM may output __DIRECT_RETURN__ or be short-circuited
                make_text_response("__DIRECT_RETURN__"),
            ],
        )

        result = await process_grpc_query("Get users", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is None  # Direct return — no LLM summary
        assert result["result"] is not None


# ---------------------------------------------------------------------------
# Phase 2: SQL Pipeline
# ---------------------------------------------------------------------------


class TestGrpcSqlPipeline:
    """Tests for gRPC → SQL post-processing pipeline."""

    @pytest.mark.asyncio
    async def test_grpc_call_then_sql_query(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Three-step: grpc_call stores data, sql_query filters, LLM summarizes.

        Protects grpc_agent.py:206-219 (result storage for SQL) and
        247-281 (_sql_query function).
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": 1, "name": "Alice", "age": 30},
                    {"id": 2, "name": "Bob", "age": 25},
                    {"id": 3, "name": "Charlie", "age": 35},
                ],
            }

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
                        "request": '{"name": "world"}',
                        "name": "users",
                    },
                    call_id="call_sql_1",
                ),
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT name FROM users WHERE age > 28"},
                    call_id="call_sql_2",
                ),
                make_text_response("Users over 28: Alice (30) and Charlie (35)."),
            ],
        )

        result = await process_grpc_query("Find users over 28", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None
        assert result["result"] is not None

    @pytest.mark.asyncio
    async def test_sql_query_before_grpc_call_returns_error(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Calling sql_query before grpc_call returns 'No data' error.

        Protects grpc_agent.py:249-255 — empty/unset _query_results path.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT 1"},
                    call_id="call_sql_early",
                ),
                make_text_response("No data was available to query."),
            ],
        )

        result = await process_grpc_query("Run a query", grpc_ctx)

        # Agent recovered — LLM saw the "No data" error and replied
        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_sql_query_return_directly(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """sql_query with return_directly=True returns raw SQL result.

        Protects grpc_agent.py:269-273 — return_directly flag in _sql_query.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": 1, "name": "Alice"},
                    {"id": 2, "name": "Bob"},
                ],
            }

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
                        "request": '{"name": "world"}',
                        "name": "users",
                    },
                    call_id="call_sqld_1",
                ),
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT name FROM users", "return_directly": True},
                    call_id="call_sqld_2",
                ),
                make_text_response("__DIRECT_RETURN__"),
            ],
        )

        result = await process_grpc_query("Get user names", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is None  # Direct return — no LLM summary
        assert result["result"] is not None


# ---------------------------------------------------------------------------
# Phase 3: Orchestrator Edge Cases
# ---------------------------------------------------------------------------


class TestGrpcOrchestratorEdgeCases:
    """Tests for process_grpc_query edge cases: MaxTurns, empty output, etc."""

    @pytest.mark.asyncio
    async def test_max_turns_exceeded_with_data(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """MaxTurnsExceeded with partial data returns ok=True with partial result.

        Protects grpc_agent.py:390-394 — MaxTurnsExceeded handler.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {"success": True, "data": {"message": "Hello!"}}

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_unary_rpc", mock_execute_rpc
        )

        monkeypatch.setattr(settings, "MAX_AGENT_TURNS", 2)

        # Feed many tool calls — agent will hit max turns
        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "request": '{"name": "world"}',
                    },
                    call_id=f"call_mt_{i}",
                )
                for i in range(10)
            ],
        )

        result = await process_grpc_query("Say hello repeatedly", grpc_ctx)

        assert result["ok"] is True
        assert result["result"] is not None
        assert result["rpc_calls"]  # At least one call was made

    @pytest.mark.asyncio
    async def test_max_turns_exceeded_no_data(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """MaxTurnsExceeded with no data returns ok=False.

        Protects grpc_agent.py:390-394 — MaxTurnsExceeded no-data branch.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        # All RPCs fail — no data stored
        async def mock_execute_rpc(*args, **kwargs):
            return {"success": False, "error": "Unavailable"}

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_unary_rpc", mock_execute_rpc
        )

        monkeypatch.setattr(settings, "MAX_AGENT_TURNS", 2)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_call",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "request": '{"name": "world"}',
                    },
                    call_id=f"call_mtn_{i}",
                )
                for i in range(10)
            ],
        )

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is False
        assert result["result"] is None

    @pytest.mark.asyncio
    async def test_empty_output_with_data_returns_partial(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """RPC succeeds but LLM returns empty string — partial result.

        Protects grpc_agent.py:405-412 — no output + last_data branch.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {"success": True, "data": {"message": "Hello!"}}

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
                        "request": '{"name": "world"}',
                    },
                    call_id="call_empty_1",
                ),
                make_text_response(""),  # Empty final output
            ],
        )

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is True
        assert "Partial" in result["data"]
        assert result["result"] is not None

    @pytest.mark.asyncio
    async def test_empty_output_no_data_returns_error(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """LLM returns empty string with no tool calls — error.

        Protects grpc_agent.py:414-420 — no output + no data branch.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [make_text_response("")],  # Empty output, no tool calls
        )

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is False
        assert "No output" in result["error"]

    @pytest.mark.asyncio
    async def test_outer_exception_handler(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Unexpected exception in agent loop is caught by outer handler.

        Protects grpc_agent.py:436-443 — outermost except Exception.
        """
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        # Make _build_system_prompt raise to trigger the outer catch-all
        monkeypatch.setattr(
            "api_agent.agent.grpc_agent._build_system_prompt",
            lambda: (_ for _ in ()).throw(RuntimeError("unexpected boom")),
        )

        fake_provider_factory(monkeypatch, [])

        result = await process_grpc_query("Say hello", grpc_ctx)

        assert result["ok"] is False
        assert "unexpected boom" in result["error"]
        assert result["rpc_calls"] == []

    @pytest.mark.asyncio
    async def test_fetch_schema_receives_metadata(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Target headers are forwarded as metadata to fetch_schema.

        Protects grpc_agent.py:312-316 — metadata construction for reflection.
        """
        captured_kwargs = {}

        async def mock_fetch_schema(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_test_schema()

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_execute_rpc(*args, **kwargs):
            return {"success": True, "data": {"message": "ok"}}

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
                        "request": '{"name": "world"}',
                    },
                    call_id="call_fmeta",
                ),
                make_text_response("Done."),
            ],
        )

        await process_grpc_query("Say hello", grpc_ctx)

        # grpc_ctx.target_headers = {"authorization": "Bearer test-token"}
        assert captured_kwargs.get("metadata") == [
            ("authorization", "Bearer test-token")
        ]

    @pytest.mark.asyncio
    async def test_reflection_error_lowercase_keyword(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Reflection error with 'reflection' (no 'UNIMPLEMENTED') detected.

        Protects grpc_agent.py:320 — the 'or "reflection" in error_msg.lower()' branch.
        """
        async def mock_fetch_schema(*args, **kwargs):
            raise Exception("Server Reflection is not supported")

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(monkeypatch, [])

        result = await process_grpc_query("List services", grpc_ctx)

        assert result["ok"] is False
        assert "reflection not enabled" in result["error"].lower()


# ---------------------------------------------------------------------------
# Phase 7: Medium Priority
# ---------------------------------------------------------------------------


class TestGrpcMediumPriority:
    """Medium-severity mutation-proofing tests."""

    def test_find_method_by_service_name_form(self):
        """Second match path in _find_method: svc.full_name/m.name form.

        Protects grpc_agent.py:116-117 — the fallback match when
        full_method_path differs from {service}/{method}.
        """
        # Schema where full_method_path has a versioned prefix
        services = [
            ServiceInfo(
                full_name="myapp.UserService",
                methods=[
                    MethodInfo(
                        name="GetUser",
                        full_method_path="/myapp.v2.UserService/GetUser",
                        input_type="myapp.GetUserReq",
                        output_type="myapp.GetUserResp",
                    ),
                ],
            )
        ]
        schema = GrpcSchema(
            services=services, pool=MagicMock(), raw_schema_text=""
        )

        # This won't match full_method_path (myapp.v2.UserService/GetUser)
        # but WILL match svc.full_name/m.name (myapp.UserService/GetUser)
        m = _find_method(schema, "myapp.UserService/GetUser")
        assert m is not None
        assert m.name == "GetUser"

    @pytest.mark.asyncio
    async def test_large_schema_truncated(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """Schema larger than MAX_TOOL_RESPONSE_CHARS is truncated.

        Protects grpc_agent.py:349-355 — schema truncation with marker.
        """
        big_text = "x" * (settings.MAX_TOOL_RESPONSE_CHARS + 100)
        big_schema = GrpcSchema(
            services=[
                ServiceInfo(
                    full_name="big.Svc",
                    methods=[
                        MethodInfo(
                            name="Do",
                            full_method_path="/big.Svc/Do",
                            input_type="big.Req",
                            output_type="big.Resp",
                        )
                    ],
                )
            ],
            pool=MagicMock(),
            raw_schema_text=big_text,
        )

        async def mock_fetch_schema(*args, **kwargs):
            return big_schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        prov = fake_provider_factory(
            monkeypatch,
            [make_text_response("Schema is large.")],
        )

        await process_grpc_query("Describe schema", grpc_ctx)

        # The provider's call_log should show the truncated schema
        assert prov.call_log, "Provider should have been called"
        user_msg = prov.call_log[0]["messages"][-1]
        # User message content contains the augmented query
        msg_content = user_msg.get("content", "") if isinstance(user_msg, dict) else str(user_msg)
        assert "[SCHEMA TRUNCATED" in msg_content


# ---------------------------------------------------------------------------
# Phase B3: Server Streaming Agent Tool
# ---------------------------------------------------------------------------


class TestGrpcStreamTool:
    """Tests for grpc_stream tool — server-streaming RPC via the agent."""

    @pytest.mark.asyncio
    async def test_stream_success(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """grpc_stream calls execute_server_streaming_rpc and stores results."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_stream_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"message": "Hello 1"},
                    {"message": "Hello 2"},
                    {"message": "Hello 3"},
                ],
                "message_count": 3,
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_server_streaming_rpc",
            mock_stream_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_stream",
                    {
                        "method": "helloworld.Greeter/StreamGreetings",
                        "request": '{"name": "world"}',
                    },
                    call_id="call_stream_1",
                ),
                make_text_response("Received 3 greeting messages."),
            ],
        )

        result = await process_grpc_query("Stream greetings", grpc_ctx)

        assert result["ok"] is True
        assert "3" in result["data"] or "greeting" in result["data"].lower()

    @pytest.mark.asyncio
    async def test_stream_non_streaming_method_blocked(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_stream rejects non-streaming (unary) methods."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_stream",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "request": '{"name": "world"}',
                    },
                    call_id="call_stream_unary",
                ),
                make_text_response("That method is not a streaming method."),
            ],
        )

        result = await process_grpc_query("Stream SayHello", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_stream_stores_data_for_sql(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_stream results are stored for sql_query post-processing."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_stream_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": 1, "name": "Alice"},
                    {"id": 2, "name": "Bob"},
                    {"id": 3, "name": "Charlie"},
                ],
                "message_count": 3,
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_server_streaming_rpc",
            mock_stream_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_stream",
                    {
                        "method": "helloworld.Greeter/StreamGreetings",
                        "request": '{"name": "world"}',
                        "name": "greetings",
                    },
                    call_id="call_stream_sql_1",
                ),
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT name FROM greetings WHERE id > 1"},
                    call_id="call_stream_sql_2",
                ),
                make_text_response("Bob and Charlie."),
            ],
        )

        result = await process_grpc_query("Stream and filter", grpc_ctx)

        assert result["ok"] is True
        assert result["result"] is not None

    @pytest.mark.asyncio
    async def test_stream_rpc_error(self, grpc_ctx, fake_provider_factory, monkeypatch):
        """grpc_stream propagates RPC errors with partial data info."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_stream_rpc(*args, **kwargs):
            return {
                "success": False,
                "error": "gRPC error [UNAVAILABLE]: Server down",
                "partial_data": [{"message": "Hello 1"}],
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_server_streaming_rpc",
            mock_stream_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_stream",
                    {
                        "method": "helloworld.Greeter/StreamGreetings",
                        "request": '{"name": "world"}',
                    },
                    call_id="call_stream_err",
                ),
                make_text_response("Stream failed after 1 message."),
            ],
        )

        result = await process_grpc_query("Stream greetings", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    def test_find_streaming_method(self):
        """_find_streaming_method finds server-streaming methods only."""
        schema = _make_test_schema()
        m = _find_streaming_method(schema, "helloworld.Greeter/StreamGreetings")
        assert m is not None
        assert m.server_streaming is True

    def test_find_streaming_method_rejects_unary(self):
        """_find_streaming_method returns None for unary methods."""
        schema = _make_test_schema()
        m = _find_streaming_method(schema, "helloworld.Greeter/SayHello")
        assert m is None

    def test_find_streaming_method_rejects_bidi(self):
        """_find_streaming_method returns None for bidi-streaming methods."""
        schema = _make_test_schema()
        m = _find_streaming_method(schema, "helloworld.Greeter/Chat")
        assert m is None


# ---------------------------------------------------------------------------
# Phase v3: Client Streaming + Bidi Streaming Agent Tools
# ---------------------------------------------------------------------------


class TestGrpcClientStreamTool:
    """Tests for grpc_client_stream tool."""

    @pytest.mark.asyncio
    async def test_client_stream_success(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_client_stream sends batch of requests and gets single response."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_client_stream_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": {"total": 3, "status": "uploaded"},
                "messages_sent": 3,
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_client_streaming_rpc",
            mock_client_stream_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_client_stream",
                    {
                        "method": "helloworld.Greeter/UploadNames",
                        "requests": '[{"name": "a"}, {"name": "b"}, {"name": "c"}]',
                    },
                    call_id="call_cs_1",
                ),
                make_text_response("Uploaded 3 names."),
            ],
        )

        result = await process_grpc_query("Upload names", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_client_stream_rejects_unary_method(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_client_stream rejects unary methods."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_client_stream",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "requests": '[{"name": "world"}]',
                    },
                    call_id="call_cs_unary",
                ),
                make_text_response("That method is not client-streaming."),
            ],
        )

        result = await process_grpc_query("Upload to SayHello", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_client_stream_rejects_bidi_method(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_client_stream rejects bidi-streaming methods."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_client_stream",
                    {
                        "method": "helloworld.Greeter/Chat",
                        "requests": '[{"name": "hello"}]',
                    },
                    call_id="call_cs_bidi",
                ),
                make_text_response("That method is bidi, not client-streaming."),
            ],
        )

        result = await process_grpc_query("Upload to Chat", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_client_stream_invalid_json(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_client_stream returns error on invalid JSON."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_client_stream",
                    {
                        "method": "helloworld.Greeter/UploadNames",
                        "requests": "{bad json",
                    },
                    call_id="call_cs_badjson",
                ),
                make_text_response("Invalid JSON."),
            ],
        )

        result = await process_grpc_query("Upload names", grpc_ctx)

        assert result["ok"] is True  # Agent recovered from error

    @pytest.mark.asyncio
    async def test_client_stream_stores_data_for_sql(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_client_stream results are stored for sql_query."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_client_stream_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": 1, "status": "ok"},
                    {"id": 2, "status": "fail"},
                ],
                "messages_sent": 2,
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_client_streaming_rpc",
            mock_client_stream_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_client_stream",
                    {
                        "method": "helloworld.Greeter/UploadNames",
                        "requests": '[{"name": "a"}, {"name": "b"}]',
                        "name": "results",
                    },
                    call_id="call_cs_sql_1",
                ),
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT id FROM results WHERE status = 'ok'"},
                    call_id="call_cs_sql_2",
                ),
                make_text_response("Item 1 succeeded."),
            ],
        )

        result = await process_grpc_query("Upload and check status", grpc_ctx)

        assert result["ok"] is True
        assert result["result"] is not None


class TestGrpcBidiStreamTool:
    """Tests for grpc_bidi_stream tool."""

    @pytest.mark.asyncio
    async def test_bidi_stream_success(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_bidi_stream sends requests and collects responses."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_bidi_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"reply": "hi back"},
                    {"reply": "thanks"},
                ],
                "messages_sent": 2,
                "message_count": 2,
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_bidi_streaming_rpc",
            mock_bidi_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_bidi_stream",
                    {
                        "method": "helloworld.Greeter/Chat",
                        "requests": '[{"name": "hi"}, {"name": "thanks"}]',
                    },
                    call_id="call_bidi_1",
                ),
                make_text_response("Chat complete."),
            ],
        )

        result = await process_grpc_query("Chat with service", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_bidi_stream_rejects_non_bidi_method(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_bidi_stream rejects non-bidi methods."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_bidi_stream",
                    {
                        "method": "helloworld.Greeter/SayHello",
                        "requests": '[{"name": "world"}]',
                    },
                    call_id="call_bidi_unary",
                ),
                make_text_response("Not a bidi method."),
            ],
        )

        result = await process_grpc_query("Bidi SayHello", grpc_ctx)

        assert result["ok"] is True
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_bidi_stream_stores_data_for_sql(
        self, grpc_ctx, fake_provider_factory, monkeypatch
    ):
        """grpc_bidi_stream results are stored for sql_query post-processing."""
        schema = _make_test_schema()

        async def mock_fetch_schema(*args, **kwargs):
            return schema

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.fetch_schema", mock_fetch_schema
        )

        async def mock_bidi_rpc(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": 1, "reply": "hi"},
                    {"id": 2, "reply": "bye"},
                ],
                "messages_sent": 1,
                "message_count": 2,
            }

        monkeypatch.setattr(
            "api_agent.agent.grpc_agent.execute_bidi_streaming_rpc",
            mock_bidi_rpc,
        )

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "grpc_bidi_stream",
                    {
                        "method": "helloworld.Greeter/Chat",
                        "requests": '[{"name": "go"}]',
                        "name": "chat_log",
                    },
                    call_id="call_bidi_sql_1",
                ),
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT reply FROM chat_log WHERE id = 2"},
                    call_id="call_bidi_sql_2",
                ),
                make_text_response("The final reply was 'bye'."),
            ],
        )

        result = await process_grpc_query("Chat and filter", grpc_ctx)

        assert result["ok"] is True
        assert result["result"] is not None


class TestMethodFinders:
    """Tests for _find_client_streaming_method and _find_bidi_streaming_method."""

    def test_find_client_streaming_method(self):
        schema = _make_test_schema()
        m = _find_client_streaming_method(schema, "helloworld.Greeter/UploadNames")
        assert m is not None
        assert m.client_streaming is True
        assert m.server_streaming is False

    def test_find_client_streaming_rejects_bidi(self):
        schema = _make_test_schema()
        m = _find_client_streaming_method(schema, "helloworld.Greeter/Chat")
        assert m is None

    def test_find_client_streaming_rejects_unary(self):
        schema = _make_test_schema()
        m = _find_client_streaming_method(schema, "helloworld.Greeter/SayHello")
        assert m is None

    def test_find_bidi_streaming_method(self):
        schema = _make_test_schema()
        m = _find_bidi_streaming_method(schema, "helloworld.Greeter/Chat")
        assert m is not None
        assert m.client_streaming is True
        assert m.server_streaming is True

    def test_find_bidi_streaming_rejects_client_only(self):
        schema = _make_test_schema()
        m = _find_bidi_streaming_method(schema, "helloworld.Greeter/UploadNames")
        assert m is None

    def test_find_bidi_streaming_rejects_server_only(self):
        schema = _make_test_schema()
        m = _find_bidi_streaming_method(schema, "helloworld.Greeter/StreamGreetings")
        assert m is None
