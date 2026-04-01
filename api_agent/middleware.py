"""FastMCP middleware for dynamic tool naming per session."""

import json
import re
from collections.abc import Sequence

from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import Tool as FastMCPTool
from fastmcp.tools.base import ToolResult
from mcp import types as mt
from mcp.types import TextContent

from .config import settings
from .context import MissingHeaderError, extract_api_name, get_full_hostname, get_request_context
from .recipe import build_api_id, build_recipe_docstring
from .recipe.common import create_params_model
from .recipe.naming import sanitize_tool_name
from .recipe.runner import execute_recipe_tool, load_schema_and_base_url
from .recipe.store import RECIPE_STORE, sha256_hex

# Internal tool name suffix pattern
INTERNAL_TOOL_PATTERN = re.compile(r"^_(.+)$")
MAX_TOOL_NAME_LEN = 60
RECIPE_NAME_PREFIX = "r"
RECIPE_PREFIX_STR = f"{RECIPE_NAME_PREFIX}_"


def _get_tool_suffix(internal_name: str) -> str:
    """Extract suffix from internal tool name (_query -> query)."""
    match = INTERNAL_TOOL_PATTERN.match(internal_name)
    return match.group(1) if match else internal_name


def _inject_api_context(description: str, hostname: str, api_type: str) -> str:
    """Inject API context into tool description using full hostname."""
    api_type_labels = {"graphql": "GraphQL", "grpc": "gRPC"}
    api_type_label = api_type_labels.get(api_type, "REST")
    prefix = f"[{hostname} {api_type_label} API] "
    return prefix + description


def _max_slug_length() -> int:
    """Calculate max slug length that fits within tool name limit."""
    return max(1, MAX_TOOL_NAME_LEN - len(RECIPE_PREFIX_STR))


def _build_recipe_tool_name(slug: str) -> str:
    """Build MCP tool name from a pre-sanitized slug."""
    base = f"{RECIPE_NAME_PREFIX}_{slug}"
    if len(base) <= MAX_TOOL_NAME_LEN:
        return base
    max_slug = _max_slug_length()
    return f"{RECIPE_NAME_PREFIX}_{slug[:max_slug]}"


def _build_recipe_input_schema(params_spec: dict, tool_name: str) -> dict:
    """Build flat JSON Schema for recipe tool input.

    All declared params are top-level required fields (no defaults).
    Uses Pydantic ``create_params_model`` for schema generation.
    """
    Model = create_params_model(params_spec, tool_name)
    schema = Model.model_json_schema()

    # Add return_directly as optional top-level field
    schema["properties"]["return_directly"] = {"type": "boolean", "default": True}
    schema.pop("title", None)
    schema["additionalProperties"] = False

    return schema


async def _list_recipe_tools(
    hostname: str,
    req_ctx,
    raw_schema: str,
    base_url: str,
) -> list[FastMCPTool]:
    if not settings.ENABLE_RECIPES:
        return []

    if not raw_schema:
        return []

    schema_hash = sha256_hex(raw_schema)
    api_id = build_api_id(req_ctx, req_ctx.api_type, base_url)
    recipes = await RECIPE_STORE.list_recipes(api_id=api_id, schema_hash=schema_hash)
    tools: list[FastMCPTool] = []

    # Group by tool slug (truncated to fit name) and pick most recent
    max_slug_len = _max_slug_length()
    by_slug: dict[str, list[dict]] = {}
    for r in recipes:
        name = r.get("tool_name") or "recipe"
        slug = sanitize_tool_name(name)[:max_slug_len]
        by_slug.setdefault(slug, []).append(r)

    for slug, group in by_slug.items():
        group.sort(key=lambda r: (r.get("last_used_at", 0), r.get("created_at", 0)), reverse=True)
        r = group[0]
        tool_name = _build_recipe_tool_name(slug)
        params_spec = r.get("params", {}) or {}
        desc = build_recipe_docstring(
            r.get("question", ""),
            r.get("steps", []),
            r.get("sql_steps", []),
            req_ctx.api_type,
            params_spec,
        )
        desc += f"\nRecipe Name: {r.get('tool_name') or 'recipe'}\n"
        if len(group) > 1:
            desc += f"Note: {len(group)} recipes share this name; using most recent.\n"
        description = _inject_api_context(desc, hostname, req_ctx.api_type)
        tools.append(
            FastMCPTool(
                name=tool_name,
                description=description,
                parameters=_build_recipe_input_schema(params_spec, slug),
                tags={"recipe"},
            )
        )

    return tools


class DynamicToolNamingMiddleware(Middleware):
    """Middleware that dynamically names tools based on session context."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next,
    ) -> Sequence[FastMCPTool]:
        """Transform tool names and descriptions based on session headers."""
        tools: Sequence[FastMCPTool] = await call_next(context)

        try:
            headers = get_http_headers()
        except LookupError:
            # No HTTP context (e.g., stdio transport) - return unchanged
            return tools

        try:
            req_ctx = get_request_context()
        except MissingHeaderError as e:
            raise RuntimeError(str(e)) from e

        raw_schema, base_url = await load_schema_and_base_url(req_ctx)
        if not raw_schema and req_ctx.api_type != "grpc":
            schema_type = "GraphQL" if req_ctx.api_type == "graphql" else "OpenAPI"
            raise RuntimeError(
                f"Failed to load {schema_type} schema. Check X-Target-URL and auth headers."
            )

        target_url = req_ctx.target_url
        api_type = req_ctx.api_type or "api"

        # Short prefix for tool name, full hostname for description
        name_prefix = extract_api_name(headers)
        full_hostname = get_full_hostname(target_url)

        transformed = []
        for tool in tools:
            suffix = _get_tool_suffix(tool.name)
            new_name = f"{name_prefix}_{suffix}"
            new_desc = _inject_api_context(tool.description or "", full_hostname, api_type)

            modified_tool = tool.model_copy(update={"name": new_name, "description": new_desc})
            transformed.append(modified_tool)

        recipe_tools = await _list_recipe_tools(full_hostname, req_ctx, raw_schema, base_url)
        return [*transformed, *recipe_tools]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next,
    ) -> ToolResult:
        """Validate and transform tool name back to internal name."""
        try:
            headers = get_http_headers()
        except LookupError:
            # No HTTP context - pass through unchanged
            return await call_next(context)

        api_name = extract_api_name(headers)
        tool_name = context.message.name

        # Handle recipe tools directly
        if tool_name.startswith(RECIPE_PREFIX_STR):
            recipe_slug = tool_name.removeprefix(RECIPE_PREFIX_STR)
            try:
                req_ctx = get_request_context()
            except MissingHeaderError as e:
                raise ToolError(str(e)) from e

            arguments = context.message.arguments or {}
            if not isinstance(arguments, dict):
                raise ValidationError("Invalid arguments: expected object.")

            return_directly = bool(arguments.get("return_directly", True))
            params = {k: v for k, v in arguments.items() if k != "return_directly"} or None

            raw_schema, base_url = await load_schema_and_base_url(req_ctx)
            if not raw_schema:
                raise ToolError("schema not loaded")

            schema_hash = sha256_hex(raw_schema)
            api_id = build_api_id(req_ctx, req_ctx.api_type, base_url)
            recipe_meta = await RECIPE_STORE.find_recipe_by_tool_slug(
                api_id=api_id,
                schema_hash=schema_hash,
                tool_slug=recipe_slug,
                max_slug_len=_max_slug_length(),
            )
            if not recipe_meta:
                raise NotFoundError(f"recipe not found: {recipe_slug}")

            recipe_id = recipe_meta["recipe_id"]
            result = await execute_recipe_tool(
                req_ctx,
                recipe_id,
                params,
                return_directly,
                raw_schema=raw_schema,
                base_url=base_url,
            )

            # Parse and handle errors from recipe execution
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = None

            if isinstance(parsed, dict) and parsed.get("success") is False:
                err_msg = parsed.get("error", "recipe execution failed")
                if isinstance(err_msg, str) and err_msg.startswith(
                    ("missing required param:", "unexpected params:")
                ):
                    raise ValidationError(err_msg)
                raise ToolError(err_msg)

            return ToolResult(content=[TextContent(type="text", text=result)])

        # Validate tool name matches session's API for non-recipe tools
        expected_prefix = f"{api_name}_"
        if not tool_name.startswith(expected_prefix):
            raise NotFoundError(
                f"Tool '{tool_name}' not valid for API '{api_name}'. "
                f"Expected tool name starting with '{expected_prefix}' "
                "or recipe tool prefix 'r_'."
            )

        # Transform back to internal name (_suffix)
        suffix = tool_name.removeprefix(expected_prefix)
        internal_name = f"_{suffix}"

        # Create modified context with internal tool name
        modified_params = mt.CallToolRequestParams(
            name=internal_name,
            arguments=context.message.arguments,
        )
        modified_context = context.copy(message=modified_params)

        return await call_next(modified_context)
