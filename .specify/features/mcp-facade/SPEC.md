# MCP Facade — Functional Specification

**Version:** 1.0
**Date:** 2026-03-10

---

## User Stories

### US-1: NL Query Against a Known MCP Server

As a user of a Ratatoskr instance configured to facade an MCP server, I want to ask natural
language questions ("What open PRs are assigned to me?", "List all Jira tickets in the backlog")
and receive structured answers, so that I get NL intelligence without loading the full MCP tool
schema into my client's context.

**Acceptance criteria:**

- Given `X-API-Type: mcp` and `X-Target-URL: mcp://github` (or a direct SSE URL), the `_query`
  tool routes to the MCP agent.
- The agent connects to the target MCP server, lists its tools, and presents them to the LLM as
  a compact schema.
- The LLM calls the `mcp_call` proxy tool with the correct tool name and arguments.
- Results are returned to the user, optionally post-processed via `sql_query`.
- The response matches the standard Ratatoskr shape: `{ok, data, mcp_calls, error}`.

---

### US-2: Discovery via External Command

As an operator running `mcp-proxy` or `mcp-bridge`, I want to configure Ratatoskr with
`MCP_DISCOVERY_COMMAND=mcp-proxy list` so that it automatically learns about my MCP servers
without me duplicating server definitions.

**Acceptance criteria:**

- When `MCP_DISCOVERY_COMMAND` is set, Ratatoskr runs the command and parses the JSON output.
- The output is an array of server descriptors with at least `{name, type, command/url, args?, env?}`.
- A request with `X-Target-URL: mcp://github` resolves the "github" server from the discovery
  output and connects to it.
- If discovery command fails (non-zero exit, invalid JSON), the error is propagated cleanly
  (no crash, `{"ok": false, "error": "Discovery failed: ..."}` returned).
- Discovery result is cached in the process for the lifetime of the request batch (no re-exec
  per tool call within one request).

---

### US-3: Direct SSE/Streamable-HTTP Target

As an operator with a running MCP server at `http://localhost:3001/mcp`, I want to set
`X-Target-URL: http://localhost:3001/mcp` and `X-API-Type: mcp` to facade it directly.

**Acceptance criteria:**

- SSE/streamable-http targets are detected by an `http://` or `https://` scheme in `X-Target-URL`.
- The agent connects using `streamablehttp_client` (preferred, falls back to `sse_client`).
- SSRF protection applies (same as REST/GraphQL): private IP blocking, scheme validation.

---

### US-4: Direct stdio Target (No Discovery)

As an operator, I want to configure a stdio MCP server directly via environment variables
(`MCP_TARGET_TRANSPORT=stdio`, `MCP_TARGET_COMMAND=npx`,
`MCP_TARGET_ARGS=-y,@modelcontextprotocol/server-github`) without needing a discovery binary.

**Acceptance criteria:**

- When `MCP_TARGET_TRANSPORT=stdio` is set, the agent spawns the command using
  `stdio_client(StdioServerParameters(...))`.
- The spawned process is cleaned up after each request (no leaked processes).
- `MCP_TARGET_ENV` is a JSON object merged with the current process environment.

---

### US-5: Endpoint Allowlist for MCP Tools

As a security-conscious operator, I want to configure `ALLOW_ENDPOINTS_MCP=get_*,list_*` to
restrict which MCP tools the LLM agent can call, so that write tools are never exposed.

**Acceptance criteria:**

- The tool list from `list_tools()` is filtered by `ALLOW_ENDPOINTS_MCP` config patterns and
  `X-Allow-Endpoints` header patterns (intersection semantics, same as other protocols).
- Filtered tools are not visible in the schema text presented to the LLM.
- Filtered tools are rejected at the `mcp_call` level even if the LLM somehow names them.
- If allowlist filters to zero tools, the response is `{ok: false, error: "No MCP tools match..."}`.

---

### US-6: Recipe Extraction and Replay for MCP

As a repeat user, I want successful MCP agent runs to be extracted as recipes so that
subsequent similar queries use the cached parameterized pipeline rather than redoing LLM
planning.

**Acceptance criteria:**

- After a successful NL-to-MCP-tools run, `maybe_extract_and_save_recipe` is called with
  `api_type="mcp"` and steps of kind `"mcp"`.
- A recipe step records `{kind: "mcp", tool: "...", args: {...}, name: "..."}`.
- Recipe replay uses a `mcp_step_executor` that calls the target MCP server directly without
  re-running the LLM.
- Recipes parameterize arguments via `{{param}}` template substitution (same as GraphQL).

---

### US-7: Observability (Metrics + Tracing)

As an SRE monitoring Ratatoskr, I want MCP requests to emit the same metrics and traces as
other protocols.

**Acceptance criteria:**

- `record_request("mcp", status, latency_ms)` is called after every MCP query.
- `record_schema_fetch(latency_ms, "mcp")` is called after `list_tools()`.
- `trace_span("schema.fetch", {"protocol": "mcp"})` wraps the tool discovery.
- `trace_span("agent.tool_loop", {"agent_type": "mcp"})` is emitted by the orchestrator
  (no change needed — orchestrator already does this).

---

## Non-Functional Requirements

- **No new top-level dependencies**: `mcp` SDK is already a transitive dependency (via `fastmcp`).
  No additional packages needed.
- **Test coverage**: Every new module gets a dedicated test file. All public functions tested.
  Tests use `FakeLLMProvider` and mock session/transport — no live MCP connections in unit tests.
- **TDD**: Tests are written before implementation in each sub-task.
- **Schema text size**: Tool list schema is rendered compactly (name + required params only,
  descriptions truncated to 200 chars). Must fit within `MAX_SCHEMA_CHARS` or be truncated
  gracefully.

---

## Clarifications

**Q: Should `mcp://` URIs bypass SSRF validation since they resolve to local processes?**
A: Named `mcp://name` targets (stdio via discovery) bypass IP validation. Direct
`http://`/`https://` SSE targets go through the existing SSRF validation path. The `mcp://`
scheme is added to `ALLOWED_URL_SCHEMES` but is handled specially (no IP check for
stdio-resolved targets).

**Q: What does the discovery command JSON contract look like?**
A: Ratatoskr normalizes to a common descriptor format:
```json
[
  {"name": "github", "type": "stdio", "command": "npx", "args": ["-y", "@mcp/server-github"], "env": {}},
  {"name": "jetbrains", "type": "sse", "url": "http://localhost:64342/sse"}
]
```
Both `mcp-proxy list` and `mcp-bridge list` can produce this (or a superset). Unknown fields
are ignored.

**Q: How are `mcp-proxy list` and `mcp-bridge list` JSON formats different?**
A: `mcp-proxy` uses `upstreamServers` wrapper; `mcp-bridge` uses `localServers`/`remoteServers`.
Ratatoskr's discovery parser normalizes both to the common descriptor array by checking for
known wrapper keys, then falling back to treating the root as an array.

**Q: Is discovery result cached across requests?**
A: Yes — cached at module level with the command string as key. No TTL in MVP (operators
restart the process to refresh). This is correct because MCP server inventory changes rarely.

**Q: What `X-Target-URL` value is used for `api_id` in the recipe store?**
A: For named targets: `mcp:mcp://github`. For direct URL targets: `mcp:http://host/path`.
Same pattern as `build_api_id` for other protocols.
