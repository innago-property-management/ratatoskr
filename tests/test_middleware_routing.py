"""Tests for middleware on_call_tool non-recipe path (T022).

Covers: tool name transformation, prefix validation, context forwarding.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware import MiddlewareContext
from mcp import types as mt
from mcp.types import TextContent

from api_agent.context import RequestContext
from api_agent.middleware import DynamicToolNamingMiddleware


def _make_call_context(tool_name: str, arguments: dict | None = None) -> MiddlewareContext:
    """Create a MiddlewareContext for on_call_tool tests."""
    message = mt.CallToolRequestParams(name=tool_name, arguments=arguments or {})
    return MiddlewareContext(message=message)


def _graphql_ctx() -> RequestContext:
    return RequestContext(
        target_url="https://api.example.com/graphql",
        api_type="graphql",
        target_headers={"Authorization": "Bearer token"},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
    )


class TestNonRecipeToolRouting:
    """Test on_call_tool for standard (non-recipe) tool calls."""

    @pytest.mark.asyncio
    async def test_transforms_prefixed_name_to_internal(self):
        """flights_query -> _query internal name."""
        middleware = DynamicToolNamingMiddleware()
        ctx = _graphql_ctx()
        context = _make_call_context("example_query", {"question": "list users"})

        captured_contexts = []

        async def call_next(modified_context):
            captured_contexts.append(modified_context)
            return [TextContent(type="text", text="ok")]

        with patch("api_agent.middleware.get_http_headers") as mock_headers:
            with patch("api_agent.middleware.get_request_context", return_value=ctx):
                with patch("api_agent.middleware.extract_api_name", return_value="example"):
                    mock_headers.return_value = {
                        "x-target-url": ctx.target_url,
                        "x-api-type": ctx.api_type,
                    }
                    await middleware.on_call_tool(context, call_next)

        assert len(captured_contexts) == 1
        modified = captured_contexts[0]
        assert modified.message.name == "_query"
        assert modified.message.arguments == {"question": "list users"}

    @pytest.mark.asyncio
    async def test_transforms_execute_tool_name(self):
        """myapi_execute -> _execute internal name."""
        middleware = DynamicToolNamingMiddleware()
        ctx = _graphql_ctx()
        context = _make_call_context("myapi_execute", {"query": "{ users { id } }"})

        captured = []

        async def call_next(modified_context):
            captured.append(modified_context)
            return [TextContent(type="text", text="ok")]

        with patch("api_agent.middleware.get_http_headers") as mock_headers:
            with patch("api_agent.middleware.get_request_context", return_value=ctx):
                with patch("api_agent.middleware.extract_api_name", return_value="myapi"):
                    mock_headers.return_value = {
                        "x-target-url": ctx.target_url,
                        "x-api-type": ctx.api_type,
                    }
                    await middleware.on_call_tool(context, call_next)

        assert captured[0].message.name == "_execute"

    @pytest.mark.asyncio
    async def test_rejects_wrong_prefix(self):
        """Tool name with wrong API prefix raises NotFoundError."""
        middleware = DynamicToolNamingMiddleware()
        ctx = _graphql_ctx()
        context = _make_call_context("wrong_prefix_query", {})

        async def call_next(modified_context):
            return [TextContent(type="text", text="ok")]

        with patch("api_agent.middleware.get_http_headers") as mock_headers:
            with patch("api_agent.middleware.get_request_context", return_value=ctx):
                with patch("api_agent.middleware.extract_api_name", return_value="correct_api"):
                    mock_headers.return_value = {
                        "x-target-url": ctx.target_url,
                        "x-api-type": ctx.api_type,
                    }
                    with pytest.raises(NotFoundError, match="not valid for API"):
                        await middleware.on_call_tool(context, call_next)

    @pytest.mark.asyncio
    async def test_preserves_arguments_through_transform(self):
        """Arguments are forwarded unchanged to the internal tool."""
        middleware = DynamicToolNamingMiddleware()
        ctx = _graphql_ctx()
        original_args = {"query": "{ users { id name } }", "variables": {"limit": 10}}
        context = _make_call_context("api_execute", original_args)

        captured = []

        async def call_next(modified_context):
            captured.append(modified_context)
            return [TextContent(type="text", text="ok")]

        with patch("api_agent.middleware.get_http_headers") as mock_headers:
            with patch("api_agent.middleware.get_request_context", return_value=ctx):
                with patch("api_agent.middleware.extract_api_name", return_value="api"):
                    mock_headers.return_value = {
                        "x-target-url": ctx.target_url,
                        "x-api-type": ctx.api_type,
                    }
                    await middleware.on_call_tool(context, call_next)

        assert captured[0].message.arguments == original_args

    @pytest.mark.asyncio
    async def test_no_http_context_passes_through(self):
        """Without HTTP headers (stdio transport), tool call passes through unchanged."""
        middleware = DynamicToolNamingMiddleware()
        context = _make_call_context("_query", {"question": "test"})

        captured = []

        async def call_next(ctx):
            captured.append(ctx)
            return [TextContent(type="text", text="ok")]

        with patch("api_agent.middleware.get_http_headers", side_effect=LookupError):
            await middleware.on_call_tool(context, call_next)

        # Should pass through unchanged (no HTTP context = no transformation)
        assert len(captured) == 1
        assert captured[0].message.name == "_query"

    @pytest.mark.asyncio
    async def test_multipart_api_name_prefix(self):
        """API name with underscores (e.g., flights_api) strips correctly."""
        middleware = DynamicToolNamingMiddleware()
        ctx = _graphql_ctx()
        context = _make_call_context("flights_api_query", {})

        captured = []

        async def call_next(modified_context):
            captured.append(modified_context)
            return [TextContent(type="text", text="ok")]

        with patch("api_agent.middleware.get_http_headers") as mock_headers:
            with patch("api_agent.middleware.get_request_context", return_value=ctx):
                with patch("api_agent.middleware.extract_api_name", return_value="flights_api"):
                    mock_headers.return_value = {
                        "x-target-url": ctx.target_url,
                        "x-api-type": ctx.api_type,
                    }
                    await middleware.on_call_tool(context, call_next)

        assert captured[0].message.name == "_query"


class TestGrpcMiddleware:
    """Test middleware gRPC-specific paths."""

    def test_inject_api_context_grpc_label(self):
        """gRPC api_type produces 'gRPC' label in description prefix."""
        from api_agent.middleware import _inject_api_context

        result = _inject_api_context("Query the API", "grpc.example.com", "grpc")
        assert "[grpc.example.com gRPC API]" in result
        assert "Query the API" in result

    @pytest.mark.asyncio
    async def test_on_list_tools_grpc_skips_schema_check(self):
        """gRPC sessions don't fail when load_schema_and_base_url returns empty."""
        # gRPC doesn't need upfront schema (reflection happens in agent)
        # So when load_schema_and_base_url returns ("",""), it should NOT raise RuntimeError
        from api_agent.middleware import DynamicToolNamingMiddleware

        middleware = DynamicToolNamingMiddleware()

        grpc_ctx = RequestContext(
            target_url="grpc://localhost:50051",
            api_type="grpc",
            target_headers={},
            allow_unsafe_paths=(),
            base_url=None,
            include_result=False,
            poll_paths=(),
        )

        # Simulate the on_list_tools path:
        # 1. call_next returns some tools
        # 2. get_http_headers returns headers
        # 3. get_request_context returns grpc_ctx
        # 4. load_schema_and_base_url returns ("", "") for gRPC
        # 5. Should NOT raise RuntimeError

        from fastmcp.tools.tool import Tool as FastMCPTool

        mock_tool = MagicMock(spec=FastMCPTool)
        mock_tool.name = "_query"
        mock_tool.description = "Query tool"
        mock_tool.model_copy = MagicMock(return_value=mock_tool)

        context = MagicMock()  # MiddlewareContext

        async def call_next(ctx):
            return [mock_tool]

        with patch(
            "api_agent.middleware.get_http_headers",
            return_value={
                "x-target-url": "grpc://localhost:50051",
                "x-api-type": "grpc",
            },
        ):
            with patch("api_agent.middleware.get_request_context", return_value=grpc_ctx):
                with patch("api_agent.middleware.load_schema_and_base_url", return_value=("", "")):
                    with patch("api_agent.middleware.extract_api_name", return_value="mygrpc"):
                        with patch(
                            "api_agent.middleware.get_full_hostname", return_value="localhost"
                        ):
                            with patch("api_agent.middleware._list_recipe_tools", return_value=[]):
                                # This should NOT raise - gRPC is exempt from schema check
                                result = await middleware.on_list_tools(context, call_next)

        assert len(result) >= 1
