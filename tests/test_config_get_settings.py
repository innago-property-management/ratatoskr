"""Tests for get_settings() function."""

from api_agent.config import Settings, get_settings, settings


class TestGetSettings:
    def test_returns_settings_instance(self):
        result = get_settings()
        assert isinstance(result, Settings)

    def test_returns_module_singleton(self):
        assert get_settings() is settings
