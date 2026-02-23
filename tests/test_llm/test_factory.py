"""Tests for api_agent.llm.factory — provider creation."""

import os
from unittest.mock import patch

import pytest

from api_agent.llm.anthropic_provider import AnthropicProvider
from api_agent.llm.factory import create_provider
from api_agent.llm.openai_compat import OpenAICompatProvider
from api_agent.llm.openai_provider import OpenAIProvider


class TestCreateProvider:
    def test_openai_default(self):
        provider = create_provider("openai", api_key="sk-test")
        assert isinstance(provider, OpenAIProvider)
        assert provider.model == "gpt-4o"

    def test_openai_custom_model(self):
        provider = create_provider("openai", model="gpt-4-turbo", api_key="sk-test")
        assert provider.model == "gpt-4-turbo"

    def test_anthropic(self):
        provider = create_provider("anthropic", api_key="sk-ant-test")
        assert isinstance(provider, AnthropicProvider)
        assert provider.model == "claude-sonnet-4-20250514"

    def test_anthropic_custom_model(self):
        provider = create_provider("anthropic", model="claude-opus-4-20250514", api_key="sk-ant-test")
        assert provider.model == "claude-opus-4-20250514"

    def test_openai_compat_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            create_provider("openai-compat")

    def test_openai_compat_with_base_url(self):
        provider = create_provider(
            "openai-compat",
            base_url="http://localhost:11434/v1",
            model="llama3",
        )
        assert isinstance(provider, OpenAICompatProvider)
        assert provider.model == "llama3"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("google-vertex")

    @patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"})
    def test_openai_env_fallback(self):
        provider = create_provider("openai")
        assert isinstance(provider, OpenAIProvider)

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-ant-key"})
    def test_anthropic_env_fallback(self):
        provider = create_provider("anthropic")
        assert isinstance(provider, AnthropicProvider)

    @patch.dict(os.environ, {"OPENAI_BASE_URL": "http://custom.api/v1"})
    def test_openai_compat_env_base_url(self):
        provider = create_provider("openai-compat")
        assert isinstance(provider, OpenAICompatProvider)
