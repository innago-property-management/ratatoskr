"""Request context extraction from HTTP headers."""

import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from fastmcp.server.dependencies import get_http_headers

from .config import settings

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class MissingHeaderError(Exception):
    """Required header missing from request."""

    pass


def validate_target_url(url: str, api_type: str) -> str:
    """Validate target URL against SSRF protections.

    Checks: scheme whitelist, private IP blocklist, cloud metadata blocklist,
    optional host allowlist.

    Raises MissingHeaderError on validation failure.
    Returns the URL unchanged if valid.
    """
    parsed = urlparse(url)

    # Scheme validation (per protocol)
    allowed_schemes = {s.strip() for s in settings.ALLOWED_URL_SCHEMES.split(",")}
    if api_type == "grpc":
        valid_schemes = allowed_schemes & {"grpc", "grpcs"}
        if parsed.scheme not in valid_schemes:
            raise MissingHeaderError(
                f"Invalid scheme '{parsed.scheme}' for gRPC. Allowed: {sorted(valid_schemes)}"
            )
    else:
        valid_schemes = allowed_schemes & {"http", "https"}
        if parsed.scheme not in valid_schemes:
            raise MissingHeaderError(
                f"Invalid scheme '{parsed.scheme}' for {api_type}. Allowed: {sorted(valid_schemes)}"
            )

    hostname = parsed.hostname
    if not hostname:
        raise MissingHeaderError("X-Target-URL must include a hostname")

    # Blocked hosts (cloud metadata, etc.)
    blocked = [h.strip().lower() for h in settings.BLOCKED_HOSTS.split(",") if h.strip()]
    if hostname.lower() in blocked:
        raise MissingHeaderError(f"Host '{hostname}' is blocked (security policy)")

    # Private IP blocking
    if settings.BLOCK_PRIVATE_IPS:
        try:
            ip = ipaddress.ip_address(hostname)
            # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:10.0.0.1 → 10.0.0.1)
            check_ip = getattr(ip, "ipv4_mapped", None) or ip
            for network in _PRIVATE_NETWORKS:
                try:
                    if check_ip in network:
                        raise MissingHeaderError(
                            f"Private/internal IP addresses are blocked: {hostname}"
                        )
                except TypeError:
                    continue  # IPv4/IPv6 version mismatch — skip this network
        except ValueError:
            pass  # DNS name, not an IP literal — acceptable

    # Host allowlist (if configured)
    if settings.ALLOWED_TARGET_HOSTS:
        allowed_hosts = [
            h.strip().lower() for h in settings.ALLOWED_TARGET_HOSTS.split(",") if h.strip()
        ]
        if hostname.lower() not in allowed_hosts:
            raise MissingHeaderError(f"Host '{hostname}' not in allowed target hosts")

    return url


@dataclass(frozen=True)
class RequestContext:
    """Per-request context extracted from headers."""

    target_url: str  # X-Target-URL: GraphQL endpoint, OpenAPI spec URL, or gRPC target
    api_type: str  # X-API-Type: "graphql", "rest", or "grpc"
    target_headers: dict  # X-Target-Headers: parsed JSON headers
    allow_unsafe_paths: tuple[str, ...]  # X-Allow-Unsafe-Paths: glob patterns for POST/etc
    base_url: str | None  # X-Base-URL: override base URL (REST only)
    include_result: bool  # X-Include-Result: whether to include full result in output
    poll_paths: tuple[str, ...]  # X-Poll-Paths: paths that require polling (enables poll tool)
    grpc_allow_unsafe_rpcs: tuple[
        str, ...
    ] = ()  # X-Allow-Unsafe-RPCs: glob patterns for gRPC mutations


def get_request_context() -> RequestContext:
    """Extract context from current request headers.

    Required headers:
        X-Target-URL: Target API endpoint (GraphQL) or OpenAPI spec URL (REST)
        X-API-Type: "graphql" or "rest"

    Optional headers:
        X-Target-Headers: JSON object with auth headers to forward
        X-Allow-Unsafe-Paths: JSON array of glob patterns for POST/PUT/DELETE/PATCH
        X-Base-URL: Override base URL for REST API calls
        X-Include-Result: Include full uncapped result in output (default: false)
        X-Poll-Paths: JSON array of paths requiring polling (enables poll tool)
        X-Allow-Unsafe-RPCs: JSON array of glob patterns for gRPC mutations

    Raises:
        MissingHeaderError: If required headers are missing or invalid
    """
    headers = get_http_headers()

    target_url = headers.get("x-target-url")
    api_type = headers.get("x-api-type")
    target_headers_raw = headers.get("x-target-headers") or "{}"
    allow_unsafe_paths_raw = headers.get("x-allow-unsafe-paths") or "[]"
    base_url_raw = headers.get("x-base-url")
    include_result_raw = headers.get("x-include-result", "false")
    poll_paths_raw = headers.get("x-poll-paths") or "[]"
    grpc_allow_unsafe_rpcs_raw = headers.get("x-allow-unsafe-rpcs") or "[]"

    base_url = base_url_raw if base_url_raw else None
    include_result = (include_result_raw or "").lower() in ("true", "1", "yes")

    if not target_url:
        raise MissingHeaderError("X-Target-URL header required")

    if not api_type:
        raise MissingHeaderError("X-API-Type header required (graphql|rest|grpc)")

    if api_type not in ("graphql", "rest", "grpc"):
        raise MissingHeaderError(
            f"X-API-Type must be 'graphql', 'rest', or 'grpc', got '{api_type}'"
        )

    # SSRF protection: validate URL before using it
    validate_target_url(target_url, api_type)

    try:
        target_headers = json.loads(target_headers_raw)
    except json.JSONDecodeError:
        target_headers = {}

    try:
        allow_unsafe_paths = tuple(json.loads(allow_unsafe_paths_raw))
    except json.JSONDecodeError:
        allow_unsafe_paths = ()

    try:
        poll_paths = tuple(json.loads(poll_paths_raw))
    except json.JSONDecodeError:
        poll_paths = ()

    try:
        parsed = json.loads(grpc_allow_unsafe_rpcs_raw)
        grpc_allow_unsafe_rpcs = (
            tuple(v for v in parsed if isinstance(v, str)) if isinstance(parsed, list) else ()
        )
    except json.JSONDecodeError:
        grpc_allow_unsafe_rpcs = ()

    return RequestContext(
        target_url=target_url,
        api_type=api_type,
        target_headers=target_headers,
        allow_unsafe_paths=allow_unsafe_paths,
        base_url=base_url,
        include_result=include_result,
        poll_paths=poll_paths,
        grpc_allow_unsafe_rpcs=grpc_allow_unsafe_rpcs,
    )


def _to_snake_case(name: str) -> str:
    """Convert string to snake_case."""
    name = re.sub(r"[\s\-]+", "_", name)
    name = re.sub(r"[^a-zA-Z0-9_]", "", name)
    return name.lower().strip("_")


def get_full_hostname(url: str | None) -> str:
    """Get full hostname from URL for description."""
    if not url:
        return "api"
    parsed = urlparse(url)
    return parsed.hostname or "api"


def get_tool_name_prefix(url: str | None) -> str:
    """Get semantic prefix for tool name (≤32 chars).

    Extracts meaningful parts from hostname, skipping generic TLDs and infra names.
    Example: flights-api-qa.internal.example.com → flights_api_example
    """
    if not url:
        return "api"

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if not hostname:
        return "api"

    parts = hostname.split(".")
    # Skip generic TLDs and internal infra names
    skip = {
        "com",
        "io",
        "is",
        "net",
        "org",
        "privatecloud",
        "qa",
        "dev",
        "internal",
        "api",
    }
    meaningful = [_to_snake_case(p) for p in parts if p.lower() not in skip and p]

    # Join meaningful parts, cap at 32 chars
    return "_".join(meaningful)[:32] or "api"


def extract_api_name(headers: dict | None = None) -> str:
    """Extract API name prefix from headers. Priority: X-API-Name > parse X-Target-URL."""
    if headers is None:
        headers = get_http_headers()

    # Explicit header takes priority
    if api_name := headers.get("x-api-name"):
        return _to_snake_case(api_name)[:32]

    # Fall back to semantic prefix from URL
    target_url = headers.get("x-target-url", "")
    return get_tool_name_prefix(target_url)
