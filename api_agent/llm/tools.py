"""Tool decorator — replaces @function_tool from the Agents SDK.

Extracts parameter schema from type hints and docstrings.
"""

from __future__ import annotations

import inspect
import re
import types
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin, get_type_hints

from .types import ToolDefinition

# Python type → JSON Schema type
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _parse_docstring_params(doc: str | None) -> dict[str, str]:
    """Extract param descriptions from Google-style docstring Args section."""
    if not doc:
        return {}
    descriptions: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if stripped.lower().startswith("returns:") or stripped.lower().startswith("raises:"):
                break
            m = re.match(r"(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)", stripped)
            if m:
                descriptions[m.group(1)] = m.group(2).strip()
    return descriptions


def tool(fn: Callable[..., Any]) -> ToolDefinition:
    """Decorator that converts a Python function into a ToolDefinition.

    Usage:
        @tool
        def my_tool(query: str, limit: int = 10) -> str:
            '''Search for things.

            Args:
                query: Search query string
                limit: Max results to return
            '''
            ...
    """
    hints = get_type_hints(fn)
    sig = inspect.signature(fn)
    doc = inspect.getdoc(fn) or ""
    param_docs = _parse_docstring_params(doc)

    # Build JSON Schema properties
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        hint = hints.get(name, str)

        # Unwrap Optional[T] (Union[T, None]) to get the inner type
        origin = get_origin(hint)
        if origin is Union or origin is types.UnionType:
            args = [a for a in get_args(hint) if a is not type(None)]
            if args:
                hint = args[0]

        json_type = _TYPE_MAP.get(hint, "string")

        prop: dict[str, Any] = {"type": json_type}
        if name in param_docs:
            prop["description"] = param_docs[name]

        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(name)

        properties[name] = prop

    # Extract first line of docstring as description
    fn_name = getattr(fn, "__name__", "unknown")
    description = doc.split("\n")[0].strip() if doc else fn_name

    parameters = {
        "type": "object",
        "properties": properties,
    }
    if required:
        parameters["required"] = required

    return ToolDefinition(
        name=fn_name,
        description=description,
        parameters=parameters,
        function=fn,
    )
