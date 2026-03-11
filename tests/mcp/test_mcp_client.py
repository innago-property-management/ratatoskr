"""Tests for api_agent/mcp/client.py — MCP session context managers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_session(initialized: bool = True) -> AsyncMock:
    """Build a mock ClientSession whose initialize() is a coroutine."""
    session = AsyncMock()
    session.initialize = AsyncMock(return_value=None)
    return session


def make_async_cm(value: Any):
    """Wrap a value in an async context manager."""

    @asynccontextmanager
    async def _cm(*args, **kwargs):
        yield value

    return _cm


# ---------------------------------------------------------------------------
# Task A1 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_session_initializes():
    """mcp_stdio_session must call session.initialize() and yield the session."""
    fake_session = make_fake_session()

    # The real transport yields (read, write); ClientSession wraps them.
    # We patch at the module level so the implementation uses our mocks.
    @asynccontextmanager
    async def fake_stdio_client(params):
        read, write = MagicMock(), MagicMock()
        yield read, write

    @asynccontextmanager
    async def fake_client_session(read, write):
        yield fake_session

    with (
        patch("api_agent.mcp.client.stdio_client", fake_stdio_client),
        patch("api_agent.mcp.client.ClientSession", fake_client_session),
    ):
        from mcp.client.stdio import StdioServerParameters

        from api_agent.mcp.client import mcp_stdio_session

        params = StdioServerParameters(
            command="npx", args=["-y", "@modelcontextprotocol/server-github"]
        )
        async with mcp_stdio_session(params) as session:
            assert session is fake_session

    fake_session.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_session_prefers_streamablehttp():
    """mcp_http_session tries streamablehttp_client first."""
    fake_session = make_fake_session()

    @asynccontextmanager
    async def fake_streamablehttp_client(url):
        read, write = MagicMock(), MagicMock()
        get_session_id = MagicMock()
        yield read, write, get_session_id

    @asynccontextmanager
    async def fake_client_session(read, write):
        yield fake_session

    sse_called = []

    @asynccontextmanager
    async def fake_sse_client(url):
        sse_called.append(True)
        read, write = MagicMock(), MagicMock()
        yield read, write

    with (
        patch("api_agent.mcp.client.streamablehttp_client", fake_streamablehttp_client),
        patch("api_agent.mcp.client.sse_client", fake_sse_client),
        patch("api_agent.mcp.client.ClientSession", fake_client_session),
    ):
        from api_agent.mcp.client import mcp_http_session

        async with mcp_http_session("http://localhost:3001/mcp") as session:
            assert session is fake_session

    # SSE fallback should NOT have been called
    assert not sse_called, "sse_client was called even though streamablehttp succeeded"
    fake_session.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_session_falls_back_to_sse():
    """mcp_http_session falls back to sse_client when streamablehttp raises."""
    fake_session = make_fake_session()

    @asynccontextmanager
    async def failing_streamablehttp_client(url):
        raise ConnectionError("streamable-http not supported")
        yield  # noqa: F541 — unreachable, needed to make this a generator

    @asynccontextmanager
    async def fake_sse_client(url):
        read, write = MagicMock(), MagicMock()
        yield read, write

    @asynccontextmanager
    async def fake_client_session(read, write):
        yield fake_session

    with (
        patch("api_agent.mcp.client.streamablehttp_client", failing_streamablehttp_client),
        patch("api_agent.mcp.client.sse_client", fake_sse_client),
        patch("api_agent.mcp.client.ClientSession", fake_client_session),
    ):
        from api_agent.mcp.client import mcp_http_session

        async with mcp_http_session("http://localhost:3001/mcp") as session:
            assert session is fake_session

    fake_session.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_session_does_not_catch_non_connectivity_errors():
    """Non-connectivity errors (e.g. ValueError) propagate instead of falling back to SSE."""

    @asynccontextmanager
    async def bad_streamablehttp_client(url):
        raise ValueError("protocol mismatch")
        yield  # noqa: F541 — unreachable, needed to make this a generator

    sse_called = []

    @asynccontextmanager
    async def fake_sse_client(url):
        sse_called.append(True)
        yield MagicMock(), MagicMock()

    with (
        patch("api_agent.mcp.client.streamablehttp_client", bad_streamablehttp_client),
        patch("api_agent.mcp.client.sse_client", fake_sse_client),
    ):
        from api_agent.mcp.client import mcp_http_session

        with pytest.raises(ValueError, match="protocol mismatch"):
            async with mcp_http_session("http://localhost:3001/mcp"):
                pass

    assert not sse_called, "SSE fallback should NOT trigger for non-connectivity errors"


@pytest.mark.asyncio
async def test_session_cleanup_on_exception():
    """Context manager exits cleanly even when the body raises."""
    fake_session = make_fake_session()

    @asynccontextmanager
    async def fake_stdio_client(params):
        read, write = MagicMock(), MagicMock()
        yield read, write

    @asynccontextmanager
    async def fake_client_session(read, write):
        yield fake_session

    with (
        patch("api_agent.mcp.client.stdio_client", fake_stdio_client),
        patch("api_agent.mcp.client.ClientSession", fake_client_session),
    ):
        from mcp.client.stdio import StdioServerParameters

        from api_agent.mcp.client import mcp_stdio_session

        params = StdioServerParameters(command="echo", args=[])
        raised = False
        try:
            async with mcp_stdio_session(params):
                raise RuntimeError("simulated body error")
        except RuntimeError:
            raised = True

    assert raised, "RuntimeError should have propagated out of context manager"


@pytest.mark.asyncio
async def test_http_session_both_transports_fail():
    """SSE exception propagates when both transports fail."""

    @asynccontextmanager
    async def failing_streamablehttp(url):
        raise ConnectionError("no streamable-http")
        yield  # pragma: no cover

    @asynccontextmanager
    async def failing_sse(url):
        raise ConnectionError("no SSE either")
        yield  # pragma: no cover

    with (
        patch("api_agent.mcp.client.streamablehttp_client", failing_streamablehttp),
        patch("api_agent.mcp.client.sse_client", failing_sse),
    ):
        from api_agent.mcp.client import mcp_http_session

        with pytest.raises(ConnectionError, match="no SSE either"):
            async with mcp_http_session("http://localhost:3001/mcp"):
                pass


@pytest.mark.asyncio
async def test_http_body_oserror_propagates_not_falls_back():
    """OSError raised in session body must NOT trigger SSE fallback."""
    fake_session = make_fake_session()
    sse_called = []

    @asynccontextmanager
    async def fake_streamablehttp_client(url):
        yield MagicMock(), MagicMock(), MagicMock()

    @asynccontextmanager
    async def fake_sse_client(url):
        sse_called.append(True)
        yield MagicMock(), MagicMock()

    @asynccontextmanager
    async def fake_client_session(read, write):
        yield fake_session

    with (
        patch("api_agent.mcp.client.streamablehttp_client", fake_streamablehttp_client),
        patch("api_agent.mcp.client.sse_client", fake_sse_client),
        patch("api_agent.mcp.client.ClientSession", fake_client_session),
    ):
        from api_agent.mcp.client import mcp_http_session

        with pytest.raises(OSError, match="dropped"):
            async with mcp_http_session("http://localhost:3001/mcp"):
                raise OSError("dropped")

    assert not sse_called, "SSE fallback must not fire for body-originated errors"
