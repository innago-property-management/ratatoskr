"""Tests for structured logging configuration, redaction, and correlation IDs."""

import json
import logging
import uuid

import structlog

from api_agent.logging import (
    _SENSITIVE_KEYS,
    _add_request_id,
    _redact_sensitive_data,
    configure_logging,
    get_request_id,
    set_request_id,
)


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
        test_logger.info("test_event", key="value")

        captured = capsys.readouterr()
        # structlog writes to stderr via our handler
        line = captured.err.strip().split("\n")[-1]
        parsed = json.loads(line)
        assert parsed["event"] == "test_event"
        assert parsed["key"] == "value"
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
