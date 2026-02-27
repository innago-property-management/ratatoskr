"""LLM-assisted extraction of parameterized recipes from successful executions."""

from __future__ import annotations

import json
import re
from typing import Any

pass  # LLM imports deferred to extract_recipe()

from .store import get_example_values, normalize_ws, render_param_refs, render_text_template

_EXTRACTOR_INSTRUCTIONS = """You are a recipe extractor. Convert executed API calls into reusable templates.

INPUT:
- api_type: "graphql", "rest", or "grpc"
- question: user's question
- steps: executed API calls (preserve order)
- sql_steps: executed SQL queries (preserve order)
- existing_recipes: list of existing recipes for this API/schema (tool_name, question, steps, sql_steps, params)

OUTPUT: Single JSON object (no markdown):
{
  "tool_name": "<python_function_name>",
  "params": {"paramName": {"type": "str|int|float|bool", "default": <value_from_execution>}},
  "steps": [<same length as input>],
  "sql_steps": [<same length as input>]
}

TOOL_NAME REQUIREMENTS:
- snake_case Python identifier (lowercase, underscores only)
- Max 40 characters
- Start with a verb (get, list, fetch, find, search, etc.)
- Descriptive but concise (e.g., "get_recent_users" not "get_all_users_who_registered_recently")
- No special characters, only letters, numbers, underscores
- If an existing recipe already matches this execution, reuse its tool_name.
- Otherwise choose a tool_name not in existing_recipes.

STEP FORMATS:
- GraphQL: {"kind": "graphql", "name": "...", "query_template": "...{{param}}..."}
- REST: {"kind": "rest", "name": "...", "method": "GET", "path": "/x", "path_params": {}, "query_params": {}, "body": {}}
  Use {"$param": "paramName"} for parameterized values in REST objects.
- gRPC: {"kind": "grpc", "name": "...", "method": "/pkg.Service/Method", "request": {...}}
  Use {"$param": "paramName"} for parameterized values in the request object.
- SQL: Use {{param}} for parameterized values in sql_steps strings.
  Example: "WHERE name ILIKE '{{startsWith}}%'" with param startsWith default "A"

PARAMETERIZE these (user-specific values):
- IDs, limits, offsets, search terms, filters, dates, LIKE/ILIKE patterns

DO NOT parameterize:
- API paths, HTTP methods, field names, table names, static config

RULES:
- Keep SAME number of steps in SAME order
- Default values MUST match the original execution (so template renders back to original)
- Output valid JSON only
"""


def _parse_json_maybe(text: str) -> dict[str, Any] | None:
    """Parse JSON dict, extracting from surrounding text if needed."""
    if not text:
        return None

    # Try direct parse
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            val = json.loads(text[start : end + 1])
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass

    return None


def _get_params_defaults(params_spec: dict[str, Any] | None) -> dict[str, Any]:
    return get_example_values(params_spec or {}, {})


def _canon_obj(v: Any) -> Any:
    """Normalize None to empty dict for comparisons."""
    return {} if v is None else v


_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def _find_used_params(recipe: dict[str, Any], api_type: str) -> set[str]:
    """Find all {{param}} and $param references in recipe templates."""
    used: set[str] = set()

    # Check sql_steps for {{param}}
    for sql in recipe.get("sql_steps", []):
        if isinstance(sql, str):
            used.update(_PLACEHOLDER_RE.findall(sql))

    # Check steps
    for step in recipe.get("steps", []):
        if not isinstance(step, dict):
            continue
        # GraphQL: check query_template
        if api_type == "graphql":
            tmpl = step.get("query_template", "")
            if isinstance(tmpl, str):
                used.update(_PLACEHOLDER_RE.findall(tmpl))
        # gRPC: check $param refs in request object
        elif api_type == "grpc":
            _find_param_refs(step.get("request"), used)
        # REST: check $param refs in path_params, query_params, body
        else:
            for key in ("path_params", "query_params", "body"):
                _find_param_refs(step.get(key), used)

    return used


def _find_param_refs(obj: Any, found: set[str]) -> None:
    """Recursively find {'$param': 'name'} refs."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"$param"} and isinstance(obj.get("$param"), str):
            found.add(obj["$param"])
        else:
            for v in obj.values():
                _find_param_refs(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _find_param_refs(v, found)


def _validate_step_graphql(orig: dict, recipe_step: dict, params: dict) -> bool:
    """Validate GraphQL step renders to original."""
    if recipe_step.get("name") != orig.get("name"):
        return False
    tmpl = recipe_step.get("query_template")
    if not isinstance(tmpl, str):
        return False
    return normalize_ws(render_text_template(tmpl, params)) == normalize_ws(
        str(orig.get("query", ""))
    )


def _validate_step_grpc(orig: dict, recipe_step: dict, params: dict) -> bool:
    """Validate gRPC step renders to original."""
    if recipe_step.get("name") != orig.get("name"):
        return False
    if recipe_step.get("method") != orig.get("method"):
        return False
    rendered = render_param_refs(_canon_obj(recipe_step.get("request")), params)
    orig_req = orig.get("request")
    # Original request is stored as JSON string — parse for comparison
    if isinstance(orig_req, str):
        try:
            orig_req = json.loads(orig_req)
        except (json.JSONDecodeError, TypeError):
            pass
    return rendered == _canon_obj(orig_req)


def _validate_step_rest(orig: dict, recipe_step: dict, params: dict) -> bool:
    """Validate REST step renders to original."""
    if recipe_step.get("name") != orig.get("name"):
        return False
    if str(recipe_step.get("method", "")).upper() != str(orig.get("method", "")).upper():
        return False
    if recipe_step.get("path") != orig.get("path"):
        return False

    for key in ("path_params", "query_params", "body"):
        rendered = render_param_refs(_canon_obj(recipe_step.get(key)), params)
        if rendered != _canon_obj(orig.get(key)):
            return False
    return True


def _validate_equivalence(
    *,
    api_type: str,
    original_steps: list[dict[str, Any]],
    original_sql: list[str],
    recipe: dict[str, Any],
) -> bool:
    """Validate recipe renders back to original execution."""
    params_spec = recipe.get("params")
    params = _get_params_defaults(params_spec if isinstance(params_spec, dict) else {})

    r_steps = recipe.get("steps")
    r_sql = recipe.get("sql_steps")
    if not isinstance(r_steps, list) or not isinstance(r_sql, list):
        return False
    if len(r_steps) != len(original_steps) or len(r_sql) != len(original_sql):
        return False

    for orig, rec in zip(original_steps, r_steps):
        if not isinstance(rec, dict) or rec.get("kind") != orig.get("kind"):
            return False

        if api_type == "graphql":
            validator = _validate_step_graphql
        elif api_type == "grpc":
            validator = _validate_step_grpc
        else:
            validator = _validate_step_rest
        if not validator(orig, rec, params):
            return False

    for o_sql, r_tmpl in zip(original_sql, r_sql):
        if not isinstance(r_tmpl, str):
            return False
        if normalize_ws(render_text_template(r_tmpl, params)) != normalize_ws(o_sql):
            return False

    return True


async def extract_recipe(
    *,
    api_type: str,
    question: str,
    steps: list[dict[str, Any]],
    sql_steps: list[str],
    existing_recipes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Extract parameterized recipe from execution trace. Returns recipe or None."""
    from ..agent.model import provider

    payload = {
        "api_type": api_type,
        "question": question,
        "steps": steps,
        "sql_steps": sql_steps,
        "existing_recipes": existing_recipes or [],
    }

    messages = [
        {"role": "system", "content": _EXTRACTOR_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(payload, indent=2)},
    ]

    response = await provider.complete(messages, temperature=0.0, max_tokens=4096)
    if not response.content:
        return None

    recipe = _parse_json_maybe(response.content)
    if not recipe:
        return None

    # Basic structure check
    if "steps" not in recipe or "sql_steps" not in recipe:
        return None
    if not isinstance(recipe.get("params"), dict):
        recipe["params"] = {}

    # Validate tool_name: must be valid Python identifier, max 40 chars
    tool_name = recipe.get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if not re.match(r"^[a-z][a-z0-9_]{0,39}$", tool_name):
        return None

    # Validate declared params are actually used in templates
    declared_params = set(recipe.get("params", {}).keys())
    used_params = _find_used_params(recipe, api_type)
    if declared_params and not used_params:
        # Params declared but none used - LLM didn't parameterize templates
        return None
    if declared_params != used_params:
        # Mismatch - prune unused params, reject if used params undeclared
        undeclared = used_params - declared_params
        if undeclared:
            return None  # Template refs param not in params spec
        # Remove unused declared params
        recipe["params"] = {k: v for k, v in recipe["params"].items() if k in used_params}

    # Core validation: render(template, defaults) == original
    if not _validate_equivalence(
        api_type=api_type,
        original_steps=steps,
        original_sql=sql_steps,
        recipe=recipe,
    ):
        return None

    return recipe
