"""Tests for endpoint allowlist filtering logic."""

import pytest

from api_agent.filtering import (
    is_endpoint_allowed,
    matches_any_pattern,
    parse_config_allowlist,
)


# ---------------------------------------------------------------------------
# parse_config_allowlist
# ---------------------------------------------------------------------------


class TestParseConfigAllowlist:
    """Parse CSV config string into tuple or None."""

    def test_empty_string_returns_none(self):
        assert parse_config_allowlist("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_config_allowlist("   ") is None

    def test_single_pattern(self):
        assert parse_config_allowlist("GET /users/*") == ("GET /users/*",)

    def test_multiple_patterns(self):
        result = parse_config_allowlist("GET /users/*,POST /orders")
        assert result == ("GET /users/*", "POST /orders")

    def test_strips_whitespace(self):
        result = parse_config_allowlist(" GET /users/* , POST /orders ")
        assert result == ("GET /users/*", "POST /orders")

    def test_ignores_empty_segments(self):
        result = parse_config_allowlist("GET /users/*,,POST /orders,")
        assert result == ("GET /users/*", "POST /orders")


# ---------------------------------------------------------------------------
# matches_any_pattern
# ---------------------------------------------------------------------------


class TestMatchesAnyPattern:
    """fnmatch-based pattern matching."""

    def test_none_patterns_allows_all(self):
        assert matches_any_pattern("anything", None) is True

    def test_empty_tuple_blocks_all(self):
        assert matches_any_pattern("anything", ()) is False

    def test_exact_match(self):
        assert matches_any_pattern("GET /users", ("GET /users",)) is True

    def test_glob_wildcard(self):
        assert matches_any_pattern("GET /users/123", ("GET /users/*",)) is True

    def test_glob_star_all(self):
        assert matches_any_pattern("GET /anything", ("*",)) is True

    def test_no_match(self):
        assert matches_any_pattern("POST /users", ("GET /users/*",)) is False

    def test_multiple_patterns_any_matches(self):
        patterns = ("GET /users/*", "GET /orders/*")
        assert matches_any_pattern("GET /users/1", patterns) is True
        assert matches_any_pattern("GET /orders/1", patterns) is True
        assert matches_any_pattern("GET /products/1", patterns) is False

    def test_case_sensitive(self):
        """fnmatch is case-sensitive by default."""
        assert matches_any_pattern("GET /users", ("get /users",)) is False

    def test_question_mark_wildcard(self):
        assert matches_any_pattern("GET /users/a", ("GET /users/?",)) is True
        assert matches_any_pattern("GET /users/ab", ("GET /users/?",)) is False


# ---------------------------------------------------------------------------
# is_endpoint_allowed (intersection logic)
# ---------------------------------------------------------------------------


class TestIsEndpointAllowed:
    """Config + header intersection semantics."""

    def test_both_none_allows_all(self):
        """Neither config nor header set = allow everything."""
        assert is_endpoint_allowed("GET /users", None, None) is True

    def test_config_only_matches(self):
        assert is_endpoint_allowed("GET /users", ("GET /users",), None) is True

    def test_config_only_no_match(self):
        assert is_endpoint_allowed("POST /users", ("GET /users",), None) is False

    def test_header_only_matches(self):
        assert is_endpoint_allowed("GET /users", None, ("GET /users",)) is True

    def test_header_only_no_match(self):
        assert is_endpoint_allowed("POST /users", None, ("GET /users",)) is False

    def test_both_set_intersection_both_match(self):
        """Both must match for intersection."""
        config = ("GET /users/*", "GET /orders/*")
        header = ("GET /users/*",)
        assert is_endpoint_allowed("GET /users/1", config, header) is True

    def test_both_set_intersection_header_narrows(self):
        """Header narrows config — orders in config but not header."""
        config = ("GET /users/*", "GET /orders/*")
        header = ("GET /users/*",)
        assert is_endpoint_allowed("GET /orders/1", config, header) is False

    def test_both_set_intersection_disjoint(self):
        """Disjoint config and header = nothing allowed."""
        config = ("GET /users/*",)
        header = ("GET /orders/*",)
        assert is_endpoint_allowed("GET /users/1", config, header) is False
        assert is_endpoint_allowed("GET /orders/1", config, header) is False

    def test_header_cannot_widen_config(self):
        """Header allows broader pattern but config restricts."""
        config = ("GET /users/*",)
        header = ("*",)  # wildcard header
        assert is_endpoint_allowed("GET /users/1", config, header) is True
        assert is_endpoint_allowed("GET /orders/1", config, header) is False
