# Security Audit: Ratatoskr Pre-Existing Vulnerabilities

**Date:** 2026-02-27
**Auditor:** Claude Opus 4.6 (security review mode)
**Scope:** Delphi panel findings #1-#5 plus secondary findings during review
**Codebase:** `/Volumes/Repos/ratatoskr` @ commit `4165b9a` (branch `feat/grpc-recipes`)

---

## Executive Summary

Five vulnerabilities were flagged by a Delphi panel. After source-level review, I confirm all five are real but vary significantly in practical severity. Two are **High** (SSRF, SQL injection via recipes), one is **Medium** (gRPC mutation gap), and two are **Low** (cross-session recipe store, GraphQL mutation regex). I also discovered three additional issues during review: DuckDB SQL injection from LLM-generated queries, unvalidated table names in `executor.py`, and CORS wildcard default.

---

## Finding 1: SSRF via `X-Target-URL` (No URL Validation)

**File:** `/Volumes/Repos/ratatoskr/api_agent/context.py` lines 49-61
**Reported severity:** High
**Assessed severity:** HIGH

### Verification

Confirmed. The `get_request_context()` function accepts any string from the `X-Target-URL` header and stores it directly as `target_url` with zero validation:

```python
# context.py:49-61
target_url = headers.get("x-target-url")
# ...
if not target_url:
    raise MissingHeaderError("X-Target-URL header required")
# No scheme validation, no allowlist, no blocklist, no RFC check
return RequestContext(target_url=target_url, ...)
```

This URL is then passed directly to:
- `httpx.AsyncClient.post()` for GraphQL queries (`graphql/client.py:49`)
- `httpx.AsyncClient.request()` for REST calls (`rest/client.py:126-133`)
- `grpc.aio.insecure_channel()` / `grpc.aio.secure_channel()` for gRPC connections (`grpc/client.py:18-23`)
- `grpc.insecure_channel()` / `grpc.secure_channel()` for reflection (`grpc/reflection.py:232-236`)

### Exploitability: PRACTICALLY EXPLOITABLE

Any MCP client can set `X-Target-URL` to an internal address. Examples:

1. **Cloud metadata exfil:** `X-Target-URL: http://169.254.169.254/latest/meta-data/iam/security-credentials/` with `X-API-Type: rest` and a query like "list everything" would cause the agent to fetch IAM credentials from the EC2 metadata service.

2. **Internal service scanning:** `X-Target-URL: http://internal-service:8080/openapi.json` probes internal hosts.

3. **gRPC internal access:** `X-Target-URL: grpc://internal-payment-service:50051` with `X-API-Type: grpc` connects to internal gRPC services, performs reflection (discovering all methods), and can execute any RPC.

4. **File read (httpx):** While httpx does not support `file://` by default, scheme confusion attacks (e.g., `http://0.0.0.0:0/...`) could still probe internal interfaces.

The threat model depends on deployment: if Ratatoskr runs inside a VPC/cluster, SSRF is critical because it bridges the MCP client (external) to internal infrastructure. If running on a developer laptop, it is lower severity but still allows probing localhost services.

### Proposed Fix

Add URL validation in `context.py` with a configurable allowlist:

```python
import ipaddress
from urllib.parse import urlparse

# New config settings:
#   ALLOWED_URL_SCHEMES: str = "http,https,grpc,grpcs"
#   BLOCKED_HOSTS: str = "169.254.169.254,metadata.google.internal"
#   ALLOWED_HOSTS: str = ""  # empty = allow all non-blocked

_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def validate_target_url(url: str, api_type: str) -> str:
    """Validate and normalize target URL. Raises MissingHeaderError on invalid."""
    parsed = urlparse(url)

    # Scheme validation
    if api_type == "grpc":
        allowed_schemes = {"grpc", "grpcs"}
    else:
        allowed_schemes = {"http", "https"}
    if parsed.scheme not in allowed_schemes:
        raise MissingHeaderError(
            f"Invalid scheme '{parsed.scheme}' for {api_type}. Allowed: {allowed_schemes}"
        )

    # Host validation
    hostname = parsed.hostname
    if not hostname:
        raise MissingHeaderError("X-Target-URL must include a hostname")

    # Block cloud metadata endpoints
    blocked = settings.BLOCKED_HOSTS.split(",") if settings.BLOCKED_HOSTS else []
    blocked.extend(["169.254.169.254", "metadata.google.internal"])
    if hostname in blocked:
        raise MissingHeaderError(f"Host '{hostname}' is blocked")

    # Optional: block private IPs (configurable)
    if settings.BLOCK_PRIVATE_IPS:
        try:
            ip = ipaddress.ip_address(hostname)
            if any(ip in net for net in _PRIVATE_RANGES):
                raise MissingHeaderError(f"Private IP addresses are blocked: {hostname}")
        except ValueError:
            pass  # hostname is a DNS name, not an IP

    # Allowlist check (if configured)
    if settings.ALLOWED_HOSTS:
        allowed = [h.strip() for h in settings.ALLOWED_HOSTS.split(",")]
        if hostname not in allowed:
            raise MissingHeaderError(f"Host '{hostname}' not in allowlist")

    return url
```

**Minimal change:** At minimum, block RFC 1918/link-local/cloud metadata addresses by default. The allowlist (`ALLOWED_HOSTS`) provides defense-in-depth for production deployments.

---

## Finding 2: Recipe `{{param}}` String Interpolation into DuckDB SQL

**File:** `/Volumes/Repos/ratatoskr/api_agent/recipe/store.py` lines 50-66
**Also:** `/Volumes/Repos/ratatoskr/api_agent/recipe/common.py` lines 255-284
**Reported severity:** High
**Assessed severity:** HIGH

### Verification

Confirmed. The `render_text_template()` function performs raw string substitution into SQL templates:

```python
# store.py:50-66
def render_text_template(template: str, params: dict[str, Any]) -> str:
    """Render {{param}} placeholders using raw string insertion."""
    def _as_text(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if v is None:
            return "null"
        return str(v)  # No escaping, no quoting, no sanitization

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"missing param: {name}")
        return _as_text(params[name])  # RAW insertion

    return _PLACEHOLDER_RE.sub(repl, template)
```

This is called in `_execute_sql_steps()` (`common.py:272`) which then passes the result directly to `execute_sql()`:

```python
# common.py:272-273
sql = render_text_template(sql_tmpl, params)
res = execute_sql(results, sql)
```

And `execute_sql()` runs it directly on DuckDB:

```python
# executor.py:170
result = conn.execute(query).fetchall()
```

### Exploitability: PRACTICALLY EXPLOITABLE

The attack chain:

1. An MCP client triggers a successful agent run, which creates a recipe with a SQL template like:
   `SELECT * FROM data WHERE name ILIKE '{{searchTerm}}%'`

2. A subsequent call to this recipe with `searchTerm` set to `'; DROP TABLE data; --` would produce:
   `SELECT * FROM data WHERE name ILIKE ''; DROP TABLE data; --%'`

3. DuckDB does support `conn.execute()` with multiple statements by default. Even if multi-statement is not supported, an attacker can use SQL injection within a single statement:
   - `' UNION SELECT * FROM read_csv_auto('/etc/passwd') --` to read local files
   - DuckDB's `read_csv_auto`, `read_json_auto`, `read_parquet` can read arbitrary files
   - DuckDB's `COPY ... TO ...` can write files

**Critical:** DuckDB runs in the server process with full file system access. SQL injection here is equivalent to arbitrary file read/write on the server.

### Mitigating factors

- Recipe SQL templates are generated by the LLM extractor, not user-controlled directly.
- Recipe params are type-validated by `validate_recipe_params()` (Pydantic model with `extra="forbid"`), but the types (`str`, `int`, `float`, `bool`) still allow arbitrary string content for `str` params.
- The injection depends on a recipe existing with a `{{param}}` in a SQL `sql_steps` template.

### Proposed Fix

Use DuckDB parameterized queries instead of string interpolation for SQL steps:

```python
# Option A: Convert {{param}} to DuckDB $1, $2 positional params
def render_sql_template_safe(
    template: str, params: dict[str, Any]
) -> tuple[str, list[Any]]:
    """Convert {{param}} template to parameterized SQL."""
    ordered_params: list[Any] = []

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"missing param: {name}")
        ordered_params.append(params[name])
        return "?"

    sql = _PLACEHOLDER_RE.sub(repl, template)
    return sql, ordered_params

# In _execute_sql_steps:
sql, sql_params = render_sql_template_safe(sql_tmpl, params)
res = execute_sql_parameterized(results, sql, sql_params)

# In executor.py:
def execute_sql_parameterized(data, query, params):
    # ... setup tables ...
    result = conn.execute(query, params).fetchall()
```

**Minimal change:** Since DuckDB supports `conn.execute(query, parameters)` with `?` placeholders, this is a straightforward fix. The `render_text_template` function should NOT be used for SQL; create a separate `render_sql_safe()` that returns `(query, params)` tuple.

---

## Finding 3: No gRPC Mutation Blocking

**File:** `/Volumes/Repos/ratatoskr/api_agent/agent/grpc_agent.py`
**Also:** `/Volumes/Repos/ratatoskr/api_agent/tools/execute.py`
**Reported severity:** High
**Assessed severity:** MEDIUM

### Verification

Confirmed. Unlike GraphQL (which blocks mutations at `graphql/client.py:36`) and REST (which blocks POST/PUT/DELETE/PATCH at `rest/client.py:100-106`), the gRPC agent has zero mutation protection. The `_create_grpc_call_tool()` function at `grpc_agent.py:175-314` will execute ANY method regardless of whether it modifies state.

Similarly, the `_execute` tool (`tools/execute.py:123-254`) will execute any gRPC method with no safety checks.

The recipe runner (`recipe/runner.py:126-225`) also executes gRPC methods with no safety filtering.

### Exploitability: CONDITIONALLY EXPLOITABLE

Severity depends on the gRPC services exposed:

1. **No semantic signal:** Unlike REST (where HTTP verb semantics are standardized) and GraphQL (where `mutation` keyword exists), gRPC protobuf method names have no standard convention for read vs. write. A method named `UpdateUser` is clearly a mutation, but `ProcessPayment` could be read-only (returning payment info) or write (creating a payment).

2. **Reflection exposes everything:** When the gRPC agent connects, it discovers ALL services and methods via reflection. The LLM is then given the full service catalog and could call any method if the user's question implies it.

3. **Attack scenario:** User asks a seemingly read-only question, but the LLM decides to call a state-modifying RPC. Or an adversarial prompt injection in the question tricks the LLM into calling destructive methods.

### Mitigating factors

- gRPC services typically require authentication (metadata), limiting blast radius.
- The `X-Target-Headers` forwarding means the client controls what credentials are available.
- Unlike REST/GraphQL, there is no universal convention for gRPC "safe" vs "unsafe" methods.

### Proposed Fix

Add an opt-in method allowlist/blocklist for gRPC, parallel to the REST `X-Allow-Unsafe-Paths` pattern:

```python
# New headers:
#   X-gRPC-Allow-Methods: JSON array of method patterns (glob)
#   X-gRPC-Block-Methods: JSON array of method patterns to block

# In context.py, add:
grpc_allow_methods: tuple[str, ...]  # X-gRPC-Allow-Methods
grpc_block_methods: tuple[str, ...]  # X-gRPC-Block-Methods

# In grpc_agent.py, add validation in _create_grpc_call_tool:
def _is_method_allowed(method_path: str, ctx: RequestContext) -> bool:
    """Check if gRPC method is allowed by allowlist/blocklist."""
    if ctx.grpc_allow_methods:
        return any(fnmatch.fnmatch(method_path, p) for p in ctx.grpc_allow_methods)
    if ctx.grpc_block_methods:
        return not any(fnmatch.fnmatch(method_path, p) for p in ctx.grpc_block_methods)
    return True  # No restrictions = allow all (current behavior)
```

**Alternative (stronger):** Default to read-only by only exposing methods that match common read patterns (Get*, List*, Search*, Find*, Query*, Describe*, Count*, Check*, Lookup*) and requiring explicit opt-in for others. This parallels the REST approach where POST/PUT/DELETE/PATCH are blocked by default.

**NOTE:** This finding is directly related to the planned "endpoint allowlist" feature. A unified allowlist across all three protocol types would be the most robust approach.

---

## Finding 4: Global `RECIPE_STORE` Cross-Session

**File:** `/Volumes/Repos/ratatoskr/api_agent/recipe/store.py` line 351
**Reported severity:** Medium
**Assessed severity:** LOW

### Verification

Confirmed. `RECIPE_STORE` is a module-level singleton:

```python
# store.py:351
RECIPE_STORE = RecipeStore(max_size=settings.RECIPE_CACHE_SIZE)
```

It is shared across ALL MCP sessions. Recipes created in one session are visible to all sessions. There is no session-level isolation.

### Exploitability: LIMITED

The cross-session nature creates two potential issues:

1. **Information leakage:** Session A connects to `api-internal.company.com`, asks a query, and a recipe is created. Session B connects to the same API and can see the recipe question text (which may reveal business logic or data patterns from Session A's usage).

2. **Recipe poisoning:** Session A could craft a question that generates a malicious recipe. Session B, connecting to the same API, might execute this recipe unknowingly via the recipe suggestion system.

However, several factors reduce severity:

- **Recipes are keyed by `(api_id, schema_hash)`:** A recipe for `graphql:https://api-a.com` is never suggested for `graphql:https://api-b.com`. The `api_id` includes the full target URL.
- **Schema hash validation:** If the API schema changes, all cached recipes for that API are effectively orphaned (hash mismatch at `runner.py:70`).
- **Recipe execution re-validates:** The `execute_recipe_tool()` function checks `meta.get("schema_hash") != schema_hash or meta.get("api_id") != api_id` before running.
- **In-memory only:** Recipes do not persist across server restarts.
- **LRU eviction:** Limited to 64 entries by default.

### Proposed Fix

For production deployments, scope the recipe store per session or per client:

```python
# Option 1: Disable recipes in multi-tenant mode (simplest)
# config.py:
ENABLE_RECIPES: bool = False  # default to off for multi-tenant

# Option 2: Session-scoped recipe store
# Store recipes per MCP session ID rather than globally
# This requires threading the session ID through from FastMCP context
```

**Minimal change:** Add a config option `RECIPE_ISOLATION: str = "global"` with options `"global"` (current behavior) and `"disabled"`. Document the security implications in the README.

---

## Finding 5: GraphQL Mutation Regex Bypassable

**File:** `/Volumes/Repos/ratatoskr/api_agent/graphql/client.py` line 12
**Reported severity:** High
**Assessed severity:** LOW

### Verification

The regex is:

```python
# client.py:12
_MUTATION_PATTERN = re.compile(r"^\s*mutation\b", re.IGNORECASE | re.MULTILINE)
```

The Delphi panel flagged that this "only checks `^\s*mutation\b`". Let me analyze actual bypass vectors:

### Bypass Analysis

**Claimed bypass: leading characters.** A query like `\nmutation { ... }` or `# comment\nmutation { ... }` -- but wait, `re.MULTILINE` means `^` matches at the start of ANY line. So `# comment\nmutation { ... }` WOULD be caught because `mutation` starts a new line.

**Actual bypass vectors:**

1. **Query aliases with mutation keyword:** `query { mutation_field { ... } }` -- this is NOT a mutation but would be caught by the regex. However, this is a false positive (blocks valid queries), not a bypass.

2. **Comment-obscured mutation:** `{mutation{deleteUser(id:1){id}}}` -- the shorthand anonymous operation. GraphQL allows omitting the `query` keyword for queries. But this starts with `{`, not `mutation`, so the regex wouldn't catch it. HOWEVER, this is actually a query (anonymous operations are queries by default in GraphQL), not a mutation. A mutation MUST have the `mutation` keyword in valid GraphQL.

3. **Unicode tricks:** Attempting to use Unicode homoglyphs for "mutation" -- GraphQL spec requires ASCII, so this would be rejected by the GraphQL server itself.

4. **Named query wrapping mutation:** `query { __typename }` followed by a separate mutation in the same request -- but `execute_query` sends a single `query` field, not batched operations.

5. **Fragment-based bypass:** Fragments cannot contain operation definitions; they are sub-selections.

### Conclusion: NOT PRACTICALLY BYPASSABLE

The GraphQL specification requires that mutation operations start with the `mutation` keyword. The `re.MULTILINE` flag means the regex checks every line. There is no way to express a GraphQL mutation without the `mutation` keyword appearing at the start of a line (or after only whitespace).

The regex is actually correct for its purpose. The only minor improvement would be to also catch mutations buried after comments:

```python
# Slightly more robust (handles leading comments on same line)
_MUTATION_PATTERN = re.compile(r"^\s*mutation\b", re.IGNORECASE | re.MULTILINE)
```

But the current regex is already correct because GraphQL comments (`#`) end at newline, and `re.MULTILINE` handles the next line.

### Proposed Fix

No fix needed. The current regex is correct. Optionally, for defense-in-depth, parse the operation type properly:

```python
def _is_mutation(query: str) -> bool:
    """Check if GraphQL query is a mutation using lightweight parsing."""
    # Strip comments
    stripped = re.sub(r"#[^\n]*", "", query)
    # Strip whitespace
    stripped = stripped.strip()
    # Check if it starts with 'mutation'
    return bool(re.match(r"mutation\b", stripped, re.IGNORECASE))
```

This is marginally more robust but the current approach is not exploitable.

---

## Additional Finding A: DuckDB SQL Injection from LLM-Generated Queries

**Files:**
- `/Volumes/Repos/ratatoskr/api_agent/executor.py` lines 140-183
- `/Volumes/Repos/ratatoskr/api_agent/agent/graphql_agent.py` line 420 (`sql_query()`)
- `/Volumes/Repos/ratatoskr/api_agent/agent/rest_agent.py` line 494 (`sql_query()`)
- `/Volumes/Repos/ratatoskr/api_agent/agent/grpc_agent.py` line 725 (`_sql_query()`)
**Assessed severity:** MEDIUM

### Description

The `sql_query` tools across all three agents accept LLM-generated SQL and pass it directly to `execute_sql()`:

```python
# executor.py:170
result = conn.execute(query).fetchall()
```

DuckDB supports functions like `read_csv_auto('/etc/passwd')`, `read_json_auto('/path/to/file')`, `COPY ... TO '/path/to/file'`, and `httpfs` extension for HTTP requests.

### Exploitability

This is a **prompt injection vector**: if an adversarial API response contains text that tricks the LLM into generating malicious SQL, the DuckDB query could:

- Read arbitrary files from the server filesystem
- Write data to the server filesystem
- Make HTTP requests if httpfs extension is loaded

However, the LLM is the intermediary -- it would need to be tricked into generating the malicious SQL, which is plausible but not trivial.

### Proposed Fix

Restrict DuckDB capabilities:

```python
def execute_sql(data: Any, query: str) -> dict[str, Any]:
    conn = duckdb.connect()
    # Disable dangerous functions
    conn.execute("SET enable_external_access = false")
    # ... rest of setup and execution
```

DuckDB's `enable_external_access = false` blocks file reads, HTTP requests, and file writes from SQL. This single setting eliminates the file-system attack surface.

---

## Additional Finding B: Unvalidated Table Names in `executor.py`

**File:** `/Volumes/Repos/ratatoskr/api_agent/executor.py` lines 67, 162
**Assessed severity:** LOW

### Description

Table names derived from user-controlled `name` parameter are interpolated directly into SQL:

```python
# executor.py:67
conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM read_json_auto('{temp_file}')")

# executor.py:162
conn.execute(f"CREATE TABLE {key} AS SELECT * FROM read_json_auto('{f.name}')")
```

The `table_name` comes from the `name` parameter of `grpc_call()`, `rest_call()`, and `graphql_query()` tools, which defaults to `"data"` but can be set by the LLM.

### Exploitability

Since the LLM controls the `name` parameter (not the end user directly), this requires prompt injection. A malicious API response could trick the LLM into passing `name="x; DROP TABLE data; --"` though this is unlikely given typical LLM behavior.

### Proposed Fix

Sanitize table names:

```python
import re

def _safe_table_name(name: str) -> str:
    """Sanitize table name to prevent SQL injection."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = "t_" + sanitized
    return sanitized[:64]
```

---

## Additional Finding C: CORS Wildcard Default

**File:** `/Volumes/Repos/ratatoskr/api_agent/config.py` line 65
**Assessed severity:** LOW (contextual)

### Description

```python
# config.py:65
CORS_ALLOWED_ORIGINS: str = "*"
```

The default CORS configuration allows all origins. Combined with `allow_credentials=True` (`__main__.py:126`), this could allow cross-origin requests with credentials from any website.

### Proposed Fix

Change default to empty (no CORS) or localhost-only:

```python
CORS_ALLOWED_ORIGINS: str = "http://localhost:3000"
```

---

## Severity Summary

| # | Finding | Severity | Exploitable? | Fix Complexity |
|---|---------|----------|-------------|----------------|
| 1 | SSRF via X-Target-URL | **HIGH** | Yes (trivial) | Medium |
| 2 | Recipe SQL injection via `{{param}}` | **HIGH** | Yes (requires recipe) | Low (use parameterized queries) |
| 3 | No gRPC mutation blocking | **MEDIUM** | Conditional | Medium |
| 4 | Global RECIPE_STORE cross-session | **LOW** | Limited | Low (config flag) |
| 5 | GraphQL mutation regex bypass | **LOW** | Not exploitable | None needed |
| A | DuckDB SQL injection from LLM | **MEDIUM** | Via prompt injection | Low (one DuckDB setting) |
| B | Unvalidated table names | **LOW** | Via prompt injection | Low (sanitize) |
| C | CORS wildcard default | **LOW** | Contextual | Low (config change) |

## Recommended Fix Priority

1. **Immediate (pre-release):** Finding 2 (recipe SQL injection) -- use DuckDB parameterized queries
2. **Immediate (pre-release):** Finding A (DuckDB external access) -- `SET enable_external_access = false`
3. **High priority:** Finding 1 (SSRF) -- implement URL validation with cloud metadata blocklist
4. **Medium priority:** Finding 3 (gRPC mutations) -- implement alongside endpoint allowlist feature
5. **Low priority:** Findings 4, B, C -- configuration and sanitization improvements

---

## Relationship to Endpoint Allowlist Feature

Findings 1 and 3 are both addressed by the planned endpoint allowlist feature. A unified allowlist that covers:

- **Which target URLs are permitted** (addresses Finding 1/SSRF)
- **Which API endpoints/methods are permitted** (addresses Finding 3/gRPC mutations and strengthens REST/GraphQL safety)
- **Which recipe operations are permitted** (addresses Finding 4/cross-session)

...would be the most architecturally sound approach. The individual fixes proposed above are immediate mitigations; the endpoint allowlist feature should be designed as the long-term solution.
