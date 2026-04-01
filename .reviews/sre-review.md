# SRE Review: Ratatoskr (api-agent-ratatoskr)

**Date:** 2026-03-04
**Reviewer:** Claude Code (SRE review agent)
**Scope:** Full codebase review for open-source production readiness
**Commit:** `65389b4` (main)

---

## Executive Summary

Ratatoskr is a Python MCP server (FastMCP 3.0) that proxies LLM agents to external APIs (GraphQL, REST, gRPC), with DuckDB for SQL post-processing. The codebase is well-structured with strong test coverage (848 tests), good security controls (SSRF protection, mutation blocking, DuckDB sandboxing), and a clean multi-stage Dockerfile. However, several production readiness gaps exist around **resource management**, **graceful shutdown**, **horizontal scalability**, and **deployment manifests**.

### Severity Distribution

| Severity | Count |
|----------|-------|
| CRITICAL | 2 |
| HIGH | 6 |
| MEDIUM | 8 |
| LOW | 5 |
| INFO | 6 |

---

## 1. Dockerfile & Container

### [HIGH] F-01: No read-only filesystem or tmpdir restriction

The container runs as `appuser` (good), but the filesystem is writable. DuckDB creates temp files via `tempfile.NamedTemporaryFile` in `/tmp` which is fine, but there's no restriction on writes elsewhere.

**File:** `Dockerfile:37-51`

**Recommendation:**
```dockerfile
# Add to runtime stage
RUN mkdir -p /tmp/duckdb && chown appuser:appuser /tmp/duckdb
# In k8s: readOnlyRootFilesystem: true with /tmp as emptyDir
```

### [MEDIUM] F-02: Base image not pinned to digest

Using `python:3.11.15-slim` is good (specific patch version), but not pinned to a SHA256 digest. A supply chain attack could replace the tag.

**File:** `Dockerfile:2,26`

**Recommendation:** Pin to digest for production builds:
```dockerfile
FROM python:3.11.15-slim@sha256:<digest> AS builder
```

### [LOW] F-03: uv binary version pinned (good)

`COPY --from=ghcr.io/astral-sh/uv:0.9.30` — this is correctly pinned. No action needed.

### [INFO] F-04: Multi-stage build quality is good

- Builder stage correctly separates deps from source (cache-friendly)
- git only in builder (not in runtime)
- Non-root user (`appuser`) with `--system --no-create-home`
- HEALTHCHECK configured with reasonable intervals (30s/5s/3 retries)
- Multi-arch build (amd64/arm64) in release pipeline

### [MEDIUM] F-05: HEALTHCHECK uses Python import overhead

`healthcheck.sh` spawns a full Python interpreter for each check. Under resource constraints this could be slow.

**File:** `healthcheck.sh:2`

**Recommendation:** Consider `curl` or a statically-linked binary for healthchecks, or accept the ~200ms Python startup overhead as acceptable.

---

## 2. Resource Management

### [CRITICAL] R-01: httpx clients created per-request, no connection pooling

Every GraphQL query and REST API call creates a new `httpx.AsyncClient` via `async with httpx.AsyncClient(timeout=30.0) as client:`. This means:
- New TCP connection per request (no keepalive reuse)
- New TLS handshake per HTTPS request
- Under load, connection storm to target APIs

**Files:** `api_agent/graphql/client.py:54`, `api_agent/rest/client.py:126`

**Impact:** Significant latency increase under load; potential connection exhaustion on target APIs.

**Recommendation:** Create a shared `httpx.AsyncClient` per target URL (or a small pool) with connection limits:
```python
# Singleton or per-target-url pooled client
_client = httpx.AsyncClient(
    timeout=30.0,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)
```
The client should be created at startup and closed on shutdown.

### [CRITICAL] R-02: gRPC channels created and closed per-RPC call

Every gRPC call in `api_agent/grpc/client.py` creates a new channel, executes one RPC, and closes it. gRPC channels are designed to be long-lived and multiplexed.

**File:** `api_agent/grpc/client.py:84,114,183,240,304,342,412,470`

**Impact:** Each call incurs full TCP+TLS+HTTP/2 setup. Under concurrent load this will be extremely slow and may exhaust file descriptors.

**Recommendation:** Implement a channel pool keyed by `(target, tls)` with TTL-based eviction:
```python
class ChannelPool:
    def __init__(self, max_idle_seconds=300):
        self._channels: dict[tuple[str, bool], grpc.aio.Channel] = {}

    async def get(self, target: str, tls: bool) -> grpc.aio.Channel:
        key = (target, tls)
        if key not in self._channels:
            self._channels[key] = _create_channel(target, tls)
        return self._channels[key]
```

### [HIGH] R-03: DuckDB connections not bounded

`executor.py` creates a new in-memory DuckDB connection per SQL execution and per schema extraction. While connections are closed in `finally` blocks (good), there's no concurrency limit. Under load, many concurrent agent turns could create dozens of DuckDB instances simultaneously.

**Files:** `api_agent/executor.py:19-22,96,190`

**Impact:** Memory pressure under concurrent requests. Each DuckDB instance allocates its own buffer pool.

**Recommendation:** Add a semaphore to bound concurrent DuckDB operations:
```python
_duckdb_semaphore = asyncio.Semaphore(10)  # Max 10 concurrent DuckDB sessions

async def execute_sql_bounded(data, query):
    async with _duckdb_semaphore:
        return execute_sql(data, query)
```

### [HIGH] R-04: Temp file cleanup relies on finally blocks only

DuckDB operations write JSON data to temp files (`tempfile.NamedTemporaryFile(delete=False)`). If the process crashes between creation and cleanup, files accumulate. In a long-running container, this could fill `/tmp`.

**Files:** `api_agent/executor.py:92,197-198,205-206`

**Recommendation:**
1. Set `TMPDIR` env var to a dedicated directory
2. Add periodic cleanup of stale temp files (cron or background task)
3. Consider using DuckDB's `read_json_auto()` with stdin/memory instead of temp files where possible

### [MEDIUM] R-05: LLM provider is a global singleton — no per-request timeout override

The LLM provider (`api_agent/agent/model.py`) is a lazy singleton. All requests share the same client instance, which is correct for connection pooling, but there's no per-request timeout. A slow LLM response blocks the agent turn.

**File:** `api_agent/agent/model.py:12-53`

**Recommendation:** Add configurable timeout to `LLMProvider.complete()` and propagate from request context.

---

## 3. Graceful Shutdown

### [HIGH] G-01: No graceful shutdown handling

There is **zero** signal handling in the codebase. The `start.sh` uses `exec` (good — PID 1 receives signals), and uvicorn has basic SIGTERM handling, but:

1. No application-level shutdown hook to drain in-flight agent loops
2. No cleanup of gRPC channels (if pooled in future)
3. No cleanup of httpx clients (if pooled in future)
4. The tool-calling loop (`provider.py:106-149`) runs up to 30 turns with no cancellation check

**Files:** `start.sh:5`, `api_agent/__main__.py:159`

**Impact:** On pod termination, in-flight LLM agent loops (which can run 30 turns × ~5s each = 2.5 minutes) will be hard-killed. Users get no response. LLM API calls may be billed but results discarded.

**Recommendation:**
```python
import signal
import asyncio

shutdown_event = asyncio.Event()

def _handle_signal(sig, frame):
    shutdown_event.set()

signal.signal(signal.SIGTERM, _handle_signal)

# In the tool loop, check:
if shutdown_event.is_set():
    return partial_result  # Return what we have
```

Also set `terminationGracePeriodSeconds: 180` in k8s pod spec to allow agent loops to complete.

### [MEDIUM] G-02: No request draining on shutdown

uvicorn's default shutdown behavior is to stop accepting new connections but doesn't wait for SSE/streaming responses to complete. MCP uses `streamable-http` transport which may have long-lived connections.

**Recommendation:** Configure uvicorn with `--timeout-graceful-shutdown 120` or equivalent programmatic config.

---

## 4. Resilience

### [HIGH] R-06: No retry logic on LLM API calls

LLM providers (`openai_provider.py`, `anthropic_provider.py`, `openai_compat.py`) make a single `await self.client...create()` call with no retry on transient failures (429, 500, 502, 503, network timeouts).

**Files:** `api_agent/llm/openai_provider.py:40`, `api_agent/llm/anthropic_provider.py:52`

**Impact:** A single LLM API hiccup fails the entire multi-turn agent loop. OpenAI and Anthropic SDKs have built-in retry logic, but it's not configured or documented.

**Note:** Both `openai` and `anthropic` Python SDKs do include automatic retry with exponential backoff by default. Verify this is sufficient and document the behavior. Consider adding `max_retries` configuration.

### [MEDIUM] R-07: No circuit breaker on target API calls

If a target API is down, every request will wait 30s (httpx timeout) before failing. With the 30-turn agent loop, a single query could take up to 15 minutes of retrying against a dead endpoint.

**Recommendation:** Implement a simple circuit breaker per target URL:
- After N consecutive failures, short-circuit for M seconds
- Return fast error instead of waiting for timeout

### [LOW] R-08: Agent turn limit is the only backpressure mechanism

`MAX_AGENT_TURNS=30` caps the agent loop, which is good. But there's no limit on concurrent requests or concurrent agent loops. A burst of requests could spawn many parallel agent loops, each making LLM calls and target API calls.

**Recommendation:** Add a concurrency limiter (semaphore) at the query tool level:
```python
_agent_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_AGENTS or 10)
```

### [MEDIUM] R-09: OpenAI-compat retry-without-tools is broad

`openai_compat.py:53` catches any exception where "tool" or "function" appears in the error message and retries without tools. This is clever but could mask real errors.

**File:** `api_agent/llm/openai_compat.py:52-57`

**Recommendation:** Narrow the exception matching to specific HTTP status codes (400, 422) rather than string matching on any exception.

---

## 5. Configuration

### [INFO] C-01: 12-factor compliance is good

- Settings via `pydantic-settings` with `API_AGENT_` env prefix ✓
- `.env` file support for local dev ✓
- CLI args override env vars ✓
- No hardcoded secrets ✓
- Port configurable via `PORT` env var ✓

### [MEDIUM] C-02: API key logged in provider creation path

While the key isn't explicitly logged, the `Settings` object is created multiple times (`config.py:106`, `__main__.py:149`, `model.py:30`) and the key is stored as a plain attribute. If debug logging is enabled and an exception occurs in settings creation, the key could appear in stack traces.

**Recommendation:**
1. Mark `API_KEY` field as `repr=False` in pydantic:
   ```python
   API_KEY: str = Field(default="", repr=False, ...)
   ```
2. Consider using `SecretStr` type for key fields.

### [LOW] C-03: CORS allows all origins by default

`CORS_ALLOWED_ORIGINS: str = "*"` allows any origin. For an MCP server this may be intentional (clients vary), but should be documented as a conscious security decision.

**File:** `api_agent/config.py:63`

### [INFO] C-04: Settings singleton created at module import time

`settings = Settings()` at `config.py:106` runs at import time before CLI overrides. The code works around this by creating new `Settings()` instances after overrides (`__main__.py:149`, `model.py:30`). This is functional but fragile.

**Recommendation:** Consider a `get_settings()` function pattern for cleaner lazy initialization.

### [LOW] C-04: toon_format pinned to git commit hash

```
toon_format @ git+https://github.com/toon-format/toon-python.git@6b26984a
```

Pinning to a commit hash is good for reproducibility, but:
- No PyPI package available = harder to audit
- If the repo is deleted, builds break
- The `uv.lock` mitigates this somewhat

**File:** `pyproject.toml:34`

---

## 6. Scalability

### [HIGH] S-01: Global mutable state prevents horizontal scaling

Several global singletons hold mutable state:

| State | Location | Impact |
|-------|----------|--------|
| `RECIPE_STORE` | `recipe/store.py:390` | In-memory LRU cache, not shared across pods |
| `_provider` | `agent/model.py:12` | Singleton LLM client (acceptable) |
| `_tracer_ready` | `tracing.py:12` | Per-process flag (acceptable) |

The `RECIPE_STORE` is the main concern. Recipes learned by one pod are invisible to others. Cache warm-up is lost on pod restart.

**Impact:** In a multi-pod deployment, recipe cache hit rate drops proportionally. No data loss (recipes are an optimization), but degraded performance.

**Recommendation (ordered by effort):**
1. **Accept it** — for single-tenant deployments, in-memory is fine. Document the limitation.
2. **Redis/Valkey backend** — share recipes across pods with TTL-based eviction.
3. **Sticky sessions** — route same API to same pod (viable with k8s session affinity).

### [MEDIUM] S-02: ContextVar-based request isolation is correct but fragile

The codebase correctly uses `ContextVar` for per-request state (`_graphql_queries`, `_query_results`, etc.) and documents the mutable-container pattern for child tasks. This is well-designed for async concurrency.

However, module-level ContextVar instances in `graphql_agent.py` (lines 54-59) are shared across all requests. If someone accidentally calls `.set()` without the proper task context, it would corrupt state.

**Status:** Currently correct. No action needed, but worth documenting the invariant.

### [INFO] S-03: No WebSocket or long-polling state

The MCP `streamable-http` transport is stateless per-request. No sticky session requirement for the transport layer itself.

---

## 7. CI/CD

### [INFO] CI-01: Test workflow is solid

- Matrix: Python 3.11, 3.12 ✓
- Tests, linting, type checking ✓
- Docker build smoke test ✓
- Actions pinned to SHA (not just tags) ✓
- Minimal permissions (`contents: read`) ✓

### [INFO] CI-02: Release pipeline is well-designed

- PyPI via Trusted Publishing (OIDC, no tokens) ✓
- GHCR with semver tags + latest ✓
- Multi-arch (amd64/arm64) ✓
- GHA build cache ✓
- Parallel jobs (PyPI + Docker) ✓

### [LOW] CI-03: Docker smoke test is weak

The CI Docker test (`test.yml:53-60`) only checks that the container starts and doesn't crash within 5 seconds. It doesn't verify the health endpoint responds.

**Recommendation:**
```yaml
- name: Test Docker image health
  run: |
    docker run -d --name test-container -e OPENAI_API_KEY=test -p 3000:3000 api-agent:test
    for i in $(seq 1 30); do
      curl -sf http://localhost:3000/health && exit 0
      sleep 1
    done
    docker logs test-container
    exit 1
```

### [MEDIUM] CI-04: No SBOM or vulnerability scanning in pipeline

No Trivy, Grype, or Snyk scan on the Docker image. No SBOM generation.

**Recommendation:** Add to release pipeline:
```yaml
- name: Run Trivy vulnerability scanner
  uses: aquasecurity/trivy-action@master
  with:
    image-ref: ghcr.io/${{ github.repository }}:${{ steps.meta.outputs.version }}
    format: 'sarif'
    output: 'trivy-results.sarif'
```

---

## 8. Deployment Readiness

### [HIGH] D-01: No Kubernetes manifests, Helm chart, or deployment configuration

There are no k8s manifests, Helm charts, Kustomize overlays, or any deployment configuration in the repository. For an open-source project targeting Docker/Kubernetes, this is a significant gap.

**Recommendation:** At minimum, provide:

```yaml
# deploy/deployment.yaml (example)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ratatoskr
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: ratatoskr
        image: ghcr.io/innago-property-management/ratatoskr:latest
        ports:
        - containerPort: 3000
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: "1"
            memory: 512Mi
        livenessProbe:
          httpGet:
            path: /health
            port: 3000
          initialDelaySeconds: 10
          periodSeconds: 15
        readinessProbe:
          httpGet:
            path: /health
            port: 3000
          initialDelaySeconds: 5
          periodSeconds: 5
        env:
        - name: API_AGENT_API_KEY
          valueFrom:
            secretKeyRef:
              name: ratatoskr-secrets
              key: api-key
      terminationGracePeriodSeconds: 180
```

### [INFO] D-02: Health endpoint is minimal

`/health` returns `{"status": "ok"}` unconditionally. It doesn't check:
- LLM provider connectivity
- Whether the provider is configured (API key present)

For a liveness probe this is fine (cheap, fast). Consider adding a `/ready` endpoint that validates provider configuration for readiness probes.

---

## 9. Observability

### [MEDIUM] O-01: Logging is basic stdlib — no structured output

Uses `logging.basicConfig` with plain text format. In a containerized environment, structured JSON logging is strongly preferred for log aggregation (Loki, Datadog, CloudWatch).

**File:** `api_agent/__main__.py:19-22`

**Recommendation:**
```python
import json, logging

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })
```

Or use `python-json-logger` for production deployments.

### [LOW] O-02: No metrics endpoint

No Prometheus metrics, no `/metrics` endpoint. Key metrics to track:
- `agent_requests_total` (by protocol, status)
- `agent_turns_histogram` (turns per request)
- `llm_api_duration_seconds` (by provider)
- `target_api_duration_seconds` (by target URL)
- `recipe_cache_hits_total` / `recipe_cache_misses_total`
- `duckdb_queries_total`

### [INFO] O-03: OpenTelemetry tracing is optional and well-implemented

Tracing via OTLP is cleanly optional (`tracing.py`), auto-instruments OpenAI/Anthropic SDKs when available, and degrades gracefully. Good implementation.

---

## Summary of Recommended Actions

### Immediate (before production)

| ID | Severity | Action |
|----|----------|--------|
| R-01 | CRITICAL | Implement httpx connection pooling |
| R-02 | CRITICAL | Implement gRPC channel pooling |
| G-01 | HIGH | Add graceful shutdown with signal handling |
| R-03 | HIGH | Bound concurrent DuckDB operations |
| R-06 | HIGH | Verify/configure SDK retry behavior |
| D-01 | HIGH | Create basic k8s deployment manifests |

### Short-term (first month)

| ID | Severity | Action |
|----|----------|--------|
| S-01 | HIGH | Document recipe store limitations; consider Redis |
| F-01 | HIGH | Enable read-only root filesystem in k8s |
| CI-04 | MEDIUM | Add container vulnerability scanning |
| O-01 | MEDIUM | Switch to structured JSON logging |
| G-02 | MEDIUM | Configure graceful shutdown timeout |
| R-07 | MEDIUM | Add circuit breaker for target APIs |
| R-08 | LOW | Add concurrent agent limiter |

### Backlog

| ID | Severity | Action |
|----|----------|--------|
| F-02 | MEDIUM | Pin base images to digest |
| C-02 | MEDIUM | Use SecretStr for API keys |
| R-09 | MEDIUM | Narrow openai-compat retry matching |
| O-02 | LOW | Add Prometheus metrics |
| CI-03 | LOW | Improve Docker smoke test |
| D-02 | INFO | Add /ready endpoint |

---

## Positive Findings

The codebase has several strengths worth highlighting:

1. **Security controls are above average** — SSRF protection, mutation blocking (GraphQL + gRPC + REST), DuckDB sandboxing, endpoint allowlisting
2. **Test coverage is excellent** — 848 tests across 32+ files with good boundary testing
3. **Multi-stage Docker build** is clean and cache-friendly
4. **CI pinning to SHA** for GitHub Actions is best practice
5. **ContextVar usage** for request isolation is correct and well-documented
6. **Recipe system** with LRU eviction and thread-safe locking is well-implemented
7. **Protocol abstraction** (orchestrator pattern) is clean and extensible
8. **SSRF protection** covers IPv4-mapped IPv6, cloud metadata, and configurable allowlists
9. **Trusted Publishing** for PyPI releases (no token management)
10. **Schema reduction** pipeline is graceful (TOON → Haiku → hard truncation fallback)
