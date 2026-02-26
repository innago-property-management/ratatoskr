"""Tests for REST client."""

import pytest

from api_agent.rest.client import _build_url, _is_path_allowed, execute_request


class TestIsPathAllowed:
    """Test path allowlist matching."""

    def test_exact_match(self):
        assert _is_path_allowed("/search", ["/search"]) is True

    def test_no_match(self):
        assert _is_path_allowed("/users", ["/search"]) is False

    def test_glob_star(self):
        assert _is_path_allowed("/api/v1/search", ["/api/*/search"]) is True
        assert _is_path_allowed("/api/v2/search", ["/api/*/search"]) is True
        assert _is_path_allowed("/api/search", ["/api/*/search"]) is False

    def test_multiple_patterns(self):
        patterns = ["/search", "/_search", "/api/*/query"]
        assert _is_path_allowed("/search", patterns) is True
        assert _is_path_allowed("/_search", patterns) is True
        assert _is_path_allowed("/api/v1/query", patterns) is True
        assert _is_path_allowed("/users", patterns) is False

    def test_empty_patterns(self):
        assert _is_path_allowed("/search", []) is False

    def test_nested_wildcard_pattern_matching(self):
        """Verify nested wildcard patterns match expected paths."""
        pattern = "/api/booking/search/*"

        # Should match
        assert _is_path_allowed("/api/booking/search/v1/hotels", [pattern]) is True
        assert _is_path_allowed("/api/booking/search/anything", [pattern]) is True

        # Should NOT match
        assert _is_path_allowed("/api/booking/search", [pattern]) is False
        assert _is_path_allowed("/api/booking/other", [pattern]) is False
        assert _is_path_allowed("/api/other/search/v1", [pattern]) is False


class TestBuildUrl:
    """Test URL building."""

    def test_simple_path(self):
        url = _build_url("/users", base_url="https://api.example.com")
        assert url == "https://api.example.com/users"

    def test_path_params(self):
        url = _build_url(
            "/users/{id}", base_url="https://api.example.com", path_params={"id": "123"}
        )
        assert url == "https://api.example.com/users/123"

    def test_query_params(self):
        url = _build_url(
            "/users", base_url="https://api.example.com", query_params={"limit": 10, "offset": 0}
        )
        assert "limit=10" in url
        assert "offset=0" in url

    def test_query_params_filters_none(self):
        url = _build_url(
            "/users", base_url="https://api.example.com", query_params={"limit": 10, "offset": None}
        )
        assert "limit=10" in url
        assert "offset" not in url

    def test_no_base_url_raises(self):
        with pytest.raises(ValueError, match="No base URL provided"):
            _build_url("/users", base_url="")

    # --- Bug fix regression tests ---

    def test_base_url_with_path_prefix_preserved(self):
        """Bug 1: urljoin drops base path prefixes like /v2."""
        url = _build_url("/users", base_url="https://api.example.com/v2")
        assert url == "https://api.example.com/v2/users"

    def test_base_url_with_deep_path_prefix(self):
        """Bug 1: Deeper path prefixes must also survive."""
        url = _build_url("/items/{id}", base_url="https://api.example.com/v2/admin", path_params={"id": "42"})
        assert url == "https://api.example.com/v2/admin/items/42"

    def test_base_url_with_trailing_slash_and_bare_path(self):
        """Bug 1: Preserve single slash when base_url already ends with '/' and path has no leading slash."""
        url = _build_url("users", base_url="https://api.example.com/v2/")
        assert url == "https://api.example.com/v2/users"

    def test_base_url_with_trailing_slash_and_leading_slash_path(self):
        """Bug 1: Avoid double slashes when both base_url and path provide separators."""
        url = _build_url("/users", base_url="https://api.example.com/v2/")
        assert url == "https://api.example.com/v2/users"

    def test_relative_base_url_raises(self):
        """Bug 3: Relative server URLs like /v2 lack a netloc and should raise."""
        with pytest.raises(ValueError, match="base URL"):
            _build_url("/users", base_url="/v2")

    def test_multi_value_query_params(self):
        """Bug 4: List-valued query params need doseq=True."""
        from urllib.parse import parse_qs, urlparse

        url = _build_url(
            "/search",
            base_url="https://api.example.com",
            query_params={"ids": [1, 2, 3], "q": "foo"},
        )
        # Must NOT produce the mangled form ids=%5B1%2C+2%2C+3%5D
        assert "%5B" not in url

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert query == {"ids": ["1", "2", "3"], "q": ["foo"]}


class TestExecuteRequest:
    """Test request execution and method blocking."""

    @pytest.mark.asyncio
    async def test_blocks_post_by_default(self):
        result = await execute_request(
            "POST",
            "/users",
            base_url="https://api.example.com",
            body={"name": "test"},
            allow_unsafe=False,
        )
        assert result["success"] is False
        assert "not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_put_by_default(self):
        result = await execute_request(
            "PUT",
            "/users/123",
            base_url="https://api.example.com",
            body={"name": "test"},
            allow_unsafe=False,
        )
        assert result["success"] is False
        assert "not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_delete_by_default(self):
        result = await execute_request(
            "DELETE",
            "/users/123",
            base_url="https://api.example.com",
            allow_unsafe=False,
        )
        assert result["success"] is False
        assert "not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_patch_by_default(self):
        result = await execute_request(
            "PATCH",
            "/users/123",
            base_url="https://api.example.com",
            body={"name": "test"},
            allow_unsafe=False,
        )
        assert result["success"] is False
        assert "not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_no_base_url_returns_error(self):
        result = await execute_request("GET", "/users", base_url="")
        assert result["success"] is False
        assert "No base URL" in result["error"]

    @pytest.mark.asyncio
    async def test_post_allowed_with_matching_path(self):
        # POST is allowed when path matches allow_unsafe_paths
        result = await execute_request(
            "POST",
            "/search",
            base_url="https://api.example.com",
            body={"query": "test"},
            allow_unsafe_paths=["/search", "/_search"],
        )
        # Will fail with connection error (no real server) but NOT blocked
        assert "not allowed" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_post_blocked_with_non_matching_path(self):
        result = await execute_request(
            "POST",
            "/users",
            base_url="https://api.example.com",
            body={"name": "test"},
            allow_unsafe_paths=["/search"],
        )
        assert result["success"] is False
        assert "not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_post_allowed_with_glob_pattern(self):
        result = await execute_request(
            "POST",
            "/api/v1/search",
            base_url="https://api.example.com",
            body={"query": "test"},
            allow_unsafe_paths=["/api/*/search"],
        )
        # Will fail with connection error but NOT blocked
        assert "not allowed" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_nested_search_pattern(self):
        """Test nested wildcard pattern for search APIs."""
        # Should match /api/booking/search/v1/hotels
        result = await execute_request(
            "POST",
            "/api/booking/search/v1/hotels",
            base_url="https://api.example.com",
            body={"query": "test"},
            allow_unsafe_paths=["/api/booking/search/*"],
        )
        # Will fail with connection error but NOT blocked
        assert "not allowed" not in result.get("error", "")
