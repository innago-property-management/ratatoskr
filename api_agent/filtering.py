"""Endpoint allowlist filtering logic.

Shared by all protocol agents (REST, GraphQL, gRPC) to filter schemas
before the LLM sees them. Supports config (ops ceiling) + header
(per-session focus) with intersection semantics.
"""

from __future__ import annotations

import copy
import fnmatch
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_config_allowlist(csv_value: str) -> tuple[str, ...] | None:
    """Parse CSV config value into tuple of patterns.

    Returns None if empty (= no constraint, allow all).
    """
    patterns = [p.strip() for p in csv_value.split(",") if p.strip()]
    return tuple(patterns) if patterns else None


def matches_any_pattern(target: str, patterns: tuple[str, ...] | None) -> bool:
    """Check if target matches any fnmatch pattern.

    None patterns = allow all (no constraint).
    Empty tuple = block all (explicit empty allowlist).
    """
    if patterns is None:
        return True
    return any(fnmatch.fnmatch(target, p) for p in patterns)


def is_endpoint_allowed(
    endpoint_key: str,
    config_patterns: tuple[str, ...] | None,
    header_patterns: tuple[str, ...] | None,
) -> bool:
    """Check if endpoint is allowed by both config and header allowlists.

    Intersection semantics:
    - Both None: allow all (backwards compatible)
    - Config only: must match config
    - Header only: must match header
    - Both set: must match at least one pattern from EACH
    """
    return matches_any_pattern(endpoint_key, config_patterns) and matches_any_pattern(
        endpoint_key, header_patterns
    )


# ---------------------------------------------------------------------------
# REST: OpenAPI spec filtering
# ---------------------------------------------------------------------------

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


def filter_openapi_spec(
    spec: dict[str, Any],
    config_patterns: tuple[str, ...] | None,
    header_patterns: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Filter OpenAPI spec to only allowed endpoints.

    Match target: "METHOD /path" (e.g., "GET /users/{id}").
    Returns a new spec dict — does not mutate the original.
    Components/schemas are always preserved.
    """
    if config_patterns is None and header_patterns is None:
        return spec

    result = copy.deepcopy(spec)
    paths = result.get("paths", {})
    if not isinstance(paths, dict):
        return result

    filtered_paths: dict[str, Any] = {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        filtered_item: dict[str, Any] = {}
        # Preserve non-method keys (parameters, x- extensions, etc.)
        for key, value in path_item.items():
            if key not in _HTTP_METHODS:
                filtered_item[key] = value

        # Filter methods
        for method in _HTTP_METHODS:
            if method not in path_item:
                continue
            endpoint_key = f"{method.upper()} {path}"
            if is_endpoint_allowed(endpoint_key, config_patterns, header_patterns):
                filtered_item[method] = path_item[method]

        # Only keep path if at least one method survived
        has_methods = any(k in filtered_item for k in _HTTP_METHODS)
        if has_methods:
            filtered_paths[path] = filtered_item

    result["paths"] = filtered_paths
    return result
