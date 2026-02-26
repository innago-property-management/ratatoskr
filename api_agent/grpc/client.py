"""gRPC client — execute unary RPC calls with dynamic message construction."""

import logging
from typing import Any

import grpc
import grpc.aio
from google.protobuf import descriptor_pool as dp_module
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message_factory import GetMessageClass

from .reflection import parse_grpc_target

logger = logging.getLogger(__name__)


def _create_channel(target: str, tls: bool, skip_tls_verify: bool = False) -> grpc.aio.Channel:
    """Create an async gRPC channel. Extracted for testability."""
    if tls:
        creds = grpc.ssl_channel_credentials()
        if skip_tls_verify:
            logger.warning("TLS verification disabled for gRPC target %s", target)
        return grpc.aio.secure_channel(target, creds)
    return grpc.aio.insecure_channel(target)


async def execute_unary_rpc(
    target_url: str,
    method_path: str,
    request_json: dict[str, Any],
    pool: dp_module.DescriptorPool,
    input_type_name: str,
    output_type_name: str,
    metadata: list[tuple[str, str]] | None = None,
    skip_tls_verify: bool = False,
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
        skip_tls_verify: Skip TLS verification (dev only)
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

    channel = _create_channel(target, tls, skip_tls_verify)

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

        response_dict = MessageToDict(
            response_msg, preserving_proto_field_name=True
        )

        return {"success": True, "data": response_dict}

    except grpc.RpcError as e:
        code = e.code()
        details = e.details() or str(code)
        hint = _error_hint(code)
        error_msg = f"gRPC error [{code.name}]: {details}"
        if hint:
            error_msg += f". {hint}"
        return {"success": False, "error": error_msg}
    except Exception as e:
        return {"success": False, "error": f"RPC call failed: {e}"}
    finally:
        await channel.close()


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
