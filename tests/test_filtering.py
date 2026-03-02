"""Tests for endpoint allowlist filtering logic."""

import pytest

from api_agent.filtering import (
    filter_openapi_spec,
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


# ---------------------------------------------------------------------------
# filter_openapi_spec (REST)
# ---------------------------------------------------------------------------

_SAMPLE_SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/users": {
            "get": {"summary": "List users", "responses": {}},
            "post": {"summary": "Create user", "responses": {}},
        },
        "/users/{id}": {
            "get": {"summary": "Get user", "responses": {}},
            "delete": {"summary": "Delete user", "responses": {}},
        },
        "/orders": {
            "get": {"summary": "List orders", "responses": {}},
        },
    },
    "components": {
        "schemas": {"User": {"type": "object"}, "Order": {"type": "object"}}
    },
}


class TestFilterOpenapiSpec:
    """Filter OpenAPI spec paths by allowlist."""

    def test_none_patterns_returns_unchanged(self):
        """No constraints = full spec."""
        result = filter_openapi_spec(_SAMPLE_SPEC, None, None)
        assert set(result["paths"].keys()) == {"/users", "/users/{id}", "/orders"}

    def test_filter_by_method_and_path(self):
        """Only GET /users/* allowed."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /users*",), None)
        assert "/users" in result["paths"]
        assert "get" in result["paths"]["/users"]
        assert "post" not in result["paths"]["/users"]
        assert "/users/{id}" in result["paths"]
        assert "/orders" not in result["paths"]

    def test_multiple_patterns(self):
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /users*", "GET /orders"), None)
        assert "/users" in result["paths"]
        assert "/orders" in result["paths"]

    def test_wildcard_method(self):
        """'* /users' matches any method on /users."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("* /users",), None)
        assert "get" in result["paths"]["/users"]
        assert "post" in result["paths"]["/users"]
        assert "/orders" not in result["paths"]

    def test_all_filtered_empty_paths(self):
        """Nothing matches = empty paths."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /nonexistent",), None)
        assert result["paths"] == {}

    def test_components_preserved(self):
        """Schemas/components always preserved regardless of path filtering."""
        result = filter_openapi_spec(_SAMPLE_SPEC, ("GET /users",), None)
        assert result["components"]["schemas"]["User"] == {"type": "object"}
        assert result["components"]["schemas"]["Order"] == {"type": "object"}

    def test_intersection_narrows(self):
        """Config allows users+orders, header narrows to users only."""
        config = ("GET /users*", "GET /orders")
        header = ("GET /users*",)
        result = filter_openapi_spec(_SAMPLE_SPEC, config, header)
        assert "/users" in result["paths"]
        assert "/orders" not in result["paths"]

    def test_does_not_mutate_original(self):
        """Filtering returns a new dict, original is unchanged."""
        import copy

        original = copy.deepcopy(_SAMPLE_SPEC)
        filter_openapi_spec(_SAMPLE_SPEC, ("GET /users",), None)
        assert _SAMPLE_SPEC == original

    def test_path_level_params_preserved(self):
        """Path-level keys other than methods are preserved."""
        spec = {
            "paths": {
                "/users": {
                    "parameters": [{"name": "limit", "in": "query"}],
                    "get": {"summary": "List", "responses": {}},
                }
            }
        }
        result = filter_openapi_spec(spec, ("GET /users",), None)
        assert "parameters" in result["paths"]["/users"]

    def test_extension_keys_preserved(self):
        """x- extension keys on path items are preserved."""
        spec = {
            "paths": {
                "/users": {
                    "x-custom": "value",
                    "get": {"summary": "List", "responses": {}},
                }
            }
        }
        result = filter_openapi_spec(spec, ("GET /users",), None)
        assert result["paths"]["/users"].get("x-custom") == "value"
