"""Tests for MCP recipe support — step executor, extraction, validation, equivalence."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api_agent.context import RequestContext
from api_agent.recipe.common import (
    _recipes_equivalent,
    build_api_id,
    build_recipe_docstring,
)
from api_agent.recipe.extractor import (
    _find_used_params,
    _validate_step_mcp,
)
from api_agent.recipe.store import RECIPE_STORE, sha256_hex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_ctx(url: str = "mcp://github") -> RequestContext:
    return RequestContext(
        target_url=url,
        target_headers={"Authorization": "Bearer test"},
        api_type="mcp",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


def _make_call_result(data: dict[str, Any], is_error: bool = False) -> MagicMock:
    """Build a mock MCP call_tool result."""
    result = MagicMock()
    content_item = MagicMock()
    content_item.type = "text"
    content_item.text = json.dumps(data)
    result.content = [content_item]
    result.isError = is_error
    return result


# ---------------------------------------------------------------------------
# build_api_id
# ---------------------------------------------------------------------------


class TestMcpBuildApiId:
    def test_mcp_api_id_format(self):
        ctx = _mcp_ctx()
        api_id = build_api_id(ctx, "mcp")
        assert api_id == "mcp:mcp://github"

    def test_mcp_api_id_different_url(self):
        ctx = _mcp_ctx("mcp://filesystem")
        api_id = build_api_id(ctx, "mcp")
        assert api_id == "mcp:mcp://filesystem"


# ---------------------------------------------------------------------------
# _recipes_equivalent for MCP
# ---------------------------------------------------------------------------


class TestMcpRecipesEquivalent:
    def test_equivalent_mcp_recipes(self):
        a = {
            "params": {"repo": {"type": "str", "default": "ratatoskr"}},
            "steps": [
                {
                    "kind": "mcp",
                    "name": "data",
                    "tool_name": "read_repo",
                    "arguments": '{"repo": "ratatoskr"}',
                }
            ],
            "sql_steps": [],
        }
        b = {
            "params": {"repo": {"type": "str", "default": "ratatoskr"}},
            "steps": [
                {
                    "kind": "mcp",
                    "name": "data",
                    "tool_name": "read_repo",
                    "arguments": '{"repo": "ratatoskr"}',
                }
            ],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "mcp") is True

    def test_different_tool_name_not_equivalent(self):
        a = {
            "params": {},
            "steps": [
                {"kind": "mcp", "name": "data", "tool_name": "read_repo", "arguments": "{}"}
            ],
            "sql_steps": [],
        }
        b = {
            "params": {},
            "steps": [
                {"kind": "mcp", "name": "data", "tool_name": "write_repo", "arguments": "{}"}
            ],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "mcp") is False

    def test_different_arguments_not_equivalent(self):
        a = {
            "params": {},
            "steps": [
                {
                    "kind": "mcp",
                    "name": "data",
                    "tool_name": "read_repo",
                    "arguments": '{"repo": "a"}',
                }
            ],
            "sql_steps": [],
        }
        b = {
            "params": {},
            "steps": [
                {
                    "kind": "mcp",
                    "name": "data",
                    "tool_name": "read_repo",
                    "arguments": '{"repo": "b"}',
                }
            ],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "mcp") is False


# ---------------------------------------------------------------------------
# build_recipe_docstring for MCP
# ---------------------------------------------------------------------------


class TestMcpDocstring:
    def test_mcp_docstring_single_step(self):
        doc = build_recipe_docstring("List repos", [{"kind": "mcp"}], [], api_type="mcp")
        assert "1 MCP tool call" in doc
        assert "MCP tool calls" not in doc

    def test_mcp_docstring_multiple_steps(self):
        steps = [{"kind": "mcp"}, {"kind": "mcp"}]
        doc = build_recipe_docstring("List repos", steps, [], api_type="mcp")
        assert "2 MCP tool calls" in doc


# ---------------------------------------------------------------------------
# _validate_step_mcp
# ---------------------------------------------------------------------------


class TestValidateStepMcp:
    def test_valid_mcp_step(self):
        orig = {
            "kind": "mcp",
            "name": "data",
            "tool_name": "read_repo",
            "arguments": '{"owner": "innago", "repo": "ratatoskr"}',
        }
        recipe_step = {
            "kind": "mcp",
            "name": "data",
            "tool_name": "read_repo",
            "arguments": {"owner": {"$param": "owner"}, "repo": {"$param": "repo"}},
        }
        params = {"owner": "innago", "repo": "ratatoskr"}
        assert _validate_step_mcp(orig, recipe_step, params) is True

    def test_name_mismatch(self):
        orig = {"kind": "mcp", "name": "data", "tool_name": "read_repo", "arguments": "{}"}
        recipe_step = {"kind": "mcp", "name": "other", "tool_name": "read_repo", "arguments": {}}
        assert _validate_step_mcp(orig, recipe_step, {}) is False

    def test_tool_name_mismatch(self):
        orig = {"kind": "mcp", "name": "data", "tool_name": "read_repo", "arguments": "{}"}
        recipe_step = {"kind": "mcp", "name": "data", "tool_name": "write_repo", "arguments": {}}
        assert _validate_step_mcp(orig, recipe_step, {}) is False

    def test_string_template_arguments(self):
        """String-form {{param}} arguments are rendered and validated."""
        orig = {
            "kind": "mcp",
            "name": "data",
            "tool_name": "search",
            "arguments": '{"query": "ratatoskr"}',
        }
        recipe_step = {
            "kind": "mcp",
            "name": "data",
            "tool_name": "search",
            "arguments": '{"query": "{{term}}"}',
        }
        params = {"term": "ratatoskr"}
        assert _validate_step_mcp(orig, recipe_step, params) is True

    def test_string_template_mismatch(self):
        """String-form arguments that render differently are rejected."""
        orig = {
            "kind": "mcp",
            "name": "data",
            "tool_name": "search",
            "arguments": '{"query": "foo"}',
        }
        recipe_step = {
            "kind": "mcp",
            "name": "data",
            "tool_name": "search",
            "arguments": '{"query": "{{term}}"}',
        }
        params = {"term": "bar"}
        assert _validate_step_mcp(orig, recipe_step, params) is False

    def test_wrong_kind_rejected(self):
        orig = {"kind": "mcp", "name": "data", "tool_name": "read_repo", "arguments": "{}"}
        recipe_step = {"kind": "rest", "name": "data", "tool_name": "read_repo", "arguments": {}}
        assert _validate_step_mcp(orig, recipe_step, {}) is False


# ---------------------------------------------------------------------------
# _find_used_params for MCP
# ---------------------------------------------------------------------------


class TestFindUsedParamsMcp:
    def test_finds_param_refs_in_arguments_object(self):
        recipe = {
            "steps": [
                {
                    "kind": "mcp",
                    "tool_name": "read_repo",
                    "arguments": {"owner": {"$param": "owner"}, "repo": {"$param": "repo"}},
                }
            ],
            "sql_steps": [],
        }
        found = _find_used_params(recipe, "mcp")
        assert found == {"owner", "repo"}

    def test_finds_placeholder_in_arguments_string(self):
        recipe = {
            "steps": [
                {
                    "kind": "mcp",
                    "tool_name": "search",
                    "arguments": '{"query": "{{search_term}}"}',
                }
            ],
            "sql_steps": [],
        }
        found = _find_used_params(recipe, "mcp")
        assert found == {"search_term"}

    def test_finds_nested_param_refs_in_arguments_object(self):
        """Nested $param refs inside lists/dicts are found recursively."""
        recipe = {
            "steps": [
                {
                    "kind": "mcp",
                    "tool_name": "read_repo",
                    "arguments": {
                        "owner": {"$param": "owner"},
                        "filters": [
                            {"branch": {"$param": "branch"}},
                            {"labels": [{"$param": "label"}]},
                        ],
                    },
                }
            ],
            "sql_steps": [],
        }
        found = _find_used_params(recipe, "mcp")
        assert found == {"owner", "branch", "label"}

    def test_finds_params_in_sql_steps(self):
        recipe = {
            "steps": [{"kind": "mcp", "tool_name": "t", "arguments": {}}],
            "sql_steps": ["SELECT * FROM data WHERE name = '{{name}}'"],
        }
        found = _find_used_params(recipe, "mcp")
        assert "name" in found


# ---------------------------------------------------------------------------
# Step executor factory
# ---------------------------------------------------------------------------


class TestMcpStepExecutor:
    @pytest.mark.asyncio
    async def test_successful_step_execution(self):
        """Step executor calls session.call_tool and returns table data."""
        from api_agent.agent.mcp_agent import _make_mcp_step_executor_factory

        session = AsyncMock()
        session.call_tool = AsyncMock(
            return_value=_make_call_result({"files": ["README.md", "setup.py"]})
        )

        factory = _make_mcp_step_executor_factory(session)
        executor = factory("recipe-123")

        step = {
            "kind": "mcp",
            "tool_name": "list_files",
            "arguments": '{"path": "/"}',
            "name": "files",
        }
        results: dict[str, Any] = {}

        ok, table, err_msg, call_rec = await executor(0, step, {}, results)

        assert ok is True
        assert call_rec["tool_name"] == "list_files"
        assert call_rec["success"] is True
        session.call_tool.assert_awaited_once_with("list_files", {"path": "/"})

    @pytest.mark.asyncio
    async def test_invalid_step_kind(self):
        """Non-MCP step kind returns failure."""
        from api_agent.agent.mcp_agent import _make_mcp_step_executor_factory

        session = AsyncMock()
        factory = _make_mcp_step_executor_factory(session)
        executor = factory("recipe-123")

        step = {"kind": "rest", "tool_name": "x", "arguments": "{}"}
        ok, table, err_msg, call_rec = await executor(0, step, {}, {})

        assert ok is False
        assert "invalid recipe step kind" in err_msg

    @pytest.mark.asyncio
    async def test_template_rendering(self):
        """Arguments are rendered with params before calling tool."""
        from api_agent.agent.mcp_agent import _make_mcp_step_executor_factory

        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=_make_call_result({"ok": True}))

        factory = _make_mcp_step_executor_factory(session)
        executor = factory("recipe-123")

        step = {
            "kind": "mcp",
            "tool_name": "search",
            "arguments": '{"query": "{{term}}"}',
            "name": "results",
        }
        params = {"term": "ratatoskr"}

        ok, table, err_msg, call_rec = await executor(0, step, params, {})

        assert ok is True
        session.call_tool.assert_awaited_once_with("search", {"query": "ratatoskr"})

    @pytest.mark.asyncio
    async def test_invalid_json_after_rendering(self):
        """Bad JSON after template rendering returns failure."""
        from api_agent.agent.mcp_agent import _make_mcp_step_executor_factory

        session = AsyncMock()
        factory = _make_mcp_step_executor_factory(session)
        executor = factory("recipe-123")

        step = {
            "kind": "mcp",
            "tool_name": "t",
            "arguments": "{{broken",
            "name": "data",
        }

        ok, table, err_msg, call_rec = await executor(0, step, {}, {})

        assert ok is False
        assert "Invalid arguments" in err_msg

    @pytest.mark.asyncio
    async def test_tool_call_error_propagates(self):
        """Error from _invoke_mcp_tool returns failure tuple."""
        from api_agent.agent.mcp_agent import _make_mcp_step_executor_factory

        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=_make_call_result({"err": "boom"}, is_error=True))

        factory = _make_mcp_step_executor_factory(session)
        executor = factory("recipe-123")

        step = {"kind": "mcp", "tool_name": "fail_tool", "arguments": "{}", "name": "data"}

        ok, table, err_msg, call_rec = await executor(0, step, {}, {})

        assert ok is False
        payload = json.loads(err_msg)
        assert payload["success"] is False
