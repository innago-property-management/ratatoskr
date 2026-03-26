"""Schema store — load once at startup, serve from memory.

Ratatoskr only talks to APIs declared in its config. Schemas load once at
startup and are held in memory for the lifetime of the process. Agents
never fetch schemas per-query.

For HTTP URLs: fetch once at startup, save to disk for next restart.
For file paths (file:// or bare): read from disk at startup.
On subsequent startups: if a cached file exists, read it instead of fetching.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import structlog

from .spec_io import SpecReader, SpecWriter

logger = structlog.get_logger(__name__)

Fetcher = Callable[[str], Awaitable[dict[str, Any] | str | None]]


def is_local_path(source: str) -> tuple[bool, str]:
    """Detect if source is a local file path. Returns (is_local, resolved_path).

    Guards against bare host:port strings (e.g., "localhost:50051") which
    urlparse misparses as scheme="localhost".
    """
    if source.startswith("file://"):
        return True, source[7:]
    if source.startswith("/") or source.startswith("./"):
        return True, source
    # Bare host:port or host:port/path (e.g., "localhost:50051", "localhost:8080/api")
    # — urlparse sees "localhost" as scheme. These are network targets, not local paths.
    if ":" in source and not source.startswith(("/", ".")):
        parsed = urlparse(source)
        if parsed.scheme and parsed.scheme.isalpha() and "." not in parsed.scheme:
            # Single-word "scheme" like "localhost" — likely host:port, not a file
            return False, ""
    parsed = urlparse(source)
    if not parsed.scheme or parsed.scheme not in ("http", "https", "grpc", "grpcs"):
        # Only treat as local if it looks like a path (has path separators)
        if "/" in source or source.startswith("."):
            return True, source
        # Bare strings without slashes (e.g., "localhost:50051") are not local paths
        return False, ""
    return False, ""


def _cache_filename(source: str) -> str:
    """Generate a stable filename from a source URL."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16] + ".json"


class SchemaStore:
    """Pre-loaded schema store. Populated at startup, read-only at runtime.

    Usage (startup):
        store = SchemaStore(cache_dir="/var/cache/ratatoskr/schemas")
        await store.load(url_or_path, fetcher=my_http_fetch_fn)

    Usage (runtime — agents):
        spec = store.get(url_or_path)
    """

    def __init__(self, cache_dir: str | None = None) -> None:
        self._schemas: dict[str, dict[str, Any] | str] = {}
        self._cache_dir = os.path.expanduser(cache_dir) if cache_dir else None

    async def load(
        self,
        source: str,
        *,
        fetcher: Fetcher,
    ) -> dict[str, Any] | str | None:
        """Load a schema at startup. Called once per configured target.

        For file paths: reads from disk.
        For HTTP URLs: checks disk cache first, fetches if miss, saves to disk.
        Result is held in memory for the process lifetime.
        """
        if not source:
            return None

        # File path — read directly
        is_local, local_path = is_local_path(source)
        if is_local:
            result = SpecReader.read_file(local_path)
            if result is not None:
                self._schemas[source] = result
                logger.info("schema_loaded_from_file", source=source)
            else:
                logger.warning("schema_file_not_found", source=source)
            return result

        # HTTP URL — check disk cache first
        if self._cache_dir:
            cache_path = os.path.join(self._cache_dir, _cache_filename(source))
            cached = SpecReader.read_file(cache_path)
            if cached is not None:
                self._schemas[source] = cached
                logger.info("schema_loaded_from_cache", source=source, path=cache_path)
                return cached

        # Fetch from network
        try:
            result = await fetcher(source)
        except Exception:
            logger.warning("schema_fetch_failed", source=source, exc_info=True)
            return None

        if result is None:
            return None

        # Save to memory + disk
        self._schemas[source] = result
        if self._cache_dir:
            cache_path = os.path.join(self._cache_dir, _cache_filename(source))
            try:
                SpecWriter.write_file(cache_path, result)
                logger.info("schema_saved_to_cache", source=source, path=cache_path)
            except Exception:
                logger.warning("schema_cache_write_failed", path=cache_path, exc_info=True)

        return result

    def get(self, source: str) -> dict[str, Any] | str | None:
        """Get a pre-loaded schema. Returns None if not loaded."""
        return self._schemas.get(source)

    def is_loaded(self, source: str) -> bool:
        """Check if a schema has been loaded for this source."""
        return source in self._schemas


def _create_schema_store() -> SchemaStore:
    """Create the module-level SchemaStore from config."""
    from ..config import settings

    cache_dir = settings.SCHEMA_CACHE_DIR or None
    return SchemaStore(cache_dir=cache_dir)


# Module-level singleton — populated at startup, read-only at runtime.
SCHEMA_STORE = _create_schema_store()
