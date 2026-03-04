"""Security sanitization for LLM-facing content.

Provides defense-in-depth against prompt injection via API schemas and
information leakage via error messages passed to LLM context.
"""

import re

# Max length for individual schema descriptions (fields, types, etc.)
MAX_DESCRIPTION_CHARS = 500

# Patterns that look like prompt injection attempts in schema descriptions.
# Compiled once at module load for performance.
_INJECTION_PATTERNS = re.compile(
    r"(?im)"
    r"(?:ignore\s+(?:previous|all|above|prior)\s+(?:instructions?|prompts?|rules?))"
    r"|(?:you\s+are\s+(?:a|an|now)\s+)"
    r"|(?:system\s*:\s*)"
    r"|(?:^\s*assistant\s*:\s*)"
    r"|(?:human\s*:\s*)"
    r"|(?:^\s*user\s*:\s*)"
    r"|(?:forget\s+(?:everything|all|previous))"
    r"|(?:disregard\s+(?:previous|all|above|prior))"
    r"|(?:new\s+instructions?\s*:)"
    r"|(?:override\s+(?:previous|system|all))"
    r"|(?:do\s+not\s+follow\s+(?:previous|prior|above))"
    r"|(?:instead\s*,?\s+(?:you\s+(?:should|must|will)))"
)

# XML/HTML-like tags that could be used for prompt structure injection
_TAG_PATTERN = re.compile(r"</?(?:system|prompt|instructions?|role|context|rules?)\b[^>]*>", re.I)


def sanitize_schema_text(text: str | None) -> str:
    """Sanitize API schema description text before passing to LLM.

    Strips common prompt injection patterns and truncates to a safe length.
    Designed for API schema descriptions (OpenAPI, GraphQL, gRPC comments).

    Args:
        text: Raw description text from an API schema

    Returns:
        Sanitized text safe for LLM context inclusion
    """
    if not text:
        return ""

    # Strip injection patterns
    result = _INJECTION_PATTERNS.sub("[redacted]", text)

    # Strip XML-like prompt structure tags
    result = _TAG_PATTERN.sub("", result)

    # Truncate to max length (subtract 3 for the "..." suffix to stay within budget)
    if len(result) > MAX_DESCRIPTION_CHARS:
        result = result[: MAX_DESCRIPTION_CHARS - 3] + "..."

    return result


def sanitize_error(exc: Exception) -> str:
    """Sanitize an exception for LLM context.

    Returns only the exception type and a generic message — no stack traces,
    no internal paths, no database schema details.

    Args:
        exc: The caught exception

    Returns:
        A safe error string suitable for LLM consumption
    """
    exc_type = type(exc).__name__
    msg = str(exc)

    # Strip file paths (Unix and Windows)
    msg = re.sub(r"(?:/[\w./-]+|[A-Z]:\\[\w.\\-]+)", "[path]", msg)

    # Strip line numbers and tracebacks
    msg = re.sub(r"line \d+", "line [N]", msg, flags=re.I)
    msg = re.sub(r"Traceback \(most recent call last\):.*", "", msg, flags=re.S)

    # Truncate long messages
    if len(msg) > 200:
        msg = msg[:200] + "..."

    return f"{exc_type}: {msg}".strip()
