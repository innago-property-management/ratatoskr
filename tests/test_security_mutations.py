"""Security tests for mutation safety (PR C).

Tests cover:
- F5: gRPC method blocking (unsafe RPC methods blocked by default)
- F6: GraphQL mutation regex hardening (comment stripping)
"""

import json

import pytest

from api_agent.graphql.client import _is_mutation, execute_query


# ---------------------------------------------------------------------------
# F6: GraphQL mutation regex hardening
# ---------------------------------------------------------------------------


class TestGraphQLCommentStripping:
    """Verify GraphQL mutation detection handles comments correctly."""

    def test_comment_before_mutation_detected(self):
        """A mutation preceded by a comment should still be detected."""
        assert _is_mutation("# this is a comment\nmutation { deleteUser(id: 1) { id } }") is True

    def test_multiline_comments_before_mutation_detected(self):
        """Multiple comment lines before mutation should still be detected."""
        assert _is_mutation("# line 1\n# line 2\n# line 3\nmutation { createUser { id } }") is True

    def test_comment_mentioning_mutation_not_detected(self):
        """A query with 'mutation' only in a comment should NOT be detected."""
        assert _is_mutation("# mutation info query\nquery { users { id } }") is False

    def test_inline_comment_with_mutation_word_not_detected(self):
        """Query with inline comment mentioning mutation should NOT be detected."""
        assert _is_mutation("query { users { id } } # mutation") is False

    def test_comment_disguised_mutation_detected(self):
        """Comment hiding a real mutation on next line should still be caught."""
        assert _is_mutation("# innocent comment\n  mutation { deleteAll { count } }") is True

    @pytest.mark.asyncio
    async def test_mutation_blocked_at_execute_level(self):
        """Integration: mutation actually blocked by execute_query."""
        result = await execute_query(
            "# comment\nmutation { deleteUser(id: 1) { id } }",
            endpoint="https://api.example.com/graphql",
        )
        assert result["success"] is False
        assert "not allowed" in result["error"].lower()


# ---------------------------------------------------------------------------
# F5: gRPC method blocking
# ---------------------------------------------------------------------------


class TestGrpcMethodSafety:
    """Verify _is_grpc_method_safe function blocks unsafe methods."""

    def test_get_method_safe(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/GetUser", ()) is True

    def test_list_method_safe(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/ListUsers", ()) is True

    def test_search_method_safe(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/SearchItems", ()) is True

    def test_create_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/CreateUser", ()) is False

    def test_delete_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/DeleteUser", ()) is False

    def test_update_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/UpdateUser", ()) is False

    def test_remove_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/RemoveItem", ()) is False

    def test_set_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/SetConfig", ()) is False

    def test_destroy_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/DestroyResource", ()) is False

    def test_upsert_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/UpsertRecord", ()) is False

    def test_write_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/WriteData", ()) is False

    def test_put_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/PutItem", ()) is False

    def test_add_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/AddEntry", ()) is False

    def test_modify_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/ModifyRecord", ()) is False

    def test_patch_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/PatchField", ()) is False

    def test_insert_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/InsertRow", ()) is False

    def test_drop_method_blocked(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        assert _is_grpc_method_safe("package.Service/DropTable", ()) is False


class TestGrpcAllowUnsafeRpcs:
    """Verify that allow_unsafe_rpcs overrides unsafe method blocking."""

    def test_allowlist_overrides_create(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        allow = ("package.Service/CreateUser",)
        assert _is_grpc_method_safe("package.Service/CreateUser", allow) is True

    def test_allowlist_glob_pattern(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        allow = ("package.Service/*",)
        assert _is_grpc_method_safe("package.Service/DeleteAll", allow) is True

    def test_allowlist_does_not_match_other_service(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        allow = ("other.Service/CreateUser",)
        assert _is_grpc_method_safe("package.Service/CreateUser", allow) is False

    def test_allowlist_wildcard_all(self):
        from api_agent.agent.grpc_agent import _is_grpc_method_safe

        allow = ("*",)
        assert _is_grpc_method_safe("any.Service/DeleteEverything", allow) is True


class TestGrpcToolBlocking:
    """Integration tests: verify gRPC tool functions block unsafe methods."""

    def _make_schema(self):
        """Build a schema with safe + unsafe methods."""
        from unittest.mock import MagicMock

        from api_agent.grpc.reflection import GrpcSchema, MethodInfo, ServiceInfo

        pool = MagicMock()
        services = [
            ServiceInfo(
                full_name="users.UserService",
                methods=[
                    MethodInfo(
                        name="GetUser",
                        full_method_path="/users.UserService/GetUser",
                        input_type="users.GetUserRequest",
                        output_type="users.UserResponse",
                    ),
                    MethodInfo(
                        name="CreateUser",
                        full_method_path="/users.UserService/CreateUser",
                        input_type="users.CreateUserRequest",
                        output_type="users.UserResponse",
                    ),
                    MethodInfo(
                        name="DeleteStream",
                        full_method_path="/users.UserService/DeleteStream",
                        input_type="users.DeleteRequest",
                        output_type="users.DeleteResponse",
                        server_streaming=True,
                    ),
                    MethodInfo(
                        name="UpdateBatch",
                        full_method_path="/users.UserService/UpdateBatch",
                        input_type="users.UpdateRequest",
                        output_type="users.UpdateResponse",
                        client_streaming=True,
                    ),
                    MethodInfo(
                        name="SetBidi",
                        full_method_path="/users.UserService/SetBidi",
                        input_type="users.SetRequest",
                        output_type="users.SetResponse",
                        client_streaming=True,
                        server_streaming=True,
                    ),
                ],
            )
        ]
        raw_text = "<services>\nusers.UserService\n</services>"
        return GrpcSchema(services=services, raw_schema_text=raw_text, pool=pool)

    def _make_ctx(self, allow_unsafe_rpcs=()):
        from api_agent.context import RequestContext

        return RequestContext(
            target_url="grpc://localhost:50051",
            target_headers={},
            api_type="grpc",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
            grpc_allow_unsafe_rpcs=allow_unsafe_rpcs,
        )

    @pytest.mark.asyncio
    async def test_unary_safe_method_allowed(self):
        from api_agent.agent.grpc_agent import _create_grpc_call_tool

        schema = self._make_schema()
        ctx = self._make_ctx()
        tool_fn = _create_grpc_call_tool(ctx, schema)
        # Safe method — should proceed (will fail at RPC layer, but should NOT return safety error)
        result_json = await tool_fn.function(method="users.UserService/GetUser", request="{}")
        result = json.loads(result_json)
        # It will fail at RPC call, but the error should NOT be about safety
        if not result.get("success"):
            assert "not allowed" not in result.get("error", "").lower()
            assert "read-only" not in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_unary_unsafe_method_blocked(self):
        from api_agent.agent.grpc_agent import _create_grpc_call_tool

        schema = self._make_schema()
        ctx = self._make_ctx()
        tool_fn = _create_grpc_call_tool(ctx, schema)
        result_json = await tool_fn.function(method="users.UserService/CreateUser", request="{}")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not allowed" in result["error"].lower() or "read-only" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_unary_unsafe_method_allowed_via_header(self):
        from api_agent.agent.grpc_agent import _create_grpc_call_tool

        schema = self._make_schema()
        ctx = self._make_ctx(allow_unsafe_rpcs=("users.UserService/CreateUser",))
        tool_fn = _create_grpc_call_tool(ctx, schema)
        result_json = await tool_fn.function(method="users.UserService/CreateUser", request="{}")
        result = json.loads(result_json)
        # Should NOT be blocked by safety check (may fail at RPC layer)
        if not result.get("success"):
            assert "not allowed" not in result.get("error", "").lower()
            assert "read-only" not in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_stream_unsafe_method_blocked(self):
        from api_agent.agent.grpc_agent import _create_grpc_stream_tool

        schema = self._make_schema()
        ctx = self._make_ctx()
        tool_fn = _create_grpc_stream_tool(ctx, schema)
        result_json = await tool_fn.function(method="users.UserService/DeleteStream", request="{}")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not allowed" in result["error"].lower() or "read-only" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_client_stream_unsafe_method_blocked(self):
        from api_agent.agent.grpc_agent import _create_grpc_client_stream_tool

        schema = self._make_schema()
        ctx = self._make_ctx()
        tool_fn = _create_grpc_client_stream_tool(ctx, schema)
        result_json = await tool_fn.function(method="users.UserService/UpdateBatch", requests="[{}]")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not allowed" in result["error"].lower() or "read-only" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_bidi_stream_unsafe_method_blocked(self):
        from api_agent.agent.grpc_agent import _create_grpc_bidi_stream_tool

        schema = self._make_schema()
        ctx = self._make_ctx()
        tool_fn = _create_grpc_bidi_stream_tool(ctx, schema)
        result_json = await tool_fn.function(method="users.UserService/SetBidi", requests="[{}]")
        result = json.loads(result_json)
        assert result["success"] is False
        assert "not allowed" in result["error"].lower() or "read-only" in result["error"].lower()


class TestGrpcUnsafeRpcsHeader:
    """Verify X-Allow-Unsafe-RPCs header is parsed into RequestContext."""

    def test_grpc_allow_unsafe_rpcs_field_exists(self):
        from api_agent.context import RequestContext

        ctx = RequestContext(
            target_url="grpc://localhost:50051",
            target_headers={},
            api_type="grpc",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
            grpc_allow_unsafe_rpcs=("pkg.Svc/Create",),
        )
        assert ctx.grpc_allow_unsafe_rpcs == ("pkg.Svc/Create",)

    def test_grpc_allow_unsafe_rpcs_default_empty(self):
        from api_agent.context import RequestContext

        ctx = RequestContext(
            target_url="grpc://localhost:50051",
            target_headers={},
            api_type="grpc",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
        )
        assert ctx.grpc_allow_unsafe_rpcs == ()

    def test_header_parsing_valid_json(self):
        """get_request_context parses X-Allow-Unsafe-RPCs JSON array."""
        from unittest.mock import patch

        from api_agent.context import get_request_context

        headers = {
            "x-target-url": "https://api.example.com/graphql",
            "x-api-type": "graphql",
            "x-allow-unsafe-rpcs": '["users.Svc/CreateUser", "orders.Svc/*"]',
        }
        with patch("api_agent.context.get_http_headers", return_value=headers):
            ctx = get_request_context()
        assert ctx.grpc_allow_unsafe_rpcs == ("users.Svc/CreateUser", "orders.Svc/*")

    def test_header_parsing_invalid_json_falls_back(self):
        """Invalid JSON in X-Allow-Unsafe-RPCs falls back to empty tuple."""
        from unittest.mock import patch

        from api_agent.context import get_request_context

        headers = {
            "x-target-url": "https://api.example.com/graphql",
            "x-api-type": "graphql",
            "x-allow-unsafe-rpcs": "not-valid-json",
        }
        with patch("api_agent.context.get_http_headers", return_value=headers):
            ctx = get_request_context()
        assert ctx.grpc_allow_unsafe_rpcs == ()


class TestGrpcRecipeExecutorSafety:
    """Verify recipe step executor also enforces mutation safety."""

    def _make_schema(self):
        from unittest.mock import MagicMock

        from api_agent.grpc.reflection import GrpcSchema, MethodInfo, ServiceInfo

        pool = MagicMock()
        services = [
            ServiceInfo(
                full_name="users.UserService",
                methods=[
                    MethodInfo(
                        name="CreateUser",
                        full_method_path="/users.UserService/CreateUser",
                        input_type="users.CreateUserRequest",
                        output_type="users.UserResponse",
                    ),
                ],
            )
        ]
        raw_text = "<services>\nusers.UserService\n</services>"
        return GrpcSchema(services=services, raw_schema_text=raw_text, pool=pool)

    @pytest.mark.asyncio
    async def test_recipe_executor_blocks_unsafe_method(self):
        """Recipe step executor should block unsafe methods (M1 fix)."""
        from api_agent.agent.grpc_agent import _make_grpc_step_executor_factory
        from api_agent.context import RequestContext

        schema = self._make_schema()
        ctx = RequestContext(
            target_url="grpc://localhost:50051",
            target_headers={},
            api_type="grpc",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
            grpc_allow_unsafe_rpcs=(),  # no allowlist
        )

        factory = _make_grpc_step_executor_factory(ctx, schema)
        executor = factory("test-recipe-id")

        step = {
            "kind": "grpc",
            "method": "users.UserService/CreateUser",
            "request_template": '{"name": "Alice"}',
            "name": "data",
        }
        success, data, error_json, call_rec = await executor(0, step, {}, {})
        assert success is False
        result = json.loads(error_json)
        assert "not allowed" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_recipe_executor_allows_with_allowlist(self):
        """Recipe step executor should allow unsafe methods when allowlisted."""
        from api_agent.agent.grpc_agent import _make_grpc_step_executor_factory
        from api_agent.context import RequestContext

        schema = self._make_schema()
        ctx = RequestContext(
            target_url="grpc://localhost:50051",
            target_headers={},
            api_type="grpc",
            base_url=None,
            include_result=False,
            allow_unsafe_paths=(),
            poll_paths=(),
            grpc_allow_unsafe_rpcs=("users.UserService/CreateUser",),
        )

        factory = _make_grpc_step_executor_factory(ctx, schema)
        executor = factory("test-recipe-id")

        step = {
            "kind": "grpc",
            "method": "users.UserService/CreateUser",
            "request_template": '{"name": "Alice"}',
            "name": "data",
        }
        # Will fail at RPC layer (no real server), but should NOT fail at safety check
        success, data, error_json, call_rec = await executor(0, step, {}, {})
        if not success and error_json:
            result = json.loads(error_json)
            assert "not allowed" not in result.get("error", "").lower()


class TestGrpcUnsafeConfigPatterns:
    """Verify config setting for unsafe method patterns."""

    def test_default_unsafe_patterns_exist(self):
        from api_agent.config import settings

        assert settings.GRPC_UNSAFE_METHOD_PATTERNS
        patterns = [p.strip() for p in settings.GRPC_UNSAFE_METHOD_PATTERNS.split(",")]
        assert "Create*" in patterns
        assert "Delete*" in patterns
        assert "Update*" in patterns
        assert "Put*" in patterns
        assert "Add*" in patterns
        assert "Modify*" in patterns
        assert "Patch*" in patterns
        assert "Insert*" in patterns
        assert "Drop*" in patterns
