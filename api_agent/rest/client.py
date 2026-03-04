"""REST API client with unsafe method blocking."""

import fnmatch
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import structlog

from ..pool import pool

logger = structlog.get_logger(__name__)

# Unsafe HTTP methods (blocked by default)
_UNSAFE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _is_path_allowed(path: str, patterns: list[str]) -> bool:
    """Check if path matches any allowed pattern (fnmatch glob)."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _build_url(
    path: str,
    base_url: str,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
) -> str:
    """Build full URL with path and query params.

    Args:
        path: API path (e.g., /users/{id})
        base_url: Base URL for API
        path_params: Values to substitute in path (e.g., {"id": "123"})
        query_params: Query string parameters (e.g., {"limit": 10})

    Returns:
        Full URL string
    """
    # Substitute path params
    if path_params:
        for key, value in path_params.items():
            path = path.replace(f"{{{key}}}", str(value))

    if not base_url:
        raise ValueError("No base URL provided")

    # If base_url is missing a scheme, default to https
    if "://" not in base_url:
        base_url = f"https://{base_url}"

    # Validate that the URL has a proper netloc (host)
    parsed = urlparse(base_url)
    if not parsed.netloc:
        raise ValueError(f"Invalid base URL (no host): {base_url}")

    url = base_url.rstrip("/") + "/" + path.lstrip("/")

    # Add query params
    if query_params:
        # Filter out None values
        filtered = {k: v for k, v in query_params.items() if v is not None}
        if filtered:
            url = f"{url}?{urlencode(filtered, doseq=True)}"

    return url


async def execute_request(
    method: str,
    path: str,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    allow_unsafe: bool = False,
    allow_unsafe_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Execute REST API request. Unsafe methods blocked unless explicitly allowed.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, PATCH)
        path: API path (e.g., /users/{id})
        path_params: Values to substitute in path
        query_params: Query string parameters
        body: Request body (for POST/PUT/PATCH)
        base_url: Base URL for API (required)
        headers: Headers to send (e.g., Authorization)
        allow_unsafe: Allow all POST/PUT/DELETE/PATCH methods
        allow_unsafe_paths: Glob patterns for paths where unsafe methods are allowed

    Returns:
        Dict with success/data or error
    """
    method = method.upper()

    # Block unsafe methods by default
    if method in _UNSAFE_METHODS and not allow_unsafe:
        # Check if path matches allowlist
        if not allow_unsafe_paths or not _is_path_allowed(path, allow_unsafe_paths):
            return {
                "success": False,
                "error": f"{method} method not allowed (read-only mode). Use X-Allow-Unsafe-Paths header.",
            }

    try:
        url = _build_url(path, base_url, path_params, query_params)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    # Log request details without leaking header values (e.g., auth tokens).
    logger.info(
        "rest_request",
        method=method,
        base_url=base_url,
        path=path,
        url=url,
        header_keys=sorted(request_headers.keys()),
    )

    client = await pool.get_http_client(base_url)
    try:
        if method == "GET":
            resp = await client.get(url, headers=request_headers)
        elif method in {"POST", "PUT", "PATCH"}:
            request_headers["Content-Type"] = "application/json"
            resp = await client.request(method, url, json=body, headers=request_headers)
        elif method == "DELETE":
            resp = await client.delete(url, headers=request_headers)
        else:
            return {"success": False, "error": f"Unsupported method: {method}"}

        resp.raise_for_status()

        # Handle different content types
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            data = resp.json()
        else:
            data = resp.text

        return {"success": True, "data": data}

    except httpx.HTTPStatusError as e:
        # Try to get error body
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text[:500]
        return {
            "success": False,
            "error": f"HTTP {e.response.status_code}: {error_body}",
        }
    except Exception as e:
        logger.exception("rest_api_error")
        return {"success": False, "error": str(e)}
