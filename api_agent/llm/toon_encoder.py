"""TOON encoding for tool result payloads.

Wraps toon_format with graceful degradation for use in the agent tool loop.
Applied to list-of-dict API results before they are serialized and sent to
the LLM as tool results.

TOON achieves 30-60% token reduction vs JSON for homogeneous arrays —
exactly the shape produced by GraphQL, REST, gRPC, and SQL queries.
"""

from __future__ import annotations

import functools
import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


_TOON_HEADER = "[success:true format:toon]\n"


@functools.lru_cache(maxsize=1)
def _log_toon_unavailable() -> None:
    """Log toon_format ImportError once per process (thread-safe via lru_cache)."""
    logger.warning("toon_package_missing_tool_results")


class ToolResultEncoder:
    """TOON-encode tool result payloads for LLM consumption.

    Encodes list[dict] API results as TOON format when it produces a
    smaller result than JSON.  Falls back to JSON on any failure.

    Design:
    - Stateless — safe to instantiate per-call with no overhead
    - Lazy import — toon_format is an optional dep; absent = graceful JSON fallback
    - Never raises — all exceptions are caught and logged
    """

    def encode(self, data: list[Any]) -> tuple[str, bool]:
        """Encode data as TOON if smaller than JSON, otherwise return JSON.

        Args:
            data: List of dicts (API result rows).

        Returns:
            (encoded_string, was_toon_applied)
            - encoded_string: TOON text if compressed, else JSON string
            - was_toon_applied: True only if TOON was used and is smaller
        """
        # Baseline: serialize as JSON for size comparison
        try:
            json_str = json.dumps(data)
        except (TypeError, ValueError):
            # Unserializable data — return empty list JSON
            return ("[]", False)

        original_len = len(json_str)

        # Lazy import of optional dependency
        try:
            from toon_format import encode as toon_encode
        except ImportError:
            _log_toon_unavailable()
            return (json_str, False)

        try:
            toon_str = toon_encode(data)
        except Exception:
            logger.warning("toon_encoding_failed_tool_results", exc_info=True)
            return (json_str, False)

        toon_len = len(toon_str)

        if toon_len + len(_TOON_HEADER) >= original_len:
            logger.debug(
                "toon_tool_result_no_gain",
                original_chars=original_len,
                toon_chars=toon_len,
            )
            return (json_str, False)

        total_toon_len = toon_len + len(_TOON_HEADER)
        reduction_pct = round((1 - total_toon_len / original_len) * 100, 1)
        logger.info(
            "toon_tool_result_compressed",
            original_chars=original_len,
            toon_chars=total_toon_len,
            reduction_pct=reduction_pct,
        )
        # Prepend a compact header so the LLM knows this is TOON, not JSON.
        # The header also carries the success=true envelope that JSON paths include.
        return (f"{_TOON_HEADER}{toon_str}", True)
