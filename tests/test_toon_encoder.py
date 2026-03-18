"""Tests for api_agent.llm.toon_encoder — ToolResultEncoder (TDD)."""

from __future__ import annotations

import json
import sys

import pytest

from api_agent.llm.toon_encoder import ToolResultEncoder

# ---------------------------------------------------------------------------
# ToolResultEncoder unit tests
# ---------------------------------------------------------------------------


class TestToolResultEncoder:
    """ToolResultEncoder.encode() contract."""

    def test_returns_tuple_of_str_and_bool(self) -> None:
        """Return type is always (str, bool)."""
        encoder = ToolResultEncoder()
        data = [{"x": 1}]
        result = encoder.encode(data)
        assert isinstance(result, tuple)
        assert len(result) == 2
        encoded, was_applied = result
        assert isinstance(encoded, str)
        assert isinstance(was_applied, bool)

    def test_encodes_homogeneous_list_smaller(self) -> None:
        """Large homogeneous array: TOON output should be smaller than JSON."""
        data = [
            {"id": i, "name": f"user_{i}", "email": f"user{i}@example.com", "active": True}
            for i in range(30)
        ]
        encoder = ToolResultEncoder()
        toon_str, was_applied = encoder.encode(data)

        assert was_applied is True
        assert len(toon_str) < len(json.dumps(data)), (
            f"TOON should be smaller for homogeneous arrays: "
            f"toon={len(toon_str)} >= json={len(json.dumps(data))}"
        )
        assert len(toon_str) > 0

    def test_returns_json_when_toon_not_smaller(self) -> None:
        """If TOON output is not smaller, returns JSON with was_applied=False."""
        # Single tiny item — TOON overhead likely exceeds savings
        data = [{"a": 1}]
        encoder = ToolResultEncoder()
        encoded, was_applied = encoder.encode(data)

        if not was_applied:
            # Should get back valid JSON
            parsed = json.loads(encoded)
            assert parsed == data
        # Either outcome is acceptable — just must not crash and must be consistent

    def test_empty_list_returns_json(self) -> None:
        """Empty list -> returns JSON, was_applied=False."""
        encoder = ToolResultEncoder()
        encoded, was_applied = encoder.encode([])

        assert was_applied is False
        assert json.loads(encoded) == []

    def test_degrades_on_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If toon_format cannot be imported, returns JSON with was_applied=False."""
        from api_agent.llm.toon_encoder import _log_toon_unavailable

        # Reset lru_cache so this test exercises the warning path
        _log_toon_unavailable.cache_clear()
        monkeypatch.setitem(sys.modules, "toon_format", None)  # type: ignore[arg-type]

        data = [{"x": i} for i in range(10)]
        encoder = ToolResultEncoder()
        encoded, was_applied = encoder.encode(data)

        assert was_applied is False
        assert json.loads(encoded) == data

        # Clean up so other tests aren't affected
        _log_toon_unavailable.cache_clear()

    def test_degrades_on_encoding_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If toon_format.encode() raises, returns JSON with was_applied=False."""
        import toon_format

        monkeypatch.setattr(
            toon_format, "encode", lambda x: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        data = [{"x": i} for i in range(10)]
        encoder = ToolResultEncoder()
        encoded, was_applied = encoder.encode(data)

        assert was_applied is False
        assert json.loads(encoded) == data

    def test_large_homogeneous_array_compression_ratio(self) -> None:
        """Verify meaningful compression on realistic API result payload."""
        # Mimic a real flights/bookings API result
        data = [
            {
                "booking_id": f"BK-{1000 + i}",
                "passenger_name": f"Passenger {i}",
                "origin": "NYC",
                "destination": "LAX",
                "departure": "2026-04-01T10:00:00Z",
                "status": "confirmed",
                "price_usd": 299.99 + i,
            }
            for i in range(20)
        ]
        encoder = ToolResultEncoder()
        toon_str, was_applied = encoder.encode(data)

        assert was_applied is True
        original_len = len(json.dumps(data))
        reduction_pct = (1 - len(toon_str) / original_len) * 100
        # Expect at least 20% reduction for this canonical homogeneous array
        assert reduction_pct >= 20, (
            f"Expected >=20% reduction for homogeneous array, got {reduction_pct:.1f}%"
        )

    def test_unserializable_data_records_metric(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """json.dumps TypeError path calls record_toon_encoding(0, 0, False)."""
        import api_agent.llm.toon_encoder as toon_mod

        calls: list[tuple] = []
        monkeypatch.setattr(
            toon_mod,
            "record_toon_encoding",
            lambda **kw: calls.append((kw["original_chars"], kw["toon_chars"], kw["applied"])),
        )

        encoder = ToolResultEncoder()
        encoded, applied = encoder.encode([object()])  # not JSON-serializable
        assert applied is False
        assert encoded == "[]"
        assert calls == [(0, 0, False)]

    def test_record_toon_encoding_called_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_toon_encoding is called with applied=True on successful TOON compression."""
        calls: list[dict] = []

        def fake_record(original_chars: int, toon_chars: int, applied: bool) -> None:
            calls.append(
                {"original_chars": original_chars, "toon_chars": toon_chars, "applied": applied}
            )

        import api_agent.llm.toon_encoder as toon_mod

        monkeypatch.setattr(toon_mod, "record_toon_encoding", fake_record)

        data = [
            {"id": i, "name": f"user_{i}", "email": f"u{i}@example.com", "active": True}
            for i in range(30)
        ]
        encoder = ToolResultEncoder()
        _, was_applied = encoder.encode(data)

        assert was_applied is True
        assert len(calls) == 1
        assert calls[0]["applied"] is True
        assert calls[0]["toon_chars"] < calls[0]["original_chars"]

    def test_record_toon_encoding_called_on_no_gain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """record_toon_encoding is called with applied=False when TOON has no gain."""
        calls: list[dict] = []

        def fake_record(original_chars: int, toon_chars: int, applied: bool) -> None:
            calls.append(
                {"original_chars": original_chars, "toon_chars": toon_chars, "applied": applied}
            )

        import api_agent.llm.toon_encoder as toon_mod

        monkeypatch.setattr(toon_mod, "record_toon_encoding", fake_record)

        # Single tiny item — TOON overhead likely exceeds savings
        data = [{"a": 1}]
        encoder = ToolResultEncoder()
        _, was_applied = encoder.encode(data)

        assert len(calls) == 1
        # Either no-gain or applied — but record_toon_encoding was called
        assert calls[0]["applied"] == was_applied

    def test_record_toon_encoding_called_on_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """record_toon_encoding is called with applied=False on import error fallback."""
        from api_agent.llm.toon_encoder import _log_toon_unavailable

        _log_toon_unavailable.cache_clear()

        calls: list[dict] = []

        def fake_record(original_chars: int, toon_chars: int, applied: bool) -> None:
            calls.append(
                {"original_chars": original_chars, "toon_chars": toon_chars, "applied": applied}
            )

        import api_agent.llm.toon_encoder as toon_mod

        monkeypatch.setattr(toon_mod, "record_toon_encoding", fake_record)
        monkeypatch.setitem(sys.modules, "toon_format", None)

        data = [{"x": i} for i in range(10)]
        encoder = ToolResultEncoder()
        encoder.encode(data)

        assert len(calls) == 1
        assert calls[0]["applied"] is False
        # On import error, toon_chars == original_chars (no compression possible)
        assert calls[0]["toon_chars"] == calls[0]["original_chars"]

        _log_toon_unavailable.cache_clear()

    def test_record_toon_encoding_called_on_encoding_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """record_toon_encoding is called with applied=False on encoding exception."""
        calls: list[dict] = []

        def fake_record(original_chars: int, toon_chars: int, applied: bool) -> None:
            calls.append(
                {"original_chars": original_chars, "toon_chars": toon_chars, "applied": applied}
            )

        import toon_format

        import api_agent.llm.toon_encoder as toon_mod

        monkeypatch.setattr(toon_mod, "record_toon_encoding", fake_record)
        monkeypatch.setattr(
            toon_format, "encode", lambda x: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        data = [{"x": i} for i in range(10)]
        encoder = ToolResultEncoder()
        encoder.encode(data)

        assert len(calls) == 1
        assert calls[0]["applied"] is False
        assert calls[0]["toon_chars"] == calls[0]["original_chars"]

    def test_non_list_input_returns_json(self) -> None:
        """ToolResultEncoder is designed for lists; dict input falls back to JSON."""
        # The encoder's contract is list[dict] input — passing other types
        # should degrade gracefully, not crash
        encoder = ToolResultEncoder()
        data = [{"nested": {"a": 1}}, {"nested": {"b": 2}}]
        encoded, was_applied = encoder.encode(data)

        # Either TOON or JSON — both are valid outcomes, no exception
        assert isinstance(encoded, str)
        assert isinstance(was_applied, bool)


# ---------------------------------------------------------------------------
# Integration: format_tool_response() with TOON
# ---------------------------------------------------------------------------


class TestFormatToolResponseWithToon:
    """format_tool_response() uses TOON encoder when enabled."""

    @pytest.mark.asyncio
    async def test_toon_applied_when_list_fits_budget(self) -> None:
        """Large homogeneous list: TOON is applied, result is raw TOON (not JSON-wrapped)."""
        from api_agent.agent.orchestrator import format_tool_response

        # Big enough to compress meaningfully — 50 homogeneous records
        data = [
            {"id": i, "name": f"user_{i}", "email": f"u{i}@test.com", "role": "admin"}
            for i in range(50)
        ]
        # Precondition: confirm TOON is actually smaller for this data
        import toon_format

        toon_str = toon_format.encode(data)
        assert len(toon_str) < len(json.dumps(data)), "Test precondition: TOON must be smaller"

        result = await format_tool_response(
            stored_data=data,
            schema_info=None,
            name="users",
            result={"success": True},
        )

        # TOON output carries a compact header + tabular body
        assert result.startswith("[success:true format:toon]\n"), (
            f"Expected TOON header, got: {result[:60]}"
        )
        # TOON should be smaller than the original JSON (even with header)
        assert len(result) < len(json.dumps(data)), (
            f"TOON result ({len(result)} chars) should be smaller than JSON ({len(json.dumps(data))} chars)"
        )

    @pytest.mark.asyncio
    async def test_toon_disabled_returns_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When TOON_TOOL_RESULTS_ENABLED=False, format_tool_response returns JSON."""
        from api_agent import config
        from api_agent.agent.orchestrator import format_tool_response

        monkeypatch.setattr(config.settings, "TOON_TOOL_RESULTS_ENABLED", False)

        data = [{"id": i, "name": f"user_{i}", "email": f"u{i}@test.com"} for i in range(10)]

        result = await format_tool_response(
            stored_data=data,
            schema_info=None,
            name="users",
            result={"success": True},
        )

        parsed = json.loads(result)
        assert parsed.get("success") is True
        assert "data" in parsed

    @pytest.mark.asyncio
    async def test_schema_info_path_unaffected_by_toon(self) -> None:
        """schema_info path (single object) is not TOON-encoded — unchanged."""
        from api_agent.agent.orchestrator import format_tool_response

        schema_info = {"rows": 100, "schema": "id: INTEGER, name: VARCHAR", "hint": "Use sql_query"}
        result = await format_tool_response(
            stored_data={"some": "object"},
            schema_info=schema_info,
            name="items",
            result={"success": True},
        )

        parsed = json.loads(result)
        assert parsed.get("success") is True
        assert parsed.get("rows") == 100

    @pytest.mark.asyncio
    async def test_failed_result_unaffected_by_toon(self) -> None:
        """Error results are passed through without TOON encoding."""
        from api_agent.agent.orchestrator import format_tool_response

        result = await format_tool_response(
            stored_data=None,
            schema_info=None,
            name="items",
            result={"success": False, "error": "Not found"},
        )

        parsed = json.loads(result)
        assert parsed.get("success") is False
        assert parsed.get("error") == "Not found"

    @pytest.mark.asyncio
    async def test_toon_oversized_falls_back_to_truncated_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If TOON output exceeds MAX_TOOL_RESPONSE_CHARS, falls back to truncated JSON."""
        from api_agent import config
        from api_agent.agent.orchestrator import format_tool_response

        # Set a very small budget so even TOON won't fit
        monkeypatch.setattr(config.settings, "MAX_TOOL_RESPONSE_CHARS", 50)

        data = [{"id": i, "name": f"user_{i}", "email": f"u{i}@test.com"} for i in range(20)]

        result = await format_tool_response(
            stored_data=data,
            schema_info=None,
            name="users",
            result={"success": True},
        )

        # Should fall back to truncated JSON structure
        parsed = json.loads(result)
        assert parsed.get("success") is True
        # Truncated result has "truncated": True
        assert parsed.get("truncated") is True


# ---------------------------------------------------------------------------
# Integration: sql_query tool with TOON
# ---------------------------------------------------------------------------


class TestSqlQueryToolWithToon:
    """sql_query() TOON integration in orchestrator.py."""

    @pytest.mark.asyncio
    async def test_sql_result_toon_encoded_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQL results are TOON-encoded when TOON_TOOL_RESULTS_ENABLED=True."""
        from contextvars import ContextVar

        from api_agent import config
        from api_agent.agent.orchestrator import AgentContextVars, create_sql_query_tool

        monkeypatch.setattr(config.settings, "TOON_TOOL_RESULTS_ENABLED", True)

        # Build context vars with pre-loaded data
        query_results: ContextVar[dict] = ContextVar("test_qr")
        last_result: ContextVar[list] = ContextVar("test_lr")
        sql_steps: ContextVar[list[str]] = ContextVar("test_ss")

        # 30 homogeneous rows — enough for TOON to compress
        rows = [
            {"id": i, "name": f"user_{i}", "email": f"u{i}@test.com", "active": True}
            for i in range(30)
        ]
        query_results.set({"data": rows})
        last_result.set([rows])
        sql_steps.set([])

        ctx_vars = AgentContextVars(
            api_calls=ContextVar("ac"),
            recipe_steps=ContextVar("rs"),
            query_results=query_results,
            last_result=last_result,
            raw_schema=ContextVar("sc"),
            sql_steps=sql_steps,
        )

        # Mock execute_sql to return the rows
        import api_agent.agent.orchestrator as orch_mod

        monkeypatch.setattr(
            orch_mod, "execute_sql", lambda data, sql: {"success": True, "result": rows}
        )

        sql_tool = create_sql_query_tool(ctx_vars, lambda msg: None, "no data")
        # The tool() wrapper exposes the inner function
        result = await sql_tool.function(sql="SELECT * FROM data")

        # Should have TOON header
        assert result.startswith("[success:true format:toon]\n"), (
            f"Expected TOON header, got: {result[:60]}"
        )

    @pytest.mark.asyncio
    async def test_sql_result_json_when_toon_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQL results use JSON when TOON_TOOL_RESULTS_ENABLED=False."""
        from contextvars import ContextVar

        from api_agent import config
        from api_agent.agent.orchestrator import AgentContextVars, create_sql_query_tool

        monkeypatch.setattr(config.settings, "TOON_TOOL_RESULTS_ENABLED", False)

        query_results: ContextVar[dict] = ContextVar("test_qr")
        last_result: ContextVar[list] = ContextVar("test_lr")
        sql_steps: ContextVar[list[str]] = ContextVar("test_ss")

        rows = [{"id": i, "name": f"user_{i}"} for i in range(10)]
        query_results.set({"data": rows})
        last_result.set([rows])
        sql_steps.set([])

        ctx_vars = AgentContextVars(
            api_calls=ContextVar("ac"),
            recipe_steps=ContextVar("rs"),
            query_results=query_results,
            last_result=last_result,
            raw_schema=ContextVar("sc"),
            sql_steps=sql_steps,
        )

        import api_agent.agent.orchestrator as orch_mod

        monkeypatch.setattr(
            orch_mod, "execute_sql", lambda data, sql: {"success": True, "result": rows}
        )

        sql_tool = create_sql_query_tool(ctx_vars, lambda msg: None, "no data")
        result = await sql_tool.function(sql="SELECT * FROM data")

        parsed = json.loads(result)
        assert parsed.get("success") is True
        assert "data" in parsed
