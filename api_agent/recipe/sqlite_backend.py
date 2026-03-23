"""SQLite persistence backend for the recipe store."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from typing import Any

import structlog

from .backend import RecipeBackend

logger = structlog.get_logger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS recipes (
    api_id TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    PRIMARY KEY (api_id, schema_hash, recipe_id)
);
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_recipes_schema ON recipes(api_id, schema_hash);"


class SQLiteBackend(RecipeBackend):
    """SQLite-backed recipe persistence.

    Uses connection-per-operation inside asyncio.to_thread() to sidestep
    sqlite3 thread-safety concerns. WAL mode for concurrent write safety.
    """

    def __init__(self, db_path: str, max_recipes: int = 256) -> None:
        self._db_path = db_path
        self._max_recipes = max(1, int(max_recipes))

        # Create parent directory if needed
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # Initialize schema and WAL mode on first connect
        self._init_db()

    def _init_db(self) -> None:
        """Create table and enable WAL mode."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a new connection. WAL mode is persisted in the DB file by _init_db()."""
        return sqlite3.connect(self._db_path)

    @staticmethod
    def resolve_path(explicit_path: str) -> str:
        """Resolve the database file path using the fallback chain.

        1. explicit_path if non-empty
        2. $XDG_CACHE_HOME/ratatoskr/recipes.db
        3. $HOME/.cache/ratatoskr/recipes.db
        4. tempdir/ratatoskr/recipes.db (with warning)
        """
        if explicit_path:
            return explicit_path

        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            return os.path.join(xdg, "ratatoskr", "recipes.db")

        home = os.environ.get("HOME")
        if home:
            return os.path.join(home, ".cache", "ratatoskr", "recipes.db")

        # Last resort: temp directory
        logger.warning(
            "recipe_sqlite_path_tempdir_fallback",
            detail="No HOME or XDG_CACHE_HOME set, using temp directory for recipe DB",
        )
        return os.path.join(tempfile.gettempdir(), "ratatoskr", "recipes.db")

    async def load_all(
        self,
    ) -> list[tuple[str, str, str, dict[str, Any], float, float]]:
        """Load all recipes, skipping corrupted JSON, capped at max_recipes."""

        def _load() -> list[tuple[str, str, str, dict[str, Any], float, float]]:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "SELECT api_id, schema_hash, recipe_id, recipe_json, created_at, last_used_at "
                    "FROM recipes ORDER BY last_used_at DESC LIMIT ?",
                    (self._max_recipes,),
                )
                results: list[tuple[str, str, str, dict[str, Any], float, float]] = []
                for row in cursor.fetchall():
                    api_id, schema_hash, recipe_id, recipe_json, created_at, last_used_at = row
                    try:
                        recipe_dict = json.loads(recipe_json)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "recipe_load_skip_corrupted",
                            recipe_id=recipe_id,
                        )
                        continue
                    results.append(
                        (api_id, schema_hash, recipe_id, recipe_dict, created_at, last_used_at)
                    )
                return results
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_load)
        except Exception:
            logger.warning("recipe_load_all_failed", exc_info=True)
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
        """Persist a recipe. Failures logged, never raised."""

        def _save() -> None:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO recipes "
                    "(api_id, schema_hash, recipe_id, recipe_json, created_at, last_used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (api_id, schema_hash, recipe_id, json.dumps(recipe), created_at, last_used_at),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await asyncio.to_thread(_save)
        except Exception:
            logger.warning(
                "recipe_save_failed",
                recipe_id=recipe_id,
                exc_info=True,
            )

    async def delete_by_schema(self, api_id: str, schema_hash: str) -> int:
        """Delete recipes for an API+schema pair. Returns count deleted."""

        def _delete() -> int:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM recipes WHERE api_id = ? AND schema_hash = ?",
                    (api_id, schema_hash),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_delete)
        except Exception:
            logger.warning(
                "recipe_delete_by_schema_failed",
                api_id=api_id,
                schema_hash=schema_hash,
                exc_info=True,
            )
            return 0
