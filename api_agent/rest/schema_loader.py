"""OpenAPI 3.x spec loader and compact schema context builder."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import yaml

from ..config import settings

logger = logging.getLogger(__name__)


async def load_openapi_spec(
    spec_url: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Load OpenAPI 3.x spec from URL.

    Args:
        spec_url: URL to OpenAPI 3.x spec
        headers: Optional auth headers

    Returns:
        Parsed OpenAPI spec dict, or empty dict on error.
    """
    if not spec_url:
        return {}

    request_headers = dict(headers) if headers else {}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(spec_url, headers=request_headers)
            resp.raise_for_status()
            raw = resp.text

        # Parse JSON or YAML
        if raw.strip().startswith("{"):
            spec = json.loads(raw)
        else:
            spec = yaml.safe_load(raw)

        if not isinstance(spec, dict):
            logger.warning("OpenAPI spec root is not an object")
            return {}

        # Validate OpenAPI 3.x
        openapi_version = spec.get("openapi", "")
        if not isinstance(openapi_version, str) or not openapi_version.startswith("3."):
            logger.warning(f"Unsupported OpenAPI version: {openapi_version}, expected 3.x")
            return {}

        return spec

    except Exception as e:
        logger.exception(f"Failed to load OpenAPI spec: {e}")
        return {}


def get_base_url_from_spec(spec: dict[str, Any], spec_url: str = "") -> str:
    """Extract base URL from OpenAPI spec's servers[0], or derive from spec URL."""
    servers = spec.get("servers", [])
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            url = first.get("url", "")
            if isinstance(url, str):
                return url

    # Fallback: derive from spec URL (e.g., https://api.example.com/openapi.json -> https://api.example.com)
    if spec_url:
        from urllib.parse import urlparse

        parsed = urlparse(spec_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

    return ""


def _infer_string_format(field_name: str) -> str:
    """Infer string format from field name when not in schema."""
    if not field_name:
        return ""
    name_lower = field_name.lower()
    if "datetime" in name_lower:
        return "date-time"
    if "date" in name_lower and "update" not in name_lower:
        return "date"
    if "time" in name_lower and "update" not in name_lower:
        return "time"
    return ""


def _schema_to_type(
    schema: Any,
    schemas: dict[str, Any] | None = None,
    field_name: str = "",
) -> str:
    """Convert JSON Schema to compact type notation."""
    # OpenAPI 3.1 / JSON Schema allows boolean schemas (true/false)
    if schema is True or schema is False:
        return "any"

    if not schema:
        return "any"

    if not isinstance(schema, dict):
        return "any"

    # Handle $ref
    if "$ref" in schema:
        ref = schema["$ref"]
        # Extract type name from #/components/schemas/TypeName
        if isinstance(ref, str):
            return ref.split("/")[-1]
        return "any"

    schema_type = schema.get("type", "any")
    if not isinstance(schema_type, (str, list)):
        return "any"
    # OpenAPI 3.1 allows type arrays like ["string", "null"] - use first non-null type
    if isinstance(schema_type, list):
        non_null = [t for t in schema_type if t != "null"]
        schema_type = non_null[0] if non_null else "any"

    if schema_type == "array":
        items = schema.get("items", {})
        return f"{_schema_to_type(items, schemas)}[]"

    if schema_type == "object":
        # Check for additionalProperties (dict type)
        additional = schema.get("additionalProperties")
        if additional is True:
            return "dict[str, any]"
        if isinstance(additional, dict):
            val_type = _schema_to_type(additional, schemas)
            return f"dict[str, {val_type}]"
        if additional is not None:
            return "dict[str, any]"
        return "object"

    # Preserve string format (date-time, date, uri, etc.)
    if schema_type == "string":
        fmt = schema.get("format", "") or _infer_string_format(field_name)
        return f"str({fmt})" if fmt else "str"

    type_map = {
        "integer": "int",
        "number": "float",
        "boolean": "bool",
    }
    return type_map.get(schema_type, schema_type)


def _format_params(params: list[Any]) -> str:
    """Format ONLY required parameters. Optional params stripped."""
    parts = []
    for p in params:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        required = p.get("required", p.get("in") == "path")  # path params always required
        if not required:
            continue  # Skip optional params
        schema = p.get("schema", {})
        type_str = _schema_to_type(schema, field_name=name)
        parts.append(f"{name}: {type_str}")
    return ", ".join(parts)


def _extract_response_type(responses: Any) -> str:
    """Extract return type from responses."""
    if not isinstance(responses, dict):
        return "any"

    # Check 200/201 responses
    for code in ["200", "201", "default"]:
        if code in responses:
            resp = responses[code]
            if not isinstance(resp, dict):
                continue
            content = resp.get("content", {})
            if not isinstance(content, dict):
                continue
            json_content = content.get("application/json", {})
            if not isinstance(json_content, dict):
                continue
            schema = json_content.get("schema", {})
            if schema:
                return _schema_to_type(schema)
    return "any"


def _format_schema(name: str, schema: Any) -> str:
    """Format schema definition with ONLY required fields.

    Optional fields are stripped to prevent agent from inventing values.
    """
    if schema is True or schema is False or not isinstance(schema, dict):
        return f"{name}: {_schema_to_type(schema)}"

    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            props = {}
        raw_required = schema.get("required", [])
        if not isinstance(raw_required, list):
            raw_required = []
        required = set(r for r in raw_required if isinstance(r, str))
        fields = []
        # Only include required fields
        for field_name, field_schema in props.items():
            if field_name not in required:
                continue  # Skip optional fields
            field_type = _schema_to_type(field_schema, field_name=field_name)
            fields.append(f"{field_name}: {field_type}!")
        return f"{name} {{ {', '.join(fields)} }}"

    elif schema.get("enum"):
        enum_vals = schema.get("enum")
        if not isinstance(enum_vals, list):
            enum_vals = [enum_vals]
        vals = " | ".join(str(v) for v in enum_vals)
        return f"{name}: enum({vals})"

    return f"{name}: {_schema_to_type(schema)}"


def build_schema_context(spec: dict[str, Any]) -> str:
    """Build compact schema context from OpenAPI spec.

    Format:
        <endpoints>
        GET /users(limit?: int, offset?: int) -> User[]  # List users
        GET /users/{id}() -> User  # Get user by ID

        <schemas>
        User { id: str!, name: str!, email?: str }

        <auth>
        Bearer token in Authorization header
    """
    if not spec:
        return ""

    lines = ["<endpoints>"]

    # Process paths
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        paths = {}
    for path, path_item in paths.items():
        # Skip OpenAPI extension keys (e.g., x-foo) and malformed entries
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        if not isinstance(path_item, dict):
            continue
        for method in ["get", "post", "put", "delete", "patch"]:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue

            params = op.get("parameters", [])
            if not isinstance(params, list):
                params = []
            # Also include path-level parameters
            path_params = path_item.get("parameters", [])
            if not isinstance(path_params, list):
                path_params = []
            params = path_params + params

            # Extract request body type for POST/PUT/PATCH
            body_type = ""
            if method in ["post", "put", "patch"]:
                req_body = op.get("requestBody", {})
                if isinstance(req_body, dict):
                    content = req_body.get("content", {})
                    if isinstance(content, dict):
                        json_content = content.get("application/json", {})
                        if isinstance(json_content, dict):
                            body_schema = json_content.get("schema", {})
                            if body_schema:
                                required = bool(req_body.get("required", False))
                                body_type = _schema_to_type(body_schema)
                                suffix = "!" if required else ""
                                body_type = f"body: {body_type}{suffix}"

            param_str = _format_params(params)
            # Prepend body type if present
            if body_type:
                param_str = f"{body_type}, {param_str}" if param_str else body_type

            response_type = _extract_response_type(op.get("responses", {}))
            summary = op.get("description") or op.get("summary") or op.get("operationId", "")
            if not isinstance(summary, str):
                summary = ""

            desc = f"  # {summary}" if summary else ""
            lines.append(f"{method.upper()} {path}({param_str}) -> {response_type}{desc}")

    # Process schemas
    components = spec.get("components", {})
    if not isinstance(components, dict):
        components = {}
    schemas = components.get("schemas", {})
    if isinstance(schemas, dict) and schemas:
        lines.append("\n<schemas>")
        for name, schema in schemas.items():
            lines.append(_format_schema(name, schema))

    # Auth info
    security_schemes = components.get("securitySchemes", {})
    if isinstance(security_schemes, dict) and security_schemes:
        lines.append("\n<auth>")
        for name, scheme in security_schemes.items():
            if not isinstance(scheme, dict):
                continue
            scheme_type = scheme.get("type", "")
            if scheme_type == "http":
                bearer_format = scheme.get("bearerFormat", "")
                lines.append(f"{name}: HTTP {scheme.get('scheme', '')} {bearer_format}".strip())
            elif scheme_type == "apiKey":
                in_loc = scheme.get("in", "")
                key_name = scheme.get("name", "")
                lines.append(f"{name}: API key in {in_loc} '{key_name}'")
            elif scheme_type == "oauth2":
                lines.append(f"{name}: OAuth2")
            else:
                lines.append(f"{name}: {scheme_type}")

    return "\n".join(lines)


async def fetch_schema_context(
    spec_url: str,
    headers: dict[str, str] | None = None,
    spec_filter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    question: str = "",
) -> tuple[str, str, str]:
    """Fetch and build schema context.

    Args:
        spec_url: URL to OpenAPI spec
        headers: Optional auth headers
        spec_filter: Optional filter to apply to spec before building DSL
            (used by endpoint allowlist to remove non-allowed paths)
        question: User's NL question (guides smart schema reduction)

    Returns:
        Tuple of (reduced_context, base_url, raw_spec_json)
    """
    spec = await load_openapi_spec(spec_url, headers)
    if not spec:
        return "", "", ""

    # Apply endpoint allowlist filter (if configured)
    if spec_filter is not None:
        spec = spec_filter(spec)

    # Raw spec JSON for grep-like search (preserves all info — FILTERED)
    try:
        raw_spec_json = json.dumps(spec, indent=2)
    except TypeError:
        raw_spec_json = json.dumps(spec, indent=2, default=str)

    # Build DSL for LLM context (from FILTERED spec)
    try:
        dsl_context = build_schema_context(spec)
    except Exception:
        logger.exception("Failed to build OpenAPI schema context")
        dsl_context = ""
    base_url = get_base_url_from_spec(spec, spec_url)

    # Smart schema reduction (TOON + Haiku + hard truncation fallback)
    from ..schema.reducer import reduce_schema

    result = await reduce_schema(
        schema_text=dsl_context,
        question=question,
        threshold=settings.MAX_SCHEMA_CHARS,
        api_key=settings.SCHEMA_REDUCTION_API_KEY,
        model=settings.SCHEMA_REDUCTION_MODEL,
        timeout_ms=settings.SCHEMA_REDUCTION_TIMEOUT_MS,
        enabled=settings.SCHEMA_REDUCTION_ENABLED,
        max_input_chars=settings.SCHEMA_REDUCTION_MAX_INPUT_CHARS,
        max_output_tokens=settings.SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS,
    )
    context = result.schema_text

    return context, base_url, raw_spec_json
