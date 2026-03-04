"""Tests for gRPC client — unary, server-streaming, client-streaming, bidi-streaming."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from api_agent.grpc.client import (
    _error_hint,
    execute_bidi_streaming_rpc,
    execute_client_streaming_rpc,
    execute_server_streaming_rpc,
    execute_unary_rpc,
)


def _make_mock_pool(input_name="test.Req", output_name="test.Resp"):
    """Create a mock DescriptorPool with input/output message types."""
    pool = MagicMock()

    def find_message(name):
        if name not in (input_name, output_name):
            raise KeyError(name)
        desc = MagicMock()
        desc.full_name = name
        return desc

    pool.FindMessageTypeByName = find_message
    return pool


def _make_mock_channel(response=None, error=None):
    """Create a mock gRPC channel.

    unary_unary() is sync (returns a callable stub).
    The stub itself is async (awaited for RPC).
    close() is async.
    """
    channel = MagicMock()
    if error:
        stub = AsyncMock(side_effect=error)
    else:
        stub = AsyncMock(return_value=response or MagicMock())
    channel.unary_unary.return_value = stub
    channel.close = AsyncMock()
    return channel


class TestExecuteUnaryRpc:
    """Test execute_unary_rpc with mocked channels and protobuf."""

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_successful_unary_call(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Successful unary RPC returns success=True with data."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {"greeting": "hello world"}

        mock_channel = _make_mock_channel()
        mock_get_channel.return_value = mock_channel

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={"name": "world"},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["data"] == {"greeting": "hello world"}
        mock_channel.unary_unary.assert_called_once()
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_rpc_error_returns_failure(
        self, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """gRPC error returns success=False with error details."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.NOT_FOUND,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Method not found",
        )

        mock_channel = _make_mock_channel(error=mock_error)
        mock_get_channel.return_value = mock_channel

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Missing",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "NOT_FOUND" in result["error"]
        assert "Method not found" in result["error"]
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    async def test_unknown_input_type_returns_error(self):
        """Unknown input message type returns error without connecting."""
        pool = MagicMock()
        pool.FindMessageTypeByName.side_effect = KeyError("not found")

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="unknown.Type",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "unknown.Type" in result["error"]
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client.GetMessageClass")
    async def test_unknown_output_type_returns_error(self, mock_get_class):
        """Unknown output message type returns error."""
        pool = MagicMock()

        call_count = 0

        def find_message(name):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                desc = MagicMock()
                desc.full_name = name
                return desc
            raise KeyError(name)

        pool.FindMessageTypeByName = find_message
        mock_get_class.return_value = MagicMock()

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="unknown.Resp",
        )

        assert result["success"] is False
        assert "unknown.Resp" in result["error"]

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_method_path_normalized(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Method path without leading / gets normalized."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_get_channel.return_value = mock_channel

        await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        call_args = mock_channel.unary_unary.call_args
        assert call_args[0][0] == "/test.Svc/Hello"

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_tls_channel_used_for_grpcs(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """grpcs:// URL passes tls=True to channel creation."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_get_channel.return_value = mock_channel

        await execute_unary_rpc(
            target_url="grpcs://api.example.com:443",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        mock_get_channel.assert_awaited_once_with("api.example.com:443", True)

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_parse_dict_failure_returns_error(self, mock_parse_dict, mock_get_class):
        """Invalid request JSON (wrong fields) returns build error."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.side_effect = ValueError("Unknown field 'bad_field'")

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={"bad_field": "value"},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "Failed to build request" in result["error"]

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_metadata_passed_to_stub(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Metadata is forwarded to the RPC call."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_get_channel.return_value = mock_channel

        test_metadata = [("authorization", "Bearer tok123")]

        await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            metadata=test_metadata,
        )

        stub = mock_channel.unary_unary.return_value
        call_kwargs = stub.call_args[1]
        assert call_kwargs["metadata"] == test_metadata

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_generic_exception_returns_rpc_failed(
        self, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Non-gRPC exception during RPC returns 'RPC call failed' error."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        # Use ValueError (not grpc.RpcError) to hit the generic except
        mock_channel = _make_mock_channel(error=ValueError("unexpected serialization error"))
        mock_get_channel.return_value = mock_channel

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "RPC call failed" in result["error"]
        assert "unexpected serialization error" in result["error"]
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_rpc_error_empty_details_uses_code(
        self, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """When RPC error details are empty, falls back to str(code)."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.INTERNAL,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="",  # empty string — triggers the `or str(code)` fallback
        )

        mock_channel = _make_mock_channel(error=mock_error)
        mock_get_channel.return_value = mock_channel

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "INTERNAL" in result["error"]

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_rpc_error_includes_hint_text(
        self, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Error message includes the hint text for known error codes."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.NOT_FOUND,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Method not found",
        )

        mock_channel = _make_mock_channel(error=mock_error)
        mock_get_channel.return_value = mock_channel

        result = await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Missing",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        # The hint for NOT_FOUND is "The method or service was not found on the server"
        assert "not found on the server" in result["error"].lower()

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_timeout_passed_to_stub(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Custom timeout is forwarded to the RPC stub call."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_get_channel.return_value = mock_channel

        await execute_unary_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            timeout_s=5.0,
        )

        stub = mock_channel.unary_unary.return_value
        call_kwargs = stub.call_args[1]
        assert call_kwargs["timeout"] == 5.0


class TestErrorHint:
    """Test error hint helper."""

    def test_known_codes_have_hints(self):
        assert "auth" in _error_hint(grpc.StatusCode.UNAUTHENTICATED).lower()
        assert "permission" in _error_hint(grpc.StatusCode.PERMISSION_DENIED).lower()
        assert "unavailable" in _error_hint(grpc.StatusCode.UNAVAILABLE).lower()

    def test_unknown_code_returns_empty(self):
        assert _error_hint(grpc.StatusCode.INTERNAL) == ""

    def test_all_six_codes_have_nonempty_hints(self):
        """Every mapped status code returns a non-empty hint string."""
        codes_with_hints = [
            grpc.StatusCode.UNAUTHENTICATED,
            grpc.StatusCode.PERMISSION_DENIED,
            grpc.StatusCode.NOT_FOUND,
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.UNIMPLEMENTED,
        ]
        for code in codes_with_hints:
            hint = _error_hint(code)
            assert hint, f"Expected non-empty hint for {code.name}"


# ---------------------------------------------------------------------------
# Helpers for server-streaming tests
# ---------------------------------------------------------------------------


class MockAsyncStreamIterator:
    """Simulates a gRPC async stream response (UnaryStreamCall).

    Yields messages from a list, optionally raising an error mid-stream.
    """

    def __init__(
        self,
        messages: list,
        error_after: int | None = None,
        error: Exception | None = None,
    ):
        self._messages = messages
        self._error_after = error_after
        self._error = error
        self._index = 0
        self._cancelled = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._cancelled:
            raise StopAsyncIteration
        if self._error_after is not None and self._index >= self._error_after:
            if self._error:
                raise self._error
            raise StopAsyncIteration
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg

    def cancel(self):
        self._cancelled = True


def _make_streaming_channel(stream_iterator=None, error=None):
    """Create a mock gRPC channel for server-streaming RPCs.

    unary_stream() is sync (returns a callable stub).
    The stub, when called, returns the stream_iterator (async iterable).
    close() is async.
    """
    channel = MagicMock()
    if error:
        # Error on the call itself (before streaming starts)
        stub = MagicMock(side_effect=error)
    else:
        stub = MagicMock(return_value=stream_iterator or MockAsyncStreamIterator([]))
    channel.unary_stream.return_value = stub
    channel.close = AsyncMock()
    return channel


class TestExecuteServerStreamingRpc:
    """Test execute_server_streaming_rpc with mocked channels and protobuf."""

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_happy_path_three_messages(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Stream returning 3 messages collects all into a list."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        # MessageToDict called once per streamed message
        mock_to_dict.side_effect = [
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "beta"},
            {"id": 3, "name": "gamma"},
        ]

        stream = MockAsyncStreamIterator([MagicMock(), MagicMock(), MagicMock()])
        mock_channel = _make_streaming_channel(stream_iterator=stream)
        mock_get_channel.return_value = mock_channel

        result = await execute_server_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/ListItems",
            request_json={"filter": "all"},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["message_count"] == 3
        assert len(result["data"]) == 3
        assert result["data"][0] == {"id": 1, "name": "alpha"}
        assert result["data"][1] == {"id": 2, "name": "beta"}
        assert result["data"][2] == {"id": 3, "name": "gamma"}
        mock_channel.unary_stream.assert_called_once()
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_empty_stream(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Empty stream returns success with empty list."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        stream = MockAsyncStreamIterator([])
        mock_channel = _make_streaming_channel(stream_iterator=stream)
        mock_get_channel.return_value = mock_channel

        result = await execute_server_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/ListItems",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["message_count"] == 0
        assert result["data"] == []
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_max_messages_caps_collection(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Stream with 200 messages but max_messages=5 -- only 5 collected."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        # Generate 200 mock messages
        messages = [MagicMock() for _ in range(200)]
        mock_to_dict.side_effect = [{"index": i} for i in range(200)]

        stream = MockAsyncStreamIterator(messages)
        mock_channel = _make_streaming_channel(stream_iterator=stream)
        mock_get_channel.return_value = mock_channel

        result = await execute_server_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/ListItems",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            max_messages=5,
        )

        assert result["success"] is True
        assert result["message_count"] == 5
        assert len(result["data"]) == 5
        # First 5 messages should be collected
        for i in range(5):
            assert result["data"][i] == {"index": i}
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_rpc_error_during_stream_returns_partial(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """RPC error mid-stream returns partial results + error info."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_to_dict.side_effect = [
            {"id": 1, "val": "first"},
            {"id": 2, "val": "second"},
        ]

        rpc_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.INTERNAL,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="stream broken",
        )

        # Stream yields 2 messages then raises error
        stream = MockAsyncStreamIterator(
            [MagicMock(), MagicMock()],
            error_after=2,
            error=rpc_error,
        )
        mock_channel = _make_streaming_channel(stream_iterator=stream)
        mock_get_channel.return_value = mock_channel

        result = await execute_server_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/ListItems",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "INTERNAL" in result["error"]
        assert "stream broken" in result["error"]
        assert len(result["partial_data"]) == 2
        assert result["partial_data"][0] == {"id": 1, "val": "first"}
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_timeout_handled_gracefully(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Timeout (asyncio.TimeoutError) during streaming returns partial data."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_to_dict.side_effect = [{"id": 1}]

        # Custom stream that raises TimeoutError after first message
        class TimeoutStream:
            def __init__(self):
                self._yielded = False
                self._cancelled = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._cancelled:
                    raise StopAsyncIteration
                if not self._yielded:
                    self._yielded = True
                    return MagicMock()
                raise asyncio.TimeoutError()

            def cancel(self):
                self._cancelled = True

        stream = TimeoutStream()
        mock_channel = _make_streaming_channel(stream_iterator=stream)
        mock_get_channel.return_value = mock_channel

        result = await execute_server_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/ListItems",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            timeout_s=0.1,
        )

        assert result["success"] is False
        assert "timeout" in result["error"].lower() or "timed out" in result["error"].lower()
        assert len(result["partial_data"]) == 1
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_channel_closed_after_error(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Channel is always closed, even when the stream raises a generic exception."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        # Stream that raises a non-gRPC exception immediately
        class ErrorStream:
            def __init__(self):
                self._cancelled = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("unexpected stream error")

            def cancel(self):
                self._cancelled = True

        stream = ErrorStream()
        mock_channel = _make_streaming_channel(stream_iterator=stream)
        mock_get_channel.return_value = mock_channel

        result = await execute_server_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/ListItems",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "unexpected stream error" in result["error"]
        # CRITICAL: channel must be closed even after errors
        # Channel lifecycle managed by pool — no close assertion


# ---------------------------------------------------------------------------
# Helpers for client-streaming tests
# ---------------------------------------------------------------------------


class MockClientStreamCall:
    """Simulates a gRPC client-streaming call (StreamUnaryCall).

    Tracks written messages and returns a single response when awaited.
    """

    def __init__(self, response=None, write_error: Exception | None = None):
        self._response = response or MagicMock()
        self._write_error = write_error
        self._written: list = []
        self._done_writing_called = False
        self._cancelled = False

    async def write(self, msg):
        if self._write_error:
            raise self._write_error
        self._written.append(msg)

    async def done_writing(self):
        self._done_writing_called = True

    def cancel(self):
        self._cancelled = True

    def __await__(self):
        async def _get_response():
            return self._response

        return _get_response().__await__()


def _make_client_streaming_channel(call=None, error=None):
    """Create a mock gRPC channel for client-streaming RPCs.

    stream_unary() is sync (returns a callable stub).
    The stub, when called, returns the call object.
    """
    channel = MagicMock()
    if error:
        stub = MagicMock(side_effect=error)
    else:
        stub = MagicMock(return_value=call or MockClientStreamCall())
    channel.stream_unary.return_value = stub
    channel.close = AsyncMock()
    return channel


class TestExecuteClientStreamingRpc:
    """Test execute_client_streaming_rpc with mocked channels and protobuf."""

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_client_stream_happy_path(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Send 3 messages via client stream, receive single response."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {"total": 3, "status": "ok"}

        response_msg = MagicMock()
        call = MockClientStreamCall(response=response_msg)
        mock_channel = _make_client_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[{"item": "a"}, {"item": "b"}, {"item": "c"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["data"] == {"total": 3, "status": "ok"}
        assert result["messages_sent"] == 3
        assert len(call._written) == 3
        assert call._done_writing_called
        mock_channel.stream_unary.assert_called_once()
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_client_stream_empty_request_list(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Empty request array sends nothing but still gets a response."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {"total": 0}

        call = MockClientStreamCall(response=MagicMock())
        mock_channel = _make_client_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["messages_sent"] == 0
        assert len(call._written) == 0
        assert call._done_writing_called

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_client_stream_rpc_error(self, mock_parse_dict, mock_get_class, mock_get_channel):
        """RPC error after sending messages returns failure."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        rpc_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.RESOURCE_EXHAUSTED,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Too many items",
        )

        mock_channel = _make_client_streaming_channel(error=rpc_error)
        mock_get_channel.return_value = mock_channel

        result = await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[{"item": "a"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "RESOURCE_EXHAUSTED" in result["error"]
        assert "Too many items" in result["error"]
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    async def test_client_stream_unknown_input_type(self):
        """Unknown input type returns error without connecting."""
        pool = MagicMock()
        pool.FindMessageTypeByName.side_effect = KeyError("not found")

        result = await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[{"item": "a"}],
            pool=pool,
            input_type_name="unknown.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "unknown.Req" in result["error"]

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_client_stream_unknown_output_type(self, mock_parse_dict, mock_get_class):
        """Unknown output type returns error."""
        pool = MagicMock()

        def find_message(name):
            if name == "test.Req":
                desc = MagicMock()
                desc.full_name = name
                return desc
            raise KeyError(name)

        pool.FindMessageTypeByName = find_message
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        result = await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[{"item": "a"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="unknown.Resp",
        )

        assert result["success"] is False
        assert "unknown.Resp" in result["error"]

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_client_stream_metadata_forwarded(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Metadata is forwarded to the RPC call."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        call = MockClientStreamCall(response=MagicMock())
        mock_channel = _make_client_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        test_metadata = [("authorization", "Bearer tok456")]

        await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[{"item": "a"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            metadata=test_metadata,
        )

        stub = mock_channel.stream_unary.return_value
        call_kwargs = stub.call_args[1]
        assert call_kwargs["metadata"] == test_metadata

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_client_stream_method_path_normalized(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Method path without leading / gets normalized."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        call = MockClientStreamCall(response=MagicMock())
        mock_channel = _make_client_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="test.Svc/Upload",
            requests_json=[],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        call_args = mock_channel.stream_unary.call_args
        assert call_args[0][0] == "/test.Svc/Upload"

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_client_stream_generic_exception(
        self, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Non-gRPC exception returns generic error and closes channel."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_channel = _make_client_streaming_channel(
            error=RuntimeError("unexpected serialization error")
        )
        mock_get_channel.return_value = mock_channel

        result = await execute_client_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Upload",
            requests_json=[{"item": "a"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "Client-streaming RPC failed" in result["error"]
        # Channel lifecycle managed by pool — no close assertion


# ---------------------------------------------------------------------------
# Helpers for bidi-streaming tests
# ---------------------------------------------------------------------------


class MockBidiStreamCall:
    """Simulates a gRPC bidi-streaming call (StreamStreamCall).

    Accepts a request_iterator (consumed to track sent messages),
    yields responses, optionally raising errors mid-stream.
    """

    def __init__(
        self,
        responses: list | None = None,
        error_after: int | None = None,
        error: Exception | None = None,
    ):
        self._responses = responses or []
        self._error_after = error_after
        self._error = error
        self._index = 0
        self._cancelled = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._cancelled:
            raise StopAsyncIteration
        if self._error_after is not None and self._index >= self._error_after:
            if self._error:
                raise self._error
            raise StopAsyncIteration
        if self._index >= len(self._responses):
            raise StopAsyncIteration
        msg = self._responses[self._index]
        self._index += 1
        return msg

    def cancel(self):
        self._cancelled = True


def _make_bidi_streaming_channel(call=None, error=None):
    """Create a mock gRPC channel for bidi-streaming RPCs.

    stream_stream() is sync (returns a callable stub).
    The stub, when called with request_iterator, returns the call object.
    """
    channel = MagicMock()
    if error:
        stub = MagicMock(side_effect=error)
    else:
        stub = MagicMock(return_value=call or MockBidiStreamCall())
    channel.stream_stream.return_value = stub
    channel.close = AsyncMock()
    return channel


class TestExecuteBidiStreamingRpc:
    """Test execute_bidi_streaming_rpc with mocked channels and protobuf."""

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_bidi_stream_happy_path(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Send 2 messages, receive 3 responses."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.side_effect = [
            {"id": 1, "echo": "a"},
            {"id": 2, "echo": "b"},
            {"id": 3, "echo": "done"},
        ]

        call = MockBidiStreamCall(responses=[MagicMock(), MagicMock(), MagicMock()])
        mock_channel = _make_bidi_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[{"msg": "hello"}, {"msg": "world"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["messages_sent"] == 2
        assert result["message_count"] == 3
        assert len(result["data"]) == 3
        assert result["data"][0] == {"id": 1, "echo": "a"}
        mock_channel.stream_stream.assert_called_once()
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_bidi_stream_empty_requests(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Empty request array — server may still send responses."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.side_effect = [{"status": "idle"}]

        call = MockBidiStreamCall(responses=[MagicMock()])
        mock_channel = _make_bidi_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is True
        assert result["messages_sent"] == 0
        assert result["message_count"] == 1

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_bidi_stream_max_messages_caps(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Response capped at max_messages."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        responses = [MagicMock() for _ in range(50)]
        mock_to_dict.side_effect = [{"i": i} for i in range(50)]

        call = MockBidiStreamCall(responses=responses)
        mock_channel = _make_bidi_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[{"msg": "go"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            max_messages=5,
        )

        assert result["success"] is True
        assert result["message_count"] == 5
        assert len(result["data"]) == 5

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_bidi_stream_rpc_error_returns_partial(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """RPC error mid-stream returns partial data."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.side_effect = [{"id": 1}, {"id": 2}]

        rpc_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.INTERNAL,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="stream broken",
        )

        call = MockBidiStreamCall(
            responses=[MagicMock(), MagicMock()],
            error_after=2,
            error=rpc_error,
        )
        mock_channel = _make_bidi_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[{"msg": "hi"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "INTERNAL" in result["error"]
        assert len(result["partial_data"]) == 2
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_bidi_stream_timeout_returns_partial(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Timeout during bidi collection returns partial data."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.side_effect = [{"id": 1}]

        class TimeoutBidiStream:
            def __init__(self):
                self._yielded = False
                self._cancelled = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._cancelled:
                    raise StopAsyncIteration
                if not self._yielded:
                    self._yielded = True
                    return MagicMock()
                raise asyncio.TimeoutError()

            def cancel(self):
                self._cancelled = True

        call = TimeoutBidiStream()
        mock_channel = _make_bidi_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[{"msg": "hi"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "timed out" in result["error"].lower() or "timeout" in result["error"].lower()
        assert len(result["partial_data"]) == 1
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_bidi_stream_metadata_forwarded(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Metadata is forwarded to the bidi RPC call."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        call = MockBidiStreamCall(responses=[])
        mock_channel = _make_bidi_streaming_channel(call=call)
        mock_get_channel.return_value = mock_channel

        test_metadata = [("x-api-key", "secret")]

        await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
            metadata=test_metadata,
        )

        stub = mock_channel.stream_stream.return_value
        call_kwargs = stub.call_args[1]
        assert call_kwargs["metadata"] == test_metadata

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._get_channel", new_callable=AsyncMock)
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_bidi_stream_generic_exception(
        self, mock_parse_dict, mock_get_class, mock_get_channel
    ):
        """Non-gRPC exception returns generic error and closes channel."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()

        mock_channel = _make_bidi_streaming_channel(error=RuntimeError("unexpected bidi error"))
        mock_get_channel.return_value = mock_channel

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[{"msg": "hi"}],
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "Bidi-streaming RPC failed" in result["error"]
        # Channel lifecycle managed by pool — no close assertion

    @pytest.mark.asyncio
    async def test_bidi_stream_unknown_input_type(self):
        """Unknown input type returns error without connecting."""
        pool = MagicMock()
        pool.FindMessageTypeByName.side_effect = KeyError("not found")

        result = await execute_bidi_streaming_rpc(
            target_url="grpc://localhost:50051",
            method_path="/test.Svc/Chat",
            requests_json=[{"msg": "hi"}],
            pool=pool,
            input_type_name="unknown.Req",
            output_type_name="test.Resp",
        )

        assert result["success"] is False
        assert "unknown.Req" in result["error"]
