"""MCP server discovery — runs external commands and normalizes descriptors."""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class MCPServerDescriptor:
    """Normalized descriptor for an MCP server target.

    For stdio servers: command + args + env are set.
    For SSE/streamable-http servers: url is set.
    """

    name: str
    transport: str  # "stdio" or "sse"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""


# Module-level cache keyed by the discovery command string.
# Cleared between tests by callers (tests import and clear directly).
_DISCOVERY_CACHE: dict[str, list[MCPServerDescriptor]] = {}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


async def _run_command(command: str) -> str:
    """Run *command* in a subprocess and return stdout as a string.

    Raises:
        RuntimeError: if the process exits with a non-zero return code.
    """
    parts = shlex.split(command)
    proc = await asyncio.create_subprocess_exec(
        *parts,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command '{command}' exited with non-zero status {proc.returncode}: "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace")


def _parse_discovery_output(raw_json: str) -> list[dict[str, Any]]:
    """Parse JSON output from a discovery command into a list of raw server dicts.

    Supports three formats:
    - Root array: ``[{...}, ...]``
    - mcp-proxy: ``{"upstreamServers": [{...}, ...]}``
    - mcp-bridge: ``{"localServers": [{...}, ...]}``

    Raises:
        ValueError: if ``raw_json`` is not valid JSON or not a recognised shape.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from discovery command: {exc}") from exc

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("upstreamServers", "localServers"):
            if key in data and isinstance(data[key], list):
                return data[key]

    raise ValueError(
        f"Invalid discovery output shape — expected a JSON array or "
        f"object with 'upstreamServers'/'localServers' key, got: {type(data).__name__}"
    )


def _normalize_descriptor(raw: dict[str, Any]) -> MCPServerDescriptor:
    """Convert a raw dict from discovery output to an MCPServerDescriptor."""
    name = str(raw.get("name", ""))
    # Accept both "type" and "transport" keys (mcp-proxy uses "type")
    transport = str(raw.get("type") or raw.get("transport") or "stdio")
    # Normalise to "stdio" or "sse"
    if transport not in ("stdio", "sse"):
        transport = "sse" if transport in ("http", "https", "streamable-http") else "stdio"

    if transport == "stdio":
        return MCPServerDescriptor(
            name=name,
            transport="stdio",
            command=str(raw.get("command", "")),
            args=list(raw.get("args") or []),
            env=dict(raw.get("env") or {}),
            url="",
        )
    else:
        return MCPServerDescriptor(
            name=name,
            transport="sse",
            command="",
            args=[],
            env={},
            url=str(raw.get("url", "")),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def discover_servers(command: str) -> list[MCPServerDescriptor]:
    """Run *command* and return a list of MCPServerDescriptors.

    Results are cached per *command* string.  Clear ``_DISCOVERY_CACHE``
    between tests.

    Raises:
        RuntimeError: if the command exits non-zero.
        ValueError: if the output is not valid JSON or not a recognised format.
    """
    if command in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE[command]

    raw_output = await _run_command(command)
    raw_list = _parse_discovery_output(raw_output)
    descriptors = [_normalize_descriptor(entry) for entry in raw_list]
    _DISCOVERY_CACHE[command] = descriptors
    return descriptors


async def resolve_target(target_url: str, settings: Any) -> MCPServerDescriptor:
    """Convert an X-Target-URL value to an MCPServerDescriptor.

    Routing logic:
    - ``http://`` / ``https://`` — return a direct SSE descriptor (no lookup).
    - ``mcp://<name>`` — look up ``<name>`` in discovery results.
    - Empty / unrecognised scheme — use ``MCP_TARGET_COMMAND`` from settings.

    Args:
        target_url: Value of the ``X-Target-URL`` header.
        settings: Settings object with MCP_DISCOVERY_COMMAND etc.

    Raises:
        ValueError: if an ``mcp://`` name is not found in discovery results.
    """
    parsed = urlparse(target_url)

    # Direct HTTP/HTTPS target — SSE descriptor, no discovery needed
    if parsed.scheme in ("http", "https"):
        return MCPServerDescriptor(
            name=parsed.netloc,
            transport="sse",
            url=target_url,
        )

    # mcp:// scheme — look up by name
    if parsed.scheme == "mcp":
        server_name = parsed.netloc or parsed.path.lstrip("/")

        # Try discovery command first
        if settings.MCP_DISCOVERY_COMMAND:
            servers = await discover_servers(settings.MCP_DISCOVERY_COMMAND)
            for srv in servers:
                if srv.name == server_name:
                    return srv
            raise ValueError(
                f"MCP server '{server_name}' not found in discovery output "
                f"(command: '{settings.MCP_DISCOVERY_COMMAND}')"
            )

        # Fall back to direct config
        transport = settings.MCP_TARGET_TRANSPORT or "stdio"
        if transport == "stdio" and settings.MCP_TARGET_COMMAND:
            args_str = settings.MCP_TARGET_ARGS or ""
            args = [a.strip() for a in args_str.split(",") if a.strip()]
            env: dict[str, str] = {}
            try:
                env = json.loads(settings.MCP_TARGET_ENV or "{}")
            except json.JSONDecodeError:
                pass
            return MCPServerDescriptor(
                name=server_name,
                transport="stdio",
                command=settings.MCP_TARGET_COMMAND,
                args=args,
                env=env,
            )

        raise ValueError(
            f"MCP server '{server_name}' could not be resolved: "
            "no discovery command configured and no MCP_TARGET_COMMAND set"
        )

    # Empty / unrecognised scheme — fall back to direct config
    transport = settings.MCP_TARGET_TRANSPORT or "stdio"
    command = settings.MCP_TARGET_COMMAND or ""
    if not command:
        raise ValueError(
            "Cannot resolve MCP target: X-Target-URL is empty/invalid and "
            "MCP_TARGET_COMMAND is not configured"
        )
    args_str = settings.MCP_TARGET_ARGS or ""
    args = [a.strip() for a in args_str.split(",") if a.strip()]
    env = {}
    try:
        env = json.loads(settings.MCP_TARGET_ENV or "{}")
    except json.JSONDecodeError:
        pass
    return MCPServerDescriptor(
        name=target_url or "mcp",
        transport=transport,
        command=command,
        args=args,
        env=env,
    )
