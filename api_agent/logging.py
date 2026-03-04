"""Structured logging configuration using structlog.

Provides JSON output for production and colored console output for development.
Integrates with stdlib logging so third-party libraries also output structured logs.

Configure via API_AGENT_LOG_FORMAT env var: 'json' (default) or 'console'.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "token",
        "password",
        "secret",
        "authorization",
        "x-api-key",
        "apikey",
        "access_token",
        "refresh_token",
    }
)


def _redact_sensitive_data(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Redact values for keys matching sensitive patterns."""
    for key in event_dict:
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def _add_request_id(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Bind request_id from ContextVar if available."""
    request_id = _request_id_var.get()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def set_request_id(request_id: str) -> None:
    """Set the request_id for the current context."""
    _request_id_var.set(request_id)


def get_request_id() -> str:
    """Get the current request_id."""
    return _request_id_var.get()


def configure_logging(log_format: str = "json", debug: bool = False) -> None:
    """Configure structlog and stdlib logging integration.

    Args:
        log_format: 'json' for production JSON output, 'console' for colored dev output.
        debug: If True, set log level to DEBUG.
    """
    log_level = logging.DEBUG if debug else logging.INFO

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _redact_sensitive_data,
    ]

    if log_format == "console":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Quiet down noisy third-party loggers
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
