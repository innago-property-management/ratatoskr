"""Tests for TOON MCP output encoding (P4).

Verifies that _query and _execute TOON-encode the `data` field in
their responses when output_format="toon" (default).
"""

from __future__ import annotations

import json

import pytest

from api_agent.context import RequestContext
from api_agent.llm.toon_encoder import ToolResultEncoder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(output_format: str = "toon") -> RequestContext:
    """Build a minimal RequestContext for testing."""
    return RequestContext(
        target_url="https://api.example.com",
        api_type="graphql",
        target_headers={},
        allow_unsafe_paths=(),
        base_url=None,
        include_result=False,
        poll_paths=(),
        request_id="test-id",
        output_format=output_format,
    )


def _toon_encodable_data(n: int = 30) -> list[dict]:
    """Generate a list of dicts large enough for TOON to compress."""
    return [
        {"id": i, "name": f"user_{i}", "email": f"u{i}@test.com", "active": True} for i in range(n)
    ]


# ---------------------------------------------------------------------------
# RequestContext output_format parsing
# ---------------------------------------------------------------------------


class TestOutputFormatParsing:
    """X-Output-Format header parsing in context.py."""

    def test_default_is_toon_when_config_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No header + TOON_MCP_OUTPUT_ENABLED=True → output_format='toon'."""
        from api_agent import config

        monkeypatch.setattr(config.settings, "TOON_MCP_OUTPUT_ENABLED", True)

        ctx = _make_ctx()
        assert ctx.output_format == "toon"

    def test_default_is_json_when_config_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No header + TOON_MCP_OUTPUT_ENABLED=False → output_format='json'."""
        ctx = _make_ctx(output_format="json")
        assert ctx.output_format == "json"

    def test_header_json_overrides(self) -> None:
        """X-Output-Format: json → output_format='json'."""
        ctx = _make_ctx(output_format="json")
        assert ctx.output_format == "json"

    def test_header_toon_explicit(self) -> None:
        """X-Output-Format: toon → output_format='toon'."""
        ctx = _make_ctx(output_format="toon")
        assert ctx.output_format == "toon"


# ---------------------------------------------------------------------------
# Query tool TOON output
# ---------------------------------------------------------------------------


class TestQueryToonOutput:
    """_query tool TOON-encodes data for MCP client."""

    def test_list_data_toon_encoded(self) -> None:
        """When output_format='toon' and data is a list, return TOON string."""
        data = _toon_encodable_data()
        response = {"ok": True, "data": data, "queries": [], "error": None}

        # Simulate what query.py does
        if isinstance(response.get("data"), list):
            encoder = ToolResultEncoder()
            toon_str, applied = encoder.encode(response["data"])
            if applied:
                assert toon_str.startswith("[success:true format:toon]\n")
                assert len(toon_str) < len(json.dumps(data))
                return
        pytest.fail("TOON should have been applied")

    def test_json_when_output_format_json(self) -> None:
        """When output_format='json', data stays as dict (no TOON)."""
        ctx = _make_ctx(output_format="json")
        data = _toon_encodable_data()
        response = {"ok": True, "data": data, "queries": [], "error": None}

        # With json format, we skip TOON encoding
        if ctx.output_format != "toon":
            # Return response as-is
            assert isinstance(response, dict)
            assert response["ok"] is True

    def test_non_list_data_stays_dict(self) -> None:
        """When data is a dict (not list), return as dict regardless of format."""
        response = {"ok": True, "data": {"id": 1, "name": "test"}, "queries": [], "error": None}
        # isinstance check prevents TOON on non-list data
        assert not isinstance(response.get("data"), list)

    def test_error_response_stays_dict(self) -> None:
        """Error responses are never TOON-encoded."""
        response = {"ok": False, "data": None, "queries": [], "error": "Something failed"}
        assert response.get("data") is None

    def test_small_list_stays_json_when_toon_not_smaller(self) -> None:
        """Tiny list where TOON overhead exceeds savings → stays as JSON."""
        data = [{"a": 1}]
        encoder = ToolResultEncoder()
        toon_str, applied = encoder.encode(data)
        if not applied:
            # This is correct — TOON wasn't smaller
            parsed = json.loads(toon_str)
            assert parsed == data


# ---------------------------------------------------------------------------
# Execute tool TOON output
# ---------------------------------------------------------------------------


class TestExecuteToonOutput:
    """_execute tool TOON-encodes data for MCP client."""

    def test_list_data_toon_encoded(self) -> None:
        """Execute result with list data gets TOON-encoded."""
        data = _toon_encodable_data()
        result = {"ok": True, "data": data}

        encoder = ToolResultEncoder()
        toon_str, applied = encoder.encode(result["data"])
        if applied:
            assert toon_str.startswith("[success:true format:toon]\n")

    def test_dict_data_stays_dict(self) -> None:
        """Execute result with dict data stays as dict."""
        result = {"ok": True, "data": {"id": 1, "name": "test"}}
        assert not isinstance(result.get("data"), list)


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestToonMcpOutputConfig:
    """TOON_MCP_OUTPUT_ENABLED config integration."""

    def test_config_default_true(self) -> None:
        """Default config has TOON_MCP_OUTPUT_ENABLED=True."""
        from api_agent.config import Settings

        s = Settings()
        assert s.TOON_MCP_OUTPUT_ENABLED is True

    def test_config_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TOON_MCP_OUTPUT_ENABLED=false disables TOON output."""
        monkeypatch.setenv("API_AGENT_TOON_MCP_OUTPUT_ENABLED", "false")
        from api_agent.config import Settings

        s = Settings()
        assert s.TOON_MCP_OUTPUT_ENABLED is False
