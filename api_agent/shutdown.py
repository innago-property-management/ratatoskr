"""Graceful shutdown — signal handlers, connection draining, temp cleanup."""

import asyncio
import signal
from typing import Any

import structlog

from .executor import cleanup_temp_files

logger = structlog.get_logger(__name__)


async def shutdown_cleanup(pool: Any) -> None:
    """Close pool connections and clean up temp files."""
    logger.info("shutdown_cleanup_start")
    try:
        await pool.close_all()
    except Exception:
        logger.exception("shutdown_pool_close_error")
    cleanup_temp_files()
    logger.info("shutdown_cleanup_complete")


def install_shutdown_signals(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
    cleanup: Any,
) -> None:
    """Install SIGTERM/SIGINT handlers for graceful shutdown.

    Args:
        loop: The running event loop
        shutdown_event: Event to set when shutdown is requested
        cleanup: Async callable to run on shutdown (e.g., shutdown_cleanup)
    """

    def _handler(sig: signal.Signals) -> None:
        logger.info("shutdown_signal_received", signal=sig.name)
        shutdown_event.set()
        loop.create_task(cleanup())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handler, sig)
