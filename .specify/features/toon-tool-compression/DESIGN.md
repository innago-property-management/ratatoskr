# TOON Tool Result Compression — Technical Design

**Version:** 1.0
**Date:** 2026-03-16
**Changes:** Initial design

## Architecture

### New Module: `api_agent/llm/toon_encoder.py`

Single class `ToolResultEncoder` with one method: `encode(data: list[dict]) -> str`.

```python
class ToolResultEncoder:
    """TOON-encode tool result payloads for LLM consumption.

    Wrapper around toon_format with graceful degradation:
    - toon_format not installed → returns JSON (logs warning once)
    - TOON output >= JSON output → returns JSON (logs info)
    - encoding error → returns JSON (logs warning)
    Never raises.
    """

    def encode(self, data: list[dict]) -> tuple[str, bool]:
        """Encode data as TOON if it produces a smaller result than JSON.

        Returns:
            (encoded_string, was_toon_applied)
        """
```

Design notes:
- Stateless — no constructor args, no config injection (config checked via settings import)
- `toon_format` import is lazy (inside the method) to tolerate missing optional dep
- Returns `(str, bool)` tuple — parallel to ToonLayer.encode() for consistency
- Logs a single warning on first ImportError (uses `lru_cache` guard to avoid log spam)

### Integration Point: `format_tool_response()` in orchestrator.py

Current flow:
```python
async def format_tool_response(stored_data, schema_info, name, result) -> str:
    if result.get("success") and stored_data:
        if schema_info:
            return json.dumps({"success": True, "table": name, **schema_info}, indent=2)
        if isinstance(stored_data, list):
            return json.dumps(
                {"success": True, **await truncate_for_context_async(stored_data, name)},
                indent=2,
            )
    return json.dumps(result, indent=2)
```

New flow — TOON applied to the list data **before** char-based truncation:
```python
async def format_tool_response(stored_data, schema_info, name, result) -> str:
    if result.get("success") and stored_data:
        if schema_info:
            return json.dumps({"success": True, "table": name, **schema_info}, indent=2)
        if isinstance(stored_data, list):
            encoder = ToolResultEncoder()
            toon_str, toon_applied = encoder.encode(stored_data)
            if toon_applied:
                # TOON is already a string — check if it fits within budget
                if len(toon_str) <= settings.MAX_TOOL_RESPONSE_CHARS:
                    return toon_str  # Return raw TOON (self-describing format)
                # Still too large — fall through to truncated JSON path
            return json.dumps(
                {"success": True, **await truncate_for_context_async(stored_data, name)},
                indent=2,
            )
    return json.dumps(result, indent=2)
```

### Integration Point: SQL results in `create_sql_query_tool()`

Same pattern — applied to the `rows` list before char-based truncation.

### Config

Add to `api_agent/config.py`:
```python
# TOON tool result compression
TOON_TOOL_RESULTS_ENABLED: bool = True
```

### Observability

Log per tool call with structlog:
```python
logger.info(
    "toon_tool_result",
    was_applied=toon_applied,
    original_chars=len(json.dumps(data)),
    toon_chars=len(toon_str),
    reduction_pct=round((1 - len(toon_str) / len(json.dumps(data))) * 100, 1),
)
```

## File Change Summary

| File | Change |
|------|--------|
| `api_agent/llm/toon_encoder.py` | New — ToolResultEncoder class |
| `api_agent/agent/orchestrator.py` | Wire ToolResultEncoder into format_tool_response() and SQL tool |
| `api_agent/config.py` | Add TOON_TOOL_RESULTS_ENABLED setting |
| `tests/test_toon_encoder.py` | New — ~12 TDD tests |
| `tests/test_config.py` | Add 2 config tests |

## Test Plan

```
TestToolResultEncoder:
  test_encodes_homogeneous_list_smaller
  test_returns_json_when_toon_larger
  test_degrades_on_import_error
  test_degrades_on_encoding_exception
  test_empty_list_returns_json
  test_single_item_list
  test_heterogeneous_list_may_not_compress
  test_returns_tuple_of_str_and_bool

TestFormatToolResponseWithToon:
  test_toon_applied_when_list_fits
  test_falls_back_to_truncated_json_when_toon_oversized
  test_schema_info_path_unaffected
  test_failed_result_unaffected

TestSqlQueryToolWithToon:
  test_sql_result_toon_encoded

TestConfigToon:
  test_toon_tool_results_enabled_default_true
  test_toon_tool_results_disabled_bypasses_encoder
```

## Rollback Strategy

Set `API_AGENT_TOON_TOOL_RESULTS_ENABLED=false` env var — no code changes needed.
The encoder is only called when `settings.TOON_TOOL_RESULTS_ENABLED` is True.
