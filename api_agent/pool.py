"""Connection pool for httpx clients and gRPC channels."""

import asyncio
import logging
from urllib.parse import urlparse

import grpc
import grpc.aio
import httpx

from .config import settings

logger = logging.getLogger(__name__)


class ConnectionPool:
    """Manages reusable httpx.AsyncClient instances (keyed by base URL)
    and gRPC channels (keyed by target address)."""

    def __init__(self) -> None:
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._grpc_channels: dict[tuple[str, bool], grpc.aio.Channel] = {}
        self._lock = asyncio.Lock()

    def _base_url_key(self, url: str) -> str:
        """Extract scheme+host+port as the pool key."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    async def get_http_client(self, url: str) -> httpx.AsyncClient:
        """Return a cached httpx.AsyncClient for the given URL's origin.

        Clients are keyed by scheme://host:port. A new client is created
        on first access with configured connection limits.
        """
        key = self._base_url_key(url)
        if key in self._http_clients:
            client = self._http_clients[key]
            if not client.is_closed:
                return client

        async with self._lock:
            # Double-check after acquiring lock
            if key in self._http_clients:
                client = self._http_clients[key]
                if not client.is_closed:
                    return client

            client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(
                    max_connections=settings.HTTP_POOL_MAX_CONNECTIONS,
                    max_keepalive_connections=settings.HTTP_POOL_MAX_KEEPALIVE,
                ),
            )
            self._http_clients[key] = client
            logger.debug("Created pooled httpx client for %s", key)
            return client

    async def get_grpc_channel(self, target: str, tls: bool) -> grpc.aio.Channel:
        """Return a cached gRPC channel for the given target+tls combo.

        Channels are reused across calls to the same target.
        """
        key = (target, tls)
        if key in self._grpc_channels:
            channel = self._grpc_channels[key]
            state = channel.get_state()  # type: ignore[union-attr]
            if state != grpc.ChannelConnectivity.SHUTDOWN:
                return channel

        async with self._lock:
            # Double-check after acquiring lock
            if key in self._grpc_channels:
                channel = self._grpc_channels[key]
                state = channel.get_state()  # type: ignore[union-attr]
                if state != grpc.ChannelConnectivity.SHUTDOWN:
                    return channel

            if tls:
                creds = grpc.ssl_channel_credentials()
                channel = grpc.aio.secure_channel(target, creds)
            else:
                channel = grpc.aio.insecure_channel(target)
            self._grpc_channels[key] = channel
            logger.debug("Created pooled gRPC channel for %s (tls=%s)", target, tls)
            return channel

    async def close_all(self) -> None:
        """Close all pooled clients and channels. Safe to call multiple times."""
        async with self._lock:
            for key, client in self._http_clients.items():
                if not client.is_closed:
                    await client.aclose()
                    logger.debug("Closed pooled httpx client for %s", key)
            self._http_clients.clear()

            for (target, tls), channel in self._grpc_channels.items():
                try:
                    await channel.close()
                    logger.debug("Closed pooled gRPC channel for %s", target)
                except Exception:
                    logger.debug("gRPC channel for %s already closed", target)
            self._grpc_channels.clear()


# Module-level singleton
pool = ConnectionPool()
