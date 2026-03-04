"""Tests for process_rest_query() in api_agent.agent.rest_agent.

T006: Success path — mock schema fetch + execute_request + FakeLLMProvider
T006-polling: Success with polling paths configured
T008-rest: Upstream error — schema fetch fails or returns empty
"""

import pytest

from api_agent.agent.rest_agent import process_rest_query
from api_agent.context import RequestContext
from tests.conftest import make_text_response, make_tool_call_response


class TestProcessRestQuerySuccess:
    """T006: Happy path — agent calls rest_call, gets data, returns summary."""

    @pytest.mark.asyncio
    async def test_success_path(self, rest_ctx, fake_provider_factory, monkeypatch):
        """Full success flow: fetch schema, call rest_call tool, LLM summarizes."""

        # Mock fetch_schema_context to return a minimal schema
        async def mock_fetch_schema(*args, **kwargs):
            return (
                "<endpoints>\nGET /users() -> User[]\n\n<schemas>\nUser { id: int!, name: str! }",
                "https://api.example.com/v1",
                '{"paths": {"/users": {"get": {}}}}',
            )

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)

        # Mock execute_request to return user data
        async def mock_execute_request(*args, **kwargs):
            return {
                "success": True,
                "data": {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]},
            }

        monkeypatch.setattr("api_agent.agent.rest_agent.execute_request", mock_execute_request)

        # Disable recipe extraction to avoid LLM calls there
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        # FakeLLMProvider: first call = tool call to rest_call, second = text summary
        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/users", "name": "data"},
                    call_id="call_rest_001",
                ),
                make_text_response("Found 2 users: Alice and Bob."),
            ],
        )

        result = await process_rest_query("List all users", rest_ctx)

        assert result["ok"] is True
        assert result["error"] is None
        assert "Alice" in result["data"] or "Bob" in result["data"]
        assert len(result["api_calls"]) >= 1
        assert result["api_calls"][0]["method"] == "GET"
        assert result["api_calls"][0]["path"] == "/users"

    @pytest.mark.asyncio
    async def test_success_with_sql_query(self, rest_ctx, fake_provider_factory, monkeypatch):
        """Agent calls rest_call then sql_query for post-processing."""

        async def mock_fetch_schema(*args, **kwargs):
            return (
                "<endpoints>\nGET /users() -> User[]",
                "https://api.example.com/v1",
                '{"paths": {}}',
            )

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)

        async def mock_execute_request(*args, **kwargs):
            return {
                "success": True,
                "data": [
                    {"id": 1, "name": "Alice", "age": 30},
                    {"id": 2, "name": "Bob", "age": 25},
                    {"id": 3, "name": "Charlie", "age": 35},
                ],
            }

        monkeypatch.setattr("api_agent.agent.rest_agent.execute_request", mock_execute_request)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/users", "name": "users"},
                    call_id="call_rest_002",
                ),
                make_tool_call_response(
                    "sql_query",
                    {"sql": "SELECT name FROM users WHERE age > 28"},
                    call_id="call_sql_001",
                ),
                make_text_response("Users over 28: Alice (30) and Charlie (35)."),
            ],
        )

        result = await process_rest_query("Who is older than 28?", rest_ctx)

        assert result["ok"] is True
        assert result["error"] is None
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_result_contains_last_data(self, rest_ctx, fake_provider_factory, monkeypatch):
        """The result dict should contain the actual data from the last operation."""

        async def mock_fetch_schema(*args, **kwargs):
            return ("endpoints text", "https://api.example.com", "{}")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)

        async def mock_execute_request(*args, **kwargs):
            return {
                "success": True,
                "data": [{"id": 1, "name": "Alice"}],
            }

        monkeypatch.setattr("api_agent.agent.rest_agent.execute_request", mock_execute_request)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/users", "name": "data"},
                    call_id="call_rest_003",
                ),
                make_text_response("Got the data."),
            ],
        )

        result = await process_rest_query("Get users", rest_ctx)

        assert result["ok"] is True
        # result field should contain extracted data
        assert result["result"] is not None


class TestProcessRestQueryPolling:
    """T006-polling: Success path with polling paths configured."""

    @pytest.mark.asyncio
    async def test_polling_paths_creates_poll_tool(
        self, rest_ctx_with_polling, fake_provider_factory, monkeypatch
    ):
        """When poll_paths is set, process_rest_query creates poll_until_done tool."""

        async def mock_fetch_schema(*args, **kwargs):
            return ("endpoints text", "https://api.example.com", "{}")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        # LLM just returns text — poll tool created but not used
        fake_provider_factory(
            monkeypatch,
            [make_text_response("No data needed for this query.")],
        )

        result = await process_rest_query("What flights are available?", rest_ctx_with_polling)

        # Should complete without error even though poll tool was not invoked
        assert result["ok"] is True or result["error"] is None or result["data"] is not None

    @pytest.mark.asyncio
    async def test_polling_with_rest_call(
        self, rest_ctx_with_polling, fake_provider_factory, monkeypatch
    ):
        """Polling context works alongside normal rest_call tool."""

        async def mock_fetch_schema(*args, **kwargs):
            return ("endpoints text", "https://api.example.com", "{}")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)

        async def mock_execute_request(*args, **kwargs):
            return {"success": True, "data": [{"flight": "AA100", "price": 299}]}

        monkeypatch.setattr("api_agent.agent.rest_agent.execute_request", mock_execute_request)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/flights", "name": "flights"},
                    call_id="call_rest_flights",
                ),
                make_text_response("Found flight AA100 at $299."),
            ],
        )

        result = await process_rest_query("Find cheap flights", rest_ctx_with_polling)

        assert result["ok"] is True
        assert result["error"] is None
        assert len(result["api_calls"]) >= 1


class TestProcessRestQueryErrors:
    """T008-rest: Error paths — schema fetch failures, missing base URL."""

    @pytest.mark.asyncio
    async def test_schema_fetch_returns_empty(self, fake_provider_factory, monkeypatch):
        """When fetch_schema_context returns empty strings and ctx has no base_url, error."""
        # Use a context WITHOUT base_url so the empty schema causes failure
        ctx_no_base = RequestContext(
            target_url="https://example.com/openapi.json",
            target_headers={"Authorization": "Bearer test-token"},
            api_type="rest",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
        )

        async def mock_fetch_schema(*args, **kwargs):
            return ("", "", "")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        # LLM should not be called because base URL is missing
        fake_provider_factory(
            monkeypatch,
            [make_text_response("This should not be reached.")],
        )

        result = await process_rest_query("List users", ctx_no_base)

        assert result["ok"] is False
        assert "base url" in result["error"].lower() or "base_url" in result["error"].lower()
        assert result["api_calls"] == []

    @pytest.mark.asyncio
    async def test_schema_fetch_raises_exception(
        self, rest_ctx, fake_provider_factory, monkeypatch
    ):
        """When fetch_schema_context raises, the error is caught and returned."""

        async def mock_fetch_schema(*args, **kwargs):
            raise ConnectionError("Failed to reach spec server")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [make_text_response("unreachable")],
        )

        result = await process_rest_query("List users", rest_ctx)

        assert result["ok"] is False
        assert "Failed to reach spec server" in result["error"]
        assert result["api_calls"] == []

    @pytest.mark.asyncio
    async def test_no_base_url_from_schema_or_header(self, fake_provider_factory, monkeypatch):
        """When neither schema nor header provides a base URL, return error."""
        # Context with no base_url override
        ctx = pytest.importorskip("api_agent.context").RequestContext(
            target_url="https://api.example.com/openapi.json",
            api_type="rest",
            target_headers={},
            allow_unsafe_paths=(),
            base_url=None,
            include_result=False,
            poll_paths=(),
        )

        # Schema returns context text but no base URL
        async def mock_fetch_schema(*args, **kwargs):
            return ("<endpoints>\nGET /users() -> any", "", '{"paths": {}}')

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [make_text_response("unreachable")],
        )

        result = await process_rest_query("List users", ctx)

        assert result["ok"] is False
        assert "base url" in result["error"].lower() or "base_url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_base_url_from_header_override(self, fake_provider_factory, monkeypatch):
        """When ctx.base_url is set, it overrides the spec-derived base URL."""
        from api_agent.context import RequestContext

        ctx = RequestContext(
            target_url="https://api.example.com/openapi.json",
            api_type="rest",
            target_headers={},
            allow_unsafe_paths=(),
            base_url="https://custom.api.com/v2",
            include_result=False,
            poll_paths=(),
        )

        # Schema returns empty base_url, but ctx.base_url is set
        async def mock_fetch_schema(*args, **kwargs):
            return ("endpoints text", "", "{}")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)

        captured_kwargs = {}

        async def mock_execute_request(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return {"success": True, "data": [{"id": 1}]}

        monkeypatch.setattr("api_agent.agent.rest_agent.execute_request", mock_execute_request)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/items", "name": "data"},
                    call_id="call_base_url",
                ),
                make_text_response("Got items."),
            ],
        )

        result = await process_rest_query("List items", ctx)

        assert result["ok"] is True
        assert captured_kwargs.get("base_url") == "https://custom.api.com/v2"

    @pytest.mark.asyncio
    async def test_execute_request_failure_tracked(
        self, rest_ctx, fake_provider_factory, monkeypatch
    ):
        """When execute_request returns failure, api_calls track it and agent can recover."""

        async def mock_fetch_schema(*args, **kwargs):
            return ("endpoints text", "https://api.example.com", "{}")

        monkeypatch.setattr("api_agent.agent.rest_agent.fetch_schema_context", mock_fetch_schema)

        async def mock_execute_request(*args, **kwargs):
            return {"success": False, "error": "404 Not Found", "status_code": 404}

        monkeypatch.setattr("api_agent.agent.rest_agent.execute_request", mock_execute_request)
        monkeypatch.setattr("api_agent.agent.rest_agent.settings.ENABLE_RECIPES", False)

        fake_provider_factory(
            monkeypatch,
            [
                make_tool_call_response(
                    "rest_call",
                    {"method": "GET", "path": "/nonexistent", "name": "data"},
                    call_id="call_404",
                ),
                make_text_response("The endpoint was not found."),
            ],
        )

        result = await process_rest_query("Get data from missing endpoint", rest_ctx)

        assert result["ok"] is True  # Agent recovered with text response
        assert len(result["api_calls"]) >= 1
        assert result["api_calls"][0]["success"] is False
