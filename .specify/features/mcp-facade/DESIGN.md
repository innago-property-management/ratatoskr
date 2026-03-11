# MCP Facade — Technical Design

**Version:** 1.0
**Date:** 2026-03-10

---

## Architecture Overview

The MCP facade follows the identical orchestrator pattern as gRPC (the most recent protocol
addition). The changes are surgical: one new client module, one new agent module, thin routing
additions in three existing files, and new config fields.

```
X-API-Type: mcp
X-Target-URL: mcp://github  (or http://host:port/mcp)
         │
         ▼
context.py::get_request_context()
  ├─ Validates "mcp" as allowed api_type
  ├─ For http/https targets: runs existing SSRF validation
  └─ For mcp:// targets: skips IP check (resolves to local process)
         │
         ▼
tools/query.py::query()
  └─ api_type == "mcp" → process_mcp_query(question, req_ctx)
         │
         ▼
agent/mcp_agent.py::process_mcp_query()
  ├─ 1. Resolve target → MCPServerDescriptor (discovery or direct)
  ├─ 2. Connect session (stdio or SSE/streamable-http)
  ├─ 3. list_tools() → tool_defs[]  [trace_span("schema.fetch")]
  ├─ 4. Filter by allowlist (reuse filtering.py::is_endpoint_allowed)
  ├─ 5. Build schema_text (compact) + raw_schema (JSON for recipes)
  ├─ 6. Create mcp_call tool (proxies session.call_tool)
  ├─ 7. Create sql_query tool (reuse orchestrator factory)
  ├─ 8. Create search_schema tool (reuse existing)
  ├─ 9. Build ProtocolConfig + run_agent_orchestration()
  └─ 10. maybe_extract_and_save_recipe()
         │
         ▼
agent/orchestrator.py  ← unchanged
  └─ run_tool_loop → LLM calls mcp_call / sql_query / search_schema
         │
         ▼
mcp/client.py::MCPSession
  ├─ connect_stdio(StdioServerParameters)
  └─ connect_sse(url)  [or connect_streamablehttp(url)]
```

---

## New Files

### `api_agent/mcp/__init__.py`
Empty init.

### `api_agent/mcp/client.py`
Session management. Thin wrappers around the `mcp` SDK that handle both transports in a
unified async context manager interface.

```python
@asynccontextmanager
async def mcp_stdio_session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    """Async context manager for a stdio MCP session."""
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def mcp_http_session(url: str) -> AsyncIterator[ClientSession]:
    """Async context manager for a streamable-http (or SSE fallback) session."""
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    except Exception:
        # Fall back to SSE
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
```

### `api_agent/mcp/discovery.py`
Runs the external discovery command and normalizes the output.

```python
@dataclass(frozen=True)
class MCPServerDescriptor:
    name: str
    transport: Literal["stdio", "sse"]
    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # sse fields
    url: str = ""


_DISCOVERY_CACHE: dict[str, list[MCPServerDescriptor]] = {}


async def discover_servers(command: str) -> list[MCPServerDescriptor]:
    """Run external command and parse server descriptors. Cached per command string."""

async def resolve_target(target_url: str) -> MCPServerDescriptor:
    """Convert X-Target-URL to an MCPServerDescriptor.

    Handles:
    - mcp://name  → look up in discovery cache or direct config
    - http://...  → SSE/streamable-http descriptor
    - stdio://... → direct stdio descriptor (MCP_TARGET_COMMAND)
    """
```

### `api_agent/agent/mcp_agent.py`
Thin wrapper over orchestrator. See SKETCH.md for the code sketch — the final implementation
will be nearly identical with these additions:

- Discovery/resolution before session open
- Allowlist filtering post-list_tools
- `_make_mcp_step_executor_factory` for recipe replay
- `_build_system_prompt` (MCP-specific instructions)
- Full error handling + `record_request` / `record_schema_fetch` metrics

---

## Modified Files

### `api_agent/config.py`
New settings block (after existing `ALLOW_ENDPOINTS_GRPC`):

```python
# MCP facade
MCP_DISCOVERY_COMMAND: str = ""
# e.g. "mcp-proxy list" or "mcp-bridge list"
MCP_TARGET_TRANSPORT: str = ""  # "stdio" | "sse" — direct target (no discovery)
MCP_TARGET_COMMAND: str = ""    # e.g. "npx"
MCP_TARGET_ARGS: str = ""       # comma-separated args
MCP_TARGET_ENV: str = "{}"      # JSON object
ALLOW_ENDPOINTS_MCP: str = ""   # CSV: "get_*,list_*"
```

### `api_agent/context.py`
- Add `"mcp"` to the `api_type` validation allowlist.
- Add special-case SSRF handling: `mcp://` scheme is allowed but bypasses IP validation
  (stdio targets are process-local, not network).
- Add `mcp://` to `ALLOWED_URL_SCHEMES` default.

### `api_agent/tools/query.py`
- Import `process_mcp_query` from `api_agent.agent.mcp_agent`.
- Add `elif req_ctx.api_type == "mcp":` branch in the dispatch.
- Add `"mcp": "mcp_calls"` to `calls_key_map`.

---

## Data Model

### Tool descriptor (from MCP SDK → normalized)
```python
{
    "name": str,           # e.g. "get_pull_request"
    "description": str,    # truncated to 200 chars in schema_text
    "input_schema": dict,  # full JSON Schema for call validation
}
```

### Recipe step
```python
{"kind": "mcp", "tool": str, "args": dict, "name": str}
```
- `args` is the concrete JSON dict passed to `session.call_tool`.
- Template parameters use `{{param}}` syntax in string values.

### Discovery descriptor (normalized)
```python
MCPServerDescriptor(
    name="github",
    transport="stdio",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-github"],
    env={"GITHUB_TOKEN": "..."},
)
```

---

## Schema Text Format

Compact representation for LLM context (stays within `MAX_SCHEMA_CHARS`):

```
<available_tools>
get_pull_request(pullNumber!: integer, owner?: string, repo?: string)
  # Get details of a specific pull request
list_issues(owner?: string, repo?: string, state?: string)
  # List issues from a repository
create_issue(title!: string, owner?: string, repo?: string)
  # Create a new issue
</available_tools>
```

Rules:
- `!` suffix = required param, `?` suffix = optional
- Type from JSON Schema `type` field (or `any`)
- Description truncated to 200 chars
- If total exceeds `MAX_SCHEMA_CHARS`, truncate to first N tools that fit
- Full `input_schema` is stored in `raw_schema` (JSON) for recipe matching

---

## Transport Resolution Logic

```
X-Target-URL resolution:
  mcp://github          → discovery lookup by name → stdio or SSE per descriptor
  mcp://host:port/path  → treat as SSE: http://host:port/path
  http://...            → direct SSE/streamable-http
  https://...           → direct SSE/streamable-http (SSRF-validated)
  (none)                → use MCP_TARGET_TRANSPORT + MCP_TARGET_COMMAND from config
```

---

## SSRF Handling

`validate_target_url` in `context.py` currently accepts only `http/https` (non-gRPC) or
`grpc/grpcs` (gRPC). MCP needs a third path:

- `mcp://` scheme: skip network validation (process-local stdio target). Add to
  `ALLOWED_URL_SCHEMES` default. Do not IP-check.
- `http://` / `https://` with `api_type=mcp`: treat identically to REST (full SSRF validation).

Implementation: add `elif api_type == "mcp":` in `validate_target_url` with `mcp://` allowed
and HTTP schemes allowed (for direct SSE targets). Skip IP check only for `mcp://`.

---

## Session Lifecycle

- **Per-request**: A fresh session is opened at the start of `process_mcp_query` and closed
  (via async context manager) before it returns. No session state leaks across requests.
- **stdio**: Each request spawns a child process. Cost is ~50–200ms (startup). Acceptable for
  MVP; pooling can be added later.
- **SSE/streamable-http**: Each request opens a new HTTP connection. Most MCP servers handle
  this fine. Persistent connection pooling is a future optimization.
- **Cleanup**: The `async with` pattern guarantees cleanup even on exception.

---

## Allowlist Filtering

MCP tools use tool names as the endpoint key:

```python
allowed_tools = [
    t for t in tool_defs
    if is_endpoint_allowed(
        t["name"],
        parse_config_allowlist(settings.ALLOW_ENDPOINTS_MCP),
        ctx.allow_endpoints or None,
    )
]
```

The `mcp_call` tool validates `tool_name` against `{t["name"] for t in allowed_tools}`, so
even if the LLM hallucinates a filtered tool name, it is blocked at call time.

---

## Recipe Step Executor

```python
def _make_mcp_step_executor_factory(descriptor: MCPServerDescriptor):
    def factory(recipe_id: str):
        async def mcp_step_executor(step_idx, step, params, results):
            # Validate kind
            # Open a fresh session (same as process_mcp_query)
            # Call session.call_tool(step["tool"], rendered_args)
            # store_result + return
        return mcp_step_executor
    return factory
```

The session is opened fresh per recipe step (same per-request lifecycle).

---

## System Prompt

MCP-specific additions over the shared orchestrator system prompt:

```
You are an MCP API agent. You answer questions by calling tools on an MCP server.

<tools>
mcp_call(tool_name, arguments?, name?, return_directly?)
  Call a tool on the target MCP server. Arguments must match the tool's input schema.
  Result is stored as a DuckDB table named `name` (default: "data").

sql_query(sql, return_directly?)
  Run DuckDB SQL on stored tool results.

search_schema(query)
  Search available tool names and descriptions.
</tools>

<workflow>
1. Review <available_tools> below
2. Call mcp_call with the right tool and arguments
3. If filtering/aggregation needed → sql_query, else return data
</workflow>
```

---

## Testing Strategy

### Files to create (TDD order)

1. `tests/mcp/test_mcp_discovery.py` — `discover_servers`, `resolve_target`, caching, error cases
2. `tests/mcp/test_mcp_client.py` — session context managers with mock transports
3. `tests/test_mcp_agent.py` — `process_mcp_query` with `FakeLLMProvider` + mock session
4. `tests/test_mcp_config.py` — new settings fields, defaults
5. `tests/test_mcp_routing.py` — query.py dispatch + context.py validation for `api_type=mcp`
6. `tests/test_mcp_recipe.py` — recipe extraction, replay, step executor

### Mock boundary

```
process_mcp_query
  ├─ Mock: MCPClientSession (mocked object with list_tools/call_tool methods)
  ├─ Mock: FakeLLMProvider (existing conftest pattern)
  ├─ Real: ContextVar isolation (copy_context)
  ├─ Real: DuckDB executor (same as other agents)
  ├─ Real: Recipe store
  └─ Real: Allowlist filtering
```

Do NOT mock session at the transport level — mock the `ClientSession` object directly.
This is simpler and tests the agent logic without transport concerns.

---

## Effort Estimate

| Task | Points | Files |
|------|--------|-------|
| mcp/client.py (TDD) | 2 | api_agent/mcp/client.py, tests/mcp/test_mcp_client.py |
| mcp/discovery.py (TDD) | 3 | api_agent/mcp/discovery.py, tests/mcp/test_mcp_discovery.py |
| config.py additions | 1 | api_agent/config.py, tests/test_mcp_config.py |
| context.py + routing | 2 | api_agent/context.py, api_agent/tools/query.py, tests/test_mcp_routing.py |
| mcp_agent.py (TDD) | 3 | api_agent/agent/mcp_agent.py, tests/test_mcp_agent.py |
| Recipe support (TDD) | 3 | (mcp_agent.py additions), tests/test_mcp_recipe.py |
| **Total** | **~13** | ~8 new files, 4 modified |

Decomposable into 2 PRs:
- **PR A**: Client + Discovery + Config + Routing (no agent yet — just the plumbing, ~6pts)
- **PR B**: Agent + Recipes + full test coverage (~7pts)
