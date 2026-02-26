"""Recipe module for parameterized API-call + SQL pipeline caching."""

from .common import (
    _return_directly_flag,
    _sanitize_for_tool_name,
    _set_return_directly,
    build_api_id,
    build_partial_result,
    build_recipe_context,
    build_recipe_docstring,
    consume_recipe_changes,
    create_params_model,
    deduplicate_tool_name,
    execute_recipe_steps,
    format_recipe_response,
    maybe_extract_and_save_recipe,
    reset_recipe_change_flag,
    search_recipes,
    validate_and_prepare_recipe,
    validate_recipe_params,
)
from .extractor import extract_recipe
from .store import (
    RECIPE_STORE,
    RecipeRecord,
    RecipeStore,
    get_example_values,
    render_param_refs,
    render_text_template,
    sha256_hex,
)

__all__ = [
    # Store
    "RECIPE_STORE",
    "_return_directly_flag",
    "_sanitize_for_tool_name",
    "_set_return_directly",
    "RecipeStore",
    "RecipeRecord",
    "sha256_hex",
    "render_text_template",
    "render_param_refs",
    "get_example_values",
    # Extractor
    "extract_recipe",
    # Common
    "build_recipe_docstring",
    "consume_recipe_changes",
    "create_params_model",
    "deduplicate_tool_name",
    "execute_recipe_steps",
    "format_recipe_response",
    "maybe_extract_and_save_recipe",
    "build_partial_result",
    "build_api_id",
    "build_recipe_context",
    "reset_recipe_change_flag",
    "search_recipes",
    "validate_and_prepare_recipe",
    "validate_recipe_params",
]
