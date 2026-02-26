# PLAN: gRPC Protocol Support

**Version:** 1.0
**Date:** 2026-02-26
**Based on:** DESIGN.md v1.0

## Implementation Order (TDD, front-to-back)

### Phase 1: Foundation (deps + context)
1. Add `grpcio>=1.70.0` and `grpcio-reflection>=1.70.0` to pyproject.toml
2. Update `context.py` to accept `api_type="grpc"` + tests
3. Create `api_agent/grpc/__init__.py` (empty)

### Phase 2: Reflection (schema discovery)
4. Write tests for `grpc/reflection.py` — URL parsing, schema formatting
5. Implement `grpc/reflection.py` — reflection client, schema building

### Phase 3: Client (RPC execution)
6. Write tests for `grpc/client.py` — unary call, error handling
7. Implement `grpc/client.py` — channel management, serialization pipeline

### Phase 4: Agent (orchestration)
8. Write tests for `agent/grpc_agent.py` with FakeLLMProvider
9. Implement `agent/grpc_agent.py` — tool loop, system prompt, grpc_call tool

### Phase 5: Integration (routing + middleware)
10. Update `tools/query.py` to route `grpc` to grpc_agent + tests
11. Update `middleware.py` for gRPC label
12. Update `recipe/runner.py` with gRPC no-op branch

### Phase 6: Validation
13. Full test suite (existing 511 + new gRPC tests)
14. Lint + type check
15. PR
