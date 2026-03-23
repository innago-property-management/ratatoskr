"""Pluggable persistence backends for the recipe store."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class RecipeBackend(ABC):
    """ABC for recipe persistence backends."""

    @abstractmethod
    async def load_all(self) -> list[tuple[str, str, str, dict[str, Any], float, float]]:
        """Load all recipes.

        Returns list of (api_id, schema_hash, recipe_id, recipe_dict, created_at, last_used_at).
        """

    @abstractmethod
    async def save(
        self,
        api_id: str,
        schema_hash: str,
        recipe_id: str,
        recipe: dict[str, Any],
        created_at: float,
        last_used_at: float,
    ) -> None:
        """Persist a recipe. Fire-and-forget — failures logged, never raised."""

    @abstractmethod
    async def delete_by_schema(self, api_id: str, schema_hash: str) -> int:
        """Delete recipes for an API+schema pair. Returns count deleted."""


class MemoryBackend(RecipeBackend):
    """No-op backend — current behavior. For tests and ephemeral deployments."""

    async def load_all(
        self,
    ) -> list[tuple[str, str, str, dict[str, Any], float, float]]:
        return []

    async def save(
        self,
        api_id: str,
        schema_hash: str,
        recipe_id: str,
        recipe: dict[str, Any],
        created_at: float,
        last_used_at: float,
    ) -> None:
        pass

    async def delete_by_schema(self, api_id: str, schema_hash: str) -> int:
        return 0


def create_backend_from_config(settings: Any) -> RecipeBackend:
    """Create the appropriate backend based on settings.

    Falls back to MemoryBackend if SQLiteBackend init fails.
    """
    if settings.RECIPE_PERSISTENCE == "sqlite":
        from .sqlite_backend import SQLiteBackend

        try:
            path = SQLiteBackend.resolve_path(settings.RECIPE_SQLITE_PATH)
            return SQLiteBackend(db_path=path, max_recipes=settings.RECIPE_CACHE_SIZE * 4)
        except Exception:
            logger.warning("sqlite_backend_init_failed", exc_info=True)
            return MemoryBackend()
    return MemoryBackend()
