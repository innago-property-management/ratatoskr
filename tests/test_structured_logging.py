"""Tests for structured logging configuration, redaction, and correlation IDs."""

import json
import logging
import uuid

import pytest
import structlog

from api_agent.context import _UUID4_RE
from api_agent.logging import (
    _SENSITIVE_KEYS,
    _add_request_id,
    _redact_sensitive_data,
    configure_logging,
    get_request_id,
    set_request_id,
)
from api_agent.rest.client import _redact_url


class TestRedaction:
    """Test sensitive data redaction processor."""

    def test_redacts_api_key(self):
        event = {"event": "test", "api_key": "sk-secret-123"}
        result = _redact_sensitive_data(None, "info", event)
        assert result["api_key"] == "[REDACTED]"

    def test_redacts_token(self):
        event = {"event": "test", "token": "bearer-abc"}
        result = _redact_sensitive_data(None, "info", event)
        assert result["token"] == "[REDACTED]"

    def test_redacts_password(self):
        event = {"event": "test", "password": "hunter2"}
        result = _redact_sensitive_data(None, "info", event)
        assert result["password"] == "[REDACTED]"

    def test_redacts_authorization(self):
        event = {"event": "test", "authorization": "Bearer xyz"}
        result = _redact_sensitive_data(None, "info", event)
        assert result["authorization"] == "[REDACTED]"

    def test_redacts_secret(self):
        event = {"event": "test", "secret": "s3cr3t"}
        result = _redact_sensitive_data(None, "info", event)
        assert result["secret"] == "[REDACTED]"

    def test_preserves_non_sensitive_keys(self):
        event = {"event": "test", "host": "example.com", "port": 3000}
        result = _redact_sensitive_data(None, "info", event)
        assert result["host"] == "example.com"
        assert result["port"] == 3000

    def test_redacts_case_insensitive(self):
        event = {"event": "test", "API_KEY": "secret", "Token": "tok"}
        result = _redact_sensitive_data(None, "info", event)
        assert result["API_KEY"] == "[REDACTED]"
        assert result["Token"] == "[REDACTED]"

    def test_all_sensitive_keys_covered(self):
        """Every key in _SENSITIVE_KEYS should be redacted."""
        for key in _SENSITIVE_KEYS:
            event = {"event": "test", key: "value"}
            result = _redact_sensitive_data(None, "info", event)
            assert result[key] == "[REDACTED]", f"Key '{key}' was not redacted"


class TestRequestId:
    """Test request_id ContextVar integration."""

    @pytest.fixture(autouse=True)
    def _reset_request_id(self):
        set_request_id("")
        yield
        set_request_id("")

    def test_set_and_get_request_id(self):
        rid = str(uuid.uuid4())
        set_request_id(rid)
        assert get_request_id() == rid

    def test_default_request_id_is_empty(self):
        set_request_id("")
        assert get_request_id() == ""

    def test_add_request_id_processor_binds_id(self):
        rid = str(uuid.uuid4())
        set_request_id(rid)
        event = {"event": "test"}
        result = _add_request_id(None, "info", event)
        assert result["request_id"] == rid

    def test_add_request_id_skips_when_empty(self):
        set_request_id("")
        event = {"event": "test"}
        result = _add_request_id(None, "info", event)
        assert "request_id" not in result


class TestConfigureLogging:
    """Test structlog configuration."""

    def test_json_format_produces_valid_json(self, capsys):
        configure_logging(log_format="json", debug=False)
        test_logger = structlog.get_logger("test.json_output")
        test_logger.info("test_event", color="blue")

        captured = capsys.readouterr()
        # structlog writes to stderr via our handler
        line = captured.err.strip().split("\n")[-1]
        parsed = json.loads(line)
        assert parsed["event"] == "test_event"
        assert parsed["color"] == "blue"
        assert "timestamp" in parsed
        assert parsed["level"] == "info"

    def test_json_format_includes_request_id(self, capsys):
        configure_logging(log_format="json", debug=False)
        rid = str(uuid.uuid4())
        set_request_id(rid)
        test_logger = structlog.get_logger("test.request_id")
        test_logger.info("correlated_event")

        captured = capsys.readouterr()
        line = captured.err.strip().split("\n")[-1]
        parsed = json.loads(line)
        assert parsed["request_id"] == rid

    def test_json_format_redacts_in_output(self, capsys):
        configure_logging(log_format="json", debug=False)
        test_logger = structlog.get_logger("test.redaction")
        test_logger.info("auth_event", api_key="sk-secret-123", host="example.com")

        captured = capsys.readouterr()
        line = captured.err.strip().split("\n")[-1]
        parsed = json.loads(line)
        assert parsed["api_key"] == "[REDACTED]"
        assert parsed["host"] == "example.com"

    def test_console_format_does_not_crash(self, capsys):
        configure_logging(log_format="console", debug=False)
        test_logger = structlog.get_logger("test.console")
        test_logger.info("console_event", key="value")
        captured = capsys.readouterr()
        assert "console_event" in captured.err

    def test_debug_mode_sets_debug_level(self):
        configure_logging(log_format="json", debug=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_info_mode_sets_info_level(self):
        configure_logging(log_format="json", debug=False)
        assert logging.getLogger().level == logging.INFO

    def test_third_party_loggers_produce_structured_output(self, capsys):
        """stdlib loggers from third-party libs should also produce JSON."""
        configure_logging(log_format="json", debug=False)
        stdlib_logger = logging.getLogger("some.third.party.lib")
        stdlib_logger.warning("something happened")

        captured = capsys.readouterr()
        line = captured.err.strip().split("\n")[-1]
        parsed = json.loads(line)
        assert parsed["event"] == "something happened"
        assert parsed["level"] == "warning"


class TestSubstringRedaction:
    """Test substring-based sensitive key matching."""

    @pytest.mark.parametrize(
        "key",
        [
            "auth_token",
            "client_secret",
            "db_password",
            "bearer_value",
            "user_credential",
            "oauth_token",
            "AUTH_HEADER",
            "MySecretKey",
        ],
    )
    def test_substring_match_redacts(self, key):
        """Keys containing sensitive substrings must be redacted."""
        event = {"event": "test", key: "sensitive-value"}
        result = _redact_sensitive_data(None, "info", event)
        assert result[key] == "[REDACTED]", f"Key '{key}' was not redacted"

    def test_preserves_safe_keys(self):
        """Keys that do not match any sensitive pattern are preserved."""
        event = {"event": "test", "host": "example.com", "method": "GET", "status": 200}
        result = _redact_sensitive_data(None, "info", event)
        assert result["host"] == "example.com"
        assert result["method"] == "GET"
        assert result["status"] == 200


class TestNestedDictRedaction:
    """Test recursive redaction of nested dicts."""

    def test_nested_dict_redacts_sensitive_keys(self):
        event = {
            "event": "test",
            "headers": {"Authorization": "Bearer xyz", "Content-Type": "application/json"},
        }
        result = _redact_sensitive_data(None, "info", event)
        assert result["headers"]["Authorization"] == "[REDACTED]"
        assert result["headers"]["Content-Type"] == "application/json"

    def test_deeply_nested_dict_redacts(self):
        event = {
            "event": "test",
            "outer": {"inner": {"secret_value": "top-secret", "name": "safe"}},
        }
        result = _redact_sensitive_data(None, "info", event)
        assert result["outer"]["inner"]["secret_value"] == "[REDACTED]"
        assert result["outer"]["inner"]["name"] == "safe"

    def test_top_level_sensitive_key_with_dict_value(self):
        event = {"event": "test", "credentials": {"user": "admin", "pass": "hunter2"}}
        result = _redact_sensitive_data(None, "info", event)
        # "credentials" contains "credential" substring -> entire value redacted
        assert result["credentials"] == "[REDACTED]"

    def test_non_dict_values_preserved(self):
        event = {"event": "test", "data": {"count": 42, "items": [1, 2, 3]}}
        result = _redact_sensitive_data(None, "info", event)
        assert result["data"]["count"] == 42
        assert result["data"]["items"] == [1, 2, 3]


class TestRequestIdValidation:
    """Test X-Request-ID UUID4 validation."""

    def test_valid_uuid4_accepted(self):
        valid = "550e8400-e29b-41d4-a716-446655440000"
        assert _UUID4_RE.match(valid) is not None

    def test_uuid4_case_insensitive(self):
        valid_upper = "550E8400-E29B-41D4-A716-446655440000"
        assert _UUID4_RE.match(valid_upper) is not None

    def test_non_uuid_rejected(self):
        assert _UUID4_RE.match("not-a-uuid") is None
        assert _UUID4_RE.match("") is None
        assert _UUID4_RE.match("<script>alert(1)</script>") is None

    def test_uuid_v1_rejected(self):
        """Only UUID v4 (digit 4 in version field) is accepted."""
        v1 = "550e8400-e29b-11d4-a716-446655440000"  # version 1
        assert _UUID4_RE.match(v1) is None

    def test_uuid_wrong_variant_rejected(self):
        """UUID4 variant bits must be 8, 9, a, or b."""
        wrong_variant = "550e8400-e29b-41d4-c716-446655440000"  # 'c' not in [89ab]
        assert _UUID4_RE.match(wrong_variant) is None

    def test_too_short_rejected(self):
        assert _UUID4_RE.match("550e8400-e29b-41d4-a716") is None


class TestUrlRedaction:
    """Test URL query parameter redaction for REST logs."""

    def test_redacts_query_params(self):
        url = "https://api.example.com/users?api_key=secret123&page=1"
        redacted = _redact_url(url)
        assert "secret123" not in redacted
        assert "api_key" in redacted  # key preserved
        assert "[REDACTED]" in redacted

    def test_preserves_url_without_query(self):
        url = "https://api.example.com/users"
        assert _redact_url(url) == url

    def test_preserves_path(self):
        url = "https://api.example.com/users/123?token=abc"
        redacted = _redact_url(url)
        assert "/users/123" in redacted
        assert "abc" not in redacted

    def test_multiple_params_all_redacted(self):
        url = "https://api.example.com/search?q=test&limit=10&offset=0"
        redacted = _redact_url(url)
        assert "test" not in redacted
        # All param values should be redacted
        assert redacted.count("[REDACTED]") == 3
