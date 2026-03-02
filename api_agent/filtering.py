"""Endpoint allowlist filtering logic.

Shared by all protocol agents (REST, GraphQL, gRPC) to filter schemas
before the LLM sees them. Supports config (ops ceiling) + header
(per-session focus) with intersection semantics.
"""

from __future__ import annotations

import fnmatch


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
