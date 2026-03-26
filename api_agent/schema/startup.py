"""Schema startup loader — populate SCHEMA_STORE from config at boot time.

Called once during ASGI startup event. For HTTP URLs, fetches the spec
and saves to disk cache. For file paths, reads directly from disk.
gRPC targets are skipped (they require live reflection for DescriptorPool).
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from ..config import settings
from ..pool import pool
from .spec_cache import SCHEMA_STORE

logger = structlog.get_logger(__name__)

# API types that benefit from pre-loaded schemas.
# gRPC needs live reflection for its DescriptorPool — can't pre-load.
_PRELOADABLE_TYPES = {"rest"}


async def _http_fetcher(url: str) -> dict[str, Any] | str | None:
    """Fetch a spec over HTTP, parse JSON or YAML."""
    import yaml

    client = await pool.get_http_client(url)
    resp = await client.get(url)
    resp.raise_for_status()
    raw = resp.text

    if raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass  # Fall through to YAML

    parsed = yaml.safe_load(raw)
    if isinstance(parsed, dict):
        return parsed

    # Plain text (e.g., GraphQL SDL)
    if raw.strip():
        return raw

    return None


async def load_configured_schemas() -> int:
    """Load schemas for configured targets into SCHEMA_STORE.

    Returns the number of schemas successfully loaded.
    """
    target_url = settings.DEFAULT_TARGET_URL
    api_type = settings.DEFAULT_API_TYPE

    if not target_url:
        return 0

    if api_type not in _PRELOADABLE_TYPES:
        logger.debug("schema_startup_skip", api_type=api_type, reason="not preloadable")
        return 0

    try:
        result = await SCHEMA_STORE.load(target_url, fetcher=_http_fetcher)
        if result is not None:
            logger.info("schema_startup_loaded", source=target_url, api_type=api_type)
            return 1
        else:
            logger.warning("schema_startup_empty", source=target_url)
            return 0
    except Exception:
        logger.warning("schema_startup_failed", source=target_url, exc_info=True)
        return 0
