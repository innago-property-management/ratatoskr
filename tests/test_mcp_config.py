"""Tests for MCP facade config additions in api_agent/config.py."""

from __future__ import annotations

from api_agent.config import Settings

# ---------------------------------------------------------------------------
# MCP facade config defaults
# ---------------------------------------------------------------------------


def test_mcp_discovery_command_default_empty():
    """MCP_DISCOVERY_COMMAND defaults to empty string."""
    s = Settings()
    assert s.MCP_DISCOVERY_COMMAND == ""


def test_mcp_target_transport_default_empty():
    """MCP_TARGET_TRANSPORT defaults to empty string."""
    s = Settings()
    assert s.MCP_TARGET_TRANSPORT == ""


def test_mcp_target_command_default_empty():
    """MCP_TARGET_COMMAND defaults to empty string."""
    s = Settings()
    assert s.MCP_TARGET_COMMAND == ""


def test_mcp_target_args_default_empty():
    """MCP_TARGET_ARGS defaults to empty string."""
    s = Settings()
    assert s.MCP_TARGET_ARGS == ""


def test_mcp_target_env_default_json_object():
    """MCP_TARGET_ENV defaults to '{}' (empty JSON object string)."""
    s = Settings()
    assert s.MCP_TARGET_ENV == "{}"


def test_allow_endpoints_mcp_default_empty():
    """ALLOW_ENDPOINTS_MCP defaults to empty string."""
    s = Settings()
    assert s.ALLOW_ENDPOINTS_MCP == ""
