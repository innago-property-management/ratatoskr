# MCP Facade — DECISION.md

**Version:** 1.0 (Initial)
**Date:** 2026-03-10
**Author:** Claude Code (spec-driven-development workflow)

---

## Use Case

Ratatoskr currently facades REST, GraphQL, and gRPC APIs behind a single NL intelligence layer.
The MCP ecosystem is exploding — users are deploying MCP servers for GitHub, filesystems,
databases, Jira, Slack, and dozens of other services. A user who wants to ask natural language
questions like "What PRs are open against my feature branch?" or "Which Jira tickets are
blocking release?" has to either (a) connect directly to those MCP servers (consuming all their
tool schemas raw) or (b) build a custom integration.

This feature adds a 4th protocol: **MCP facade**. Ratatoskr wraps a target MCP server so that
an NL query from Claude or another client is translated by the LLM agent into the right tool
calls, results are post-processed through DuckDB, and a clean answer is returned.

```
Client NL query
  → Ratatoskr MCP (_query tool)
    → LLM agent (sees tool list as schema)
      → target MCP server (mcp_call proxy tool)
        → DuckDB post-processing (sql_query)
          → structured answer
```

## Business Value

1. **Progressive disclosure** — Ratatoskr can front large MCP servers (GitHub: 50+ tools)
   without exploding the client's context window. The LLM sees a compact schema; the client
   sees only results.
2. **Composability** — Users with `mcp-proxy` or `mcp-bridge` already describe their MCP
   server inventory in a JSON config. Ratatoskr can discover that inventory via an external
   command, route to the right server by name, and add NL+DuckDB intelligence on top.
3. **Reuse** — The orchestrator, recipe system, DuckDB, allowlist, metrics, and tracing all
   apply without modification. This is a thin 4th protocol, not a new system.
4. **Token efficiency** — Compact schema rendering (tool names + required params only) keeps
   LLM context lean. The full inputSchema is available for the proxy call itself.

## Scope Decisions

### In Scope (MVP)

- `mcp_client.py`: Session management supporting stdio and SSE transports (streamable-http as
  bonus if trivial)
- `mcp_agent.py`: Thin protocol agent following the established orchestrator pattern
- Discovery command: `MCP_DISCOVERY_COMMAND` config — run external binary (e.g. `mcp-proxy list`,
  `mcp-bridge list`), parse JSON, route by `mcp://name`
- Routing: `X-API-Type: mcp` in query.py dispatch + context.py validation
- Config: New settings (`MCP_DISCOVERY_COMMAND`, `MCP_TARGET_TRANSPORT`, `MCP_TARGET_COMMAND`,
  `MCP_TARGET_ARGS`, `MCP_TARGET_ENV`, `ALLOW_ENDPOINTS_MCP`)
- Allowlist filtering: reuse `filtering.py` with tool names as endpoint keys
- Recipes: new `kind: "mcp"` recipe step type in the existing recipe pipeline
- Full TDD: tests first, all paths covered

### Out of Scope (deferred)

- SSE connection pooling / persistent sessions across requests (per-request sessions are safe
  and correct for MVP; pooling is an optimization)
- Multi-target fan-out (query multiple MCP servers in one NL request)
- Schema reduction (TOON/Haiku) for MCP tool lists (can be added later)
- gRPC-style mutations blocking (MCP tools are already read/write; blocking is caller's job
  via allowlist)
- Streaming MCP tool responses (not a common MCP pattern yet)

### Key Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Session lifecycle | Per-request | stdio cheap; SSE reconnects fast; avoids state leaks |
| Multi-target | One target per request (via `mcp://name` routing) | Keeps orchestrator model clean |
| Discovery refresh | On-demand per request (cached in process) | Simple; discovery is fast |
| URL scheme for named targets | `mcp://name` or `mcp://host:port/path` | Consistent with other protocols using X-Target-URL |
| SSRF for MCP | Skip IP validation for `mcp://` named targets (they are process-local) | stdio never hits network; SSE validated like http |
| Tool name as endpoint key | `tool_name` (just the tool name, no method prefix) | MCP has no HTTP methods; fnmatch patterns like `get_*` or `issues/*` work naturally |
