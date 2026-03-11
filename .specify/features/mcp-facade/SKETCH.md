# MCP Facade — Initial Sketch

**Date:** 2026-03-10
**Context:** Feasibility exploration for facading another MCP server through Ratatoskr

## Concept

Ratatoskr currently facades REST/GraphQL/gRPC APIs with NL intelligence. This feature extends that to MCP servers — the flow becomes:

```
Client NL query → Ratatoskr MCP → LLM agent → target MCP tools → DuckDB post-processing → results
```

## Architecture Mapping

| Current (REST/GraphQL/gRPC) | MCP equivalent |
|---|---|
| Fetch OpenAPI/introspection/reflection | `session.list_tools()` |
| Build agent tools (`rest_call`, `graphql_query`) | `mcp_call` tool proxying to target |
| Schema search | Search tool names/descriptions/schemas |
| DuckDB post-processing | Same — works on any JSON output |
| Endpoint allowlist (fnmatch) | Filter tool names with existing infra |
| Recipes | New `kind: "mcp"` step type |

## Transport

Both stdio and SSE/streamable-http via Python `mcp` SDK client:

```python
# stdio — spawn child process
from mcp.client.stdio import stdio_client
async with stdio_client(StdioServerParameters(
    command="bun", args=["run", "/path/to/server.ts"],
    env={...},
)) as (read, write):
    async with ClientSession(read, write) as session:
        tools = await session.list_tools()

# SSE/streamable-http — connect to running server
from mcp.client.sse import sse_client
async with sse_client("http://localhost:3000/mcp") as (read, write):
    ...
```

Config: `MCP_TARGET_TRANSPORT: "stdio" | "sse"` with `MCP_TARGET_COMMAND` / `MCP_TARGET_URL`.

## Config via Executable (Discovery)

Instead of static config, discover targets by running an external command:

```python
# config.py
MCP_DISCOVERY_COMMAND: str = ""  # e.g. "mcp-proxy list" or "mcp-bridge list"
```

```bash
$ mcp-proxy list
[
  {"name": "github", "type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
  {"name": "filesystem", "type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
]
```

Routing via header: `X-API-Type: mcp`, `X-Target-URL: mcp://github`
Or dedicated instance with `DEFAULT_TARGET_URL` + `DEFAULT_API_TYPE: mcp`.

## Agent Sketch (`mcp_agent.py`)

Thin wrapper over orchestrator (same pattern as graphql/rest/grpc):

```python
"""MCP agent — facades another MCP server with NL intelligence."""

import json
import time
from contextvars import ContextVar
from typing import Any

import structlog

from ..config import settings
from ..context import RequestContext
from ..llm.tools import tool
from ..metrics import record_request, record_schema_fetch
from ..sanitize import sanitize_error
from ..tracing import trace_span
from .model import provider
from .orchestrator import (
    AgentContextVars, ProtocolConfig,
    create_sql_query_tool, format_tool_response,
    make_logger, run_agent_orchestration, store_result,
)
from .contextvar_utils import safe_append_contextvar_list

logger = structlog.get_logger(__name__)
_log = make_logger("[MCP]")

# Context-local storage
_mcp_calls: ContextVar[list] = ContextVar("mcp_calls")
_recipe_steps: ContextVar[list] = ContextVar("mcp_recipe_steps")
_query_results: ContextVar[dict] = ContextVar("mcp_query_results")
_last_result: ContextVar[list] = ContextVar("mcp_last_result")
_raw_schema: ContextVar[str] = ContextVar("mcp_raw_schema")
_sql_steps: ContextVar[list[str]] = ContextVar("mcp_sql_steps")

_ctx_vars = AgentContextVars(
    api_calls=_mcp_calls,
    recipe_steps=_recipe_steps,
    query_results=_query_results,
    last_result=_last_result,
    raw_schema=_raw_schema,
    sql_steps=_sql_steps,
)


async def _discover_tools(session) -> list[dict]:
    """List tools from target MCP server."""
    result = await session.list_tools()
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in result.tools
    ]


def _build_schema_text(tool_defs: list[dict]) -> str:
    """Format tool list as compact schema for LLM context."""
    lines = ["<available_tools>"]
    for t in tool_defs:
        params = t["input_schema"].get("properties", {})
        required = set(t["input_schema"].get("required", []))
        param_strs = []
        for name, prop in params.items():
            req = "!" if name in required else "?"
            param_strs.append(f"{name}{req}: {prop.get('type', 'any')}")
        lines.append(f"{t['name']}({', '.join(param_strs)})")
        if t["description"]:
            lines.append(f"  # {t['description'][:200]}")
    lines.append("</available_tools>")
    return "\n".join(lines)


def _create_mcp_call_tool(session, tool_defs: list[dict]) -> Any:
    """Create the mcp_call tool that proxies to the target server."""
    valid_names = {t["name"] for t in tool_defs}

    async def mcp_call(tool_name: str, arguments: str = "{}",
                       name: str = "data", return_directly: bool = False) -> str:
        """Call a tool on the target MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: JSON string of arguments
            name: Table name for sql_query (default: "data")
            return_directly: Skip LLM processing, return directly
        """
        if tool_name not in valid_names:
            return json.dumps({"success": False,
                "error": f"Unknown tool '{tool_name}'. Use search_schema to find tools."})

        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError as e:
            return json.dumps({"success": False, "error": f"Invalid JSON: {e}"})

        result = await session.call_tool(tool_name, args)

        # Extract text content
        data = None
        for content in result.content:
            if hasattr(content, "text"):
                try:
                    data = json.loads(content.text)
                except json.JSONDecodeError:
                    data = content.text
                break

        if data is None:
            return json.dumps({"success": False, "error": "No content in response"})

        success_result = {"success": True, "data": data}
        stored_data, schema_info = store_result(_ctx_vars, data, name)
        safe_append_contextvar_list(_mcp_calls, {"tool": tool_name, "args": args})
        safe_append_contextvar_list(
            _recipe_steps, {"kind": "mcp", "tool": tool_name, "args": args, "name": name}
        )

        if return_directly:
            from ..recipe import _set_return_directly
            _set_return_directly()

        return await format_tool_response(stored_data, schema_info, name, success_result)

    return tool(mcp_call)


async def process_mcp_query(question: str, ctx: RequestContext,
                            session) -> dict[str, Any]:
    """Process NL query against a target MCP server."""
    t0 = time.monotonic()
    status = "ok"
    try:
        # 1. Discover tools (static, once per request)
        with trace_span("schema.fetch", {"protocol": "mcp"}):
            tool_defs = await _discover_tools(session)

        # 2. Filter by allowlist (reuse existing fnmatch infra)
        # TODO: filter_mcp_tools(tool_defs, config_patterns, header_patterns)

        # 3. Build schema text + raw schema
        schema_text = _build_schema_text(tool_defs)
        raw_schema = json.dumps(tool_defs, indent=2)

        # 4. Create tools for LLM
        mcp_tool = _create_mcp_call_tool(session, tool_defs)
        sql_tool = create_sql_query_tool(_ctx_vars, _log, "Call mcp_call first.")
        tools = [mcp_tool, sql_tool]

        # 5. System prompt
        instructions = _build_system_prompt(tool_defs)

        # 6. Run orchestration
        config = ProtocolConfig(
            agent_type="mcp",
            log_prefix="[MCP]",
            call_key="mcp_calls",
            ctx_vars=_ctx_vars,
            schema_text=schema_text,
            raw_schema=raw_schema,
            provider=provider,
            tools=tools,
            instructions=instructions,
            api_id=f"mcp:{ctx.target_url}",
        )
        result = await run_agent_orchestration(question, config)
        return result.result_dict

    except Exception as e:
        status = "error"
        logger.exception("MCP Agent error")
        return {"ok": False, "data": None, "mcp_calls": [], "error": sanitize_error(e)}
    finally:
        record_request("mcp", status, (time.monotonic() - t0) * 1000)
```

## Effort Estimate

| Piece | Points | Notes |
|-------|--------|-------|
| `mcp_agent.py` | 3 | Thin wrapper, orchestrator does the work |
| `mcp_client.py` | 2 | Session management, stdio+SSE |
| Discovery command | 2 | Run external CLI, parse JSON |
| Routing in `query.py` | 1 | Add `"mcp"` to API type dispatch |
| Filtering | 0 | Existing fnmatch infra |
| Recipes | 3 | New `kind: "mcp"` step type |
| **Total** | **~8** | Decomposable into 2-3 PRs |

## Key Decisions to Make

1. **Session lifecycle**: Per-request (connect/disconnect) vs persistent (pool)?
   - stdio: per-request is fine (cheap process spawn)
   - SSE: persistent connection preferred
2. **Multi-target**: One Ratatoskr instance per MCP target, or multi-target routing?
3. **Discovery refresh**: Static at startup, or periodic re-discovery?
4. **Recipe templating**: How to parameterize MCP tool arguments for recipes?

## Existing Infrastructure That Reuses Cleanly

- Orchestrator pattern (ProtocolConfig, AgentContextVars, run_agent_orchestration)
- DuckDB post-processing (store_result, sql_query, truncation)
- Endpoint allowlist (fnmatch filtering)
- Recipe extraction/execution pipeline
- Metrics, tracing, structured logging
- Error sanitization
- Context isolation (ContextVar + copy_context)
