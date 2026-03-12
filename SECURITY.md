# Security

## Threat Model

Ratatoskr is a single-tenant MCP server that proxies LLM agents to external APIs (REST, GraphQL, gRPC, MCP). The threat model assumes:

- **Target APIs are operator-trusted.** The operator sets `X-Target-URL` and other routing headers via MCP client configuration. End users do not control which APIs are connected.
- **Single-tenant by default.** The server is designed for one operator per process. There is no multi-tenant authentication or authorization layer.
- **Recipe store is process-global.** Cached recipes (parameterized API call + SQL pipelines) are shared across all sessions within the process. There is no per-session isolation for the recipe store. This is acceptable for single-tenant deployments but should be considered if adapting for multi-tenant use.

## Concurrency Model

Each incoming request runs in an isolated `contextvars` context:

1. `orchestrator.py` calls `contextvars.copy_context()` to snapshot the current context.
2. The agent coroutine is scheduled via `asyncio.create_task(coro, context=ctx)` using the copied context.
3. Module-level `ContextVar` instances (query history, results, schema cache) are reset at the start of each request within the isolated copy.

This prevents cross-request data leaks -- writes to ContextVars in one request are invisible to concurrent requests. Mutable containers (lists, dicts) are used as ContextVar values because `ContextVar.set()` in child tasks does not propagate to the parent.

## Security Controls

### SSRF Protection

URL validation is enforced in `api_agent/context.py` via `validate_target_url()`:

- **Scheme whitelist**: Only `http://` and `https://` are accepted.
- **Private IP blocklist**: Blocks RFC 1918, loopback, link-local, and cloud metadata addresses (e.g., `169.254.169.254`).
- **Optional host allowlist**: Set `ALLOWED_TARGET_HOSTS` (comma-separated) to restrict target URLs to known hosts.
- **Known limitation -- DNS rebinding**: Validation is hostname-based and subject to TOCTOU. A malicious DNS server could resolve to a private IP after validation passes. Mitigate by setting `ALLOWED_TARGET_HOSTS` in production to restrict resolution to known-good hostnames.

### Mutation Blocking

All mutation blocking is enforced at the client/transport layer, not via LLM prompting:

- **GraphQL**: Regex-based mutation detection with comment stripping before pattern matching. Queries only by default.
- **REST**: `POST`, `PUT`, `DELETE`, `PATCH` blocked by default. Enable selectively via `X-Allow-Unsafe-Paths` header (JSON array of fnmatch glob patterns).
- **gRPC**: Methods matching unsafe patterns (e.g., `Create*`, `Delete*`, `Update*`) blocked by default. Enable selectively via `X-Allow-Unsafe-RPCs` header (JSON array of glob patterns).

### DuckDB Sandboxing

DuckDB is used for SQL post-processing of API results (`api_agent/executor.py`):

- **External access disabled**: `enable_external_access = false` is set after data load, preventing file reads, HTTP requests, or extension loading from within SQL.
- **Table name sanitization**: Table names are validated and sanitized before use in SQL statements.
- **SQL parameter escaping**: `render_sql_safe()` quotes values containing spaces or special characters.
- **Concurrency limit**: A semaphore bounds concurrent DuckDB executions to prevent resource exhaustion.

### Endpoint Allowlist

Schema-level filtering restricts which API surface the agent can discover and call (`api_agent/filtering.py`):

- **Config ceiling**: `ALLOW_ENDPOINTS_REST`, `ALLOW_ENDPOINTS_GRAPHQL`, `ALLOW_ENDPOINTS_GRPC` environment variables (comma-separated fnmatch patterns) set a server-wide maximum.
- **Per-session intersection**: The `X-Allow-Endpoints` header (JSON array) further narrows the allowed set. The effective allowlist is the intersection of config and header values -- the header can only restrict, never expand.
- **All protocols covered**: REST paths/methods, GraphQL query fields (with transitive type closure), and gRPC services/methods are all filtered before agent discovery.

### Prompt Injection Mitigation

LLM agents process untrusted API schemas and responses. Mitigations include:

- **Schema sanitization**: `sanitize_schema_text()` strips potentially adversarial content from schema descriptions before they reach the LLM.
- **Trust boundary markers**: Schema reduction prompts wrap untrusted content in `[BEGIN UNTRUSTED SCHEMA]` / `[END UNTRUSTED SCHEMA]` markers to help the LLM distinguish instructions from data.
- **Operator trust model**: The operator controls which APIs are connected. Prompt injection from a target API schema requires the operator to have connected a malicious API.
- **Output capping**: All tool responses are truncated to ~32k characters (`MAX_TOOL_RESPONSE_CHARS`) to limit exfiltration surface.

## Supported Versions

Only the latest release receives security updates.

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |
| Older   | No        |

## Reporting a Vulnerability

**Please do not open public issues for security vulnerabilities.**

### GitHub Security Advisories (Preferred)

Report vulnerabilities through [GitHub Security Advisories](https://github.com/innago-property-management/ratatoskr/security/advisories/new). This allows private discussion and coordinated disclosure.

### Email

Send reports to [security@innago.com](mailto:security@innago.com). Include a description, reproduction steps, potential impact, and a suggested fix if any.

## Response Timeline

- **Acknowledgment**: Within 48 hours of report
- **Critical issues**: Fix or mitigation plan within 7 days
- **Resolution target**: Within 90 days of acknowledgment
- **Disclosure**: Coordinated with the reporter after a fix is available

## Scope

This policy covers the **api-agent-ratatoskr** package itself. It does not cover third-party APIs that Ratatoskr connects to, or vulnerabilities in upstream dependencies that should be reported to their respective maintainers.

## Credit

Security researchers who responsibly disclose vulnerabilities will be credited in the release notes for the version containing the fix, unless they prefer to remain anonymous.
