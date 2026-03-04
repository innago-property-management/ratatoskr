"""Tests for api_agent.schema.reducer — ToonLayer (Phase 2)."""

from __future__ import annotations

import json
import sys

import pytest

from api_agent.schema.reducer import ToonLayer

# ---------------------------------------------------------------------------
# ToonLayer tests
# ---------------------------------------------------------------------------


class TestToonLayer:
    """ToonLayer.encode() test suite."""

    def test_toon_reduces_homogeneous_array(self) -> None:
        """Array of dicts with same keys -> TOON output is smaller."""
        data = [
            {"name": "Alice", "age": 30, "email": "alice@example.com"},
            {"name": "Bob", "age": 25, "email": "bob@example.com"},
            {"name": "Charlie", "age": 35, "email": "charlie@example.com"},
            {"name": "Diana", "age": 28, "email": "diana@example.com"},
            {"name": "Eve", "age": 32, "email": "eve@example.com"},
        ]
        original = json.dumps(data)

        layer = ToonLayer()
        encoded, was_applied = layer.encode(original)

        assert was_applied is True
        assert len(encoded) < len(original), (
            f"TOON should be smaller: {len(encoded)} >= {len(original)}"
        )
        # Encoded output should be a non-empty string
        assert isinstance(encoded, str)
        assert len(encoded) > 0

    def test_toon_no_gain_returns_original(self) -> None:
        """Nested heterogeneous data where TOON may not help -> returns original."""
        # Small, deeply nested heterogeneous dict — TOON overhead exceeds savings
        data = {"a": 1}
        original = json.dumps(data)

        layer = ToonLayer()
        encoded, was_applied = layer.encode(original)

        # For tiny data, TOON output might be >= original
        # Either way, the function should not crash
        assert isinstance(encoded, str)
        assert isinstance(was_applied, bool)
        # If TOON didn't help, we get back the original
        if not was_applied:
            assert encoded == original

    def test_toon_import_error_degrades_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If toon_format cannot be imported, returns original with was_applied=False."""
        # Save and remove toon_format from sys.modules to simulate ImportError
        saved_module = sys.modules.get("toon_format")
        monkeypatch.setitem(sys.modules, "toon_format", None)

        original = json.dumps([{"x": 1}, {"x": 2}])
        layer = ToonLayer()
        encoded, was_applied = layer.encode(original)

        assert was_applied is False
        assert encoded == original

        # Restore if it was there
        if saved_module is not None:
            monkeypatch.setitem(sys.modules, "toon_format", saved_module)

    def test_toon_with_non_json_input_skips(self) -> None:
        """DSL text (not JSON) -> TOON skipped, returns original."""
        dsl_text = """## GET /users/{id}
Retrieve a user by ID.
Parameters:
  - id (string, required): User identifier

## POST /users
Create a new user account.
Parameters:
  - name (string, required): User's full name
  - email (string, required): Email address
"""
        layer = ToonLayer()
        encoded, was_applied = layer.encode(dsl_text)

        assert was_applied is False
        assert encoded == dsl_text

    def test_toon_with_proto_idl_input_skips(self) -> None:
        """Proto IDL text (not JSON) -> TOON skipped, returns original."""
        proto_text = """service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply);
  rpc SayHelloAgain (HelloRequest) returns (HelloReply);
}

message HelloRequest {
  string name = 1;
}

message HelloReply {
  string message = 1;
}
"""
        layer = ToonLayer()
        encoded, was_applied = layer.encode(proto_text)

        assert was_applied is False
        assert encoded == proto_text

    def test_toon_with_graphql_json_schema(self) -> None:
        """JSON-serializable GraphQL introspection with flat field arrays -> TOON applies.

        TOON excels at flat homogeneous arrays (e.g., a large list of fields or
        enum values). Deeply nested structures with heterogeneous leaf shapes
        may not compress, which is expected and tested separately.
        """
        # Build a schema with many flat, homogeneous field entries — the pattern
        # where TOON compression shines. This mirrors a real large GraphQL API
        # with many similarly-structured fields.
        fields = [
            {"name": f"field_{i}", "type": "String", "required": True, "description": f"Field {i}"}
            for i in range(50)
        ]
        introspection = {"data": {"__schema": {"queryType": {"fields": fields}}}}
        original = json.dumps(introspection)

        layer = ToonLayer()
        encoded, was_applied = layer.encode(original)

        assert was_applied is True
        assert len(encoded) < len(original), (
            f"TOON should reduce flat-field GraphQL JSON: {len(encoded)} >= {len(original)}"
        )

    def test_toon_nested_json_no_gain_returns_original(self) -> None:
        """Nested heterogeneous JSON (e.g., small GraphQL introspection) may not compress.

        This is expected: TOON compresses flat homogeneous arrays, not deeply
        nested structures with variable shapes. The layer should gracefully
        return the original when there is no size benefit.
        """
        introspection = {
            "data": {
                "__schema": {
                    "types": [
                        {
                            "name": "User",
                            "kind": "OBJECT",
                            "fields": [
                                {"name": "id", "type": {"name": "ID", "kind": "SCALAR"}},
                                {"name": "name", "type": {"name": "String", "kind": "SCALAR"}},
                            ],
                        },
                    ]
                }
            }
        }
        original = json.dumps(introspection)

        layer = ToonLayer()
        encoded, was_applied = layer.encode(original)

        # For small nested structures, TOON overhead may exceed savings
        if not was_applied:
            assert encoded == original
        # If it did apply (somehow), the output should be smaller
        if was_applied:
            assert len(encoded) < len(original)

    def test_toon_with_empty_string(self) -> None:
        """Empty string is not valid JSON -> returns original."""
        layer = ToonLayer()
        encoded, was_applied = layer.encode("")

        assert was_applied is False
        assert encoded == ""

    def test_toon_encode_exception_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If toon_format.encode() raises an unexpected exception, degrades gracefully."""
        import toon_format

        original_encode = toon_format.encode
        monkeypatch.setattr(
            toon_format, "encode", lambda x: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        original = json.dumps([{"x": 1}, {"x": 2}])
        layer = ToonLayer()
        encoded, was_applied = layer.encode(original)

        assert was_applied is False
        assert encoded == original

        # Restore
        monkeypatch.setattr(toon_format, "encode", original_encode)
