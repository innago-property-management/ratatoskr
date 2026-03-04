"""Tests for connection pool (httpx clients and gRPC channels)."""

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from api_agent.pool import ConnectionPool


class TestHttpClientPooling:
    """Test httpx.AsyncClient reuse by base URL."""

    @pytest.mark.asyncio
    async def test_same_url_returns_same_client(self):
        pool = ConnectionPool()
        try:
            c1 = await pool.get_http_client("https://api.example.com/graphql")
            c2 = await pool.get_http_client("https://api.example.com/other-path")
            assert c1 is c2
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_different_hosts_return_different_clients(self):
        pool = ConnectionPool()
        try:
            c1 = await pool.get_http_client("https://api.example.com/foo")
            c2 = await pool.get_http_client("https://other.example.com/foo")
            assert c1 is not c2
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_different_schemes_return_different_clients(self):
        pool = ConnectionPool()
        try:
            c1 = await pool.get_http_client("http://api.example.com/foo")
            c2 = await pool.get_http_client("https://api.example.com/foo")
            assert c1 is not c2
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_different_ports_return_different_clients(self):
        pool = ConnectionPool()
        try:
            c1 = await pool.get_http_client("https://api.example.com:8080/foo")
            c2 = await pool.get_http_client("https://api.example.com:9090/foo")
            assert c1 is not c2
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_close_all_closes_http_clients(self):
        pool = ConnectionPool()
        c1 = await pool.get_http_client("https://api.example.com/foo")
        assert not c1.is_closed
        await pool.close_all()
        assert c1.is_closed

    @pytest.mark.asyncio
    async def test_closed_client_is_replaced(self):
        pool = ConnectionPool()
        try:
            c1 = await pool.get_http_client("https://api.example.com/foo")
            await c1.aclose()
            c2 = await pool.get_http_client("https://api.example.com/foo")
            assert c1 is not c2
            assert not c2.is_closed
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_close_all_clears_pool(self):
        pool = ConnectionPool()
        await pool.get_http_client("https://api.example.com/foo")
        assert len(pool._http_clients) == 1
        await pool.close_all()
        assert len(pool._http_clients) == 0

    @pytest.mark.asyncio
    async def test_close_all_idempotent(self):
        pool = ConnectionPool()
        await pool.get_http_client("https://api.example.com/foo")
        await pool.close_all()
        await pool.close_all()  # Should not raise


class TestGrpcChannelPooling:
    """Test gRPC channel reuse by target+tls."""

    @pytest.mark.asyncio
    @patch("api_agent.pool.grpc.aio.insecure_channel")
    async def test_same_target_returns_same_channel(self, mock_insecure):
        mock_channel = MagicMock()
        mock_channel.get_state.return_value = grpc.ChannelConnectivity.IDLE
        mock_channel.close = AsyncMock()
        mock_insecure.return_value = mock_channel

        pool = ConnectionPool()
        try:
            ch1 = await pool.get_grpc_channel("localhost:50051", tls=False)
            ch2 = await pool.get_grpc_channel("localhost:50051", tls=False)
            assert ch1 is ch2
            mock_insecure.assert_called_once()
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    @patch("api_agent.pool.grpc.aio.secure_channel")
    @patch("api_agent.pool.grpc.aio.insecure_channel")
    async def test_different_targets_return_different_channels(self, mock_insecure, mock_secure):
        ch_a = MagicMock()
        ch_a.get_state.return_value = grpc.ChannelConnectivity.IDLE
        ch_a.close = AsyncMock()
        ch_b = MagicMock()
        ch_b.get_state.return_value = grpc.ChannelConnectivity.IDLE
        ch_b.close = AsyncMock()
        mock_insecure.side_effect = [ch_a, ch_b]

        pool = ConnectionPool()
        try:
            r1 = await pool.get_grpc_channel("host-a:50051", tls=False)
            r2 = await pool.get_grpc_channel("host-b:50051", tls=False)
            assert r1 is not r2
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    @patch("api_agent.pool.grpc.aio.secure_channel")
    @patch("api_agent.pool.grpc.aio.insecure_channel")
    async def test_tls_mismatch_returns_different_channels(self, mock_insecure, mock_secure):
        ch_plain = MagicMock()
        ch_plain.get_state.return_value = grpc.ChannelConnectivity.IDLE
        ch_plain.close = AsyncMock()
        ch_tls = MagicMock()
        ch_tls.get_state.return_value = grpc.ChannelConnectivity.IDLE
        ch_tls.close = AsyncMock()
        mock_insecure.return_value = ch_plain
        mock_secure.return_value = ch_tls

        pool = ConnectionPool()
        try:
            r1 = await pool.get_grpc_channel("localhost:50051", tls=False)
            r2 = await pool.get_grpc_channel("localhost:50051", tls=True)
            assert r1 is not r2
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    @patch("api_agent.pool.grpc.aio.insecure_channel")
    async def test_shutdown_channel_is_replaced(self, mock_insecure):
        ch_old = MagicMock()
        ch_old.get_state.return_value = grpc.ChannelConnectivity.SHUTDOWN
        ch_old.close = AsyncMock()
        ch_new = MagicMock()
        ch_new.get_state.return_value = grpc.ChannelConnectivity.IDLE
        ch_new.close = AsyncMock()
        mock_insecure.side_effect = [ch_old, ch_new]

        pool = ConnectionPool()
        try:
            r1 = await pool.get_grpc_channel("localhost:50051", tls=False)
            assert r1 is ch_old  # First call returns ch_old
            # Simulate shutdown state
            ch_old.get_state.return_value = grpc.ChannelConnectivity.SHUTDOWN
            r2 = await pool.get_grpc_channel("localhost:50051", tls=False)
            assert r2 is ch_new
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    @patch("api_agent.pool.grpc.aio.insecure_channel")
    async def test_close_all_closes_grpc_channels(self, mock_insecure):
        mock_channel = MagicMock()
        mock_channel.get_state.return_value = grpc.ChannelConnectivity.IDLE
        mock_channel.close = AsyncMock()
        mock_insecure.return_value = mock_channel

        pool = ConnectionPool()
        await pool.get_grpc_channel("localhost:50051", tls=False)
        await pool.close_all()
        mock_channel.close.assert_awaited_once()
        assert len(pool._grpc_channels) == 0


class TestPoolConfig:
    """Test that pool respects configuration settings."""

    @pytest.mark.asyncio
    @patch("api_agent.pool.settings")
    async def test_http_client_uses_configured_limits(self, mock_settings):
        mock_settings.HTTP_POOL_MAX_CONNECTIONS = 50
        mock_settings.HTTP_POOL_MAX_KEEPALIVE = 25

        pool = ConnectionPool()
        try:
            client = await pool.get_http_client("https://api.example.com/foo")
            # httpx stores pool config on the transport
            transport = client._transport
            pool_impl = transport._pool
            assert pool_impl._max_connections == 50
            assert pool_impl._max_keepalive_connections == 25
        finally:
            await pool.close_all()
