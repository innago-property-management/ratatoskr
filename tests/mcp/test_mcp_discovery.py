"""Tests for api_agent/mcp/discovery.py — server discovery and target resolution."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api_agent.mcp.discovery import (
    _DISCOVERY_CACHE,
    MCPServerDescriptor,
    _parse_discovery_output,
    _run_command,
    discover_servers,
    resolve_target,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Ensure discovery cache is clean before and after every test."""
    _DISCOVERY_CACHE.clear()
    yield
    _DISCOVERY_CACHE.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subprocess_result(stdout: str, returncode: int = 0):
    """Return a mock asyncio.subprocess.Process result."""
    mock = AsyncMock()
    mock.returncode = returncode
    mock.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    return mock


# ---------------------------------------------------------------------------
# _parse_discovery_output unit tests
# ---------------------------------------------------------------------------


def test_parse_discovery_output_root_array():
    """Root-level JSON array is accepted directly."""
    raw = json.dumps(
        [
            {
                "name": "github",
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@mcp/github"],
                "env": {},
            }
        ]
    )
    result = _parse_discovery_output(raw)
    assert len(result) == 1
    assert result[0]["name"] == "github"


def test_parse_discovery_mcp_proxy_format():
    """mcp-proxy wraps in upstreamServers key."""
    raw = json.dumps(
        {
            "upstreamServers": [
                {"name": "github", "type": "stdio", "command": "npx", "args": [], "env": {}}
            ]
        }
    )
    result = _parse_discovery_output(raw)
    assert len(result) == 1
    assert result[0]["name"] == "github"


def test_parse_discovery_mcp_bridge_format():
    """mcp-bridge uses localServers key."""
    raw = json.dumps(
        {
            "localServers": [
                {"name": "fs", "type": "stdio", "command": "node", "args": ["server.js"], "env": {}}
            ]
        }
    )
    result = _parse_discovery_output(raw)
    assert len(result) == 1
    assert result[0]["name"] == "fs"


def test_parse_discovery_invalid_json_raises():
    """Garbage input raises ValueError."""
    with pytest.raises(ValueError, match="[Ii]nvalid"):
        _parse_discovery_output("not json at all")


# ---------------------------------------------------------------------------
# discover_servers tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_stdio_server():
    """discover_servers normalizes stdio descriptor from subprocess output."""

    output = json.dumps(
        [
            {
                "name": "github",
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "tok"},
            }
        ]
    )

    with patch("api_agent.mcp.discovery._run_command", AsyncMock(return_value=output)):
        servers = await discover_servers("mcp-proxy list")

    assert len(servers) == 1
    s = servers[0]
    assert isinstance(s, MCPServerDescriptor)
    assert s.name == "github"
    assert s.transport == "stdio"
    assert s.command == "npx"
    assert s.args == ["-y", "@modelcontextprotocol/server-github"]
    assert s.env == {"GITHUB_TOKEN": "tok"}
    assert s.url == ""


@pytest.mark.asyncio
async def test_discover_sse_server():
    """discover_servers normalizes SSE descriptor."""

    output = json.dumps([{"name": "remote", "type": "sse", "url": "http://localhost:3001/mcp"}])

    with patch("api_agent.mcp.discovery._run_command", AsyncMock(return_value=output)):
        servers = await discover_servers("mcp-proxy list")

    assert len(servers) == 1
    s = servers[0]
    assert s.transport == "sse"
    assert s.url == "http://localhost:3001/mcp"
    assert s.command == ""


@pytest.mark.asyncio
async def test_discovery_caching():
    """discover_servers caches results — command runs only once."""

    output = json.dumps([{"name": "x", "type": "stdio", "command": "cmd", "args": [], "env": {}}])
    mock_run = AsyncMock(return_value=output)

    with patch("api_agent.mcp.discovery._run_command", mock_run):
        await discover_servers("mcp-proxy list")
        await discover_servers("mcp-proxy list")

    assert mock_run.call_count == 1, "Command should only run once due to caching"


@pytest.mark.asyncio
async def test_discovery_invalid_json():
    """discover_servers raises ValueError on invalid JSON output."""

    with patch("api_agent.mcp.discovery._run_command", AsyncMock(return_value="garbage")):
        with pytest.raises(ValueError, match="[Ii]nvalid"):
            await discover_servers("mcp-proxy list")


@pytest.mark.asyncio
async def test_discovery_nonzero_exit():
    """discover_servers raises RuntimeError when command exits non-zero."""

    async def fail_run(command: str) -> str:
        raise RuntimeError(f"Command '{command}' exited with non-zero status")

    with patch("api_agent.mcp.discovery._run_command", fail_run):
        with pytest.raises(RuntimeError):
            await discover_servers("mcp-proxy list")


@pytest.mark.asyncio
async def test_run_command_timeout():
    """_run_command raises RuntimeError when subprocess exceeds timeout."""
    import asyncio

    async def slow_communicate():
        await asyncio.sleep(10)
        return (b"", b"")

    mock_proc = AsyncMock()
    mock_proc.communicate = slow_communicate
    mock_proc.kill = lambda: None
    mock_proc.wait = AsyncMock()

    with patch(
        "api_agent.mcp.discovery.asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)
    ):
        with pytest.raises(RuntimeError, match="exceeded timeout"):
            await _run_command("slow-cmd", timeout=0.1)


# ---------------------------------------------------------------------------
# resolve_target tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_named_target_stdio():
    """mcp://github resolves to a stdio MCPServerDescriptor via discovery."""

    descriptor = MCPServerDescriptor(
        name="github",
        transport="stdio",
        command="npx",
        args=["-y", "@mcp/github"],
        env={},
        url="",
    )
    _DISCOVERY_CACHE["mcp-proxy list"] = [descriptor]

    class FakeSettings:
        MCP_DISCOVERY_COMMAND = "mcp-proxy list"
        MCP_TARGET_TRANSPORT = ""
        MCP_TARGET_COMMAND = ""
        MCP_TARGET_ARGS = ""
        MCP_TARGET_ENV = "{}"

    result = await resolve_target("mcp://github", FakeSettings())
    assert result.name == "github"
    assert result.transport == "stdio"


@pytest.mark.asyncio
async def test_resolve_named_target_sse():
    """mcp://remote resolves to an SSE descriptor."""

    descriptor = MCPServerDescriptor(
        name="remote",
        transport="sse",
        command="",
        args=[],
        env={},
        url="http://localhost:3001/mcp",
    )
    _DISCOVERY_CACHE["mcp-proxy list"] = [descriptor]

    class FakeSettings:
        MCP_DISCOVERY_COMMAND = "mcp-proxy list"
        MCP_TARGET_TRANSPORT = ""
        MCP_TARGET_COMMAND = ""
        MCP_TARGET_ARGS = ""
        MCP_TARGET_ENV = "{}"

    result = await resolve_target("mcp://remote", FakeSettings())
    assert result.transport == "sse"
    assert result.url == "http://localhost:3001/mcp"


@pytest.mark.asyncio
async def test_resolve_http_target_direct():
    """http:// URLs become SSE descriptors without discovery lookup."""

    class FakeSettings:
        MCP_DISCOVERY_COMMAND = ""
        MCP_TARGET_TRANSPORT = ""
        MCP_TARGET_COMMAND = ""
        MCP_TARGET_ARGS = ""
        MCP_TARGET_ENV = "{}"

    result = await resolve_target("http://localhost:3001/mcp", FakeSettings())
    assert result.transport == "sse"
    assert result.url == "http://localhost:3001/mcp"


@pytest.mark.asyncio
async def test_resolve_unknown_name():
    """mcp://unknown raises ValueError when name not found in discovery."""

    _DISCOVERY_CACHE["mcp-proxy list"] = []

    class FakeSettings:
        MCP_DISCOVERY_COMMAND = "mcp-proxy list"
        MCP_TARGET_TRANSPORT = ""
        MCP_TARGET_COMMAND = ""
        MCP_TARGET_ARGS = ""
        MCP_TARGET_ENV = "{}"

    with pytest.raises(ValueError, match="unknown"):
        await resolve_target("mcp://unknown", FakeSettings())
