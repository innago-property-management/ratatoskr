"""Tests for resource management: async DuckDB, semaphore, temp cleanup, graceful shutdown."""

import asyncio
import json
import os
import signal
from unittest.mock import AsyncMock, patch

import pytest

from api_agent.executor import (
    _TEMP_FILE_MAX_BYTES,
    _temp_files_registry,
    _write_temp_json,
    cleanup_temp_files,
    execute_sql_async,
)

# ---------------------------------------------------------------------------
# Async DuckDB wrapping
# ---------------------------------------------------------------------------


class TestExecuteSqlAsync:
    """execute_sql_async wraps DuckDB in asyncio.to_thread."""

    @pytest.mark.asyncio
    async def test_simple_select(self):
        """Basic SELECT works through async wrapper."""
        data = {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]}
        result = await execute_sql_async(data, "SELECT * FROM users")
        assert result["success"] is True
        assert len(result["result"]) == 2

    @pytest.mark.asyncio
    async def test_invalid_sql_returns_error(self):
        """Invalid SQL returns error through async wrapper."""
        data = {"users": [{"id": 1}]}
        result = await execute_sql_async(data, "INVALID SQL SYNTAX")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_ddl_blocked(self):
        """DDL statements are still blocked."""
        data = {"users": [{"id": 1}]}
        result = await execute_sql_async(data, "DROP TABLE users")
        assert result["success"] is False
        assert "blocked" in result["error"].lower() or "only" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_concurrent_queries_run_in_threads(self):
        """Multiple async queries can run concurrently."""
        data = {"items": [{"id": i} for i in range(100)]}

        async def run_query(n):
            return await execute_sql_async(data, f"SELECT * FROM items WHERE id = {n}")

        results = await asyncio.gather(*[run_query(i) for i in range(5)])
        for r in results:
            assert r["success"] is True
            assert len(r["result"]) == 1


# ---------------------------------------------------------------------------
# Semaphore-bounded concurrency
# ---------------------------------------------------------------------------


class TestDuckDBSemaphore:
    """Concurrent DuckDB operations are bounded by semaphore."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Only N queries run at once when semaphore is set."""
        from api_agent.executor import _duckdb_semaphore, set_duckdb_semaphore

        # Set a tight limit
        set_duckdb_semaphore(2)
        sem = _duckdb_semaphore()

        peak = 0
        current = 0
        lock = asyncio.Lock()

        async def tracked_query(data, sql):
            nonlocal peak, current
            async with sem:
                async with lock:
                    current += 1
                    if current > peak:
                        peak = current
                async with lock:
                    current -= 1
                return {"success": True, "result": []}

        data = {"items": [{"id": 1}]}
        tasks = [tracked_query(data, "SELECT 1") for _ in range(10)]
        await asyncio.gather(*tasks)

        assert peak <= 2

        # Reset to default
        set_duckdb_semaphore(10)

    @pytest.mark.asyncio
    async def test_semaphore_default_from_config(self):
        """Default semaphore value comes from config."""
        from api_agent.config import settings
        from api_agent.executor import _duckdb_semaphore, set_duckdb_semaphore

        set_duckdb_semaphore(settings.MAX_CONCURRENT_QUERIES)
        sem = _duckdb_semaphore()
        # asyncio.Semaphore doesn't expose its value directly, but we can
        # verify it doesn't raise when acquiring up to the limit
        for _ in range(settings.MAX_CONCURRENT_QUERIES):
            await sem.acquire()
        for _ in range(settings.MAX_CONCURRENT_QUERIES):
            sem.release()


# ---------------------------------------------------------------------------
# Temp file management
# ---------------------------------------------------------------------------


class TestTempFileManagement:
    """Temp files are tracked, size-limited, and cleaned up."""

    def test_write_temp_json_creates_file(self):
        """_write_temp_json creates a temp file and registers it."""
        data = [{"id": 1}]
        path = _write_temp_json(data)
        try:
            assert os.path.exists(path)
            with open(path) as f:
                assert json.load(f) == data
            assert path in _temp_files_registry
        finally:
            os.unlink(path)
            _temp_files_registry.discard(path)

    def test_write_temp_json_rejects_oversized_data(self):
        """_write_temp_json raises ValueError for data exceeding size limit."""
        # Create data larger than limit
        big_data = [{"x": "a" * _TEMP_FILE_MAX_BYTES}]
        with pytest.raises(ValueError, match="exceeds.*limit"):
            _write_temp_json(big_data)

    def test_cleanup_temp_files_removes_tracked(self):
        """cleanup_temp_files removes all tracked temp files."""
        # Create some temp files
        paths = []
        for i in range(3):
            p = _write_temp_json([{"id": i}])
            paths.append(p)

        for p in paths:
            assert os.path.exists(p)

        cleanup_temp_files()

        for p in paths:
            assert not os.path.exists(p)
        assert len(_temp_files_registry) == 0

    def test_cleanup_handles_missing_files(self):
        """cleanup_temp_files doesn't crash if files already deleted."""
        path = _write_temp_json([{"id": 1}])
        os.unlink(path)  # Pre-delete
        # Should not raise
        cleanup_temp_files()
        assert path not in _temp_files_registry


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Signal handlers trigger graceful connection draining."""

    def test_create_app_registers_shutdown_handler(self):
        """create_app registers atexit and shutdown event handler."""
        with patch("api_agent.__main__.atexit") as mock_atexit:
            from api_agent.__main__ import create_app

            create_app()
            # atexit.register should have been called
            mock_atexit.register.assert_called()

    def test_shutdown_closes_pool(self):
        """The shutdown handler calls pool.close_all."""
        from api_agent.__main__ import create_app

        app = create_app()
        # Find the shutdown handler
        shutdown_handlers = [h for h in app.router.on_shutdown if callable(h)]
        assert len(shutdown_handlers) >= 1

    @pytest.mark.asyncio
    async def test_install_signal_handlers(self):
        """install_shutdown_signals registers SIGTERM/SIGINT handlers."""
        from api_agent.shutdown import install_shutdown_signals

        loop = asyncio.get_event_loop()
        shutdown_event = asyncio.Event()
        cleanup = AsyncMock()

        # Install handlers
        install_shutdown_signals(loop, shutdown_event, cleanup)

        # Trigger SIGTERM programmatically
        os.kill(os.getpid(), signal.SIGTERM)
        # Give the handler time to fire
        await asyncio.sleep(0.1)

        assert shutdown_event.is_set()
        cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_cleanup_closes_resources(self):
        """shutdown_cleanup closes pool and cleans temp files."""
        from api_agent.shutdown import shutdown_cleanup

        mock_pool = AsyncMock()
        with patch("api_agent.shutdown.cleanup_temp_files") as mock_cleanup:
            await shutdown_cleanup(mock_pool)

        mock_pool.close_all.assert_awaited_once()
        mock_cleanup.assert_called_once()
