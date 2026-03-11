# MCP Facade — Implementation Plan

**Version:** 1.0
**Date:** 2026-03-10

---

## Approach: TDD Red-Green-Refactor, Two PRs

All tasks follow the **test-first** discipline: write the failing test, implement until green,
refactor. Tests are written for the interface the consuming code needs (ISP — consumer controls
the interface). Cygnus supervised agents can execute PR A tasks and PR B tasks in parallel
once the module boundaries are agreed.

---

## PR A: Plumbing (Client + Discovery + Config + Routing)

**Branch:** `feat/mcp-facade-plumbing`
**Estimated points:** 6
**Goal:** Everything needed for MCP connectivity except the agent itself. At the end of PR A,
an operator can configure MCP targets and the routing is wired — but no NL queries yet.

### Task A1: `api_agent/mcp/__init__.py` and `api_agent/mcp/client.py`

**Test first:** `tests/mcp/__init__.py` + `tests/mcp/test_mcp_client.py`

Test cases:
- `test_stdio_session_initializes`: mock `stdio_client` and `ClientSession`; verify
  `session.initialize()` is called and session is yielded.
- `test_http_session_prefers_streamablehttp`: mock `streamablehttp_client`; verify used first.
- `test_http_session_falls_back_to_sse`: mock `streamablehttp_client` to raise; verify
  `sse_client` is used as fallback.
- `test_session_cleanup_on_exception`: verify context manager exits cleanly even if `yield`
  body raises.

Implementation:
```
api_agent/mcp/__init__.py          (empty)
api_agent/mcp/client.py
  - mcp_stdio_session(params) -> AsyncContextManager[ClientSession]
  - mcp_http_session(url) -> AsyncContextManager[ClientSession]
```

Dependencies verified: `mcp` 1.26.0 ships `mcp.client.stdio.stdio_client`,
`mcp.client.sse.sse_client`, `mcp.client.streamable_http.streamablehttp_client`,
`mcp.ClientSession`. All confirmed available. No new pip packages needed.

---

### Task A2: `api_agent/mcp/discovery.py`

**Test first:** `tests/mcp/test_mcp_discovery.py`

Test cases:
- `test_discover_stdio_server`: mock subprocess; verify normalizes to `MCPServerDescriptor`.
- `test_discover_sse_server`: mcp-proxy returns `{"type": "sse", "url": "..."}`.
- `test_discovery_caching`: run command twice; mock called once.
- `test_discovery_invalid_json`: command outputs garbage; verify clean error.
- `test_discovery_nonzero_exit`: command exits 1; verify clean error.
- `test_discovery_mcp_proxy_format`: mcp-proxy wraps in `upstreamServers` key; normalize.
- `test_discovery_mcp_bridge_format`: mcp-bridge uses `localServers`; normalize.
- `test_resolve_named_target_stdio`: `mcp://github` → MCPServerDescriptor(transport="stdio").
- `test_resolve_named_target_sse`: `mcp://github` resolves to SSE descriptor.
- `test_resolve_http_target_direct`: `http://localhost:3001/mcp` → SSE descriptor (no name lookup).
- `test_resolve_unknown_name`: `mcp://unknown`; returns error.

Implementation:
```
api_agent/mcp/discovery.py
  @dataclass(frozen=True)
  class MCPServerDescriptor: name, transport, command, args, env, url

  _DISCOVERY_CACHE: dict[str, list[MCPServerDescriptor]] = {}

  async def discover_servers(command: str) -> list[MCPServerDescriptor]
  async def resolve_target(target_url: str, settings) -> MCPServerDescriptor
  def _normalize_descriptor(raw: dict) -> MCPServerDescriptor
  def _parse_discovery_output(raw_json: str) -> list[dict]  (handles wrapper keys)
```

---

### Task A3: Config additions

**Test first:** `tests/test_mcp_config.py` (add to existing or create new)

Test cases (add to `tests/test_config.py` under a new section):
- `test_mcp_discovery_command_default_empty`
- `test_mcp_target_transport_default_empty`
- `test_mcp_target_command_default_empty`
- `test_mcp_target_args_default_empty`
- `test_mcp_target_env_default_json_object`
- `test_allow_endpoints_mcp_default_empty`

Implementation: Add to `api_agent/config.py` Settings class:
```python
# MCP facade
MCP_DISCOVERY_COMMAND: str = ""
MCP_TARGET_TRANSPORT: str = ""
MCP_TARGET_COMMAND: str = ""
MCP_TARGET_ARGS: str = ""
MCP_TARGET_ENV: str = "{}"
ALLOW_ENDPOINTS_MCP: str = ""
```

---

### Task A4: `context.py` and `tools/query.py` routing

**Test first:** Add to existing `tests/test_middleware_routing.py` or create
`tests/test_mcp_routing.py`.

Test cases:
- `test_api_type_mcp_accepted`: `X-API-Type: mcp` does not raise `MissingHeaderError`.
- `test_api_type_mcp_validation_error_message`: ensure error message updated (was "graphql, rest,
  or grpc").
- `test_mcp_scheme_allowed_in_validate_target_url`: `mcp://github` passes validation.
- `test_mcp_http_target_validated_by_ssrf`: `http://localhost` blocked by private IP check.
- `test_query_routes_to_mcp_agent`: `api_type=mcp` calls `process_mcp_query` (mock it).
- `test_query_mcp_calls_key_in_response`: response uses `mcp_calls` key.

Implementation changes:
- `api_agent/context.py`:
  - Line 167: extend allowed types to include `"mcp"`.
  - `validate_target_url`: add `elif api_type == "mcp":` — allow `mcp://` scheme (no IP check),
    allow `http`/`https` schemes (with IP check).
  - Add `"mcp"` to default `ALLOWED_URL_SCHEMES`.
- `api_agent/tools/query.py`:
  - Import `process_mcp_query` from `api_agent.agent.mcp_agent`.
  - Add `elif req_ctx.api_type == "mcp":` dispatch branch.
  - Add `"mcp": "mcp_calls"` to `calls_key_map`.

---

## PR B: Agent + Recipes

**Branch:** `feat/mcp-facade-agent`
**Estimated points:** 7
**Goal:** Full NL query capability, recipe extraction and replay. At the end of PR B, operators
can ask NL questions against MCP servers through Ratatoskr.

### Task B1: `api_agent/agent/mcp_agent.py` (core, no recipes yet)

**Test first:** `tests/test_mcp_agent.py`

Mock pattern: Create a `MockMCPSession` fixture (replaces `ClientSession`):
```python
class MockMCPSession:
    def __init__(self, tools, call_results):
        self._tools = tools
        self._call_results = call_results

    async def list_tools(self):
        # Returns mock ListToolsResult with .tools

    async def call_tool(self, name, args):
        # Returns mock CallToolResult with .content
```

Test cases (non-recipe):
- `test_process_mcp_query_success`: FakeLLMProvider calls `mcp_call` then returns summary.
  Assert `result["ok"]`, `result["mcp_calls"]`, `result["data"]`.
- `test_process_mcp_query_sql_postprocess`: FakeLLMProvider calls `mcp_call` then `sql_query`.
  Assert DuckDB result propagated.
- `test_process_mcp_query_tool_not_found`: LLM tries to call non-existent tool. Assert error
  response from `mcp_call`, LLM gets chance to recover.
- `test_process_mcp_query_target_resolution_error`: discovery fails. Assert `ok=False`.
- `test_process_mcp_query_allowlist_filters_tools`: `ALLOW_ENDPOINTS_MCP=read_*`. Only
  `read_*` tools appear in schema_text. LLM tries `write_repo` → blocked.
- `test_process_mcp_query_zero_tools_after_filter`: all tools filtered → `ok=False` with
  allowlist error.
- `test_process_mcp_query_empty_tool_list`: server returns no tools → graceful error.
- `test_schema_text_compact_format`: verify `_build_schema_text` output format
  (required `!`, optional `?`, description truncated).
- `test_record_request_called`: verify `record_request("mcp", ...)` is called in finally block.
- `test_record_schema_fetch_called`: verify `record_schema_fetch(...)` called after list_tools.
- `test_max_turns_exceeded_returns_partial`: FakeLLMProvider exhausts turns → `ok=True` partial.

Monkeypatch targets:
- `api_agent.agent.mcp_agent._open_session` (the session factory — a thin function that wraps
  the client module, easily monkeypatched)
- `api_agent.agent.mcp_agent.provider` and `api_agent.agent.model._provider` (standard pattern)
- `api_agent.agent.mcp_agent.settings` for allowlist tests

Implementation:
```
api_agent/agent/mcp_agent.py
  _mcp_calls, _recipe_steps, _query_results, _last_result, _raw_schema, _sql_steps (ContextVars)
  _ctx_vars = AgentContextVars(...)
  _log = make_logger("[MCP]")

  def _build_schema_text(tool_defs) -> str
  def _build_system_prompt() -> str
  def _create_mcp_call_tool(session, allowed_tool_defs, ctx_vars) -> Any
  async def _open_session(descriptor) -> AsyncContextManager[ClientSession]  ← monkeypatch seam
  async def process_mcp_query(question, ctx) -> dict[str, Any]
  async def _process_mcp_query_inner(question, ctx) -> dict[str, Any]
```

---

### Task B2: Recipe support in `mcp_agent.py`

**Test first:** `tests/test_mcp_recipe.py`

Test cases:
- `test_recipe_extraction_after_success`: successful run → `maybe_extract_and_save_recipe`
  called with `api_type="mcp"` and step kind `"mcp"`.
- `test_recipe_step_recorded`: `mcp_call` appends to `_recipe_steps` with correct structure.
- `test_recipe_replay_calls_target_tool`: step executor opens session, calls `session.call_tool`
  with rendered args.
- `test_recipe_replay_template_substitution`: `args: {"repo": "{{owner}}/{{name}}"}` with
  `params={"owner": "acme", "name": "api"}` → calls with `{"repo": "acme/api"}`.
- `test_recipe_replay_invalid_step_kind`: step with `kind: "graphql"` → executor returns error.
- `test_recipe_no_extraction_on_failure`: `ok=False` run → extraction not called.
- `test_recipe_no_extraction_on_max_turns`: max-turns exceeded → extraction not called.

Implementation additions to `mcp_agent.py`:
```python
def _make_mcp_step_executor_factory(descriptor: MCPServerDescriptor):
    def factory(recipe_id: str):
        async def mcp_step_executor(step_idx, step, params, results): ...
        return mcp_step_executor
    return factory
```

Also wire `recipe_step_executor_factory` into `ProtocolConfig` and call
`maybe_extract_and_save_recipe` after orchestration (same pattern as graphql_agent).

---

## Parallel Execution Notes (for Cygnus)

PR A tasks are sequential within PR A (A1 → A2 → A3 → A4).
PR B tasks are sequential within PR B (B1 → B2).
PR A and PR B cannot run fully in parallel because B depends on A4 (routing) for integration
testing — but B1/B2 agent code can be written in a separate branch before PR A merges.

When running with Cygnus supervised agents:
- Agent 1: PR A (plumbing)
- Agent 2: Start B1 implementation against main branch (no routing yet — use unit tests only)
- Merge PR A → rebase Agent 2's branch → complete B1 integration tests → PR B

---

## Acceptance Checklist

Before each PR is raised:
- [ ] `uv run pytest tests/ -v` passes (all existing + new tests)
- [ ] `uv run ruff check api_agent/` passes
- [ ] `uv run ruff format api_agent/` no-op
- [ ] `uv run ty check` passes
- [ ] New test files have `# type: ignore` where mcp stubs are incomplete
- [ ] MEMORY.md updated with PR number and status
- [ ] No new top-level `pip install` packages (mcp already transitive)

---

## Research Notes

### MCP SDK 1.26.0 — Verified Available Imports

```python
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
```

`StdioServerParameters` fields: `command`, `args`, `env`, `cwd`, `encoding`,
`encoding_error_handler`.

`streamablehttp_client` yields `(read, write, get_session_id)` — 3-tuple (note: different from
2-tuple for stdio/sse). The `get_session_id` is ignored in our wrapper.

### mcp-proxy list output format (observed)

`mcp-proxy list` writes to stdout the config's `upstreamServers` array as JSON. Each entry:
```json
{"name": "github", "type": "stdio", "command": "npx", "args": [...], "env": {...}}
```
Ratatoskr's discovery parser looks for `upstreamServers` wrapper first; falls back to root array.

### mcp-bridge list output format (observed)

`mcp-bridge list` calls the `list_servers` MCP tool internally, but there is no direct CLI
`list` subcommand in the current version. The config file itself (`localServers` array) is the
source of truth. Ratatoskr supports reading the config JSON directly via
`MCP_DISCOVERY_COMMAND=cat /path/to/config.json` or via the `localServers` wrapper key
normalization.

### No new dependencies needed

The `mcp` package is already present as a transitive dependency of `fastmcp>=3.0.0`. The
`stdio_client`, `sse_client`, and `streamablehttp_client` are all in `mcp` 1.26.0. Confirmed.
