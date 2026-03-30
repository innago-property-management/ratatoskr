"""Tests for create_schema_reduction_provider() — provider-generalization feature."""

from __future__ import annotations

import os

import pytest

from api_agent.config import Settings
from api_agent.llm.factory import create_schema_reduction_provider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove all API_AGENT_*, OPENAI_*, and ANTHROPIC_* env vars."""
    for key in list(os.environ):
        if key.startswith(("API_AGENT_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(key, raising=False)


class TestFactoryProviderResolution:
    """Factory returns the correct provider type based on settings."""

    def test_returns_anthropic_when_provider_is_anthropic(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.provider_name == "anthropic"

    def test_returns_openai_when_provider_is_openai(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.provider_name == "openai"

    def test_returns_compat_when_provider_is_openai_compat(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai-compat")
        monkeypatch.setenv("API_AGENT_BASE_URL", "http://localhost:11434/v1")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.provider_name == "openai-compat"

    def test_inherits_main_provider_when_override_empty(self, monkeypatch):
        """AC-5: SCHEMA_REDUCTION_PROVIDER defaults to main PROVIDER."""
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        s = Settings()
        assert s.SCHEMA_REDUCTION_PROVIDER == ""
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.provider_name == "anthropic"

    def test_override_provider_used(self, monkeypatch):
        """When SCHEMA_REDUCTION_PROVIDER is set, it takes precedence."""
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_PROVIDER", "anthropic")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.provider_name == "anthropic"


class TestFactoryApiKeyResolution:
    """Factory inherits main API_KEY when no override is set."""

    def test_inherits_main_api_key(self, monkeypatch):
        """AC-6: SCHEMA_REDUCTION_API_KEY defaults to main API_KEY."""
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-main")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.api_key == "sk-main"

    def test_override_api_key_used(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-main")
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_API_KEY", "sk-reduction")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.api_key == "sk-reduction"

    def test_returns_none_for_cloud_provider_without_key(self):
        """Cloud providers (openai, anthropic) require an API key."""
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is None

    def test_returns_provider_for_compat_without_key(self, monkeypatch):
        """openai-compat (Ollama) doesn't need an API key."""
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai-compat")
        monkeypatch.setenv("API_AGENT_BASE_URL", "http://localhost:11434/v1")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None


class TestFactoryBaseUrl:
    """Factory resolves base_url correctly."""

    def test_uses_reduction_base_url_when_set(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai-compat")
        monkeypatch.setenv("API_AGENT_BASE_URL", "http://main:11434/v1")
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_BASE_URL", "http://reduction:11434/v1")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.base_url == "http://reduction:11434/v1"

    def test_falls_back_to_main_base_url(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai-compat")
        monkeypatch.setenv("API_AGENT_BASE_URL", "http://main:11434/v1")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.base_url == "http://main:11434/v1"


class TestFactoryModel:
    """Factory uses the right model."""

    def test_uses_reduction_model_when_set(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_MODEL", "claude-haiku-4-5-20251001")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.model == "claude-haiku-4-5-20251001"

    def test_default_model_per_provider(self, monkeypatch):
        """Empty model uses provider-specific default."""
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        assert p.model == "claude-haiku-4-5-20251001"  # Anthropic default for reduction


class TestFactoryTimeout:
    """AC-13 (S-1): Factory bakes timeout into provider HTTP client."""

    def test_timeout_injected(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_TIMEOUT_MS", "5000")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is not None
        # AnthropicProvider stores client as an attribute; SDK timeout is a float
        from api_agent.llm.anthropic_provider import AnthropicProvider

        assert isinstance(p, AnthropicProvider)
        assert p.client.timeout == 5.0


class TestFactoryCompatRequiresBaseUrl:
    """openai-compat without base_url should raise ValueError."""

    def test_raises_when_compat_has_no_base_url(self, monkeypatch):
        """BUG fix: openai-compat requires base_url, same as create_provider()."""
        monkeypatch.setenv("API_AGENT_PROVIDER", "openai-compat")
        # No BASE_URL set
        s = Settings()
        with pytest.raises(ValueError, match="base_url"):
            create_schema_reduction_provider(s)


class TestFactoryUnknownProvider:
    """Unknown provider type returns None."""

    def test_returns_none_for_unknown_provider(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_PROVIDER", "llama-direct")
        monkeypatch.setenv("API_AGENT_API_KEY", "sk-test")
        s = Settings()
        p = create_schema_reduction_provider(s)
        assert p is None
