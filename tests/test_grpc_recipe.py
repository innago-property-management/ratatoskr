"""Tests for gRPC recipe support — extraction, validation, runner, common."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from api_agent.context import RequestContext
from api_agent.grpc.reflection import GrpcSchema, MethodInfo, ServiceInfo
from api_agent.recipe.common import (
    _recipes_equivalent,
    build_api_id,
    build_recipe_docstring,
    maybe_extract_and_save_recipe,
)
from api_agent.recipe.extractor import (
    _find_used_params,
    _validate_equivalence,
    _validate_step_grpc,
)
from api_agent.recipe.runner import execute_recipe_tool, load_schema_and_base_url
from api_agent.recipe.store import RECIPE_STORE, sha256_hex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_recipe_store():
    """Ensure the global recipe store is empty before and after each test."""
    RECIPE_STORE._records.clear()
    RECIPE_STORE._by_key.clear()
    RECIPE_STORE._lru.clear()
    yield
    RECIPE_STORE._records.clear()
    RECIPE_STORE._by_key.clear()
    RECIPE_STORE._lru.clear()


def _grpc_ctx(url: str = "grpc://localhost:50051") -> RequestContext:
    return RequestContext(
        target_url=url,
        target_headers={"authorization": "Bearer tok"},
        api_type="grpc",
        base_url=None,
        include_result=False,
        allow_unsafe_paths=(),
        poll_paths=(),
    )


def _make_grpc_schema() -> GrpcSchema:
    pool = MagicMock()
    services = [
        ServiceInfo(
            full_name="helloworld.Greeter",
            methods=[
                MethodInfo(
                    name="SayHello",
                    full_method_path="/helloworld.Greeter/SayHello",
                    input_type="helloworld.HelloRequest",
                    output_type="helloworld.HelloReply",
                ),
                MethodInfo(
                    name="StreamGreetings",
                    full_method_path="/helloworld.Greeter/StreamGreetings",
                    input_type="helloworld.HelloRequest",
                    output_type="helloworld.HelloReply",
                    server_streaming=True,
                ),
            ],
        )
    ]
    raw_text = (
        "<services>\nhelloworld.Greeter\n"
        "  SayHello(helloworld.HelloRequest) -> helloworld.HelloReply\n"
    )
    return GrpcSchema(services=services, pool=pool, raw_schema_text=raw_text)


# ---------------------------------------------------------------------------
# Phase 6a: common.py — build_api_id, _recipes_equivalent, docstring
# ---------------------------------------------------------------------------


class TestGrpcBuildApiId:
    def test_grpc_api_id_format(self):
        ctx = _grpc_ctx()
        api_id = build_api_id(ctx, "grpc")
        assert api_id == "grpc:grpc://localhost:50051"

    def test_grpc_api_id_different_url(self):
        ctx = _grpc_ctx("grpcs://api.example.com:443")
        api_id = build_api_id(ctx, "grpc")
        assert api_id == "grpc:grpcs://api.example.com:443"


class TestGrpcRecipesEquivalent:
    def test_equivalent_grpc_recipes(self):
        a = {
            "params": {"name": {"type": "str", "default": "world"}},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"name": "world"}'}],
            "sql_steps": [],
        }
        b = {
            "params": {"name": {"type": "str", "default": "world"}},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"name": "world"}'}],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "grpc") is True

    def test_different_method_not_equivalent(self):
        a = {
            "params": {},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": "{}"}],
            "sql_steps": [],
        }
        b = {
            "params": {},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Other", "request": "{}"}],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "grpc") is False

    def test_different_request_not_equivalent(self):
        a = {
            "params": {},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"a": 1}'}],
            "sql_steps": [],
        }
        b = {
            "params": {},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"a": 2}'}],
            "sql_steps": [],
        }
        assert _recipes_equivalent(a, b, "grpc") is False


class TestGrpcDocstring:
    def test_grpc_docstring(self):
        doc = build_recipe_docstring("Get hello", [{"kind": "grpc"}], [], api_type="grpc")
        assert "gRPC call" in doc


# ---------------------------------------------------------------------------
# Phase 6b: extractor.py — validation
# ---------------------------------------------------------------------------


class TestValidateStepGrpc:
    def test_valid_unary_step(self):
        orig = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"name": "world"}'}
        recipe_step = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": {"name": {"$param": "name"}}}
        params = {"name": "world"}
        assert _validate_step_grpc(orig, recipe_step, params) is True

    def test_name_mismatch(self):
        orig = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": "{}"}
        recipe_step = {"kind": "grpc", "name": "other", "method": "/pkg.Svc/Do", "request": {}}
        assert _validate_step_grpc(orig, recipe_step, {}) is False

    def test_method_mismatch(self):
        orig = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": "{}"}
        recipe_step = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Other", "request": {}}
        assert _validate_step_grpc(orig, recipe_step, {}) is False

    def test_request_render_mismatch(self):
        orig = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"name": "alice"}'}
        recipe_step = {"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": {"name": {"$param": "name"}}}
        params = {"name": "bob"}  # renders to {"name": "bob"} != {"name": "alice"}
        assert _validate_step_grpc(orig, recipe_step, params) is False


class TestValidateEquivalenceGrpc:
    def test_valid_recipe_renders_back(self):
        steps = [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": '{"name": "world"}'}]
        recipe = {
            "params": {"name": {"type": "str", "default": "world"}},
            "steps": [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": {"name": {"$param": "name"}}}],
            "sql_steps": [],
        }
        assert _validate_equivalence(api_type="grpc", original_steps=steps, original_sql=[], recipe=recipe) is True

    def test_step_count_mismatch(self):
        steps = [{"kind": "grpc", "name": "data", "method": "/pkg.Svc/Do", "request": "{}"}]
        recipe = {"params": {}, "steps": [], "sql_steps": []}
        assert _validate_equivalence(api_type="grpc", original_steps=steps, original_sql=[], recipe=recipe) is False


class TestFindUsedParamsGrpc:
    def test_finds_param_refs_in_request(self):
        recipe = {
            "steps": [{"kind": "grpc", "request": {"name": {"$param": "user_name"}, "limit": {"$param": "max_results"}}}],
            "sql_steps": [],
        }
        used = _find_used_params(recipe, "grpc")
        assert used == {"user_name", "max_results"}

    def test_no_params(self):
        recipe = {"steps": [{"kind": "grpc", "request": {"name": "fixed"}}], "sql_steps": []}
        used = _find_used_params(recipe, "grpc")
        assert used == set()


# ---------------------------------------------------------------------------
# Phase 6c: runner.py — load_schema_and_base_url, execute_recipe_tool
# ---------------------------------------------------------------------------


class TestLoadSchemaGrpc:
    @pytest.mark.asyncio
    async def test_grpc_schema_loading(self, monkeypatch):
        """load_schema_and_base_url fetches gRPC schema via reflection."""
        schema = _make_grpc_schema()
        mock_fetch = AsyncMock(return_value=schema)
        monkeypatch.setattr("api_agent.recipe.runner.fetch_grpc_schema", mock_fetch)

        ctx = _grpc_ctx()
        raw, base = await load_schema_and_base_url(ctx)

        assert raw == schema.raw_schema_text
        assert base == ""
        mock_fetch.assert_awaited_once()


class TestGrpcRecipeExecution:
    @pytest.mark.asyncio
    async def test_unary_recipe_execution(self, monkeypatch):
        """gRPC unary recipe executes correctly with param substitution."""
        schema = _make_grpc_schema()
        raw_schema = schema.raw_schema_text
        schema_hash = sha256_hex(raw_schema)
        ctx = _grpc_ctx()
        api_id = build_api_id(ctx, "grpc")

        recipe = {
            "tool_name": "get_greeting",
            "params": {"name": {"type": "str", "default": "world"}},
            "steps": [
                {
                    "kind": "grpc",
                    "name": "data",
                    "method": "/helloworld.Greeter/SayHello",
                    "request": {"name": {"$param": "name"}},
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(api_id=api_id, schema_hash=schema_hash, question="greet someone", recipe=recipe, tool_name="get_greeting")

        # Mock schema fetch
        mock_fetch = AsyncMock(return_value=schema)
        monkeypatch.setattr("api_agent.recipe.runner.fetch_grpc_schema", mock_fetch)

        # Mock unary RPC
        mock_rpc = AsyncMock(return_value={"success": True, "data": {"message": "Hello, Alice!"}})
        monkeypatch.setattr("api_agent.recipe.runner.execute_unary_rpc", mock_rpc)

        result_str = await execute_recipe_tool(
            ctx, recipe_id, {"name": "Alice"}, return_directly=False, raw_schema=raw_schema,
        )
        result = json.loads(result_str)

        assert result["success"] is True
        assert len(result["executed_rpcs"]) == 1
        # Verify param was substituted
        rpc_rec = result["executed_rpcs"][0]
        req = json.loads(rpc_rec["request"])
        assert req["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_recipe_schema_mismatch(self, monkeypatch):
        """Recipe rejects when schema hash doesn't match."""
        schema = _make_grpc_schema()
        ctx = _grpc_ctx()
        api_id = build_api_id(ctx, "grpc")

        recipe = {"tool_name": "get_greeting", "params": {}, "steps": [], "sql_steps": []}
        recipe_id = RECIPE_STORE.save_recipe(api_id=api_id, schema_hash="old_hash", question="greet", recipe=recipe, tool_name="get_greeting")

        mock_fetch = AsyncMock(return_value=schema)
        monkeypatch.setattr("api_agent.recipe.runner.fetch_grpc_schema", mock_fetch)

        result_str = await execute_recipe_tool(
            ctx, recipe_id, {}, return_directly=False, raw_schema=schema.raw_schema_text,
        )
        result = json.loads(result_str)

        assert result["success"] is False
        assert "match" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_recipe_with_sql_step(self, monkeypatch):
        """gRPC recipe with SQL post-processing executes both steps."""
        schema = _make_grpc_schema()
        raw_schema = schema.raw_schema_text
        schema_hash = sha256_hex(raw_schema)
        ctx = _grpc_ctx()
        api_id = build_api_id(ctx, "grpc")

        recipe = {
            "tool_name": "get_greeting_filtered",
            "params": {},
            "steps": [
                {
                    "kind": "grpc",
                    "name": "greetings",
                    "method": "/helloworld.Greeter/SayHello",
                    "request": {"name": "world"},
                }
            ],
            "sql_steps": ["SELECT * FROM greetings WHERE message LIKE '%world%'"],
        }
        recipe_id = RECIPE_STORE.save_recipe(api_id=api_id, schema_hash=schema_hash, question="filtered greet", recipe=recipe, tool_name="get_greeting_filtered")

        mock_fetch = AsyncMock(return_value=schema)
        monkeypatch.setattr("api_agent.recipe.runner.fetch_grpc_schema", mock_fetch)

        mock_rpc = AsyncMock(return_value={
            "success": True,
            "data": {"message": "Hello, world!", "extra": "value"},
        })
        monkeypatch.setattr("api_agent.recipe.runner.execute_unary_rpc", mock_rpc)

        result_str = await execute_recipe_tool(
            ctx, recipe_id, {}, return_directly=False, raw_schema=raw_schema,
        )
        result = json.loads(result_str)

        assert result["success"] is True
        assert len(result["executed_rpcs"]) == 1
        assert len(result["executed_sql"]) == 1

    @pytest.mark.asyncio
    async def test_rpc_failure_propagates(self, monkeypatch):
        """gRPC recipe propagates RPC failure."""
        schema = _make_grpc_schema()
        raw_schema = schema.raw_schema_text
        schema_hash = sha256_hex(raw_schema)
        ctx = _grpc_ctx()
        api_id = build_api_id(ctx, "grpc")

        recipe = {
            "tool_name": "get_greeting",
            "params": {},
            "steps": [
                {
                    "kind": "grpc",
                    "name": "data",
                    "method": "/helloworld.Greeter/SayHello",
                    "request": {},
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(api_id=api_id, schema_hash=schema_hash, question="greet", recipe=recipe, tool_name="get_greeting")

        mock_fetch = AsyncMock(return_value=schema)
        monkeypatch.setattr("api_agent.recipe.runner.fetch_grpc_schema", mock_fetch)

        mock_rpc = AsyncMock(return_value={"success": False, "error": "UNAVAILABLE"})
        monkeypatch.setattr("api_agent.recipe.runner.execute_unary_rpc", mock_rpc)

        result_str = await execute_recipe_tool(
            ctx, recipe_id, {}, return_directly=False, raw_schema=raw_schema,
        )
        result = json.loads(result_str)

        assert result["success"] is False
        assert "UNAVAILABLE" in result["error"]

    @pytest.mark.asyncio
    async def test_method_not_found_in_schema(self, monkeypatch):
        """gRPC recipe errors when method not found in reflected schema."""
        schema = _make_grpc_schema()
        raw_schema = schema.raw_schema_text
        schema_hash = sha256_hex(raw_schema)
        ctx = _grpc_ctx()
        api_id = build_api_id(ctx, "grpc")

        recipe = {
            "tool_name": "call_missing",
            "params": {},
            "steps": [
                {
                    "kind": "grpc",
                    "name": "data",
                    "method": "/nonexistent.Svc/Method",
                    "request": {},
                }
            ],
            "sql_steps": [],
        }
        recipe_id = RECIPE_STORE.save_recipe(api_id=api_id, schema_hash=schema_hash, question="missing", recipe=recipe, tool_name="call_missing")

        mock_fetch = AsyncMock(return_value=schema)
        monkeypatch.setattr("api_agent.recipe.runner.fetch_grpc_schema", mock_fetch)

        result_str = await execute_recipe_tool(
            ctx, recipe_id, {}, return_directly=False, raw_schema=raw_schema,
        )
        result = json.loads(result_str)

        assert result["success"] is False
        assert "not found" in result["error"].lower()
