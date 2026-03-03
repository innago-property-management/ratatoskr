"""Smart schema reduction pipeline: TOON encoding + AI-powered filtering.

Reduces oversized API schemas to fit within LLM context limits through a
two-layer pipeline:
  1. ToonLayer — lossless structural compression (JSON schemas only)
  2. HaikuLayer — AI-powered relevance filtering (future, Phase 3)

Never raises — always returns a valid schema_text, falling back gracefully
through each layer on any error.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReductionResult:
    """Result of a schema reduction attempt."""

    schema_text: str  # Final schema text to use (reduced or original)
    was_toon_applied: bool  # True if TOON encoding produced a net reduction
    was_ai_applied: bool  # True if Haiku reduction was invoked and succeeded
    original_chars: int
    final_chars: int


class ToonLayer:
    """TOON encode with graceful degradation.

    Applies TOON encoding only to valid JSON text (e.g., GraphQL introspection).
    Non-JSON text (REST DSL, proto IDL) is skipped — TOON encodes Python objects,
    not arbitrary text.

    If toon_format is not installed -> logs warning, returns original.
    If TOON output is larger -> returns original with was_applied=False.
    Never raises.
    """

    def encode(self, text: str) -> tuple[str, bool]:
        """Returns (encoded_or_original, was_applied)."""
        # First, try to parse as JSON — TOON only works on structured data
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.debug("ToonLayer: input is not JSON, skipping TOON encoding")
            return (text, False)

        # Try to import and use toon_format
        try:
            from toon_format import encode as toon_encode
        except ImportError:
            logger.warning(
                "toon_format package not installed — TOON encoding disabled. "
                "Install with: pip install 'toon_format @ git+https://github.com/toon-format/toon-python.git'"
            )
            return (text, False)

        try:
            encoded = toon_encode(parsed)
        except Exception:
            logger.warning("ToonLayer: TOON encoding failed", exc_info=True)
            return (text, False)

        # Only use TOON output if it's actually smaller
        if len(encoded) >= len(text):
            logger.info(
                "ToonLayer: TOON output not smaller (original=%d, toon=%d), using original",
                len(text),
                len(encoded),
            )
            return (text, False)

        logger.info(
            "ToonLayer: TOON reduced schema from %d to %d chars (%.1f%% reduction)",
            len(text),
            len(encoded),
            (1 - len(encoded) / len(text)) * 100,
        )
        return (encoded, True)


class HaikuLayer:
    """Claude Haiku reduction layer (stub — Phase 3).

    Only instantiated when api_key is non-empty.
    Never raises — returns original on failure.
    """

    def __init__(self, api_key: str, model: str, timeout_ms: int) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_ms = timeout_ms

    async def reduce(
        self,
        schema_text: str,
        question: str,
    ) -> tuple[str, bool]:
        """Returns (reduced_or_original, was_applied).

        Stub implementation — returns original unchanged.
        """
        return (schema_text, False)


def _get_api_key(explicit_key: str = "") -> str:
    """Resolve the API key for schema reduction.

    Priority:
      1. Explicitly passed key (from settings.SCHEMA_REDUCTION_API_KEY)
      2. ANTHROPIC_API_KEY env var
      3. Empty string (Haiku layer disabled)
    """
    if explicit_key:
        return explicit_key
    return os.environ.get("ANTHROPIC_API_KEY", "")


async def reduce_schema(
    schema_text: str,
    question: str,
    threshold: int,
    api_key: str = "",
    model: str = "claude-haiku-4-5",
    timeout_ms: int = 30_000,
    enabled: bool = True,
    max_input_chars: int = 100_000,
) -> ReductionResult:
    """Reduce schema_text to fit within threshold, guided by question.

    Pipeline:
      1. TOON encode — lossless compression, no AI call
      2. If still over threshold AND API key available -> Haiku reduction
      3. If Haiku unavailable or fails -> return TOON (or original if TOON larger)
      4. Never raises — always returns a valid schema_text

    Args:
        schema_text:  Serialized DSL text from protocol-specific builder.
        question:     User's NL question (guides Haiku's relevance filter).
        threshold:    Character limit (typically settings.MAX_SCHEMA_CHARS).
        api_key:      Anthropic API key. Empty = skip Haiku layer.
        model:        Haiku model name.
        timeout_ms:   Timeout for the Haiku API call.
        enabled:      Master switch. If False, returns original text unchanged.
        max_input_chars: Skip Haiku above this size (safety limit).

    Returns:
        ReductionResult with the best schema_text that fits (or best effort if not).
    """
    original_chars = len(schema_text) if schema_text else 0

    if not enabled or not schema_text:
        return ReductionResult(
            schema_text=schema_text or "",
            was_toon_applied=False,
            was_ai_applied=False,
            original_chars=original_chars,
            final_chars=original_chars,
        )

    # Layer 1: TOON
    toon_layer = ToonLayer()
    current_text, toon_applied = toon_layer.encode(schema_text)

    if len(current_text) <= threshold:
        result = ReductionResult(
            schema_text=current_text,
            was_toon_applied=toon_applied,
            was_ai_applied=False,
            original_chars=original_chars,
            final_chars=len(current_text),
        )
        logger.info(
            "Schema reduction complete: toon=%s, ai=%s, %d -> %d chars",
            result.was_toon_applied,
            result.was_ai_applied,
            result.original_chars,
            result.final_chars,
        )
        return result

    # Layer 2: Haiku (stub — Phase 3)
    ai_applied = False
    resolved_key = _get_api_key(api_key)
    if resolved_key and original_chars <= max_input_chars:
        haiku_layer = HaikuLayer(resolved_key, model, timeout_ms)
        reduced_text, ai_applied = await haiku_layer.reduce(current_text, question)
        if ai_applied:
            current_text = reduced_text

    # Final fallback: hard truncation with marker
    if len(current_text) > threshold:
        current_text = (
            current_text[:threshold] + "\n[SCHEMA TRUNCATED - use search_schema() to explore]"
        )

    result = ReductionResult(
        schema_text=current_text,
        was_toon_applied=toon_applied,
        was_ai_applied=ai_applied,
        original_chars=original_chars,
        final_chars=len(current_text),
    )
    logger.info(
        "Schema reduction complete: toon=%s, ai=%s, %d -> %d chars",
        result.was_toon_applied,
        result.was_ai_applied,
        result.original_chars,
        result.final_chars,
    )
    return result
