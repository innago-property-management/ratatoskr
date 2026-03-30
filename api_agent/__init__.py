"""API Agent - MCP server for querying APIs in natural language."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("api-agent-ratatoskr")
except PackageNotFoundError:
    __version__ = "unknown"

from .exceptions import APIAgentError, ProviderError, RecipeError, SchemaError

__all__ = ["APIAgentError", "ProviderError", "RecipeError", "SchemaError"]
