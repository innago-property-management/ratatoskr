"""Generic spec reader and writer — file I/O for API schemas."""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)


class SpecReader:
    """Reads API spec files from disk. Returns parsed dict or raw string."""

    @staticmethod
    def read_file(path: str) -> dict[str, Any] | str | None:
        """Read and parse a spec file.

        - JSON/YAML files with dict root → returns dict
        - Other text files (SDL, proto) → returns raw string
        - Missing/empty/corrupt → returns None

        Detection is content-based (tries JSON first, then YAML),
        not extension-based.
        """
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except (OSError, FileNotFoundError):
            logger.debug("spec_read_file_not_found", path=path)
            return None

        if not raw or not raw.strip():
            return None

        # Try JSON first
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                logger.debug("spec_read_json_parse_failed", path=path)
                # Fall through to YAML — content like {'key': 'value'} is valid YAML

        # Try YAML (only if it looks structured, not plain text)
        try:
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict):
                return parsed
        except yaml.YAMLError:
            pass

        # Plain text (SDL, proto, etc.) — return as string if non-trivial
        if len(stripped) > 1:
            return raw

        return None


class SpecWriter:
    """Writes API specs to disk."""

    @staticmethod
    def write_file(path: str, content: dict[str, Any] | str) -> None:
        """Write spec content to a file.

        - dict → written as JSON
        - str → written as-is

        Creates parent directories if needed. Raises OSError on failure.
        """
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            if isinstance(content, dict):
                json.dump(content, f, indent=2, ensure_ascii=False)
            else:
                f.write(content)
