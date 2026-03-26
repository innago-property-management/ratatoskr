"""Security tests for SSRF protection (PR B).

Tests cover:
- F4: URL validation in context.py — scheme, private IP, blocked hosts, allowlist
"""

import pytest

from api_agent.context import MissingHeaderError, validate_target_url

# ---------------------------------------------------------------------------
# Scheme validation
# ---------------------------------------------------------------------------


class TestSchemeValidation:
    """Verify URL scheme enforcement per API type."""

    def test_https_valid_for_graphql(self):
        assert validate_target_url("https://api.example.com/graphql", "graphql")

    def test_http_valid_for_graphql(self):
        assert validate_target_url("http://api.example.com/graphql", "graphql")

    def test_https_valid_for_rest(self):
        assert validate_target_url("https://api.example.com/openapi.json", "rest")

    def test_grpc_valid_for_grpc(self):
        assert validate_target_url("grpc://api.example.com:50051", "grpc")

    def test_grpcs_valid_for_grpc(self):
        assert validate_target_url("grpcs://api.example.com:443", "grpc")

    def test_ftp_rejected(self):
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("ftp://evil.com/file", "rest")

    def test_file_rejected_when_not_in_store(self):
        """file:// is blocked when not pre-loaded in SchemaStore."""
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("file:///etc/passwd", "rest")

    def test_bare_path_rejected_when_not_in_store(self):
        """Bare paths are blocked when not pre-loaded in SchemaStore."""
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("/opt/specs/openapi.json", "rest")

    def test_file_url_allowed_when_in_schema_store(self, monkeypatch):
        """file:// URLs pass validation when pre-loaded in SCHEMA_STORE at startup."""
        from api_agent.schema.spec_cache import SCHEMA_STORE

        url = "file:///opt/specs/openapi.json"
        monkeypatch.setitem(SCHEMA_STORE._schemas, url, {"openapi": "3.0.0"})

        result = validate_target_url(url, "rest")
        assert result == url

    def test_bare_path_allowed_when_in_schema_store(self, monkeypatch):
        """Bare paths pass validation when pre-loaded in SCHEMA_STORE at startup."""
        from api_agent.schema.spec_cache import SCHEMA_STORE

        url = "/opt/specs/openapi.json"
        monkeypatch.setitem(SCHEMA_STORE._schemas, url, {"openapi": "3.0.0"})

        result = validate_target_url(url, "rest")
        assert result == url

    def test_file_url_not_in_store_still_blocked(self):
        """file:// URLs not in SCHEMA_STORE are still blocked (SSRF protection)."""
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("file:///etc/shadow", "rest")

    def test_javascript_rejected(self):
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("javascript:alert(1)", "rest")

    def test_grpc_scheme_rejected_for_rest(self):
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("grpc://api.example.com:50051", "rest")

    def test_http_scheme_rejected_for_grpc(self):
        with pytest.raises(MissingHeaderError, match="(?i)scheme"):
            validate_target_url("http://api.example.com/graphql", "grpc")

    def test_no_scheme_rejected(self):
        with pytest.raises(MissingHeaderError, match="(?i)scheme|hostname"):
            validate_target_url("api.example.com/graphql", "rest")


# ---------------------------------------------------------------------------
# Hostname validation
# ---------------------------------------------------------------------------


class TestHostnameValidation:
    """Verify hostname is required."""

    def test_no_hostname_rejected(self):
        with pytest.raises(MissingHeaderError, match="(?i)hostname"):
            validate_target_url("http:///path", "rest")


# ---------------------------------------------------------------------------
# Blocked hosts (cloud metadata)
# ---------------------------------------------------------------------------


class TestBlockedHosts:
    """Verify cloud metadata and blocked hosts are rejected."""

    def test_aws_metadata_ipv4_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)blocked|private"):
            validate_target_url("http://169.254.169.254/latest/meta-data/", "rest")

    def test_gcp_metadata_hostname_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)blocked"):
            validate_target_url("http://metadata.google.internal/computeMetadata/v1/", "rest")


# ---------------------------------------------------------------------------
# Private IP blocking
# ---------------------------------------------------------------------------


class TestPrivateIPBlocking:
    """Verify private/internal IP addresses are blocked."""

    def test_loopback_127_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://127.0.0.1:8080/api", "rest")

    def test_loopback_127_range_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://127.100.0.1/api", "rest")

    def test_private_10_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://10.0.0.1/api", "rest")

    def test_private_172_16_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://172.16.0.1/api", "rest")

    def test_private_192_168_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://192.168.1.1/api", "rest")

    def test_link_local_169_254_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://169.254.1.1/api", "rest")

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://[::1]:8080/api", "rest")

    def test_ipv4_mapped_ipv6_private_blocked(self):
        """IPv4-mapped IPv6 (::ffff:10.x) must be unwrapped and blocked."""
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://[::ffff:10.0.0.1]/api", "rest")

    def test_ipv4_mapped_ipv6_loopback_blocked(self):
        """IPv4-mapped IPv6 loopback (::ffff:127.0.0.1) must be blocked."""
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://[::ffff:127.0.0.1]/api", "rest")

    def test_ipv6_unique_local_blocked(self):
        """IPv6 unique-local (fc00::/7) must be blocked."""
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://[fd00::1]/api", "rest")

    def test_ipv6_link_local_blocked(self):
        """IPv6 link-local (fe80::/10) must be blocked."""
        with pytest.raises(MissingHeaderError, match="(?i)private|blocked"):
            validate_target_url("http://[fe80::1]/api", "rest")

    def test_public_ip_passes(self):
        assert validate_target_url("http://93.184.216.34/api", "rest")

    def test_dns_hostname_passes_private_ip_check(self):
        """DNS hostnames are not blocked (no resolution at validation time)."""
        assert validate_target_url("http://internal.example.com/api", "rest")

    def test_private_ip_blocking_disabled_via_settings(self, monkeypatch):
        """BLOCK_PRIVATE_IPS=False allows private IPs."""
        from api_agent.config import settings

        monkeypatch.setattr(settings, "BLOCK_PRIVATE_IPS", False)
        assert validate_target_url("http://127.0.0.1:8080/api", "rest")


# ---------------------------------------------------------------------------
# Host allowlist
# ---------------------------------------------------------------------------


class TestHostAllowlist:
    """Verify optional host allowlist filtering."""

    def test_empty_allowlist_allows_all(self):
        """Default empty allowlist allows any non-blocked host."""
        assert validate_target_url("https://any-host.example.com/api", "rest")

    def test_allowlist_allows_listed_host(self, monkeypatch):
        from api_agent.config import settings

        monkeypatch.setattr(settings, "ALLOWED_TARGET_HOSTS", "api.example.com,other.example.com")
        assert validate_target_url("https://api.example.com/graphql", "graphql")

    def test_allowlist_blocks_unlisted_host(self, monkeypatch):
        from api_agent.config import settings

        monkeypatch.setattr(settings, "ALLOWED_TARGET_HOSTS", "api.example.com")
        with pytest.raises(MissingHeaderError, match="(?i)allowed"):
            validate_target_url("https://evil.com/graphql", "graphql")

    def test_allowlist_case_insensitive(self, monkeypatch):
        from api_agent.config import settings

        monkeypatch.setattr(settings, "ALLOWED_TARGET_HOSTS", "API.Example.COM")
        assert validate_target_url("https://api.example.com/graphql", "graphql")

    def test_blocked_hosts_configurable(self, monkeypatch):
        from api_agent.config import settings

        monkeypatch.setattr(settings, "BLOCKED_HOSTS", "evil.internal,bad.local")
        with pytest.raises(MissingHeaderError, match="(?i)blocked"):
            validate_target_url("http://evil.internal/api", "rest")
