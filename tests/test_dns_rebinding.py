"""Tests for DNS rebinding protection in validate_target_url()."""

import socket
from unittest.mock import patch

import pytest

from api_agent.context import MissingHeaderError, validate_target_url


def _addrinfo(ip: str, family: int = socket.AF_INET) -> list:
    """Build a mock getaddrinfo return value."""
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture(autouse=True)
def _enable_private_ip_blocking(monkeypatch):
    from api_agent.config import settings

    monkeypatch.setattr(settings, "BLOCK_PRIVATE_IPS", True)


class TestDNSRebindingProtection:
    """Hostnames that resolve to private IPs must be blocked."""

    @pytest.mark.parametrize(
        "ip,label",
        [
            ("10.0.0.1", "10.x"),
            ("172.16.5.1", "172.16.x"),
            ("192.168.1.1", "192.168.x"),
            ("127.0.0.1", "loopback"),
            ("169.254.169.254", "link-local/metadata"),
        ],
    )
    @patch("api_agent.context.socket.getaddrinfo")
    def test_hostname_resolving_to_private_ip_blocked(self, mock_dns, ip, label):
        mock_dns.return_value = _addrinfo(ip)
        with pytest.raises(MissingHeaderError, match="resolves to private/internal IP"):
            validate_target_url(f"https://evil-{label}.example.com/api", "rest")

    @patch("api_agent.context.socket.getaddrinfo")
    def test_hostname_resolving_to_public_ip_passes(self, mock_dns):
        mock_dns.return_value = _addrinfo("93.184.216.34")
        result = validate_target_url("https://example.com/api", "rest")
        assert result == "https://example.com/api"

    @patch("api_agent.context.socket.getaddrinfo")
    def test_dns_failure_passes_through(self, mock_dns):
        mock_dns.side_effect = socket.gaierror("Name resolution failed")
        result = validate_target_url("https://nonexistent.example.com/api", "rest")
        assert result == "https://nonexistent.example.com/api"

    @patch("api_agent.context.socket.getaddrinfo")
    def test_ipv4_mapped_ipv6_blocked(self, mock_dns):
        mock_dns.return_value = _addrinfo("::ffff:10.0.0.1", family=socket.AF_INET6)
        with pytest.raises(MissingHeaderError, match="resolves to private/internal IP"):
            validate_target_url("https://sneaky.example.com/api", "rest")


class TestIPLiteralRegression:
    """IP literals must still be blocked without DNS resolution."""

    def test_private_ip_literal_blocked(self):
        with pytest.raises(MissingHeaderError, match="Private/internal IP"):
            validate_target_url("https://10.0.0.1/api", "rest")

    def test_public_ip_literal_passes(self):
        result = validate_target_url("https://93.184.216.34/api", "rest")
        assert result == "https://93.184.216.34/api"
