"""Tests for /health and /ready endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from api_agent.__main__ import create_app


@pytest.fixture
def client():
    """Test client for the MCP app."""
    app = create_app()
    return TestClient(app)


def _fake_settings(**overrides) -> SimpleNamespace:
    """Build a minimal fake settings object for readiness probe tests."""
    defaults = {"API_KEY": "", "PROVIDER": "openai"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestHealthEndpoint:
    """Liveness probe tests."""

    def test_health_returns_200(self, client: TestClient) -> None:
        """GET /health always returns 200 OK."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestReadyEndpoint:
    """Readiness probe tests."""

    def test_ready_with_api_key_returns_200(self, client: TestClient) -> None:
        """Readiness returns 200 when API key is configured."""
        fake = _fake_settings(API_KEY="sk-test-key", PROVIDER="openai")
        with patch("api_agent.config.get_settings", return_value=fake):
            resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_ready_openai_no_key_returns_503(self, client: TestClient) -> None:
        """OpenAI provider without API key → 503 not ready."""
        fake = _fake_settings(API_KEY="", PROVIDER="openai")
        with patch("api_agent.config.get_settings", return_value=fake):
            resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"

    def test_ready_anthropic_no_key_returns_503(self, client: TestClient) -> None:
        """Anthropic provider without API key → 503 not ready."""
        fake = _fake_settings(API_KEY="", PROVIDER="anthropic")
        with patch("api_agent.config.get_settings", return_value=fake):
            resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"

    def test_ready_openai_compat_no_key_returns_200(self, client: TestClient) -> None:
        """openai-compat provider without API key → 200 ready (key optional)."""
        fake = _fake_settings(API_KEY="", PROVIDER="openai-compat")
        with patch("api_agent.config.get_settings", return_value=fake):
            resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_ready_openai_compat_with_key_returns_200(self, client: TestClient) -> None:
        """openai-compat provider with API key → 200 ready."""
        fake = _fake_settings(API_KEY="sk-optional", PROVIDER="openai-compat")
        with patch("api_agent.config.get_settings", return_value=fake):
            resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
