"""gRPC server reflection client — discovers services and message types at runtime."""

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import grpc
import grpc.aio
from google.protobuf import descriptor_pool as dp_module
from google.protobuf.descriptor import FieldDescriptor, MethodDescriptor, ServiceDescriptor

logger = logging.getLogger(__name__)

# Proto field type names for schema display
_FIELD_TYPE_NAMES = {
    FieldDescriptor.TYPE_DOUBLE: "double",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int64",
    FieldDescriptor.TYPE_UINT64: "uint64",
    FieldDescriptor.TYPE_INT32: "int32",
    FieldDescriptor.TYPE_FIXED64: "fixed64",
    FieldDescriptor.TYPE_FIXED32: "fixed32",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "string",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "uint32",
    FieldDescriptor.TYPE_SFIXED32: "sfixed32",
    FieldDescriptor.TYPE_SFIXED64: "sfixed64",
    FieldDescriptor.TYPE_SINT32: "sint32",
    FieldDescriptor.TYPE_SINT64: "sint64",
    FieldDescriptor.TYPE_ENUM: "enum",
    FieldDescriptor.TYPE_GROUP: "group",
    FieldDescriptor.TYPE_MESSAGE: "message",
}


@dataclass
class MethodInfo:
    """A single RPC method on a gRPC service."""

    name: str  # e.g. "SayHello"
    full_method_path: str  # e.g. "/helloworld.Greeter/SayHello"
    input_type: str  # e.g. "helloworld.HelloRequest"
    output_type: str  # e.g. "helloworld.HelloReply"
    client_streaming: bool = False
    server_streaming: bool = False


@dataclass
class ServiceInfo:
    """A gRPC service and its methods."""

    full_name: str  # e.g. "helloworld.Greeter"
    methods: list[MethodInfo] = field(default_factory=list)


@dataclass
class GrpcSchema:
    """Parsed gRPC schema from server reflection."""

    services: list[ServiceInfo]
    pool: Any  # DescriptorPool (typed as Any to simplify testing)
    raw_schema_text: str  # Human-readable IDL for search_schema and LLM context


def parse_grpc_target(url: str) -> tuple[str, bool]:
    """Parse gRPC target URL into (host:port, tls).

    Examples:
        grpc://localhost:50051    -> ("localhost:50051", False)
        grpcs://api.example.com  -> ("api.example.com:443", True)
        grpcs://api.example.com:8443 -> ("api.example.com:8443", True)
    """
    parsed = urlparse(url)
    tls = parsed.scheme == "grpcs"
    host = parsed.hostname or ""
    port = parsed.port

    if port:
        target = f"{host}:{port}"
    elif tls:
        target = f"{host}:443"
    else:
        target = host

    return target, tls


def _format_field_type(field_desc: FieldDescriptor) -> str:
    """Format a protobuf field type for display."""
    if field_desc.type == FieldDescriptor.TYPE_MESSAGE:
        return field_desc.message_type.full_name
    if field_desc.type == FieldDescriptor.TYPE_ENUM:
        return field_desc.enum_type.full_name
    return _FIELD_TYPE_NAMES.get(field_desc.type, "unknown")


def _format_message_type(msg_desc: Any, seen: set[str] | None = None) -> str:
    """Format a message type as compact IDL."""
    if seen is None:
        seen = set()
    if msg_desc.full_name in seen:
        return ""
    seen.add(msg_desc.full_name)

    lines = [f"{msg_desc.full_name} {{"]
    for f in msg_desc.fields:
        type_str = _format_field_type(f)
        label = ""
        if f.label == FieldDescriptor.LABEL_REPEATED:
            label = "repeated "
        lines.append(f"  {label}{type_str} {f.name}")
    lines.append("}")
    return "\n".join(lines)


def _extract_method(method_desc: MethodDescriptor, service_name: str) -> MethodInfo:
    """Extract MethodInfo from a protobuf MethodDescriptor."""
    return MethodInfo(
        name=method_desc.name,
        full_method_path=f"/{service_name}/{method_desc.name}",
        input_type=method_desc.input_type.full_name,
        output_type=method_desc.output_type.full_name,
        client_streaming=method_desc.client_streaming,
        server_streaming=method_desc.server_streaming,
    )


def build_schema_text(services: list[ServiceInfo], pool: Any) -> str:
    """Build human-readable schema text from services and descriptor pool.

    Format:
        <services>
        package.ServiceName
          MethodName(input.Type) -> output.Type
          StreamMethod(input.Type) -> output.Type  [server-streaming, unsupported-v1]

        <message_types>
        input.Type {
          field_name: type
        }
    """
    seen_messages: set[str] = set()
    svc_lines: list[str] = ["<services>"]
    msg_sections: list[str] = []

    for svc in services:
        svc_lines.append(svc.full_name)
        for m in svc.methods:
            streaming_tags = []
            if m.client_streaming:
                streaming_tags.append("client-streaming")
            if m.server_streaming:
                streaming_tags.append("server-streaming")
            if streaming_tags:
                streaming_tags.append("unsupported-v1")

            tag_str = f"  [{', '.join(streaming_tags)}]" if streaming_tags else ""
            svc_lines.append(f"  {m.name}({m.input_type}) -> {m.output_type}{tag_str}")

            # Collect message types to document
            for type_name in (m.input_type, m.output_type):
                if type_name not in seen_messages:
                    try:
                        msg_desc = pool.FindMessageTypeByName(type_name)
                        formatted = _format_message_type(msg_desc, seen_messages)
                        if formatted:
                            msg_sections.append(formatted)
                    except KeyError:
                        pass  # Type not in pool (shouldn't happen)

        svc_lines.append("")  # Blank line between services

    result = "\n".join(svc_lines)
    if msg_sections:
        result += "\n<message_types>\n" + "\n\n".join(msg_sections)

    return result


def _make_channel(
    target: str, tls: bool, skip_tls_verify: bool = False
) -> grpc.aio.Channel:
    """Create a gRPC async channel."""
    if tls:
        if skip_tls_verify:
            logger.warning("TLS verification disabled for gRPC target %s", target)
        creds = grpc.ssl_channel_credentials()
        return grpc.aio.secure_channel(target, creds)
    return grpc.aio.insecure_channel(target)


def _extract_services(
    service_names: list[str], pool: dp_module.DescriptorPool
) -> list[ServiceInfo]:
    """Walk services from pool, extract ServiceInfo with methods."""
    services: list[ServiceInfo] = []
    for svc_name in service_names:
        # Skip the reflection service itself
        if "ServerReflection" in svc_name:
            continue
        try:
            svc_desc: ServiceDescriptor = pool.FindServiceByName(svc_name)
        except KeyError:
            logger.warning("Service %s not found in descriptor pool", svc_name)
            continue

        methods = [_extract_method(m, svc_name) for m in svc_desc.methods]
        services.append(ServiceInfo(full_name=svc_name, methods=methods))

    return services


async def fetch_schema(
    target_url: str,
    metadata: list[tuple[str, str]] | None = None,
    skip_tls_verify: bool = False,
) -> GrpcSchema:
    """Connect to gRPC server, fetch reflection schema, return parsed schema.

    Args:
        target_url: gRPC target URL (grpc:// or grpcs://)
        metadata: Optional gRPC metadata (auth headers etc.)
        skip_tls_verify: Skip TLS certificate verification (dev only)

    Returns:
        GrpcSchema with services, pool, and raw schema text

    Raises:
        grpc.RpcError: If reflection call fails (e.g., reflection not enabled)
    """
    import asyncio

    from grpc_reflection.v1alpha.proto_reflection_descriptor_database import (
        ProtoReflectionDescriptorDatabase,
    )

    target, tls = parse_grpc_target(target_url)
    channel = _make_channel(target, tls, skip_tls_verify)

    try:
        # ProtoReflectionDescriptorDatabase uses sync channel internally.
        # Create a sync channel for reflection, then use pool for message construction.
        sync_channel = (
            grpc.secure_channel(target, grpc.ssl_channel_credentials())
            if tls
            else grpc.insecure_channel(target)
        )
        try:
            reflection_db = await asyncio.to_thread(
                ProtoReflectionDescriptorDatabase, sync_channel
            )
            service_names = await asyncio.to_thread(reflection_db.get_services)
        finally:
            sync_channel.close()

        pool = dp_module.DescriptorPool(reflection_db)
        services = _extract_services(list(service_names), pool)
        raw_text = build_schema_text(services, pool)

        return GrpcSchema(services=services, pool=pool, raw_schema_text=raw_text)
    finally:
        await channel.close()
