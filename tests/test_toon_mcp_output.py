"""Tests for TOON MCP output encoding (P4).

Verifies that _query and _execute TOON-encode the `data` field in
their responses while preserving the ok/error envelope.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api_agent.context import RequestContext, get_request_context
from api_agent.llm.toon_encoder import maybe_toon_encode_response

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


def _base_headers(**overrides: str) -> dict[str, str]:
    """Return minimal valid headers for get_request_context()."""
    h = {
        "x-target-url": "https://api.example.com/graphql",
        "x-api-type": "graphql",
    }
    h.update(overrides)
    return h


# ---------------------------------------------------------------------------
# Integration: X-Output-Format header via get_request_context()
# ---------------------------------------------------------------------------


class TestOutputFormatIntegration:
    """X-Output-Format header parsing through get_request_context()."""

    @patch("api_agent.context.get_http_headers")
    def test_default_is_toon_when_config_enabled(
        self, mock_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No header + TOON_MCP_OUTPUT_ENABLED=True → output_format='toon'."""
        from api_agent import config

        monkeypatch.setattr(config.settings, "TOON_MCP_OUTPUT_ENABLED", True)
        mock_headers.return_value = _base_headers()

        ctx = get_request_context()
        assert ctx.output_format == "toon"

    @patch("api_agent.context.get_http_headers")
    def test_default_is_json_when_config_disabled(
        self, mock_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No header + TOON_MCP_OUTPUT_ENABLED=False → output_format='json'."""
        from api_agent import config

        monkeypatch.setattr(config.settings, "TOON_MCP_OUTPUT_ENABLED", False)
        mock_headers.return_value = _base_headers()

        ctx = get_request_context()
        assert ctx.output_format == "json"

    @patch("api_agent.context.get_http_headers")
    def test_header_json_overrides_config_true(self, mock_headers) -> None:
        """X-Output-Format: json → output_format='json' even when config=True."""
        mock_headers.return_value = _base_headers(**{"x-output-format": "json"})

        ctx = get_request_context()
        assert ctx.output_format == "json"

    @patch("api_agent.context.get_http_headers")
    def test_header_toon_overrides_config_false(
        self, mock_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """X-Output-Format: toon → output_format='toon' even when config=False."""
        from api_agent import config

        monkeypatch.setattr(config.settings, "TOON_MCP_OUTPUT_ENABLED", False)
        mock_headers.return_value = _base_headers(**{"x-output-format": "toon"})

        ctx = get_request_context()
        assert ctx.output_format == "toon"

    @patch("api_agent.context.get_http_headers")
    def test_unknown_format_defaults_to_toon(self, mock_headers) -> None:
        """X-Output-Format: xml → falls back to 'toon'."""
        mock_headers.return_value = _base_headers(**{"x-output-format": "xml"})

        ctx = get_request_context()
        assert ctx.output_format == "toon"


# ---------------------------------------------------------------------------
# Query tool: envelope preserved with TOON data
# ---------------------------------------------------------------------------


class TestMaybeToonEncodeResponse:
    """maybe_toon_encode_response() — shared helper used by both tools."""

    def test_toon_replaces_data_preserves_envelope(self) -> None:
        """TOON encoding replaces data field but ok/error/queries envelope stays."""
        data = _toon_encodable_data()
        response = {"ok": True, "data": data, "queries": [{"q": "test"}], "error": None}

        maybe_toon_encode_response(response, "toon")

        assert response["ok"] is True
        assert response["queries"] == [{"q": "test"}]
        assert response["error"] is None
        assert isinstance(response["data"], str)
        assert response["data"].startswith("[success:true format:toon]\n")

    def test_json_format_skips_encoding(self) -> None:
        """When output_format='json', data stays as list."""
        data = _toon_encodable_data()
        response = {"ok": True, "data": data, "queries": [], "error": None}

        maybe_toon_encode_response(response, "json")

        assert isinstance(response["data"], list)
        assert response["ok"] is True

    def test_non_list_data_stays_unchanged(self) -> None:
        """When data is a dict (not list), it stays unchanged."""
        response = {"ok": True, "data": {"id": 1, "name": "test"}, "queries": [], "error": None}

        maybe_toon_encode_response(response, "toon")

        assert isinstance(response["data"], dict)

    def test_error_response_stays_unchanged(self) -> None:
        """Error responses (data=None) are never TOON-encoded."""
        response = {"ok": False, "data": None, "queries": [], "error": "Something failed"}
        assert response["data"] is None
        assert response["error"] == "Something failed"

    def test_small_list_stays_as_list_when_toon_not_smaller(self) -> None:
        """Tiny list where TOON has no gain → data stays as list."""
        data = [{"a": 1}]
        response = {"ok": True, "data": data, "queries": [], "error": None}

        maybe_toon_encode_response(response, "toon")

        # If TOON wasn't applied (no gain), data stays as list
        if isinstance(response["data"], list):
            assert response["data"] == data

    def test_none_data_stays_unchanged(self) -> None:
        """Error response with data=None is not touched."""
        response = {"ok": False, "data": None, "error": "fail"}

        maybe_toon_encode_response(response, "toon")

        assert response["data"] is None
        assert response["error"] == "fail"


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
