"""Request context extraction from HTTP headers."""

import ipaddress
import json
import re
import socket
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import structlog
from fastmcp.server.dependencies import get_http_headers

from .config import settings
from .exceptions import APIAgentError
from .logging import set_request_id

logger = structlog.get_logger(__name__)

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I
)

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


class MissingHeaderError(APIAgentError):
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

    # Local spec paths — no network involved. Early return (like mcp://).
    # file:// URIs and bare absolute/relative paths are valid lookup keys.
    if parsed.scheme == "file" or (not parsed.scheme and url.startswith(("/", "./"))):
        return url

    # Scheme validation (per protocol)
    allowed_schemes = {s.strip() for s in settings.ALLOWED_URL_SCHEMES.split(",")}
    if api_type == "grpc":
        valid_schemes = allowed_schemes & {"grpc", "grpcs"}
        if parsed.scheme not in valid_schemes:
            raise MissingHeaderError(
                f"Invalid scheme '{parsed.scheme}' for gRPC. Allowed: {sorted(valid_schemes)}"
            )
    elif api_type == "mcp":
        # mcp:// targets are process-local (stdio) — bypass IP check entirely.
        # http/https targets are direct SSE connections and undergo full SSRF validation below.
        if parsed.scheme == "mcp":
            return url  # no network reachability — stdio process target
        mcp_valid_schemes = allowed_schemes & {"http", "https", "mcp"}
        if parsed.scheme not in mcp_valid_schemes:
            raise MissingHeaderError(
                f"Invalid scheme '{parsed.scheme}' for MCP. Allowed: {sorted(mcp_valid_schemes)}"
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
            # DNS name, not an IP literal — resolve and check each address
            try:
                addrinfo = socket.getaddrinfo(hostname, None)
                for _family, _type, _proto, _canonname, sockaddr in addrinfo:
                    resolved_ip = ipaddress.ip_address(sockaddr[0])
                    check_ip = getattr(resolved_ip, "ipv4_mapped", None) or resolved_ip
                    for network in _PRIVATE_NETWORKS:
                        try:
                            if check_ip in network:
                                raise MissingHeaderError(
                                    f"Host '{hostname}' resolves to private/internal IP: {sockaddr[0]}"
                                )
                        except TypeError:
                            continue
            except socket.gaierror:
                pass  # DNS resolution failed — connection will fail later

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
    request_id: str = ""  # X-Request-ID or auto-generated UUID4
    grpc_allow_unsafe_rpcs: tuple[
        str, ...
    ] = ()  # X-Allow-Unsafe-RPCs: glob patterns for gRPC mutations
    allow_endpoints: tuple[str, ...] = ()  # X-Allow-Endpoints: glob patterns for endpoint allowlist
    output_format: str = "toon"  # X-Output-Format: "toon" (default) or "json"


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
        X-Allow-Endpoints: JSON array of glob patterns to restrict exposed endpoints
        X-Output-Format: "toon" (default) or "json" — controls TOON encoding of MCP output

    Raises:
        MissingHeaderError: If required headers are missing or invalid
    """
    headers = get_http_headers()

    # Generate or accept request ID for correlation (must be valid UUID4)
    raw_id = headers.get("x-request-id", "")
    request_id = raw_id if _UUID4_RE.match(raw_id) else str(uuid.uuid4())
    set_request_id(request_id)

    # Fall back to config defaults when headers are omitted (single-API mode)
    target_url = headers.get("x-target-url") or settings.DEFAULT_TARGET_URL or None
    api_type = headers.get("x-api-type") or settings.DEFAULT_API_TYPE or None
    target_headers_raw = headers.get("x-target-headers") or settings.DEFAULT_TARGET_HEADERS or "{}"
    allow_unsafe_paths_raw = headers.get("x-allow-unsafe-paths") or "[]"
    base_url_raw = headers.get("x-base-url") or settings.DEFAULT_BASE_URL or None
    include_result_raw = headers.get("x-include-result", "false")
    poll_paths_raw = headers.get("x-poll-paths") or "[]"
    grpc_allow_unsafe_rpcs_raw = headers.get("x-allow-unsafe-rpcs") or "[]"
    allow_endpoints_raw = headers.get("x-allow-endpoints") or "[]"
    output_format_raw = headers.get("x-output-format", "")

    base_url = base_url_raw if base_url_raw else None
    include_result = (include_result_raw or "").lower() in ("true", "1", "yes")

    # TOON output format: header overrides config default
    if output_format_raw:
        output_format = "json" if output_format_raw.lower() == "json" else "toon"
    else:
        output_format = "toon" if settings.TOON_MCP_OUTPUT_ENABLED else "json"

    if not target_url:
        raise MissingHeaderError(
            "X-Target-URL header required (or set API_AGENT_DEFAULT_TARGET_URL)"
        )

    if not api_type:
        raise MissingHeaderError("X-API-Type header required (or set API_AGENT_DEFAULT_API_TYPE)")

    if api_type not in ("graphql", "rest", "grpc", "mcp"):
        raise MissingHeaderError(
            f"X-API-Type must be 'graphql', 'rest', 'grpc', or 'mcp', got '{api_type}'"
        )

    # SSRF protection: validate URL before using it
    validate_target_url(target_url, api_type)

    try:
        target_headers = json.loads(target_headers_raw)
    except json.JSONDecodeError:
        logger.warning("malformed_header_json", header="X-Target-Headers", fallback="empty dict")
        target_headers = {}

    try:
        allow_unsafe_paths = tuple(json.loads(allow_unsafe_paths_raw))
    except json.JSONDecodeError:
        logger.warning(
            "malformed_header_json", header="X-Allow-Unsafe-Paths", fallback="empty tuple"
        )
        allow_unsafe_paths = ()

    try:
        poll_paths = tuple(json.loads(poll_paths_raw))
    except json.JSONDecodeError:
        logger.warning("malformed_header_json", header="X-Poll-Paths", fallback="empty tuple")
        poll_paths = ()

    try:
        parsed = json.loads(grpc_allow_unsafe_rpcs_raw)
        grpc_allow_unsafe_rpcs = (
            tuple(v for v in parsed if isinstance(v, str)) if isinstance(parsed, list) else ()
        )
    except json.JSONDecodeError:
        logger.warning(
            "malformed_header_json", header="X-Allow-Unsafe-RPCs", fallback="empty tuple"
        )
        grpc_allow_unsafe_rpcs = ()

    try:
        parsed_endpoints = json.loads(allow_endpoints_raw)
        allow_endpoints = (
            tuple(v for v in parsed_endpoints if isinstance(v, str))
            if isinstance(parsed_endpoints, list)
            else ()
        )
    except json.JSONDecodeError:
        logger.warning(
            "malformed_header_json", header="X-Allow-Endpoints", fallback="empty tuple"
        )
        allow_endpoints = ()

    return RequestContext(
        target_url=target_url,
        api_type=api_type,
        target_headers=target_headers,
        allow_unsafe_paths=allow_unsafe_paths,
        base_url=base_url,
        include_result=include_result,
        poll_paths=poll_paths,
        request_id=request_id,
        grpc_allow_unsafe_rpcs=grpc_allow_unsafe_rpcs,
        allow_endpoints=allow_endpoints,
        output_format=output_format,
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

    # Fall back to semantic prefix from URL (check headers, then config defaults)
    target_url = headers.get("x-target-url") or settings.DEFAULT_TARGET_URL or ""
    return get_tool_name_prefix(target_url)
