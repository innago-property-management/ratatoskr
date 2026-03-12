"""Tests for M5: malformed JSON safety headers emit warnings."""

from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from api_agent.context import get_request_context

_BASE_HEADERS = {
    "x-target-url": "https://example.com/api",
    "x-api-type": "rest",
}


def _headers_with(**overrides: str) -> dict[str, str]:
    return {**_BASE_HEADERS, **overrides}


def _patch_context():
    """Return a stack of patches for validate_target_url and set_request_id."""
    return [
        patch("api_agent.context.validate_target_url", side_effect=lambda url, _: url),
        patch("api_agent.context.set_request_id"),
    ]


# ---------- malformed JSON logs a warning ----------


@pytest.mark.parametrize(
    "header_name,header_key",
    [
        ("X-Target-Headers", "x-target-headers"),
        ("X-Allow-Unsafe-Paths", "x-allow-unsafe-paths"),
        ("X-Poll-Paths", "x-poll-paths"),
        ("X-Allow-Unsafe-RPCs", "x-allow-unsafe-rpcs"),
        ("X-Allow-Endpoints", "x-allow-endpoints"),
    ],
)
def test_malformed_json_logs_warning(header_name: str, header_key: str) -> None:
    headers = _headers_with(**{header_key: "NOT-VALID-JSON"})
    patches = _patch_context()
    with patches[0], patches[1], patch(
        "api_agent.context.get_http_headers", return_value=headers
    ), capture_logs() as cap:
        get_request_context()

    warnings = [e for e in cap if e.get("event") == "malformed_header_json"]
    assert len(warnings) == 1
    assert warnings[0]["header"] == header_name


# ---------- valid JSON does NOT log a warning ----------


@pytest.mark.parametrize(
    "header_key,valid_value",
    [
        ("x-target-headers", '{"Authorization": "Bearer tok"}'),
        ("x-allow-unsafe-paths", '["/admin/*"]'),
        ("x-poll-paths", '["/status"]'),
        ("x-allow-unsafe-rpcs", '["pkg.Service/Method"]'),
        ("x-allow-endpoints", '["GET /users"]'),
    ],
)
def test_valid_json_no_warning(header_key: str, valid_value: str) -> None:
    headers = _headers_with(**{header_key: valid_value})
    patches = _patch_context()
    with patches[0], patches[1], patch(
        "api_agent.context.get_http_headers", return_value=headers
    ), capture_logs() as cap:
        get_request_context()

    warnings = [e for e in cap if e.get("event") == "malformed_header_json"]
    assert len(warnings) == 0


# ---------- all five malformed at once ----------


def test_all_malformed_headers_log_five_warnings() -> None:
    headers = _headers_with(
        **{
            "x-target-headers": "{bad",
            "x-allow-unsafe-paths": "[bad",
            "x-poll-paths": "not json",
            "x-allow-unsafe-rpcs": "nope",
            "x-allow-endpoints": "???",
        }
    )
    patches = _patch_context()
    with patches[0], patches[1], patch(
        "api_agent.context.get_http_headers", return_value=headers
    ), capture_logs() as cap:
        ctx = get_request_context()

    warnings = [e for e in cap if e.get("event") == "malformed_header_json"]
    assert len(warnings) == 5
    warned_headers = {w["header"] for w in warnings}
    assert warned_headers == {
        "X-Target-Headers",
        "X-Allow-Unsafe-Paths",
        "X-Poll-Paths",
        "X-Allow-Unsafe-RPCs",
        "X-Allow-Endpoints",
    }

    # Verify fallback defaults are applied
    assert ctx.target_headers == {}
    assert ctx.allow_unsafe_paths == ()
    assert ctx.poll_paths == ()
    assert ctx.grpc_allow_unsafe_rpcs == ()
    assert ctx.allow_endpoints == ()
