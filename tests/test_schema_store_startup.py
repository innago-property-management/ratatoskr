"""TDD tests for SCHEMA_STORE startup loading in __main__.py.

Tests that the startup event loads schemas into SCHEMA_STORE
when DEFAULT_TARGET_URL is configured.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from api_agent.schema.spec_cache import SCHEMA_STORE

MINIMAL_OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0"},
    "paths": {"/users": {"get": {"summary": "List users"}}},
    "servers": [{"url": "https://api.example.com"}],
}


@pytest.fixture(autouse=True)
def _clean_schema_store():
    """Reset SCHEMA_STORE between tests."""
    SCHEMA_STORE._schemas.clear()
    yield
    SCHEMA_STORE._schemas.clear()


class TestLoadConfiguredSchemas:
    """load_configured_schemas() populates SCHEMA_STORE at startup."""

    @pytest.mark.asyncio
    async def test_loads_file_path_into_store(self, tmp_path, monkeypatch) -> None:
        """A file:// DEFAULT_TARGET_URL loads the spec into SCHEMA_STORE."""
        from api_agent.schema.startup import load_configured_schemas

        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(MINIMAL_OPENAPI_SPEC))
        url = f"file://{spec_file}"

        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_TARGET_URL", url)
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_API_TYPE", "rest")

        count = await load_configured_schemas()

        assert count == 1
        assert SCHEMA_STORE.get(url) == MINIMAL_OPENAPI_SPEC

    @pytest.mark.asyncio
    async def test_loads_bare_path_into_store(self, tmp_path, monkeypatch) -> None:
        """A bare absolute path loads the spec into SCHEMA_STORE."""
        from api_agent.schema.startup import load_configured_schemas

        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(MINIMAL_OPENAPI_SPEC))
        url = str(spec_file)

        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_TARGET_URL", url)
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_API_TYPE", "rest")

        count = await load_configured_schemas()

        assert count == 1
        assert SCHEMA_STORE.get(url) == MINIMAL_OPENAPI_SPEC

    @pytest.mark.asyncio
    async def test_loads_http_url_via_fetcher(self, monkeypatch) -> None:
        """An HTTP DEFAULT_TARGET_URL fetches and stores the spec."""
        from api_agent.schema.startup import load_configured_schemas

        url = "https://api.example.com/openapi.json"
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_TARGET_URL", url)
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_API_TYPE", "rest")

        # Mock the HTTP fetch
        mock_response = AsyncMock()
        mock_response.text = json.dumps(MINIMAL_OPENAPI_SPEC)
        mock_response.raise_for_status = lambda: None
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        async def mock_get_client(url):
            return mock_client

        monkeypatch.setattr("api_agent.schema.startup.pool.get_http_client", mock_get_client)

        count = await load_configured_schemas()

        assert count == 1
        assert SCHEMA_STORE.get(url) == MINIMAL_OPENAPI_SPEC

    @pytest.mark.asyncio
    async def test_skips_grpc(self, monkeypatch) -> None:
        """gRPC targets are skipped — they need live reflection."""
        from api_agent.schema.startup import load_configured_schemas

        monkeypatch.setattr(
            "api_agent.schema.startup.settings.DEFAULT_TARGET_URL", "grpc://localhost:50051"
        )
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_API_TYPE", "grpc")

        count = await load_configured_schemas()

        assert count == 0
        assert not SCHEMA_STORE.is_loaded("grpc://localhost:50051")

    @pytest.mark.asyncio
    async def test_skips_when_no_default_url(self, monkeypatch) -> None:
        """No DEFAULT_TARGET_URL configured — nothing loaded."""
        from api_agent.schema.startup import load_configured_schemas

        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_TARGET_URL", "")
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_API_TYPE", "")

        count = await load_configured_schemas()

        assert count == 0

    @pytest.mark.asyncio
    async def test_http_fetch_failure_logs_warning(self, monkeypatch) -> None:
        """Failed HTTP fetch doesn't crash startup — returns 0."""
        from api_agent.schema.startup import load_configured_schemas

        url = "https://api.example.com/openapi.json"
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_TARGET_URL", url)
        monkeypatch.setattr("api_agent.schema.startup.settings.DEFAULT_API_TYPE", "rest")

        async def mock_get_client(url):
            raise ConnectionError("unreachable")

        monkeypatch.setattr("api_agent.schema.startup.pool.get_http_client", mock_get_client)

        count = await load_configured_schemas()

        assert count == 0
        assert not SCHEMA_STORE.is_loaded(url)
