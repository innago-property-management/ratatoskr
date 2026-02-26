"""Tests for gRPC client — unary RPC execution, serialization, error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from api_agent.grpc.client import _error_hint, execute_unary_rpc


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
    @patch("api_agent.grpc.client._create_channel")
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_successful_unary_call(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_create_channel
    ):
        """Successful unary RPC returns success=True with data."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {"greeting": "hello world"}

        mock_channel = _make_mock_channel()
        mock_create_channel.return_value = mock_channel

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
        mock_channel.close.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("api_agent.grpc.client._create_channel")
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    async def test_rpc_error_returns_failure(
        self, mock_parse_dict, mock_get_class, mock_create_channel
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
        mock_create_channel.return_value = mock_channel

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
        mock_channel.close.assert_awaited_once()

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
    @patch("api_agent.grpc.client._create_channel")
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_method_path_normalized(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_create_channel
    ):
        """Method path without leading / gets normalized."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_create_channel.return_value = mock_channel

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
    @patch("api_agent.grpc.client._create_channel")
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_tls_channel_used_for_grpcs(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_create_channel
    ):
        """grpcs:// URL passes tls=True to channel creation."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_create_channel.return_value = mock_channel

        await execute_unary_rpc(
            target_url="grpcs://api.example.com:443",
            method_path="/test.Svc/Hello",
            request_json={},
            pool=pool,
            input_type_name="test.Req",
            output_type_name="test.Resp",
        )

        mock_create_channel.assert_called_once_with("api.example.com:443", True, False)

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
    @patch("api_agent.grpc.client._create_channel")
    @patch("api_agent.grpc.client.GetMessageClass")
    @patch("api_agent.grpc.client.ParseDict")
    @patch("api_agent.grpc.client.MessageToDict")
    async def test_metadata_passed_to_stub(
        self, mock_to_dict, mock_parse_dict, mock_get_class, mock_create_channel
    ):
        """Metadata is forwarded to the RPC call."""
        pool = _make_mock_pool()
        mock_get_class.return_value = MagicMock()
        mock_parse_dict.return_value = MagicMock()
        mock_to_dict.return_value = {}

        mock_channel = _make_mock_channel()
        mock_create_channel.return_value = mock_channel

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


class TestErrorHint:
    """Test error hint helper."""

    def test_known_codes_have_hints(self):
        assert "auth" in _error_hint(grpc.StatusCode.UNAUTHENTICATED).lower()
        assert "permission" in _error_hint(grpc.StatusCode.PERMISSION_DENIED).lower()
        assert "unavailable" in _error_hint(grpc.StatusCode.UNAVAILABLE).lower()

    def test_unknown_code_returns_empty(self):
        assert _error_hint(grpc.StatusCode.INTERNAL) == ""
