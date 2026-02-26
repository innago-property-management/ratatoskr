# DECISION: gRPC Protocol Support

**Version:** 1.0
**Date:** 2026-02-26
**Status:** Approved

---

## Use Case

Ratatoskr currently exposes GraphQL and REST APIs to LLM agents via MCP. Many backend services at scale use gRPC — especially microservices, internal platforms (gRPC-gateway backends), and any service using Protocol Buffers for serialization. Without gRPC support, these services are unreachable through Ratatoskr.

**Target users:**
- Developers with internal gRPC microservices who want NL querying via Claude/Cursor/etc.
- Teams that expose both gRPC and REST but want a unified MCP interface.
- Anyone who needs to introspect and call gRPC services without maintaining `.proto` files client-side.

**Business value:**
- Expands Ratatoskr's addressable protocol surface from 2 to 3 (GraphQL, REST, gRPC).
- Enables LLM agents to query gRPC services using the same MCP pattern.
- Positions Ratatoskr as the polyglot universal API gateway for LLMs.

---

## Scope Decisions

### What is in scope (v1)

- **Unary RPC calls only** — the most common pattern (request → response), analogous to a REST GET/POST.
- **Service reflection** — discover services and methods at runtime via gRPC Server Reflection API (v1alpha), without requiring `.proto` files.
- **Dynamic message construction** — build protobuf request messages from reflection descriptors at runtime.
- **JSON-native interface** — accept and return JSON in the MCP tool; serialize to/from protobuf internally.
- **Plaintext + TLS channels** — support both `grpc://` (insecure) and `grpcs://` (TLS) target URLs.
- **Header-based auth** — pass auth tokens (e.g., `Authorization: Bearer ...`) as gRPC metadata via `X-Target-Headers`.
- **Schema representation** — present gRPC service schemas to the LLM as a compact IDL-like format mirroring the GraphQL/REST pattern.
- **`search_schema` tool** — same grep-based tool as REST/GraphQL for large schemas.
- **MCP tool naming** — follow existing `{prefix}_query` middleware pattern.

### What is out of scope (v1)

- **Server-side streaming, client-side streaming, bidi streaming** — deferred to v2. Streaming adds significant complexity (response iteration, buffering) and covers a small minority of real-world gRPC methods that LLMs need to call.
- **`_execute` tool** — direct execution without NL agent loop (add in v2, consistent with REST pattern).
- **Recipe extraction** — gRPC steps are distinct from REST/GraphQL; recipe schema needs extension. Deferred.
- **gRPC-Web** — browser-over-HTTP/1.1 variant; out of scope for server-to-server use.
- **Mutual TLS (mTLS)** — complex cert management; deferred to v2.
- **Protobuf well-known types** — handled automatically by protobuf library; no special casing needed.

---

## Library Evaluation

### The Core Problem

gRPC + protobuf normally requires **code generation**: you compile `.proto` files into language-specific stubs. This does not work for a dynamic agent — we don't know what service we're calling at build time.

The solution is the **gRPC Server Reflection API** (a standard gRPC service built into most production servers). It returns `FileDescriptorProto` objects describing the service schema, from which we can dynamically construct messages and call methods — no `.proto` files needed.

### Option A: grpcio + grpc.aio (RECOMMENDED)

**Package:** `grpcio>=1.70.0`, `grpcio-reflection>=1.70.0`

**Current state (2026):** Latest is 1.78.0 (released ~2025-01). Actively maintained by Google. Python 3.11/3.12 supported. Ships with asyncio support via `grpc.aio`.

**How it handles reflection:**
`grpcio-reflection` ships `ProtoReflectionDescriptorDatabase` — a client-side adapter that implements protobuf's `DescriptorDatabase` interface by calling the server's reflection service. Combined with `DescriptorPool`, it allows:

```python
# Discover + call without .proto files
channel = grpc.aio.insecure_channel("host:port")
db = ProtoReflectionDescriptorDatabase(channel)        # live server reflection
pool = DescriptorPool(db)                              # auto-fetches descriptors
desc = pool.FindMessageTypeByName("helloworld.HelloRequest")
MsgClass = message_factory.GetMessageClass(desc)       # dynamic message class
req = MsgClass(name="world")
stub = channel.unary_unary(
    "/helloworld.Greeter/SayHello",
    request_serializer=req.SerializeToString,
    response_deserializer=MsgClass.FromString,         # for response type
)
resp = await stub(req)
```

**Pros:**
- Official Google/gRPC library. Will never be abandoned.
- `grpc.aio` provides native asyncio `Channel` — plays well with Ratatoskr's async architecture.
- `ProtoReflectionDescriptorDatabase` is exactly what we need: lazy-fetches descriptors from live server, caches them, integrates with `DescriptorPool`.
- `grpcio-reflection` versioned in lockstep with `grpcio` (no version skew risk).
- Dynamic message construction fully supported via `google.protobuf.message_factory.GetMessageClass` (protobuf 6.x — already a dep).
- JSON serialization: `google.protobuf.json_format.MessageToDict` / `ParseDict` for LLM-friendly IO.
- TLS: `grpc.ssl_channel_credentials()`, metadata auth via channel options or per-call metadata.

**Cons:**
- `grpcio` is a C extension wheel (compiled). Adds ~3MB to the distribution and requires platform wheels. However: pre-built for all major platforms on PyPI, already available in the dev environment at 1.78.0.
- Reflection is v1alpha (the stable v1 API exists too). `ProtoReflectionDescriptorDatabase` uses v1alpha by default, but servers implementing only v1 need a workaround. In practice, v1alpha is universal.

**Verdict: Strong fit.** The reflection client + dynamic message construction are production-tested, well-documented, and maintained.

### Option B: grpclib (Pure Python, asyncio-native)

**Package:** `grpclib>=0.4.9`

**Current state (2026):** Latest is 0.4.9. Pure Python asyncio implementation. Not backed by Google.

**How it handles reflection:**
grpclib does NOT include a reflection client out of the box. It provides lower-level bidi streaming primitives. To implement reflection-based dynamic calls, you would need to:
1. Manually implement the `ServerReflectionInfo` bidi streaming call.
2. Parse the returned `ServerReflectionResponse` protos.
3. Build your own `DescriptorPool` population logic.
4. Use `google-protobuf` for message construction anyway.

**Pros:**
- Pure Python — no C extension, simpler packaging.
- asyncio-native from the start (grpc.aio was added later to grpcio).

**Cons:**
- No built-in reflection client. Would require ~300–500 lines of custom reflection protocol implementation.
- Smaller ecosystem, no Google backing.
- Dynamic stub invocation is less documented.
- Version 0.x — semantically pre-stable.
- Last meaningful release was 0.4.9 (2024). Activity has slowed.
- If we're implementing the reflection protocol ourselves anyway, we lose the main advantage of a library.

**Verdict: Not recommended.** The missing reflection client forces us to build what `grpcio-reflection` already provides, with no meaningful benefit to offset that cost.

### Option C: betterproto

**Package:** `betterproto>=1.2.5`

**Current state (2026):** Latest is 1.2.5. Focused on code generation (`.proto` → Python dataclasses).

**How it handles dynamic/reflection use:**
betterproto is a **code generation library** with a runtime. It does not support dynamic message construction from reflection descriptors. Its `Message` base class is for statically-generated code only.

**Verdict: Wrong tool for the job.** betterproto is excellent for typed, codegen-based gRPC clients. For dynamic, schema-agnostic operation it is not applicable.

### Decision: Option A — grpcio + grpcio-reflection

**New dependencies:**
```
grpcio>=1.70.0
grpcio-reflection>=1.70.0
```

Both are actively maintained at 1.78.0, co-versioned, available as pre-built wheels for Python 3.11/3.12 on all platforms. `protobuf` is already a transitive dependency.

---

## Architecture Integration

### Headers

Following the existing convention, gRPC is activated via `X-API-Type: grpc`:

| Header | Required | Example |
|---|---|---|
| `X-Target-URL` | Yes | `grpc://payments.internal:50051` or `grpcs://api.example.com:443` |
| `X-API-Type` | Yes | `grpc` |
| `X-Target-Headers` | No | `{"authorization": "Bearer sk-..."}` |

The `grpc://` scheme signals plaintext; `grpcs://` signals TLS. This is analogous to `http://`/`https://` in REST.

### context.py Changes

`RequestContext.api_type` must accept `"grpc"` as a third valid value. `get_request_context()` currently hard-codes `("graphql", "rest")` — extend to `("graphql", "rest", "grpc")`.

### tools/query.py Changes

The `_query` tool must route `api_type == "grpc"` to `process_grpc_query()`.

### middleware.py Changes

`_inject_api_context()` needs to handle `"grpc"` label ("gRPC"). `load_schema_and_base_url()` in `recipe/runner.py` also needs a gRPC branch, but recipe support is out of scope for v1.

### New Modules

```
api_agent/grpc/
├── __init__.py
├── client.py        # Channel management, unary call execution
└── reflection.py    # Reflection client: service/method discovery, FileDescriptorProto fetching

api_agent/agent/grpc_agent.py   # Agent loop (mirrors graphql_agent.py / rest_agent.py)
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| gRPC reflection not enabled on target server | Medium | High — agent can't discover schema | Graceful error: "Server reflection not enabled. Check server config." |
| protobuf 6.x API changes for dynamic message construction | Low | High | Pin to `protobuf>=5.0` and test on 6.x (already in use) |
| C extension wheel unavailability on exotic platforms | Low | Medium | Document requirement; `grpcio` has wheels for all mainstream platforms |
| TLS cert validation failure for internal servers | Medium | Medium | Expose `X-GRPC-TLS-Skip-Verify` header (dev/internal use only, log warning) |
| Streaming methods confuse agent (expects unary) | Medium | Low | Detect client/server/bidi streaming in schema representation, mark as `[streaming - unsupported in v1]`, guide LLM to skip |
| Dynamic message construction fails for complex nested types | Low | Medium | Integration tests with well-known gRPC services (helloworld, bookstore) |
| grpcio-reflection v1alpha vs v1 server mismatch | Low | Low | v1alpha is effectively universal; can add v1 fallback later |

---

## Recommendation

Build gRPC support for Ratatoskr using **grpcio + grpcio-reflection** with the following scope:

- v1: Unary RPC only, reflection-based discovery, JSON I/O, plaintext + TLS, header-based auth.
- v2: Streaming support, `_execute` tool, recipe extraction, mTLS.

This delivers maximal value (covers the vast majority of real-world gRPC use cases — unary is 80%+ of gRPC methods in practice) with a well-bounded implementation surface that mirrors the existing REST agent architecture.
