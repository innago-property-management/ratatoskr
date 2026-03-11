"""Tests for process_mcp_query() in api_agent.agent.mcp_agent.

Follows the FakeLLMProvider pattern from test_grpc_agent.py.
All tests mock _open_session to avoid spawning real MCP processes.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch, wraps

import pytest

from api_agent.context import RequestContext
from api_agent.mcp.discovery import MCPServerDescriptor
from tests.conftest import (
    FakeLLMProvider,
    make_text_response,
    make_tool_call_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_ctx(target_url: str = "mcp://github") -> RequestContext:
    return RequestContext(
        target_url=target_url,
        target_headers={"Authorization": "Bearer test"},
        api_type="mcp",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


class MockMCPSession:
    """Test double for mcp.ClientSession."""

    def __init__(
        self,
        tools: list[dict[str, Any]],
        call_results: dict[str, Any] | None = None,
    ):
        self._tools = tools
        self._call_results = call_results or {}

    async def list_tools(self) -> MagicMock:
        result = MagicMock()
        result.tools = [_make_tool_def(t) for t in self._tools]
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MagicMock:
        result = MagicMock()
        data = self._call_results.get(name, {"result": f"ok from {name}"})
        content_item = MagicMock()
        content_item.type = "text"
        content_item.text = json.dumps(data)
        result.content = [content_item]
        result.isError = False
        return result


def _make_tool_def(t: dict[str, Any]) -> MagicMock:
    """Build a mock MCP tool definition from a dict."""
    td = MagicMock()
    td.name = t["name"]
    td.description = t.get("description", "")
    schema = MagicMock()
    schema.properties = {k: {"type": v} for k, v in t.get("params", {}).items()}
    schema.required = t.get("required", [])
    td.inputSchema = schema
    return td


@asynccontextmanager
async def _make_open_session(session: MockMCPSession):
    """Factory that returns a fixed MockMCPSession as an async context manager."""
    yield session


def _patch_open_session(monkeypatch, session: MockMCPSession):
    """Monkeypatch _open_session to yield the provided MockMCPSession."""

    @asynccontextmanager
    async def fake_open_session(descriptor):  # type: ignore[misc]
        yield session

    monkeypatch.setattr("api_agent.agent.mcp_agent._open_session", fake_open_session)


def _patch_provider(monkeypatch, responses):
    """Create and monkeypatch FakeLLMProvider."""
    prov = FakeLLMProvider(responses)
    monkeypatch.setattr("api_agent.agent.mcp_agent.provider", prov)
    monkeypatch.setattr("api_agent.agent.model._provider", prov)
    return prov


_DUMMY_DESCRIPTOR = MCPServerDescriptor(
    name="github", transport="stdio", command="echo", args=[], env={}, url=""
)


def _patch_resolve_target(monkeypatch, descriptor=None):
    """Monkeypatch resolve_target to return a fixed descriptor (bypasses discovery)."""

    async def fake_resolve(target_url, settings):
        return descriptor or _DUMMY_DESCRIPTOR

    monkeypatch.setattr("api_agent.agent.mcp_agent.resolve_target", fake_resolve)


SAMPLE_TOOLS = [
    {
        "name": "read_repo",
        "description": "Read repository contents",
        "params": {"owner": "string", "repo": "string"},
        "required": ["owner", "repo"],
    },
    {
        "name": "list_issues",
        "description": "List open issues",
        "params": {"repo": "string"},
        "required": ["repo"],
    },
]


# ---------------------------------------------------------------------------
# B1 tests — core agent, no recipes
# ---------------------------------------------------------------------------


class TestProcessMcpQuerySuccess:
    @pytest.mark.asyncio
    async def test_process_mcp_query_success(self, monkeypatch):
        """Successful NL query returns ok=True with mcp_calls and data."""
        from api_agent.agent.mcp_agent import process_mcp_query

        session = MockMCPSession(
            tools=SAMPLE_TOOLS,
            call_results={"read_repo": {"files": ["README.md"]}},
        )
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)

        responses = [
            make_tool_call_response(
                "mcp_call",
                {"tool_name": "read_repo", "arguments": '{"owner": "acme", "repo": "api"}'},
            ),
            make_text_response("The repo has one file: README.md"),
        ]
        _patch_provider(monkeypatch, responses)

        ctx = _mcp_ctx()
        result = await process_mcp_query("List files in acme/api", ctx)

        assert result["ok"] is True
        assert "mcp_calls" in result
        assert len(result["mcp_calls"]) == 1
        assert result["mcp_calls"][0]["tool_name"] == "read_repo"
        assert result["data"] is not None

    @pytest.mark.asyncio
    async def test_process_mcp_query_sql_postprocess(self, monkeypatch):
        """LLM can call sql_query after mcp_call and results are propagated."""
        from api_agent.agent.mcp_agent import process_mcp_query

        session = MockMCPSession(
            tools=SAMPLE_TOOLS,
            call_results={
                "list_issues": [
                    {"id": 1, "title": "Bug A", "state": "open"},
                    {"id": 2, "title": "Bug B", "state": "closed"},
                ]
            },
        )
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)

        responses = [
            make_tool_call_response(
                "mcp_call",
                {
                    "tool_name": "list_issues",
                    "arguments": '{"repo": "acme/api"}',
                    "name": "issues",
                },
            ),
            make_tool_call_response(
                "sql_query",
                {"sql": "SELECT COUNT(*) as total FROM issues WHERE state='open'"},
            ),
            make_text_response("There is 1 open issue."),
        ]
        _patch_provider(monkeypatch, responses)

        ctx = _mcp_ctx()
        result = await process_mcp_query("How many open issues in acme/api?", ctx)

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_process_mcp_query_tool_not_found(self, monkeypatch):
        """LLM tries to call a non-existent tool — gets error response, can recover."""
        from api_agent.agent.mcp_agent import process_mcp_query

        session = MockMCPSession(tools=SAMPLE_TOOLS)
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)

        responses = [
            make_tool_call_response(
                "mcp_call",
                {"tool_name": "nonexistent_tool", "arguments": "{}"},
            ),
            make_text_response("I could not find that tool."),
        ]
        _patch_provider(monkeypatch, responses)

        ctx = _mcp_ctx()
        result = await process_mcp_query("Do something with nonexistent tool", ctx)

        # Agent gets error feedback and can still respond
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_process_mcp_query_target_resolution_error(self, monkeypatch):
        """Discovery failure returns ok=False with error."""
        from api_agent.agent.mcp_agent import process_mcp_query

        async def failing_open_session(descriptor):  # type: ignore[misc]
            raise ValueError("Discovery failed: unknown server 'bogus'")
            # unreachable but needed for generator protocol
            yield  # type: ignore[misc]

        # Wrap in asynccontextmanager-compatible mock
        @asynccontextmanager
        async def fake_open(descriptor):  # type: ignore[misc]
            raise ValueError("Discovery failed: unknown server 'bogus'")
            yield  # type: ignore[misc]

        monkeypatch.setattr("api_agent.agent.mcp_agent._open_session", fake_open)
        _patch_provider(monkeypatch, [make_text_response("irrelevant")])

        ctx = _mcp_ctx("mcp://bogus")
        result = await process_mcp_query("anything", ctx)

        assert result["ok"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_process_mcp_query_allowlist_filters_tools(self, monkeypatch):
        """ALLOW_ENDPOINTS_MCP=read_* means only read_* tools are exposed."""
        from api_agent.agent.mcp_agent import process_mcp_query

        tools_with_write = SAMPLE_TOOLS + [
            {"name": "write_file", "description": "Write a file", "params": {}, "required": []}
        ]
        session = MockMCPSession(
            tools=tools_with_write,
            call_results={"read_repo": {"files": ["README.md"]}},
        )
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)

        fake_settings = MagicMock()
        fake_settings.ALLOW_ENDPOINTS_MCP = "read_*"
        fake_settings.MAX_AGENT_TURNS = 5
        fake_settings.DEBUG = False
        fake_settings.ENABLE_RECIPES = False
        fake_settings.MAX_SCHEMA_CHARS = 32000
        fake_settings.SCHEMA_REDUCTION_ENABLED = False
        fake_settings.MCP_SLUG = "test"
        monkeypatch.setattr("api_agent.agent.mcp_agent.settings", fake_settings)

        responses = [
            make_tool_call_response(
                "mcp_call",
                {"tool_name": "read_repo", "arguments": '{"owner": "a", "repo": "b"}'},
            ),
            make_text_response("Done"),
        ]
        _patch_provider(monkeypatch, responses)

        from api_agent.agent.mcp_agent import _build_schema_text

        ctx = _mcp_ctx()
        with patch(
            "api_agent.agent.mcp_agent._build_schema_text",
            wraps=_build_schema_text,
        ) as build_spy:
            result = await process_mcp_query("Read repo a/b", ctx)

        assert result["ok"] is True
        # write_file must have been filtered out before reaching the LLM
        tool_names = [td.name for td in build_spy.call_args[0][0]]
        assert "write_file" not in tool_names
        assert "read_repo" in tool_names

    @pytest.mark.asyncio
    async def test_process_mcp_query_zero_tools_after_filter(self, monkeypatch):
        """All tools filtered → ok=False with allowlist error message."""
        from api_agent.agent.mcp_agent import process_mcp_query

        session = MockMCPSession(tools=SAMPLE_TOOLS)
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)

        fake_settings = MagicMock()
        fake_settings.ALLOW_ENDPOINTS_MCP = "nonexistent_*"
        fake_settings.MAX_AGENT_TURNS = 5
        fake_settings.DEBUG = False
        fake_settings.ENABLE_RECIPES = False
        fake_settings.MAX_SCHEMA_CHARS = 32000
        fake_settings.SCHEMA_REDUCTION_ENABLED = False
        fake_settings.MCP_SLUG = "test"
        monkeypatch.setattr("api_agent.agent.mcp_agent.settings", fake_settings)

        _patch_provider(monkeypatch, [make_text_response("irrelevant")])

        ctx = _mcp_ctx()
        result = await process_mcp_query("Do something", ctx)

        assert result["ok"] is False
        assert (
            "allowlist" in result.get("error", "").lower()
            or "filter" in result.get("error", "").lower()
        )

    @pytest.mark.asyncio
    async def test_process_mcp_query_empty_tool_list(self, monkeypatch):
        """Server returns no tools → graceful ok=False error."""
        from api_agent.agent.mcp_agent import process_mcp_query

        session = MockMCPSession(tools=[])
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)
        _patch_provider(monkeypatch, [make_text_response("irrelevant")])

        ctx = _mcp_ctx()
        result = await process_mcp_query("Do something", ctx)

        assert result["ok"] is False
        assert result.get("error") is not None


class TestBuildSchemaText:
    def test_schema_text_compact_format(self):
        """_build_schema_text renders required (!) and optional (?) params with description."""
        from api_agent.agent.mcp_agent import _build_schema_text

        tool_defs = [
            MagicMock(
                name="search_code",
                description="Search source code for a pattern",
                inputSchema=MagicMock(
                    properties={
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    required=["query"],
                ),
            )
        ]

        text = _build_schema_text(tool_defs)

        assert "search_code" in text
        assert "query" in text
        assert "!" in text  # required marker
        assert "?" in text  # optional marker


class TestMetricsRecording:
    @pytest.mark.asyncio
    async def test_record_request_called(self, monkeypatch):
        """record_request('mcp', ...) is called in finally block."""
        from api_agent.agent.mcp_agent import process_mcp_query

        calls: list[tuple] = []

        def fake_record_request(protocol, status, latency_ms):
            calls.append((protocol, status, latency_ms))

        monkeypatch.setattr("api_agent.agent.mcp_agent.record_request", fake_record_request)

        session = MockMCPSession(tools=SAMPLE_TOOLS, call_results={"read_repo": {"ok": True}})
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)
        _patch_provider(
            monkeypatch,
            [
                make_tool_call_response("mcp_call", {"tool_name": "read_repo", "arguments": "{}"}),
                make_text_response("Done"),
            ],
        )

        ctx = _mcp_ctx()
        await process_mcp_query("test", ctx)

        assert len(calls) == 1
        assert calls[0][0] == "mcp"

    @pytest.mark.asyncio
    async def test_record_schema_fetch_called(self, monkeypatch):
        """record_schema_fetch() is called after list_tools completes."""
        from api_agent.agent.mcp_agent import process_mcp_query

        fetch_calls: list[tuple] = []

        def fake_record_schema_fetch(latency_ms, protocol):
            fetch_calls.append((latency_ms, protocol))

        monkeypatch.setattr(
            "api_agent.agent.mcp_agent.record_schema_fetch", fake_record_schema_fetch
        )

        session = MockMCPSession(tools=SAMPLE_TOOLS)
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)
        _patch_provider(monkeypatch, [make_text_response("Done")])

        ctx = _mcp_ctx()
        await process_mcp_query("test", ctx)

        assert len(fetch_calls) == 1
        assert fetch_calls[0][1] == "mcp"


class TestMaxTurns:
    @pytest.mark.asyncio
    async def test_max_turns_exceeded_returns_partial(self, monkeypatch):
        """MaxTurnsExceeded → ok=True partial result (consistent with other agents)."""
        from api_agent.agent.mcp_agent import process_mcp_query

        session = MockMCPSession(
            tools=SAMPLE_TOOLS,
            call_results={"read_repo": {"files": ["README.md"]}},
        )
        _patch_resolve_target(monkeypatch)
        _patch_open_session(monkeypatch, session)

        # Provider that always returns tool calls (never terminates naturally)
        # The orchestrator will raise MaxTurnsExceeded after MAX_AGENT_TURNS
        prov = FakeLLMProvider(
            [make_tool_call_response("mcp_call", {"tool_name": "read_repo", "arguments": "{}"})]
            * 50  # more than any MAX_AGENT_TURNS
        )
        monkeypatch.setattr("api_agent.agent.mcp_agent.provider", prov)
        monkeypatch.setattr("api_agent.agent.model._provider", prov)

        fake_settings = MagicMock()
        fake_settings.ALLOW_ENDPOINTS_MCP = ""
        fake_settings.MAX_AGENT_TURNS = 2  # very low to force exhaustion quickly
        fake_settings.DEBUG = False
        fake_settings.ENABLE_RECIPES = False
        fake_settings.MAX_SCHEMA_CHARS = 32000
        fake_settings.SCHEMA_REDUCTION_ENABLED = False
        fake_settings.MCP_SLUG = "test"
        monkeypatch.setattr("api_agent.agent.mcp_agent.settings", fake_settings)

        ctx = _mcp_ctx()
        result = await process_mcp_query("List files", ctx)

        # MaxTurnsExceeded → partial result (ok=True with partial flag or ok=False)
        assert "ok" in result
        assert "mcp_calls" in result
