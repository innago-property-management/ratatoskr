# Open Source Readiness — Work Plan

**Version:** 2.0
**Date:** 2026-03-04
**Updated:** 2026-03-04 (PRs A-D executed)
**Source:** 5-panel Cygnus review (security, code, SRE, observability, best-practices)
**Raw findings:** 199 (12 CRITICAL, 37 HIGH, 63 MEDIUM, 45 LOW, 42 INFO)

## Batched PRs (Priority Order)

### PR A: Connection Pooling (CRIT-1, CRIT-2) — 5 pts — **PR #36 MERGED**
- [x] httpx.AsyncClient shared per-target (ConnectionPool class)
- [x] gRPC channel reuse per-target
- [x] Connection pool lifecycle tied to server lifecycle (atexit + shutdown)
- [x] Tests: mock transport, not client construction (909 tests)
- [x] Review: dead code removed, Py3.12 compat, configurable timeout
- **TODO (future):** Add client.limits assertions, LRU eviction cap for multi-host

### PR B: ContextVar Safety (CRIT-4, HIGH-8) — 8 pts — **PR #37 (in review)**
- [x] Use task-scoped ContextVars (copy_context() + create_task)
- [x] Remove module-level reset_context_vars() pattern
- [x] Fix _return_directly_flag as proper ContextVar
- [x] OrchestrationResult.from_contextvars() classmethod
- [x] SchemaFetchResult dataclass for typed returns
- [ ] Review round 3: missing result key in error path, unused import (agent running)

### PR C: Structured Logging + Correlation IDs (CRIT-6, CRIT-7, CRIT-8, HIGH-16, HIGH-17) — 8 pts — **PR #38 (in review)**
- [x] Switch to structlog with JSON output
- [x] Add request_id to RequestContext, UUID4 validation
- [x] Replace f-strings with structured events
- [x] Redact sensitive data (exact + substring + nested dicts)
- [x] Fix log levels, add exc_info=True to tracing
- [ ] Review round 3: merge conflicts + key substring, url comment (agent running)

### PR D: Security Hardening (HIGH-1, HIGH-2, HIGH-5, HIGH-22, CRIT-5, CRIT-9) — 5 pts — **PR #35 (in review)**
- [x] Sanitize schema descriptions before LLM prompt injection
- [x] GraphQL recipe template injection protection
- [x] DuckDB: block DDL/DML + semicolons + CTE-wrapped DML
- [x] Limit str(e) leak to LLM context
- [x] Add pip-audit to CI
- [ ] Review round 3: graphql error guard, narrow patterns, truncation fix (agent running)

### PR E: Resource Management (HIGH-3, HIGH-5, HIGH-6, HIGH-4) — 5 pts
- [ ] Wrap DuckDB execute in asyncio.to_thread()
- [ ] Bound concurrent DuckDB connections (semaphore)
- [ ] Temp file size limits + cleanup on crash
- [ ] Graceful shutdown (signal handler, connection draining)

### PR F: Recipe Store Fixes (HIGH-7, HIGH-26, HIGH-27, CRIT-10) — 3 pts
- [ ] Document single-tenant assumption (or add namespace)
- [ ] Replace threading.Lock with asyncio.Lock
- [ ] Add max-attempts guard to deduplicate_tool_name
- [ ] Document render_sql_safe limitations

### PR G: Dependency & Packaging (CRIT-3, HIGH-29, HIGH-30) — 3 pts
- [ ] Vendor toon_format or make it optional
- [ ] Add upper bounds to major deps (fastmcp<4, openai<2, etc.)
- [ ] Add pip-audit / CodeQL to CI workflow
- [ ] Configure pytest-asyncio mode explicitly

### PR H: Config & Error Handling (HIGH-18, HIGH-19, HIGH-21, HIGH-25) — 3 pts
- [ ] Fix stale settings singleton (lazy property or function)
- [ ] Create proper exception hierarchy (ValidationError vs MissingHeaderError)
- [ ] Replace __DIRECT_RETURN__ magic string with sentinel object
- [ ] Consistent error return shapes across agents

### PR I: Best Practices Polish (MEDIUM/LOW items) — 2 pts
- [ ] Move FakeLLMProvider to single shared location
- [ ] Fix shadowed fixtures
- [ ] ContextVar cleanup in tests
- [ ] Monolithic execute() refactor
- [ ] Mutable list dual-contract in providers

### PR J: Observability Instrumentation (HIGH-13, HIGH-14, HIGH-15) — 5 pts
- [ ] Add OpenTelemetry metrics (counters, histograms)
- [ ] Track LLM token usage (expose from LLMResponse.usage)
- [ ] Add spans for schema fetch, reduction, tool execution, DuckDB
- [ ] /metrics endpoint (Prometheus format)

### PR K: Deployment Artifacts (HIGH-10, HIGH-11) — 3 pts
- [ ] Example Helm chart or Kustomize overlay
- [ ] Read-only filesystem in container
- [ ] Resource requests/limits examples
- [ ] Example k8s health probes

### Deferred (post-launch)
- DNS rebinding SSRF (HIGH-2) — requires connect-time IP check, complex
- LLM retry configuration — SDK-specific, document for now
- Schema caching in middleware (HIGH-20) — needs invalidation strategy
- Rate limiting — deployment-specific (ingress/API gateway)

## Execution Order
A → B → C → D → E → F → G → H → I → J → K

PRs A-D are the critical path for open-source. E-H are important. I-K are polish.
