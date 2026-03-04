"""Endpoint allowlist filtering logic.

Shared by all protocol agents (REST, GraphQL, gRPC) to filter schemas
before the LLM sees them. Supports config (ops ceiling) + header
(per-session focus) with intersection semantics.
"""

from __future__ import annotations

import copy
import fnmatch
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


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


# ---------------------------------------------------------------------------
# gRPC: Service/method filtering
# ---------------------------------------------------------------------------


def filter_grpc_services(
    services: list[Any],
    config_patterns: tuple[str, ...] | None,
    header_patterns: tuple[str, ...] | None,
) -> list[Any]:
    """Filter gRPC services to only allowed methods.

    Match target: "package.Service/Method" (e.g., "helloworld.Greeter/SayHello").
    Returns new list — does not mutate the originals.
    Services with zero remaining methods are excluded.
    """
    if config_patterns is None and header_patterns is None:
        return services

    # Lazy import to avoid circular dependency: filtering → grpc.reflection → ...
    from .grpc.reflection import ServiceInfo

    filtered: list[Any] = []
    for svc in services:
        allowed_methods = [
            m
            for m in svc.methods
            if is_endpoint_allowed(f"{svc.full_name}/{m.name}", config_patterns, header_patterns)
        ]
        if allowed_methods:
            filtered.append(ServiceInfo(full_name=svc.full_name, methods=allowed_methods))

    return filtered


# ---------------------------------------------------------------------------
# GraphQL: Introspection schema filtering with transitive type closure
# ---------------------------------------------------------------------------

_BUILTIN_SCALARS = {"String", "Int", "Float", "Boolean", "ID"}


def _extract_type_name(type_ref: dict[str, Any] | None) -> str | None:
    """Extract leaf type name from a GraphQL introspection type reference.

    Unwraps NON_NULL and LIST wrappers:
        {kind: NON_NULL, ofType: {kind: LIST, ofType: {name: "User"}}} → "User"
    """
    if not type_ref:
        return None
    kind = type_ref.get("kind")
    if kind in ("NON_NULL", "LIST"):
        return _extract_type_name(type_ref.get("ofType"))
    return type_ref.get("name")


def _collect_referenced_types(
    type_name: str,
    all_types_by_name: dict[str, dict[str, Any]],
    collected: set[str],
) -> None:
    """Recursively collect all types referenced by the given type.

    Walks fields, args, inputFields, interfaces, possibleTypes.
    Skips built-in scalars.
    """
    if type_name in collected or type_name in _BUILTIN_SCALARS:
        return
    type_def = all_types_by_name.get(type_name)
    if not type_def:
        return
    # Skip scalar types (custom scalars have no fields to recurse into)
    if type_def.get("kind") == "SCALAR":
        collected.add(type_name)
        return
    collected.add(type_name)

    # Walk fields and their args
    for field in type_def.get("fields") or []:
        ref = _extract_type_name(field.get("type"))
        if ref:
            _collect_referenced_types(ref, all_types_by_name, collected)
        for arg in field.get("args") or []:
            ref = _extract_type_name(arg.get("type"))
            if ref:
                _collect_referenced_types(ref, all_types_by_name, collected)

    # Walk input fields (INPUT_OBJECT types)
    for field in type_def.get("inputFields") or []:
        ref = _extract_type_name(field.get("type"))
        if ref:
            _collect_referenced_types(ref, all_types_by_name, collected)

    # Walk interfaces
    for iface in type_def.get("interfaces") or []:
        name = iface.get("name")
        if name:
            _collect_referenced_types(name, all_types_by_name, collected)

    # Walk union possible types
    for pt in type_def.get("possibleTypes") or []:
        name = pt.get("name")
        if name:
            _collect_referenced_types(name, all_types_by_name, collected)


def filter_graphql_schema(
    schema: dict[str, Any],
    config_patterns: tuple[str, ...] | None,
    header_patterns: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Filter GraphQL introspection schema to only allowed query fields.

    Match target: "Query.fieldName" (e.g., "Query.users").
    Computes transitive closure of types referenced by allowed fields.
    Returns a new schema dict — does not mutate the original.
    """
    if config_patterns is None and header_patterns is None:
        return schema

    result = copy.deepcopy(schema)

    # Guard against missing or null queryType (e.g. subscription-only schemas)
    if "queryType" not in result or result["queryType"] is None:
        return result

    query_type = result["queryType"]
    query_fields = query_type.get("fields") or []

    # Filter query fields
    allowed_fields = [
        f
        for f in query_fields
        if is_endpoint_allowed(f"Query.{f['name']}", config_patterns, header_patterns)
    ]
    result["queryType"]["fields"] = allowed_fields

    # Compute transitive closure of referenced types
    all_types = result.get("types", [])
    types_by_name = {t["name"]: t for t in all_types if not t["name"].startswith("__")}

    referenced: set[str] = set()
    for field in allowed_fields:
        # Collect return type
        ret_type = _extract_type_name(field.get("type"))
        if ret_type:
            _collect_referenced_types(ret_type, types_by_name, referenced)
        # Collect argument types
        for arg in field.get("args") or []:
            arg_type = _extract_type_name(arg.get("type"))
            if arg_type:
                _collect_referenced_types(arg_type, types_by_name, referenced)

    # Always keep Query type + built-in scalars (present in introspection but not rendered)
    referenced.add("Query")

    # Filter types list — keep referenced types and built-in scalars
    result["types"] = [
        t for t in all_types if t["name"] in referenced or t["name"] in _BUILTIN_SCALARS
    ]

    return result
