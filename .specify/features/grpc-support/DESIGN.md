# DESIGN: gRPC Protocol Support

**Version:** 1.0
**Date:** 2026-02-26
**Based on:** DECISION.md v1.0

---

## Overview

Add `api_type="grpc"` as a third protocol alongside `"graphql"` and `"rest"`. The gRPC agent uses server reflection to discover services and methods at runtime, constructs protobuf messages dynamically from reflection descriptors, executes unary RPCs, and returns JSON results through the same MCP tool loop as existing agents.

---

## System Context Diagram

```
MCP Client
    │
    │  X-Target-URL: grpc://service:50051
    │  X-API-Type: grpc
    │  X-Target-Headers: {"authorization": "Bearer ..."}
    ▼
DynamicToolNamingMiddleware
    │  (exposes {prefix}_query)
    ▼
tools/query.py  (_query)
    │  api_type == "grpc"
    ▼
agent/grpc_agent.py  (process_grpc_query)
    ├── grpc/reflection.py  (fetch schema via gRPC Server Reflection)
    ├── LLMProvider.run_tool_loop()
    │       └── grpc_call tool  →  grpc/client.py  (execute unary RPC)
    │       └── sql_query tool  →  executor.py (DuckDB)
    │       └── search_schema   →  agent/schema_search.py
    └── returns {ok, data, result, rpc_calls}
```

---

## New Files

### `api_agent/grpc/__init__.py`

Empty init, marks package.

### `api_agent/grpc/reflection.py`

Responsible for:
1. Connecting to gRPC server via `grpc.aio` channel.
2. Calling the gRPC Server Reflection API to fetch service names and `FileDescriptorProto` objects.
3. Building a `DescriptorPool` from those file descriptors.
4. Returning a structured service schema for the LLM context.

```python
"""gRPC server reflection client — discovers services and message types at runtime."""

import grpc.aio
import grpcio_reflection_client  # see impl below
from google.protobuf import descriptor_pool, descriptor_pb2, message_factory
from google.protobuf.json_format import MessageToDict, ParseDict

@dataclass
class GrpcServiceSchema:
    """Parsed gRPC service schema for a single connection."""
    services: list[ServiceInfo]        # Each service and its methods
    pool: descriptor_pool.DescriptorPool  # Populated from reflection
    file_descriptors: list[descriptor_pb2.FileDescriptorProto]
    raw_schema_text: str               # Human-readable IDL for search_schema

@dataclass
class ServiceInfo:
    full_name: str                     # e.g. "helloworld.Greeter"
    methods: list[MethodInfo]

@dataclass
class MethodInfo:
    name: str                          # e.g. "SayHello"
    full_method_path: str              # e.g. "/helloworld.Greeter/SayHello"
    input_type: str                    # e.g. "helloworld.HelloRequest"
    output_type: str                   # e.g. "helloworld.HelloReply"
    client_streaming: bool
    server_streaming: bool

async def fetch_schema(
    target: str,
    metadata: list[tuple[str, str]] | None = None,
    tls: bool = False,
    skip_tls_verify: bool = False,
) -> GrpcServiceSchema | None:
    """Connect to gRPC server, fetch reflection schema, return parsed schema."""
```

**Implementation approach for reflection:**

The gRPC Server Reflection service is itself a gRPC service:
- Service: `grpc.reflection.v1alpha.ServerReflection`
- Method: `ServerReflectionInfo` (bidi-streaming: stream of requests → stream of responses)

We use `ProtoReflectionDescriptorDatabase` from `grpcio-reflection`, which wraps this bidi stream as a `DescriptorDatabase`. The `DescriptorPool` lazily pulls descriptors from this database on demand.

```python
from grpc_reflection.v1alpha.proto_reflection_descriptor_database import (
    ProtoReflectionDescriptorDatabase,
)

async def fetch_schema(target, metadata, tls, skip_tls_verify):
    channel = _make_channel(target, tls, skip_tls_verify)
    try:
        # Step 1: Build descriptor database from reflection
        db = ProtoReflectionDescriptorDatabase(channel)

        # Step 2: List all services
        services_response = db.get_services()  # ["helloworld.Greeter", ...]

        # Step 3: Build pool (lazy-fetches file descriptors from server)
        pool = descriptor_pool.DescriptorPool(db)

        # Step 4: Walk each service, extract methods and message types
        services = []
        for svc_name in services_response:
            svc_desc = pool.FindServiceByName(svc_name)
            methods = [_extract_method(m) for m in svc_desc.methods]
            services.append(ServiceInfo(svc_name, methods))

        # Step 5: Serialize raw schema text for search_schema
        raw_text = _build_raw_schema_text(services, pool)

        return GrpcServiceSchema(services, pool, [], raw_text)
    finally:
        await channel.close()
```

**Note:** `ProtoReflectionDescriptorDatabase` was synchronous in older versions. We verify async compat in tests; if needed, run in `asyncio.get_event_loop().run_in_executor()`.

### `api_agent/grpc/client.py`

Responsible for:
- Creating `grpc.aio` channels (plaintext or TLS).
- Executing unary RPC calls given a full method path + JSON request body.
- Serializing JSON → protobuf, deserializing protobuf → JSON.
- Returning `{"success": bool, "data": dict | str}` consistent with REST client contract.

```python
async def execute_unary_rpc(
    target: str,
    method_path: str,             # "/package.Service/Method"
    request_json: dict,           # from LLM tool call
    pool: descriptor_pool.DescriptorPool,
    input_type_name: str,         # "package.RequestType"
    output_type_name: str,        # "package.ResponseType"
    metadata: list[tuple[str, str]] | None = None,
    tls: bool = False,
    skip_tls_verify: bool = False,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Execute unary gRPC call. Returns {"success": bool, "data": dict | str, ...}."""
```

**Serialization pipeline:**
```
JSON (from LLM)
  → ParseDict(request_json, InputMessageClass())   # json_format.ParseDict
  → InputMessageClass.SerializeToString()          # protobuf bytes
  → channel.unary_unary(method_path, ...) call     # wire
  → OutputMessageClass.FromString(raw_bytes)       # deserialize
  → MessageToDict(response_msg, preserving_proto_field_case=True)  # JSON dict
  → return to LLM
```

**Channel creation:**
```python
def _make_channel(target: str, tls: bool, skip_tls_verify: bool) -> grpc.aio.Channel:
    if tls:
        if skip_tls_verify:
            # For dev/internal; log a warning
            creds = grpc.ssl_channel_credentials()  # uses default system certs
        else:
            creds = grpc.ssl_channel_credentials()
        return grpc.aio.secure_channel(target, creds)
    return grpc.aio.insecure_channel(target)
```

### `api_agent/agent/grpc_agent.py`

Mirrors `rest_agent.py` and `graphql_agent.py`. Responsibilities:
- Parse target from `ctx.target_url`.
- Fetch schema via `grpc/reflection.py`.
- Build system prompt with compact service IDL.
- Create `grpc_call` tool and `sql_query` / `search_schema` tools.
- Run `provider.run_tool_loop()`.
- Return `{"ok", "data", "result", "rpc_calls", "error"}`.

---

## Modified Files

### `api_agent/context.py`

```python
# Change validation from:
if api_type not in ("graphql", "rest"):
    raise MissingHeaderError(f"X-API-Type must be 'graphql' or 'rest', got '{api_type}'")

# To:
if api_type not in ("graphql", "rest", "grpc"):
    raise MissingHeaderError(f"X-API-Type must be 'graphql', 'rest', or 'grpc', got '{api_type}'")
```

Also update `RequestContext` docstring.

### `api_agent/tools/query.py`

```python
from ..agent.grpc_agent import process_grpc_query

# In the query() tool handler:
if req_ctx.api_type == "graphql":
    result = await process_query(question, req_ctx)
elif req_ctx.api_type == "rest":
    result = await process_rest_query(question, req_ctx)
else:  # grpc
    result = await process_grpc_query(question, req_ctx)
```

Response key for gRPC will be `rpc_calls` (analogous to `queries` / `api_calls`).

### `api_agent/middleware.py`

`_inject_api_context()` — add gRPC label:
```python
def _inject_api_context(description: str, hostname: str, api_type: str) -> str:
    label_map = {"graphql": "GraphQL", "rest": "REST", "grpc": "gRPC"}
    api_type_label = label_map.get(api_type, api_type.upper())
    prefix = f"[{hostname} {api_type_label} API] "
    return prefix + description
```

`load_schema_and_base_url()` in `recipe/runner.py` — gRPC returns `("", "")` for now (recipes out of scope):
```python
if ctx.api_type == "grpc":
    return "", ""  # Recipe support deferred to v2
```

### `pyproject.toml`

```toml
dependencies = [
    ...
    "grpcio>=1.70.0",
    "grpcio-reflection>=1.70.0",
]
```

---

## Schema Representation (LLM Context)

The schema context passed to the LLM is a compact proto-IDL-like format:

```
<services>
helloworld.Greeter
  SayHello(helloworld.HelloRequest) -> helloworld.HelloReply

payments.PaymentService
  CreatePayment(payments.CreatePaymentRequest) -> payments.Payment  [unary]
  StreamTransactions(payments.StreamRequest) -> payments.Transaction  [server-streaming, unsupported-v1]

<message_types>
helloworld.HelloRequest {
  name: string!
}

helloworld.HelloReply {
  message: string!
}

payments.CreatePaymentRequest {
  amount: int64!
  currency: string!
  idempotency_key: string
}

payments.Payment {
  id: string!
  status: string!
  amount: int64!
  currency: string!
  created_at: google.protobuf.Timestamp
}
```

**Design decisions for schema representation:**
- Show all fields (not just required) because protobuf optional/required semantics differ from JSON Schema — LLMs should see the full shape.
- Show field numbers if helpful for debugging (optional — probably omit to reduce noise).
- Mark streaming methods explicitly as `[unsupported-v1]` so the LLM doesn't attempt them.
- Truncate at `MAX_SCHEMA_CHARS` with `[SCHEMA TRUNCATED - use search_schema() to explore]` hint.
- `raw_schema_text` stored in ContextVar for `search_schema` — full untruncated IDL text.

---

## `grpc_call` Tool Interface

The tool the LLM uses to invoke RPC methods:

```python
async def grpc_call(
    method: str,          # Full method path: "package.Service/MethodName" or "/package.Service/MethodName"
    request: str,         # JSON string: '{"field": "value"}'
    name: str = "data",   # Table name for sql_query
    return_directly: bool = False,
) -> str:
    """Execute unary gRPC RPC call.

    Args:
        method: Full method path (e.g., "helloworld.Greeter/SayHello")
        request: JSON string with request fields (e.g., '{"name": "world"}')
        name: Table name for sql_query (default: "data")
        return_directly: Return raw data directly without LLM processing

    Returns:
        JSON string with RPC response
    """
```

**LLM guidance in system prompt:**
```
grpc_call(method, request, name?, return_directly?)
  Execute unary gRPC RPC. Result stored as DuckDB table.
  - method: "package.Service/MethodName" (from <services> above)
  - request: JSON fields matching the input message type
  - Streaming methods are not supported in v1
```

---

## ContextVar Isolation Pattern

Following the established pattern (see `graphql_agent.py`, `rest_agent.py`):

```python
_rpc_calls: ContextVar[list[dict[str, Any]]] = ContextVar("rpc_calls")
_query_results: ContextVar[dict[str, Any]] = ContextVar("rpc_query_results")
_last_result: ContextVar[list] = ContextVar("rpc_last_result")
_raw_schema: ContextVar[str] = ContextVar("rpc_raw_schema")
_sql_steps: ContextVar[list[str]] = ContextVar("rpc_sql_steps")
```

Reset at start of each `process_grpc_query()` call. Mutable containers (`list`, `dict`) used for `_last_result` and accumulated collections, matching the pattern enforced by `ContextVar` in async task groups.

---

## Target URL Parsing

`X-Target-URL` for gRPC uses `grpc://` or `grpcs://` scheme:

```python
def _parse_grpc_target(url: str) -> tuple[str, bool]:
    """Parse gRPC target URL. Returns (host:port, tls).

    Examples:
        grpc://localhost:50051    -> ("localhost:50051", False)
        grpcs://api.example.com  -> ("api.example.com", True)
        grpcs://api.example.com:443  -> ("api.example.com:443", True)
    """
    parsed = urlparse(url)
    tls = parsed.scheme == "grpcs"
    host = parsed.hostname or ""
    port = parsed.port
    target = f"{host}:{port}" if port else host
    return target, tls
```

Default port: gRPC typically runs on `50051` (plaintext) or `443` (TLS). If no port, we leave host-only and let gRPC resolve.

---

## System Prompt for gRPC Agent

```python
def _build_system_prompt() -> str:
    return f"""You are a gRPC API agent that answers questions by calling RPC methods and returning data.

{SQL_RULES}

## gRPC-Specific
- All request fields must be provided as JSON matching the message type in <message_types>
- Use exact field names as shown in the schema (proto field names, snake_case)
- Streaming methods are marked [server-streaming] or [client-streaming] and are NOT supported

<tools>
grpc_call(method, request, name?, return_directly?)
  Execute unary gRPC call. Result stored as DuckDB table.
  - method: "package.Service/MethodName"
  - request: JSON string matching input message type
  - return_directly: Skip LLM analysis, return raw data directly

{SQL_TOOL_DESC}

{SEARCH_TOOL_DESC}
</tools>
<workflow>
1. Read <services> and <message_types> below
2. Identify the correct service method for the question
3. Build request JSON matching the input message type
4. Execute grpc_call()
5. Use sql_query to filter/aggregate if needed
</workflow>

{CONTEXT_SECTION.format(current_date=current_date, max_turns=settings.MAX_AGENT_TURNS)}

{DECISION_GUIDANCE}

{UNCERTAINTY_SPEC}

{PERSISTENCE_SPEC.format(max_turns=settings.MAX_AGENT_TURNS)}

{EFFECTIVE_PATTERNS}

{TOOL_USAGE_RULES}

<examples>
Simple: grpc_call("helloworld.Greeter/SayHello", '{{"name": "world"}}')
With SQL: grpc_call("payments.PaymentService/ListPayments", '{{"limit": 100}}', name="payments"); sql_query('SELECT currency, SUM(amount) FROM payments GROUP BY currency')
</examples>
"""
```

---

## Error Handling

| Scenario | Response |
|---|---|
| Reflection not enabled | `{"success": False, "error": "gRPC server reflection not enabled. Enable via grpc.reflection.v1alpha.SERVER_REFLECTION_SERVICE_NAME on the server."}` |
| Method not found | `{"success": False, "error": "Method 'X' not found. Available: [list]"}` |
| Streaming method called | `{"success": False, "error": "Method 'X' uses server/client streaming, unsupported in v1. Only unary methods are supported."}` |
| RPC error (gRPC status codes) | `{"success": False, "error": "gRPC error [STATUS_CODE]: message", "hint": "..."}` |
| JSON parse error in request | `{"success": False, "error": "Invalid request JSON: ..."}` |
| TLS error | `{"success": False, "error": "TLS handshake failed: ... Use X-GRPC-TLS-Skip-Verify: true for dev/internal endpoints."}` |
| Protobuf parse error | `{"success": False, "error": "Failed to build request: field 'X' not found in message type 'Y'"}` |

---

## Test Strategy

Following existing test patterns:

### Unit tests: `tests/grpc/test_grpc_reflection.py`
- `_build_schema_context()` — compact IDL generation from synthetic service descriptors.
- `_parse_grpc_target()` — URL parsing for grpc:// and grpcs://.
- `_build_system_prompt()` — smoke test.

### Unit tests: `tests/grpc/test_grpc_client.py`
- `execute_unary_rpc()` with mock channel and descriptors.
- JSON → protobuf → JSON round-trip for common field types.
- Error handling for gRPC status codes.

### Agent integration tests: `tests/test_grpc_agent.py`
- `process_grpc_query()` with `FakeLLMProvider` (existing test pattern).
- Mock the gRPC channel transport (httpx-style mock for gRPC).
- Full tool loop: `grpc_call` → `sql_query` → response.
- Streaming method guard: assert error when agent tries to call streaming method.

### Context validation tests: `tests/test_context.py` additions
- `X-API-Type: grpc` accepted.
- `X-API-Type: other` still rejected.

---

## Open Questions for Implementation Phase

1. **`ProtoReflectionDescriptorDatabase` async compat** — the class uses synchronous channel operations internally. Verify behavior with `grpc.aio` channel; may need to use a sync `grpc.insecure_channel` for reflection only, then `grpc.aio` for actual calls. Alternatively, run sync reflection in a thread executor.

2. **Channel lifecycle** — Should channels be cached per target (for efficiency) or created per request (for isolation)? Given that `grpc.aio` is async-safe and Ratatoskr processes one query at a time per MCP session, per-request channels are simpler and safer to start with. Cache later if latency is a concern.

3. **`X-GRPC-TLS-Skip-Verify` header** — Expose this in `RequestContext` for dev/internal use. Log a warning when used.

4. **`google.protobuf.Timestamp` and other well-known types** — `MessageToDict` handles these natively (converts to RFC3339 string). No special handling needed, but worth verifying in tests.

5. **Proto field naming** — `MessageToDict` supports `preserving_proto_field_case=True` (uses proto field names, snake_case) vs. `False` (uses camelCase). Use `False` (camelCase) to be more JSON-idiomatic for LLMs, or `True` to match what users see in `.proto` files? Lean toward `True` (preserve proto names) since the LLM sees the schema in proto field name format.

---

## File Change Summary

| File | Change |
|---|---|
| `api_agent/grpc/__init__.py` | New — empty init |
| `api_agent/grpc/reflection.py` | New — reflection client, schema fetching |
| `api_agent/grpc/client.py` | New — channel management, unary call execution |
| `api_agent/agent/grpc_agent.py` | New — agent loop, tool creation, process_grpc_query |
| `api_agent/context.py` | Modify — add "grpc" to valid api_type values |
| `api_agent/tools/query.py` | Modify — route api_type=="grpc" to grpc_agent |
| `api_agent/middleware.py` | Modify — add "grpc" to api_type label map |
| `api_agent/recipe/runner.py` | Modify — add grpc no-op branch in load_schema_and_base_url |
| `pyproject.toml` | Modify — add grpcio + grpcio-reflection deps |
| `tests/grpc/test_grpc_reflection.py` | New — unit tests for reflection + schema building |
| `tests/grpc/test_grpc_client.py` | New — unit tests for unary call execution |
| `tests/test_grpc_agent.py` | New — integration tests with FakeLLMProvider |
| `tests/test_context.py` | Modify — test api_type="grpc" accepted |
