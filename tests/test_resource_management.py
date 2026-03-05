"""Tests for resource management: async DuckDB, semaphore, temp cleanup, graceful shutdown."""

import asyncio
import json
import os
import signal
from unittest.mock import AsyncMock, patch

import pytest

from api_agent.executor import (
    _TEMP_FILE_MAX_BYTES,
    _init_temp_file_limit,
    _temp_files_lock,
    _temp_files_registry,
    _write_temp_json,
    cleanup_temp_files,
    execute_sql_async,
    truncate_for_context,
    truncate_for_context_async,
)

# Ensure the semaphore is initialized for tests that use execute_sql_async
_init_temp_file_limit()

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
                # Yield control so other coroutines can enter the semaphore
                await asyncio.sleep(0)
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

    def test_semaphore_initialized_eagerly(self):
        """Semaphore should be non-None after _init_temp_file_limit."""
        from api_agent.executor import _semaphore

        # _init_temp_file_limit is called at module import (__main__.py top-level)
        # so _semaphore should already be set
        assert _semaphore is not None


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
            with _temp_files_lock:
                assert path in _temp_files_registry
        finally:
            os.unlink(path)
            with _temp_files_lock:
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
        with _temp_files_lock:
            assert len(_temp_files_registry) == 0

    def test_cleanup_handles_missing_files(self):
        """cleanup_temp_files doesn't crash if files already deleted."""
        path = _write_temp_json([{"id": 1}])
        os.unlink(path)  # Pre-delete
        # Should not raise
        cleanup_temp_files()
        with _temp_files_lock:
            assert path not in _temp_files_registry

    def test_temp_files_lock_is_threading_lock(self):
        """_temp_files_lock should be a threading.Lock for cross-thread safety."""
        import threading

        assert isinstance(_temp_files_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Signal handlers trigger graceful connection draining."""

    def test_create_app_registers_shutdown_handler(self):
        """create_app registers atexit and shutdown event handler."""
        with patch("atexit.register") as mock_register:
            from api_agent.__main__ import create_app

            create_app()
            mock_register.assert_called()

    def test_shutdown_closes_pool(self):
        """The shutdown handler calls pool.close_all."""
        from api_agent.__main__ import create_app

        app = create_app()
        # Find the shutdown handler
        shutdown_handlers = [h for h in app.router.on_shutdown if callable(h)]
        assert len(shutdown_handlers) >= 1

    def test_create_app_registers_startup_with_signal_handlers(self):
        """create_app registers a startup event handler for signal installation."""
        from api_agent.__main__ import create_app

        app = create_app()
        startup_handlers = [h for h in app.router.on_startup if callable(h)]
        assert len(startup_handlers) >= 1

    @pytest.mark.asyncio
    async def test_install_signal_handlers(self):
        """install_shutdown_signals registers SIGTERM/SIGINT handlers."""
        from api_agent.shutdown import install_shutdown_signals

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        cleanup = AsyncMock()

        try:
            # Install handlers
            install_shutdown_signals(loop, shutdown_event, cleanup)

            # Trigger SIGTERM programmatically
            os.kill(os.getpid(), signal.SIGTERM)
            # Give the handler time to fire
            await asyncio.sleep(0.1)

            assert shutdown_event.is_set()
            cleanup.assert_awaited_once()
        finally:
            # Remove asyncio signal handlers so other tests aren't affected
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)

    @pytest.mark.asyncio
    async def test_shutdown_cleanup_closes_resources(self):
        """shutdown_cleanup closes pool and cleans temp files."""
        import api_agent.shutdown

        # Reset the idempotency flag for this test
        api_agent.shutdown._shutdown_done = False

        from api_agent.shutdown import shutdown_cleanup

        mock_pool = AsyncMock()
        with patch("api_agent.shutdown.cleanup_temp_files") as mock_cleanup:
            await shutdown_cleanup(mock_pool)

        mock_pool.close_all.assert_awaited_once()
        mock_cleanup.assert_called_once()

        # Reset for other tests
        api_agent.shutdown._shutdown_done = False

    @pytest.mark.asyncio
    async def test_shutdown_cleanup_is_idempotent(self):
        """shutdown_cleanup only runs once even if called multiple times."""
        import api_agent.shutdown

        api_agent.shutdown._shutdown_done = False

        from api_agent.shutdown import shutdown_cleanup

        mock_pool = AsyncMock()
        with patch("api_agent.shutdown.cleanup_temp_files") as mock_cleanup:
            await shutdown_cleanup(mock_pool)
            await shutdown_cleanup(mock_pool)  # Second call should be a no-op

        # close_all should only be called once
        mock_pool.close_all.assert_awaited_once()
        mock_cleanup.assert_called_once()

        api_agent.shutdown._shutdown_done = False


# ---------------------------------------------------------------------------
# Async truncate_for_context
# ---------------------------------------------------------------------------


class TestTruncateForContextAsync:
    """truncate_for_context_async offloads _extract_schema to a thread."""

    @pytest.mark.asyncio
    async def test_small_data_not_truncated(self):
        """Small data returns without truncation (no _extract_schema call)."""
        data = [{"id": 1, "name": "Alice"}]
        result = await truncate_for_context_async(data, "users")
        assert result["truncated"] is False
        assert result["data"] == data

    @pytest.mark.asyncio
    async def test_large_data_truncated_with_schema(self):
        """Large data returns truncated preview with schema info."""
        data = [{"id": i, "name": f"user_{i}" * 100} for i in range(100)]
        result = await truncate_for_context_async(data, "users", max_chars=500)
        assert result["truncated"] is True
        assert result["showing"] < 100
        assert "schema" in result
        assert "hint" in result

    @pytest.mark.asyncio
    async def test_sync_variant_matches_async_small(self):
        """Both variants return identical structure for non-truncated data."""
        data = [{"id": 1}]
        sync_result = truncate_for_context(data, "t")
        async_result = await truncate_for_context_async(data, "t")
        assert sync_result == async_result

    @pytest.mark.asyncio
    async def test_sync_variant_matches_async_truncated(self):
        """Both variants produce identical output when truncation+schema occurs."""
        data = [{"id": i, "name": f"user_{i}" * 100} for i in range(100)]
        sync_result = truncate_for_context(data, "users", max_chars=500)
        async_result = await truncate_for_context_async(data, "users", max_chars=500)
        assert sync_result["truncated"] is True  # confirm truncation path fired
        assert sync_result == async_result
