# Observability Review — Ratatoskr

**Reviewer:** Claude Code (Observability Engineering)
**Date:** 2026-03-04
**Scope:** Full codebase (`api_agent/` — 48 Python modules)
**Commit:** `65389b4` (main)

---

## Executive Summary

Ratatoskr has **minimal observability infrastructure**. The codebase relies almost entirely on Python's `logging` module with basic `%(asctime)s` formatting — no structured logging, no metrics, no correlation IDs, and tracing is optional/incomplete. For an open-source MCP server that proxies LLM agents to external APIs, the lack of observability will make production debugging extremely difficult. The tool-calling loop (up to 30 LLM turns per request) is essentially a black box.

**Severity Distribution:** 3 CRITICAL, 5 HIGH, 8 MEDIUM, 5 LOW, 4 INFO

---

## 1. Logging

### CRITICAL-01: No Structured Logging

**Files:** `api_agent/__main__.py:19-22`, all modules using `logging.getLogger()`

The entire application uses Python's basic `logging.basicConfig()` with a plain text format:

```python
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
```

**Impact:**
- Log aggregation tools (ELK, Loki, Datadog) cannot parse fields without regex
- No way to filter/query by request, API type, target URL, or user
- Multi-line stack traces break line-oriented parsers
- In a multi-request concurrent server, logs from different requests interleave without any way to distinguish them

**Recommendation:** Adopt `structlog` or `python-json-logger` for JSON-formatted structured logs. At minimum, add a JSON formatter option controlled by an env var (e.g., `API_AGENT_LOG_FORMAT=json`).

---

### CRITICAL-02: No Request Correlation IDs

**Files:** `api_agent/context.py`, `api_agent/middleware.py`, `api_agent/agent/orchestrator.py`

No correlation/trace ID is generated or propagated per request. When a single `_query` tool call triggers:
1. Schema fetch (HTTP/gRPC)
2. Schema reduction (Haiku LLM call)
3. Recipe search
4. 1-30 LLM turns, each with tool calls
5. Multiple external API calls (GraphQL/REST/gRPC)
6. DuckDB SQL executions
7. Recipe extraction (another LLM call)

...all log lines from all these steps are indistinguishable from concurrent requests.

**Impact:** In production with multiple concurrent MCP sessions, it is **impossible** to trace a single request through the system.

**Recommendation:**
- Generate a `request_id` (UUID4) in `get_request_context()` or middleware
- Store it in a `ContextVar` and inject it into all log records via a logging filter
- Forward it as a span attribute in OpenTelemetry traces
- Include it in error responses so clients can reference it in bug reports

---

### CRITICAL-03: Sensitive Data Leakage in Logs

**Files:** Multiple locations

Several modules log data that may contain sensitive information:

1. **`api_agent/__main__.py:157`** — Logs provider name (fine) but the pattern invites future additions:
   ```python
   logger.info(f"Provider: {reloaded.PROVIDER} | Model: {reloaded.MODEL_NAME or '(default)'}")
   ```

2. **`api_agent/rest/client.py:117-124`** — Good: explicitly logs only header keys, not values:
   ```python
   logger.info("REST request resolved: ... header_keys=%s", ..., sorted(request_headers.keys()))
   ```

3. **`api_agent/agent/orchestrator.py:359,477`** — Logs question content (may contain PII):
   ```python
   log(f"QUERY {question[:80]}")
   log(f"DONE calls={len(api_calls)} output={agent_output[:100]}")
   ```

4. **`api_agent/agent/graphql_agent.py:408`** — Logs query results (may contain user data):
   ```python
   _log(f"RESULT {json.dumps(result)[:200]}")
   ```

5. **`api_agent/agent/grpc_agent.py:374`** — Same pattern for gRPC:
   ```python
   _log(f"RPC {method_info.full_method_path} -> {json.dumps(result)[:200]}")
   ```

6. **`api_agent/tracing.py:57`** — Logs OTLP endpoint URL (usually internal infra):
   ```python
   logger.info(f"Tracing enabled: {otlp_endpoint}")
   ```

**Impact:** API responses proxied through the tool may contain user PII, authentication tokens in GraphQL variables, or sensitive business data. These get logged at DEBUG level (and some at INFO via the `make_logger` pattern).

**Recommendation:**
- Never log response payloads, even truncated, at INFO level
- Move all payload logging to DEBUG with a `[PAYLOAD]` prefix for easy filtering
- Add a `REDACT_LOGS` config flag that strips response data entirely
- Audit `X-Target-Headers` flow — these contain auth tokens and must never be logged

---

### HIGH-01: Debug Logging Gated by Settings, Not Log Level

**File:** `api_agent/agent/orchestrator.py:110-118`

```python
def make_logger(prefix: str) -> Callable[[str], None]:
    _logger = logging.getLogger(f"api_agent.agent.{prefix.strip('[]').lower()}")
    def _log(msg: str) -> None:
        if settings.DEBUG:
            _logger.info(f"{prefix} {msg}")
    return _log
```

This custom logger ignores Python's log level system entirely. When `settings.DEBUG` is False, these messages are silently dropped even if the logger is configured to INFO. When `settings.DEBUG` is True, they're emitted at INFO level (not DEBUG), polluting production logs.

**Impact:** Operators cannot selectively enable debug logging for specific modules using standard `LOGGING_CONFIG` or `logging.setLevel()`.

**Recommendation:** Replace with standard `logger.debug()` calls. Remove the `settings.DEBUG` gate — let Python's logging level handle it:
```python
def make_logger(prefix: str) -> Callable[[str], None]:
    _logger = logging.getLogger(f"api_agent.agent.{prefix.strip('[]').lower()}")
    def _log(msg: str) -> None:
        _logger.debug("%s %s", prefix, msg)
    return _log
```

---

### HIGH-02: f-strings in Logging Calls

**Files:** Throughout the codebase (>20 occurrences)

Most log calls use f-strings instead of `%`-style formatting:
```python
logger.info(f"Tracing enabled: {otlp_endpoint}")
logger.warning(f"Failed to setup tracing: {e}")
logger.exception(f"Failed to load OpenAPI spec: {e}")
```

**Impact:**
- String formatting occurs even when the log level is disabled (minor perf hit)
- Structured logging libraries cannot extract parameters from f-strings
- Some linting tools flag this as a code smell (ruff: `G004`)

**Recommendation:** Use `%`-style or structlog's key-value approach:
```python
logger.info("Tracing enabled: %s", otlp_endpoint)
```

---

## 2. Tracing

### HIGH-03: Tracing Only Wraps the Agent Loop — No Span Hierarchy

**File:** `api_agent/tracing.py`, `api_agent/agent/orchestrator.py:413`

The only tracing integration is:
```python
with trace_metadata({"mcp_name": settings.MCP_SLUG, "agent_type": config.agent_type}):
    result = await config.provider.run_tool_loop(...)
```

This creates metadata context for the OpenInference auto-instrumentor (OpenAI/Anthropic SDK calls), but there are **no explicit spans** for:

- **Schema fetching** (GraphQL introspection, OpenAPI loading, gRPC reflection)
- **Schema reduction** (TOON encoding + Haiku LLM call)
- **Individual tool executions** within the agent loop
- **External API calls** (GraphQL, REST, gRPC client calls)
- **DuckDB SQL execution**
- **Recipe search, extraction, and execution**
- **Middleware processing** (tool name transformation)

**Impact:** Even with OTLP enabled, operators see only the LLM provider calls (via auto-instrumentation), not the full request lifecycle.

**Recommendation:** Add spans at minimum for:
1. `api_agent.middleware.on_call_tool` — full MCP tool invocation
2. `api_agent.agent.orchestrator.run_agent_orchestration` — agent lifecycle
3. `api_agent.graphql.client.execute_query` — outbound GraphQL
4. `api_agent.rest.client.execute_request` — outbound REST
5. `api_agent.grpc.client.execute_*_rpc` — outbound gRPC
6. `api_agent.executor.execute_sql` — DuckDB queries
7. `api_agent.schema.reducer.reduce_schema` — schema reduction pipeline

---

### MEDIUM-01: `trace_span` Utility Exists but Is Never Used

**File:** `api_agent/tracing.py:77-103`

The `trace_span()` context manager is defined and tested but never called anywhere in the codebase. Only `trace_metadata()` is used (once).

**Impact:** Dead code that suggests tracing was planned but not implemented.

**Recommendation:** Either use `trace_span()` at the locations listed in HIGH-03, or remove it to avoid confusion.

---

### MEDIUM-02: No Trace Context Propagation to Downstream APIs

**Files:** `api_agent/graphql/client.py`, `api_agent/rest/client.py`, `api_agent/grpc/client.py`

Outbound HTTP calls via `httpx.AsyncClient` and gRPC calls do not propagate W3C `traceparent` headers. This means distributed traces break at Ratatoskr — downstream API servers cannot correlate their traces with Ratatoskr's.

**Impact:** In a microservices environment, the trace graph stops at Ratatoskr.

**Recommendation:**
- For HTTP (GraphQL/REST): Use `opentelemetry-instrumentation-httpx` or manually inject `traceparent` headers
- For gRPC: Use `opentelemetry-instrumentation-grpc` or inject via metadata

---

## 3. Metrics

### HIGH-04: No Metrics Instrumentation

**Files:** Entire codebase

There is zero metrics instrumentation. No counters, histograms, or gauges for:

| Metric | Why It Matters |
|--------|---------------|
| Request count by API type | Capacity planning, billing |
| Request latency (p50/p95/p99) | SLO monitoring |
| LLM turns per request | Cost control, prompt optimization |
| LLM token usage per request | Cost monitoring (missing — usage dict collected but discarded) |
| External API call count/latency | Dependency health |
| Schema size (chars/types/fields) | Schema reduction effectiveness |
| Recipe cache hit/miss rate | Cache tuning |
| Recipe extraction success/failure | Recipe system health |
| DuckDB query latency | Performance profiling |
| Error rate by type/protocol | Alerting |
| Blocked mutation count | Security audit |
| SSRF validation failures | Security audit |
| Endpoint allowlist filter ratios | Configuration validation |
| Schema reduction ratios | Reduction pipeline tuning |

**Impact:** Operators have no quantitative view of system behavior. Cannot set alerts, cannot tune, cannot do capacity planning.

**Recommendation:** Add Prometheus metrics via `prometheus-client` or OpenTelemetry Metrics API. At minimum:
1. `api_agent_requests_total{api_type, status}` — counter
2. `api_agent_request_duration_seconds{api_type}` — histogram
3. `api_agent_llm_turns_total{api_type}` — histogram
4. `api_agent_llm_tokens_total{provider, direction}` — counter
5. `api_agent_external_calls_total{protocol, status}` — counter
6. `api_agent_mutations_blocked_total{protocol}` — counter

---

### HIGH-05: LLM Token Usage Collected but Discarded

**Files:** `api_agent/llm/openai_provider.py:53-59`, `api_agent/llm/anthropic_provider.py:70-76`, `api_agent/llm/provider.py:113`

All three providers extract `usage` (prompt_tokens, completion_tokens) from LLM responses into `LLMResponse.usage`, but **no code ever reads it**. The `run_tool_loop()` discards it — `RunResult` doesn't include accumulated usage.

**Impact:** Token costs are the primary operational expense for this system, yet there's no way to track them.

**Recommendation:**
- Accumulate usage across turns in `run_tool_loop()` and include total in `RunResult`
- Log token usage at INFO level per request
- Emit as metrics (counter by provider, model)

---

## 4. Error Reporting

### MEDIUM-03: Exception Messages Expose Internal Details

**Files:** Multiple locations

Error messages returned to MCP clients sometimes include internal details:

1. **`api_agent/grpc/client.py:107`** — gRPC error details forwarded verbatim:
   ```python
   error_msg = f"gRPC error [{code.name}]: {details}"
   ```

2. **`api_agent/executor.py:226`** — DuckDB error messages forwarded:
   ```python
   return {"success": False, "error": f"SQL error: {e}"}
   ```

3. **`api_agent/agent/orchestrator.py:493`** — Generic exception `str(e)` forwarded:
   ```python
   "error": str(e),
   ```

4. **`api_agent/rest/client.py:154`** — Error body from upstream forwarded:
   ```python
   return {"success": False, "error": f"HTTP {e.response.status_code}: {error_body}"}
   ```

**Impact:** While these are tool responses consumed by LLM agents (not direct user output), they could leak internal server details, file paths, or stack traces into the MCP response visible to client applications.

**Recommendation:** For errors returned in MCP tool responses:
- Log the full exception details server-side
- Return a sanitized error message to the client with a correlation ID
- Consider an `error_code` enum for programmatic error handling

---

### MEDIUM-04: Inconsistent Error Response Shapes

**Files:** All agent and tool files

Error responses use different shapes depending on the layer:

```python
# MCP tools (query.py, execute.py)
{"ok": False, "error": "..."}

# Protocol clients (graphql/client.py, rest/client.py, grpc/client.py)
{"success": False, "error": "..."}

# Agent tools (within tool loop)
{"success": False, "error": "..."}

# Orchestrator
{"ok": False, "data": None, "error": "..."}
```

**Impact:** No consistent error contract. Consumers must handle multiple shapes. The `ok`/`success` key difference between MCP responses and internal tool responses is particularly confusing.

**Recommendation:** Define a standard error response type and use it consistently. At minimum, document the contract.

---

### MEDIUM-05: `logger.exception()` Used Inconsistently

**Files:** Various

Some exception handlers use `logger.exception()` (includes stack trace):
```python
# api_agent/agent/orchestrator.py:493
logger.exception(f"{config.log_prefix} Agent error")
```

Others use `logger.warning()` without traceback:
```python
# api_agent/tracing.py:59
logger.warning(f"Failed to setup tracing: {e}")
```

And some silently swallow exceptions:
```python
# api_agent/tools/query.py:71
except Exception:
    logger.debug("Failed to send tool list changed notification", exc_info=True)
```

**Impact:** When debugging production issues, some exceptions have stack traces and others don't, making root-cause analysis inconsistent.

**Recommendation:** Establish conventions:
- `logger.exception()` for unexpected errors that need investigation
- `logger.warning(..., exc_info=True)` for expected/recoverable errors where context helps
- `logger.debug()` for intentionally suppressed errors

---

## 5. Debugging Support

### MEDIUM-06: Agent Tool Loop Is a Black Box

**File:** `api_agent/llm/provider.py:73-149`

The core `run_tool_loop()` method has **zero logging**. During a 30-turn agent execution, there is no visibility into:
- Which turn is executing
- What tool calls the LLM requested
- What the tool returned (success/failure)
- How many tokens each turn consumed
- Why the loop terminated (max turns? direct return? no tool calls?)

The only logging is `logger.exception(f"Tool {tc.name} failed")` on tool execution errors.

**Impact:** The most complex and expensive part of the system (the agent loop) is completely opaque.

**Recommendation:** Add structured logging at each turn boundary:
```python
logger.debug("Turn %d/%d: LLM requested %d tool calls: %s",
    turns, max_turns, len(response.tool_calls),
    [tc.name for tc in response.tool_calls])
```

---

### MEDIUM-07: No Request Timing/Duration Logging

**Files:** All agent entry points

No timing information is logged for any operation:
- Schema fetch duration
- Schema reduction duration
- Agent loop total duration
- Individual tool call duration
- External API call duration

The only timing hint is the DuckDB timeout (implicit via httpx 30s timeout).

**Impact:** Cannot identify slow operations without metrics or tracing.

**Recommendation:** At minimum, log wall-clock duration for the top-level `process_query`/`process_rest_query`/`process_grpc_query` calls.

---

### MEDIUM-08: No Startup Validation Logging

**File:** `api_agent/__main__.py:141-159`

The server starts with minimal logging:
```python
logger.info(f"Starting API Agent on {host}:{port}")
logger.info(f"Provider: {reloaded.PROVIDER} | Model: {reloaded.MODEL_NAME or '(default)'}")
```

Missing from startup logs:
- Python version
- Package version
- Whether recipes are enabled
- Whether schema reduction is enabled
- SSRF protection status (block_private_ips, allowed hosts)
- Endpoint allowlist configuration
- Transport type
- CORS configuration
- Whether tracing is enabled (logged separately but only if successful)

**Impact:** When troubleshooting, operators must guess what configuration the running instance has.

**Recommendation:** Log a structured startup summary with all non-secret configuration values.

---

## 6. Health Checks

### LOW-01: Health Check Is Shallow (Liveness Only)

**File:** `api_agent/__main__.py:134-137`, `healthcheck.sh`

```python
async def health(request):
    return JSONResponse({"status": "ok"})
```

The `/health` endpoint always returns 200. It does not check:
- LLM provider connectivity (can we reach OpenAI/Anthropic?)
- Whether the provider is configured (API key present)
- Memory usage (recipe store size)

**Impact:** Container orchestrators (K8s) will consider the pod healthy even if the LLM provider is unreachable.

**Recommendation:** Add a `/ready` endpoint that validates:
1. LLM provider API key is configured (non-empty)
2. Optionally: a lightweight LLM ping (e.g., empty completion with max_tokens=1)

Keep `/health` as liveness (always 200 if process is running). Add `/ready` as readiness.

---

### LOW-02: No `/metrics` Endpoint

**Impact:** No Prometheus scrape target. Requires out-of-band metrics collection if added.

**Recommendation:** If metrics are added (HIGH-04), expose via `/metrics` endpoint or push to OTLP collector.

---

## 7. Audit Trail

### LOW-03: Mutation Blocks Logged at Wrong Level

**Files:** `api_agent/graphql/client.py:44`, `api_agent/rest/client.py:100-106`, `api_agent/agent/grpc_agent.py:106-116`

When mutations are blocked, the response is returned to the tool caller (the LLM agent) but **not logged** at all. The blocked mutation is invisible to operators.

**Impact:** Security-relevant events (mutation attempts) are not tracked. Cannot answer "was there an attempt to modify data?"

**Recommendation:** Log all blocked mutations at WARN level with:
- API type (GraphQL/REST/gRPC)
- Method/query that was blocked
- Target URL (without auth headers)
- Correlation ID (once CRITICAL-02 is addressed)

---

### LOW-04: SSRF Validation Failures Not Logged

**File:** `api_agent/context.py:31-91`

`validate_target_url()` raises `MissingHeaderError` on SSRF violations, but the exception message is returned to the caller without any server-side logging of the attempt.

**Impact:** SSRF probe attempts are invisible to operators. No way to detect scanning or attack patterns.

**Recommendation:** Log all SSRF validation failures at WARN level before raising:
```python
logger.warning("SSRF blocked: scheme=%s host=%s reason=%s", parsed.scheme, hostname, reason)
```

---

### LOW-05: No Audit of Which APIs Were Called

**Files:** `api_agent/agent/orchestrator.py`

While each agent tracks its API calls in context vars (e.g., `_graphql_queries`, `_rest_calls`), these are only used for recipe extraction and response building. There is no server-side audit log of what external APIs were called.

**Impact:** Cannot answer "what did this server call in the last hour?" without client-side logs.

**Recommendation:** Log a summary at the end of each request:
```python
logger.info("Request complete: api_type=%s target=%s calls=%d turns=%d duration_ms=%d",
    api_type, target_url, len(api_calls), turns_used, duration_ms)
```

---

## 8. Additional Observations

### INFO-01: Schema Reduction Pipeline Has Good Logging

**File:** `api_agent/schema/reducer.py`

The schema reduction pipeline is the best-instrumented part of the codebase:
- Logs TOON encoding results with sizes and percentages
- Logs Haiku reduction results with sizes and percentages
- Logs warnings on suspicious outputs (too short, too long)
- Final summary log with all reduction metadata

This is a good pattern to replicate elsewhere.

---

### INFO-02: REST Client Has Good Request Logging

**File:** `api_agent/rest/client.py:117-124`

The REST client correctly logs request details without leaking auth:
```python
logger.info("REST request resolved: method=%s base_url=%s path=%s url=%s header_keys=%s", ...)
```

This pattern should be replicated in the GraphQL and gRPC clients.

---

### INFO-03: Recipe Store Debug Logging Is Well-Structured

**File:** `api_agent/recipe/store.py:24-27`

```python
def _log_recipe(msg: str) -> None:
    if settings.DEBUG:
        logger.info(f"[Recipe] {msg}")
```

While this has the same DEBUG-gate issue as HIGH-01, the `[Recipe]` prefix and structured messages (SAVE, SUGGEST) are a good pattern.

---

### INFO-04: OpenInference Auto-Instrumentation Is a Good Start

**File:** `api_agent/tracing.py:40-53`

The auto-instrumentation of OpenAI and Anthropic SDK clients via OpenInference is well-implemented:
- Conditional import (no hard dependency)
- Graceful fallback on ImportError
- Shared TracerProvider
- Good error handling

This gives basic LLM call visibility when OTLP is configured.

---

## Priority Roadmap

### Phase 1 — Essential (blocks production use)
1. **CRITICAL-02**: Add request correlation IDs
2. **CRITICAL-01**: Add structured JSON logging option
3. **CRITICAL-03**: Audit and gate sensitive data in logs
4. **HIGH-01**: Fix debug logging to use standard log levels

### Phase 2 — Operational Visibility
5. **HIGH-03**: Add tracing spans for key operations
6. **HIGH-04**: Add core metrics (request count, latency, error rate)
7. **HIGH-05**: Track and expose LLM token usage
8. **MEDIUM-06**: Add logging to the tool loop

### Phase 3 — Production Hardening
9. **MEDIUM-02**: Propagate trace context to downstream APIs
10. **LOW-01**: Add readiness check endpoint
11. **LOW-03/04**: Log security events (mutations, SSRF)
12. **LOW-05**: Add request completion audit log
13. **MEDIUM-03/04**: Standardize error response shapes

### Phase 4 — Polish
14. **MEDIUM-01**: Use or remove `trace_span()`
15. **MEDIUM-05**: Standardize exception handling patterns
16. **MEDIUM-07/08**: Add timing and startup summary logging
17. **HIGH-02**: Fix f-string logging (can be done incrementally with ruff rule)

---

## Appendix: File-by-File Logging Inventory

| Module | Logger | Log Calls | Structured? | Sensitive Data Risk |
|--------|--------|-----------|-------------|-------------------|
| `__main__.py` | `api_agent.__main__` | 3 INFO | No | Low (config only) |
| `tracing.py` | `api_agent.tracing` | 4 (debug/info/warning) | No | Low |
| `config.py` | None | 0 | N/A | N/A |
| `context.py` | None | 0 | N/A | N/A |
| `middleware.py` | None | 0 | N/A | N/A |
| `orchestrator.py` | `api_agent.agent.orchestrator` | 1 exception + custom | No | Medium (query text, output) |
| `graphql_agent.py` | `api_agent.agent.graphql_agent` | 2 info + custom | No | High (query results) |
| `rest_agent.py` | `api_agent.agent.rest_agent` | 1 exception + custom | No | High (API results) |
| `grpc_agent.py` | `api_agent.agent.grpc_agent` | 2 info + custom | No | High (RPC results) |
| `provider.py` | `api_agent.llm.provider` | 1 exception | No | Medium (tool errors) |
| `openai_compat.py` | `api_agent.llm.openai_compat` | 1 warning | No | Low |
| `graphql/client.py` | `api_agent.graphql` | 1 exception | No | Low |
| `rest/client.py` | `api_agent.rest.client` | 2 info + 1 exception | Partial | Medium (URLs) |
| `grpc/client.py` | `api_agent.grpc.client` | 0 (errors in returns) | N/A | Low |
| `executor.py` | `api_agent.executor` | 2 exception | No | Low |
| `schema/reducer.py` | `api_agent.schema.reducer` | 8 (info/warning/debug) | No | Low |
| `rest/schema_loader.py` | `api_agent.rest.schema_loader` | 2 warning + 1 exception | No | Low |
| `recipe/store.py` | `api_agent.recipe.store` | Custom `_log_recipe` | No | Low |
| `recipe/extractor.py` | None | 0 | N/A | N/A |
| `filtering.py` | `api_agent.filtering` | 0 | N/A | N/A |
| `tools/query.py` | `api_agent.tools.query` | 1 debug | No | Low |
| `tools/execute.py` | `api_agent.tools.execute` | 0 | N/A | N/A |

**Total log statements:** ~30 across 48 modules. For comparison, a well-instrumented service of this complexity would have 100-200.
