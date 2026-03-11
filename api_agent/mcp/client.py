"""MCP session context managers for stdio and HTTP (streamable-http / SSE) transports."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def mcp_stdio_session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    """Async context manager for a stdio MCP session.

    Spawns the target process, opens a ClientSession, calls initialize(),
    and yields the ready session.  The process is terminated on context exit.

    Args:
        params: StdioServerParameters with command, args, env, etc.

    Yields:
        An initialised :class:`mcp.ClientSession`.
    """
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def mcp_http_session(url: str) -> AsyncIterator[ClientSession]:
    """Async context manager for an HTTP MCP session.

    Tries streamable-http first (MCP 1.0+), falls back to legacy SSE transport
    on network/connectivity errors only.

    Args:
        url: HTTP(S) URL of the MCP server endpoint.

    Yields:
        An initialised :class:`mcp.ClientSession`.
    """
    # Probe whether streamable-http transport works.  Only connectivity
    # errors trigger fallback to SSE — all other exceptions propagate.
    use_sse = False
    try:
        async with streamablehttp_client(url) as (_r, _w, _sid):
            pass  # transport opened successfully
    except (OSError, ConnectionError) as exc:
        logger.info(
            "streamablehttp_fallback_to_sse",
            url=url,
            error=str(exc),
        )
        use_sse = True

    if use_sse:
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        async with streamablehttp_client(url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
