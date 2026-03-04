"""gRPC client — execute unary, server-streaming, client-streaming, and bidi-streaming RPC calls."""

import asyncio
import logging
from typing import Any

import grpc
import grpc.aio
from google.protobuf import descriptor_pool as dp_module
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message_factory import GetMessageClass

from ..pool import pool as connection_pool
from .reflection import parse_grpc_target

logger = logging.getLogger(__name__)


async def _get_channel(target: str, tls: bool) -> grpc.aio.Channel:
    """Get a pooled gRPC channel for the given target."""
    return await connection_pool.get_grpc_channel(target, tls)


async def execute_unary_rpc(
    target_url: str,
    method_path: str,
    request_json: dict[str, Any],
    pool: dp_module.DescriptorPool,
    input_type_name: str,
    output_type_name: str,
    metadata: list[tuple[str, str]] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Execute a unary gRPC RPC call.

    Args:
        target_url: gRPC target URL (grpc:// or grpcs://)
        method_path: Full method path (e.g. "/package.Service/Method")
        request_json: Request fields as JSON dict
        pool: DescriptorPool with loaded service descriptors
        input_type_name: Fully qualified input message type name
        output_type_name: Fully qualified output message type name
        metadata: Optional gRPC metadata tuples
        timeout_s: RPC timeout in seconds

    Returns:
        {"success": True, "data": dict} on success
        {"success": False, "error": str} on failure
    """
    target, tls = parse_grpc_target(target_url)

    # Build request message
    try:
        input_desc = pool.FindMessageTypeByName(input_type_name)
        InputClass = GetMessageClass(input_desc)
        request_msg = ParseDict(request_json, InputClass())
    except KeyError:
        return {
            "success": False,
            "error": f"Input message type '{input_type_name}' not found in schema",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to build request: {e}",
        }

    # Build response deserializer
    try:
        output_desc = pool.FindMessageTypeByName(output_type_name)
        OutputClass = GetMessageClass(output_desc)
    except KeyError:
        return {
            "success": False,
            "error": f"Output message type '{output_type_name}' not found in schema",
        }

    # Normalize method path
    if not method_path.startswith("/"):
        method_path = f"/{method_path}"

    channel = await _get_channel(target, tls)

    try:
        stub = channel.unary_unary(
            method_path,
            request_serializer=InputClass.SerializeToString,
            response_deserializer=OutputClass.FromString,
        )

        response_msg = await stub(
            request_msg,
            metadata=metadata,
            timeout=timeout_s,
        )

        response_dict = MessageToDict(response_msg, preserving_proto_field_name=True)

        return {"success": True, "data": response_dict}

    except grpc.RpcError as e:
        code = e.code()  # type: ignore[unresolved-attribute]  # grpc stubs incomplete
        details = e.details() or str(code)  # type: ignore[unresolved-attribute]
        hint = _error_hint(code)
        error_msg = f"gRPC error [{code.name}]: {details}"
        if hint:
            error_msg += f". {hint}"
        return {"success": False, "error": error_msg}
    except Exception as e:
        return {"success": False, "error": f"RPC call failed: {e}"}


async def execute_server_streaming_rpc(
    target_url: str,
    method_path: str,
    request_json: dict[str, Any],
    pool: dp_module.DescriptorPool,
    input_type_name: str,
    output_type_name: str,
    metadata: list[tuple[str, str]] | None = None,
    timeout_s: float = 30.0,
    max_messages: int = 100,
) -> dict[str, Any]:
    """Execute a server-streaming gRPC RPC call.

    The client sends a single request; the server streams back multiple responses.
    Responses are collected into a list, capped at ``max_messages``.

    Args:
        target_url: gRPC target URL (grpc:// or grpcs://)
        method_path: Full method path (e.g. "/package.Service/ListItems")
        request_json: Request fields as JSON dict
        pool: DescriptorPool with loaded service descriptors
        input_type_name: Fully qualified input message type name
        output_type_name: Fully qualified output message type name
        metadata: Optional gRPC metadata tuples
        timeout_s: RPC timeout in seconds
        max_messages: Maximum number of streamed messages to collect

    Returns:
        {"success": True, "data": list[dict], "message_count": int} on success
        {"success": False, "error": str, "partial_data": list[dict]} on failure
    """
    target, tls = parse_grpc_target(target_url)

    # Build request message
    try:
        input_desc = pool.FindMessageTypeByName(input_type_name)
        InputClass = GetMessageClass(input_desc)
        request_msg = ParseDict(request_json, InputClass())
    except KeyError:
        return {
            "success": False,
            "error": f"Input message type '{input_type_name}' not found in schema",
            "partial_data": [],
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to build request: {e}",
            "partial_data": [],
        }

    # Build response deserializer
    try:
        output_desc = pool.FindMessageTypeByName(output_type_name)
        OutputClass = GetMessageClass(output_desc)
    except KeyError:
        return {
            "success": False,
            "error": f"Output message type '{output_type_name}' not found in schema",
            "partial_data": [],
        }

    # Normalize method path
    if not method_path.startswith("/"):
        method_path = f"/{method_path}"

    channel = await _get_channel(target, tls)
    collected: list[dict[str, Any]] = []
    call = None

    try:
        stub = channel.unary_stream(
            method_path,
            request_serializer=InputClass.SerializeToString,
            response_deserializer=OutputClass.FromString,
        )

        call = stub(
            request_msg,
            metadata=metadata,
            timeout=timeout_s,
        )

        async for response_msg in call:
            response_dict = MessageToDict(response_msg, preserving_proto_field_name=True)
            collected.append(response_dict)
            if len(collected) >= max_messages:
                break

        return {
            "success": True,
            "data": collected,
            "message_count": len(collected),
        }

    except grpc.RpcError as e:
        code = e.code()  # type: ignore[unresolved-attribute]  # grpc stubs incomplete
        details = e.details() or str(code)  # type: ignore[unresolved-attribute]
        hint = _error_hint(code)
        error_msg = f"gRPC error [{code.name}]: {details}"
        if hint:
            error_msg += f". {hint}"
        return {
            "success": False,
            "error": error_msg,
            "partial_data": collected,
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "Stream timed out — the server may be slow or unreachable",
            "partial_data": collected,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Streaming RPC failed: {e}",
            "partial_data": collected,
        }
    finally:
        # Cancel the stream if it wasn't fully exhausted
        if call is not None and hasattr(call, "cancel"):
            call.cancel()


async def execute_client_streaming_rpc(
    target_url: str,
    method_path: str,
    requests_json: list[dict[str, Any]],
    pool: dp_module.DescriptorPool,
    input_type_name: str,
    output_type_name: str,
    metadata: list[tuple[str, str]] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Execute a client-streaming gRPC RPC call (batch pattern).

    The client sends multiple request messages; the server responds once.
    All request messages are built upfront before opening the channel.

    Args:
        target_url: gRPC target URL (grpc:// or grpcs://)
        method_path: Full method path (e.g. "/package.Service/Upload")
        requests_json: List of request dicts to stream
        pool: DescriptorPool with loaded service descriptors
        input_type_name: Fully qualified input message type name
        output_type_name: Fully qualified output message type name
        metadata: Optional gRPC metadata tuples
        timeout_s: RPC timeout in seconds

    Returns:
        {"success": True, "data": dict, "messages_sent": int} on success
        {"success": False, "error": str} on failure
    """
    target, tls = parse_grpc_target(target_url)

    # Build all request messages upfront (validate before opening channel)
    try:
        input_desc = pool.FindMessageTypeByName(input_type_name)
        InputClass = GetMessageClass(input_desc)
        request_messages = [ParseDict(rj, InputClass()) for rj in requests_json]
    except KeyError:
        return {
            "success": False,
            "error": f"Input message type '{input_type_name}' not found in schema",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to build request messages: {e}",
        }

    # Build response deserializer
    try:
        output_desc = pool.FindMessageTypeByName(output_type_name)
        OutputClass = GetMessageClass(output_desc)
    except KeyError:
        return {
            "success": False,
            "error": f"Output message type '{output_type_name}' not found in schema",
        }

    # Normalize method path
    if not method_path.startswith("/"):
        method_path = f"/{method_path}"

    channel = await _get_channel(target, tls)

    try:
        stub = channel.stream_unary(
            method_path,
            request_serializer=InputClass.SerializeToString,
            response_deserializer=OutputClass.FromString,
        )

        call = stub(metadata=metadata, timeout=timeout_s)

        try:
            for msg in request_messages:
                await call.write(msg)
        finally:
            await call.done_writing()

        response_msg = await call

        response_dict = MessageToDict(response_msg, preserving_proto_field_name=True)

        return {
            "success": True,
            "data": response_dict,
            "messages_sent": len(request_messages),
        }

    except grpc.RpcError as e:
        code = e.code()  # type: ignore[unresolved-attribute]  # grpc stubs incomplete
        details = e.details() or str(code)  # type: ignore[unresolved-attribute]
        hint = _error_hint(code)
        error_msg = f"gRPC error [{code.name}]: {details}"
        if hint:
            error_msg += f". {hint}"
        return {"success": False, "error": error_msg}
    except Exception as e:
        return {"success": False, "error": f"Client-streaming RPC failed: {e}"}


async def execute_bidi_streaming_rpc(
    target_url: str,
    method_path: str,
    requests_json: list[dict[str, Any]],
    pool: dp_module.DescriptorPool,
    input_type_name: str,
    output_type_name: str,
    metadata: list[tuple[str, str]] | None = None,
    timeout_s: float = 30.0,
    max_messages: int = 100,
) -> dict[str, Any]:
    """Execute a bidirectional-streaming gRPC RPC call (fire-and-collect).

    The client sends all request messages via an async iterator, then collects
    responses. This is NOT concurrent read/write — all sends complete before
    reads begin (grpc handles done_writing via iterator exhaustion).

    Args:
        target_url: gRPC target URL (grpc:// or grpcs://)
        method_path: Full method path (e.g. "/package.Service/Chat")
        requests_json: List of request dicts to stream
        pool: DescriptorPool with loaded service descriptors
        input_type_name: Fully qualified input message type name
        output_type_name: Fully qualified output message type name
        metadata: Optional gRPC metadata tuples
        timeout_s: RPC timeout in seconds
        max_messages: Maximum number of response messages to collect

    Returns:
        {"success": True, "data": list[dict], "messages_sent": int, "message_count": int}
        {"success": False, "error": str, "partial_data": list[dict]} on failure
    """
    target, tls = parse_grpc_target(target_url)

    # Build all request messages upfront
    try:
        input_desc = pool.FindMessageTypeByName(input_type_name)
        InputClass = GetMessageClass(input_desc)
        request_messages = [ParseDict(rj, InputClass()) for rj in requests_json]
    except KeyError:
        return {
            "success": False,
            "error": f"Input message type '{input_type_name}' not found in schema",
            "partial_data": [],
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to build request messages: {e}",
            "partial_data": [],
        }

    # Build response deserializer
    try:
        output_desc = pool.FindMessageTypeByName(output_type_name)
        OutputClass = GetMessageClass(output_desc)
    except KeyError:
        return {
            "success": False,
            "error": f"Output message type '{output_type_name}' not found in schema",
            "partial_data": [],
        }

    # Normalize method path
    if not method_path.startswith("/"):
        method_path = f"/{method_path}"

    channel = await _get_channel(target, tls)
    collected: list[dict[str, Any]] = []
    call = None

    try:
        stub = channel.stream_stream(
            method_path,
            request_serializer=InputClass.SerializeToString,
            response_deserializer=OutputClass.FromString,
        )

        # Async generator yields pre-built messages; grpc handles done_writing
        async def request_iter():
            for msg in request_messages:
                yield msg

        call = stub(request_iter(), metadata=metadata, timeout=timeout_s)

        async for response_msg in call:
            response_dict = MessageToDict(response_msg, preserving_proto_field_name=True)
            collected.append(response_dict)
            if len(collected) >= max_messages:
                break

        return {
            "success": True,
            "data": collected,
            "messages_sent": len(request_messages),
            "message_count": len(collected),
        }

    except grpc.RpcError as e:
        code = e.code()  # type: ignore[unresolved-attribute]  # grpc stubs incomplete
        details = e.details() or str(code)  # type: ignore[unresolved-attribute]
        hint = _error_hint(code)
        error_msg = f"gRPC error [{code.name}]: {details}"
        if hint:
            error_msg += f". {hint}"
        return {
            "success": False,
            "error": error_msg,
            "partial_data": collected,
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "Bidi stream timed out — the server may be slow or unreachable",
            "partial_data": collected,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Bidi-streaming RPC failed: {e}",
            "partial_data": collected,
        }
    finally:
        if call is not None and hasattr(call, "cancel"):
            call.cancel()


def _error_hint(code: grpc.StatusCode) -> str:
    """Provide a helpful hint for common gRPC error codes."""
    hints = {
        grpc.StatusCode.UNAUTHENTICATED: "Check X-Target-Headers for auth credentials",
        grpc.StatusCode.PERMISSION_DENIED: "The credentials lack permission for this method",
        grpc.StatusCode.NOT_FOUND: "The method or service was not found on the server",
        grpc.StatusCode.UNAVAILABLE: "Server is unavailable — check the target URL and port",
        grpc.StatusCode.DEADLINE_EXCEEDED: "RPC timed out — the server may be slow or unreachable",
        grpc.StatusCode.UNIMPLEMENTED: "The method is not implemented on the server",
    }
    return hints.get(code, "")
