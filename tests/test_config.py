"""Tests for api_agent.config.Settings (T017)."""

import os

import pytest

from api_agent.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove all API_AGENT_*, OPENAI_*, and ANTHROPIC_* env vars before each test."""
    for key in list(os.environ):
        if key.startswith(("API_AGENT_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Default values (no env vars set)
# ---------------------------------------------------------------------------


class TestDefaults:
    """Verify every field has the documented default when no env vars are set."""

    def test_mcp_name(self):
        assert Settings().MCP_NAME == "API Agent"

    def test_service_name(self):
        assert Settings().SERVICE_NAME == "api-agent"

    def test_provider(self):
        assert Settings().PROVIDER == "openai"

    def test_api_key_empty(self):
        assert Settings().API_KEY == ""

    def test_base_url_empty(self):
        assert Settings().BASE_URL == ""

    def test_model_name_empty(self):
        assert Settings().MODEL_NAME == ""

    def test_reasoning_effort_empty(self):
        assert Settings().REASONING_EFFORT == ""

    def test_max_agent_turns(self):
        assert Settings().MAX_AGENT_TURNS == 30

    def test_max_response_chars(self):
        assert Settings().MAX_RESPONSE_CHARS == 50000

    def test_max_schema_chars(self):
        assert Settings().MAX_SCHEMA_CHARS == 32000

    def test_max_preview_rows(self):
        assert Settings().MAX_PREVIEW_ROWS == 10

    def test_max_tool_response_chars(self):
        assert Settings().MAX_TOOL_RESPONSE_CHARS == 32000

    def test_max_polls(self):
        assert Settings().MAX_POLLS == 20

    def test_default_poll_delay_ms(self):
        assert Settings().DEFAULT_POLL_DELAY_MS == 3000

    def test_debug_false(self):
        assert Settings().DEBUG is False

    def test_host(self):
        assert Settings().HOST == "0.0.0.0"

    def test_port(self):
        assert Settings().PORT == 3000

    def test_transport(self):
        assert Settings().TRANSPORT == "streamable-http"

    def test_cors_allowed_origins(self):
        assert Settings().CORS_ALLOWED_ORIGINS == "*"

    def test_enable_recipes(self):
        assert Settings().ENABLE_RECIPES is True

    def test_recipe_cache_size(self):
        assert Settings().RECIPE_CACHE_SIZE == 64

    def test_legacy_openai_api_key_empty(self):
        assert Settings().OPENAI_API_KEY == ""

    def test_legacy_openai_base_url_empty(self):
        assert Settings().OPENAI_BASE_URL == ""


# ---------------------------------------------------------------------------
# MCP_SLUG computed field
# ---------------------------------------------------------------------------


class TestMcpSlug:
    """MCP_SLUG is a computed, slugified version of MCP_NAME."""

    def test_default_slug(self):
        s = Settings()
        assert s.MCP_SLUG == "api_agent"

    def test_slug_with_special_chars(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MCP_NAME", "My Cool API!")
        s = Settings()
        assert s.MCP_SLUG == "my_cool_api"

    def test_slug_strips_leading_trailing_underscores(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MCP_NAME", "---Hello World---")
        s = Settings()
        assert s.MCP_SLUG == "hello_world"

    def test_slug_collapses_multiple_non_alnum(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MCP_NAME", "A  B  C")
        s = Settings()
        assert s.MCP_SLUG == "a_b_c"


# ---------------------------------------------------------------------------
# API_KEY alias priority
# ---------------------------------------------------------------------------


class TestApiKeyAliases:
    """API_KEY supports AliasChoices: API_AGENT_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY."""

    def test_api_agent_api_key(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_API_KEY", "agent-key-123")
        assert Settings().API_KEY == "agent-key-123"

    def test_openai_api_key_alias(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key-456")
        assert Settings().API_KEY == "openai-key-456"

    def test_anthropic_api_key_alias(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        assert Settings().API_KEY == "test"

    def test_api_agent_api_key_wins_over_openai(self, monkeypatch):
        """First alias in AliasChoices takes priority."""
        monkeypatch.setenv("API_AGENT_API_KEY", "agent-wins")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-loses")
        assert Settings().API_KEY == "agent-wins"

    def test_api_agent_api_key_wins_over_anthropic(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_API_KEY", "agent-wins")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-loses")
        assert Settings().API_KEY == "agent-wins"

    def test_openai_key_wins_over_anthropic(self, monkeypatch):
        """Second alias beats third alias."""
        monkeypatch.setenv("OPENAI_API_KEY", "openai-wins")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-loses")
        assert Settings().API_KEY == "openai-wins"


# ---------------------------------------------------------------------------
# BASE_URL alias chain
# ---------------------------------------------------------------------------


class TestBaseUrlAliases:
    """BASE_URL supports AliasChoices: API_AGENT_BASE_URL, OPENAI_BASE_URL."""

    def test_api_agent_base_url(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_BASE_URL", "https://custom.api/v1")
        assert Settings().BASE_URL == "https://custom.api/v1"

    def test_openai_base_url_alias(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.proxy/v1")
        assert Settings().BASE_URL == "https://openai.proxy/v1"

    def test_api_agent_base_url_wins_over_openai(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_BASE_URL", "https://agent-wins")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://openai-loses")
        assert Settings().BASE_URL == "https://agent-wins"


# ---------------------------------------------------------------------------
# All env var names work
# ---------------------------------------------------------------------------


class TestEnvVarNames:
    """Assert that documented env vars are picked up correctly."""

    def test_mcp_name(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MCP_NAME", "Custom Agent")
        assert Settings().MCP_NAME == "Custom Agent"

    def test_service_name(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SERVICE_NAME", "my-svc")
        assert Settings().SERVICE_NAME == "my-svc"

    def test_provider(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PROVIDER", "anthropic")
        assert Settings().PROVIDER == "anthropic"

    def test_model_name(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MODEL_NAME", "gpt-4o")
        assert Settings().MODEL_NAME == "gpt-4o"

    def test_max_agent_turns(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MAX_AGENT_TURNS", "15")
        assert Settings().MAX_AGENT_TURNS == 15

    def test_max_response_chars(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MAX_RESPONSE_CHARS", "100000")
        assert Settings().MAX_RESPONSE_CHARS == 100000

    def test_max_schema_chars(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_MAX_SCHEMA_CHARS", "64000")
        assert Settings().MAX_SCHEMA_CHARS == 64000

    def test_debug(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_DEBUG", "true")
        assert Settings().DEBUG is True

    def test_host(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_HOST", "127.0.0.1")
        assert Settings().HOST == "127.0.0.1"

    def test_port(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_PORT", "8080")
        assert Settings().PORT == 8080

    def test_transport(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_TRANSPORT", "sse")
        assert Settings().TRANSPORT == "sse"

    def test_enable_recipes(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_ENABLE_RECIPES", "false")
        assert Settings().ENABLE_RECIPES is False

    def test_recipe_cache_size(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_RECIPE_CACHE_SIZE", "128")
        assert Settings().RECIPE_CACHE_SIZE == 128


# ---------------------------------------------------------------------------
# Legacy aliases (OPENAI_API_KEY / OPENAI_BASE_URL on their own fields)
# ---------------------------------------------------------------------------


class TestLegacyAliases:
    """The OPENAI_API_KEY and OPENAI_BASE_URL fields also have alias chains."""

    def test_openai_api_key_legacy_field(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        s = Settings()
        assert s.OPENAI_API_KEY == "sk-test"

    def test_openai_base_url_legacy_field(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy/v1")
        s = Settings()
        assert s.OPENAI_BASE_URL == "https://proxy/v1"

    def test_api_agent_openai_api_key_prefix(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_OPENAI_API_KEY", "sk-prefixed")
        s = Settings()
        assert s.OPENAI_API_KEY == "sk-prefixed"

    def test_api_agent_openai_base_url_prefix(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_OPENAI_BASE_URL", "https://prefixed/v1")
        s = Settings()
        assert s.OPENAI_BASE_URL == "https://prefixed/v1"


# ---------------------------------------------------------------------------
# Schema reduction settings
# ---------------------------------------------------------------------------


class TestSchemaReductionDefaults:
    """Verify schema reduction settings have correct defaults."""

    def test_enabled_default(self):
        assert Settings().SCHEMA_REDUCTION_ENABLED is True

    def test_model_default(self):
        assert Settings().SCHEMA_REDUCTION_MODEL == ""

    def test_timeout_default(self):
        assert Settings().SCHEMA_REDUCTION_TIMEOUT_MS == 30_000

    def test_api_key_empty_default(self):
        assert Settings().SCHEMA_REDUCTION_API_KEY == ""

    def test_max_input_chars_default(self):
        assert Settings().SCHEMA_REDUCTION_MAX_INPUT_CHARS == 100_000


class TestSchemaReductionEnvOverrides:
    """Verify schema reduction settings can be overridden via env vars."""

    def test_enabled_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_ENABLED", "false")
        assert Settings().SCHEMA_REDUCTION_ENABLED is False

    def test_model_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_MODEL", "claude-haiku-4-5-20251001")
        assert Settings().SCHEMA_REDUCTION_MODEL == "claude-haiku-4-5-20251001"

    def test_timeout_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_TIMEOUT_MS", "10000")
        assert Settings().SCHEMA_REDUCTION_TIMEOUT_MS == 10_000

    def test_api_key_via_prefixed_env(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_API_KEY", "sk-ant-explicit")
        assert Settings().SCHEMA_REDUCTION_API_KEY == "sk-ant-explicit"

    def test_api_key_no_longer_falls_back_to_anthropic_key(self, monkeypatch):
        """Breaking change: ANTHROPIC_API_KEY no longer aliases to schema reduction key."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fallback")
        assert Settings().SCHEMA_REDUCTION_API_KEY == ""

    def test_explicit_api_key_set_via_env(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_API_KEY", "sk-explicit")
        assert Settings().SCHEMA_REDUCTION_API_KEY == "sk-explicit"

    def test_max_input_chars_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_MAX_INPUT_CHARS", "50000")
        assert Settings().SCHEMA_REDUCTION_MAX_INPUT_CHARS == 50_000

    def test_max_output_tokens_default(self):
        assert Settings().SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS == 8192

    def test_max_output_tokens_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS", "16384")
        assert Settings().SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS == 16_384


class TestSchemaAIReductionThreshold:
    """Tests for SCHEMA_AI_REDUCTION_THRESHOLD setting."""

    def test_default_is_zero(self):
        assert Settings().SCHEMA_AI_REDUCTION_THRESHOLD == 0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_AI_REDUCTION_THRESHOLD", "50000")
        assert Settings().SCHEMA_AI_REDUCTION_THRESHOLD == 50_000

    def test_rejects_negative(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_AI_REDUCTION_THRESHOLD", "-1")
        with pytest.raises(Exception):
            Settings()


class TestRecipePersistenceConfig:
    """Tests for recipe persistence settings (T006)."""

    def test_recipe_persistence_default_sqlite(self):
        assert Settings().RECIPE_PERSISTENCE == "sqlite"

    def test_recipe_sqlite_path_default_empty(self):
        assert Settings().RECIPE_SQLITE_PATH == ""

    def test_recipe_persistence_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_RECIPE_PERSISTENCE", "memory")
        assert Settings().RECIPE_PERSISTENCE == "memory"

    def test_recipe_sqlite_path_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_RECIPE_SQLITE_PATH", "/tmp/custom.db")
        assert Settings().RECIPE_SQLITE_PATH == "/tmp/custom.db"


class TestToonConfig:
    """Tests for TOON tool result compression settings."""

    def test_toon_tool_results_enabled_default_true(self):
        assert Settings().TOON_TOOL_RESULTS_ENABLED is True

    def test_toon_tool_results_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_TOON_TOOL_RESULTS_ENABLED", "false")
        assert Settings().TOON_TOOL_RESULTS_ENABLED is False


# ---------------------------------------------------------------------------
# PROFILE preset (provider-generalization feature)
# ---------------------------------------------------------------------------


class TestProfileLocal:
    """PROFILE=local sets sensible defaults for local development."""

    def test_profile_local_sets_block_private_ips_false(self, monkeypatch):
        """AC-1: PROFILE=local disables SSRF guard for private IPs."""
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        s = Settings()
        assert s.BLOCK_PRIVATE_IPS is False

    def test_profile_local_sets_log_format_console(self, monkeypatch):
        """AC-1: PROFILE=local sets human-readable log format."""
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        s = Settings()
        assert s.LOG_FORMAT == "console"

    def test_profile_local_sets_schema_reduction_disabled(self, monkeypatch):
        """AC-3: PROFILE=local disables schema reduction by default."""
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        s = Settings()
        assert s.SCHEMA_REDUCTION_ENABLED is False

    def test_profile_local_explicit_override_wins(self, monkeypatch):
        """AC-2: Explicit env var overrides profile default."""
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        monkeypatch.setenv("API_AGENT_BLOCK_PRIVATE_IPS", "true")
        s = Settings()
        assert s.BLOCK_PRIVATE_IPS is True

    def test_profile_local_explicit_schema_reduction_wins(self, monkeypatch):
        """AC-2: Explicit SCHEMA_REDUCTION_ENABLED=true wins over profile."""
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_ENABLED", "true")
        s = Settings()
        assert s.SCHEMA_REDUCTION_ENABLED is True

    def test_profile_empty_no_change(self, monkeypatch):
        """AC-4: Empty profile (default) produces no change in defaults."""
        s = Settings()
        assert s.PROFILE == ""
        assert s.BLOCK_PRIVATE_IPS is True
        assert s.LOG_FORMAT == "json"
        assert s.SCHEMA_REDUCTION_ENABLED is True

    def test_profile_invalid_raises_value_error(self, monkeypatch):
        """AC-18: Invalid PROFILE value raises a clear error at startup."""
        monkeypatch.setenv("API_AGENT_PROFILE", "production")
        with pytest.raises(ValueError, match="Supported profiles"):
            Settings()

    def test_profile_invalid_error_message_is_human_readable(self, monkeypatch):
        """AC-18: Error message includes the invalid value and valid options."""
        monkeypatch.setenv("API_AGENT_PROFILE", "staging")
        with pytest.raises(ValueError, match="staging"):
            Settings()

    def test_profile_case_insensitive(self, monkeypatch):
        """PROFILE=LOCAL (uppercase) should work like 'local'."""
        monkeypatch.setenv("API_AGENT_PROFILE", "LOCAL")
        s = Settings()
        assert s.BLOCK_PRIVATE_IPS is False

    def test_profile_overrides_list_populated(self, monkeypatch):
        """_profile_overrides is populated when PROFILE=local is applied."""
        from api_agent.config import _profile_overrides

        _profile_overrides.clear()
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        Settings()
        assert len(_profile_overrides) > 0
        assert any("BLOCK_PRIVATE_IPS" in o for o in _profile_overrides)

    def test_profile_overrides_cleared_on_non_profile_settings(self, monkeypatch):
        """Regression: _profile_overrides must be cleared when PROFILE is empty.

        Without this, a previous PROFILE=local call leaves stale data.
        """
        from api_agent.config import _profile_overrides

        # First: populate with profile=local
        monkeypatch.setenv("API_AGENT_PROFILE", "local")
        Settings()
        assert len(_profile_overrides) > 0

        # Second: create Settings without profile — overrides must be cleared
        monkeypatch.delenv("API_AGENT_PROFILE")
        Settings()
        assert len(_profile_overrides) == 0


# ---------------------------------------------------------------------------
# Schema reduction provider fields (provider-generalization feature)
# ---------------------------------------------------------------------------


class TestSchemaReductionProviderFields:
    """New fields for schema reduction provider generalization."""

    def test_schema_reduction_provider_default_empty(self):
        """AC-5: Defaults to empty (inherit from main PROVIDER)."""
        assert Settings().SCHEMA_REDUCTION_PROVIDER == ""

    def test_schema_reduction_provider_env_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_PROVIDER", "anthropic")
        assert Settings().SCHEMA_REDUCTION_PROVIDER == "anthropic"

    def test_schema_reduction_base_url_default_empty(self):
        assert Settings().SCHEMA_REDUCTION_BASE_URL == ""

    def test_schema_reduction_base_url_env_override(self, monkeypatch):
        monkeypatch.setenv("API_AGENT_SCHEMA_REDUCTION_BASE_URL", "http://localhost:11434/v1")
        assert Settings().SCHEMA_REDUCTION_BASE_URL == "http://localhost:11434/v1"

    def test_schema_reduction_model_default_empty(self):
        """AC-7: Model defaults to empty (provider decides)."""
        assert Settings().SCHEMA_REDUCTION_MODEL == ""

    def test_schema_reduction_api_key_no_anthropic_alias(self, monkeypatch):
        """AC-6: ANTHROPIC_API_KEY no longer falls back for schema reduction.

        The key is now resolved at factory level from main API_KEY.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-reach")
        # ANTHROPIC_API_KEY feeds API_KEY via its alias, but NOT SCHEMA_REDUCTION_API_KEY
        assert Settings().SCHEMA_REDUCTION_API_KEY == ""
