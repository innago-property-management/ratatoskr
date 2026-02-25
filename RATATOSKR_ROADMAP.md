# Ratatoskr Roadmap

**Status:** Polyglot LLM fork complete. All providers working.

## Completed

### Phase 1: Audit LLM Coupling
- Full audit of upstream `api-agent` LLM coupling points
- Identified 6 files (~800 LOC) requiring changes

### Phase 2-5: Provider Abstraction & Implementation
- `LLMProvider` abstract base class with `complete()` and formatting methods
- `LLMResponse`, `ToolCall`, `ToolDefinition`, `ToolResult` shared types
- **OpenAI provider** — native SDK, function calling, preserves upstream behavior
- **Anthropic provider** — native SDK, tool_use blocks, system message extraction
- **OpenAI-compatible provider** — configurable base_url, retry-without-tools fallback
- Provider factory with lazy singleton initialization
- All agent call sites (`graphql_agent`, `rest_agent`, `extractor`) migrated

### Phase 6: Testing
- 511 tests passing (148 added in test coverage spec)
- Integration tests for both orchestrators
- Safety boundary tests (mutation blocker, execute tool)
- Provider SDK surface tests with recorded fixtures
- Configuration contract tests
- Recipe runner behavioral tests

### Phase 7: CLI & Configuration
- `--provider`, `--api-key`, `--base-url`, `--model` CLI flags
- `API_AGENT_PROVIDER`, `API_AGENT_API_KEY`, `API_AGENT_BASE_URL` env vars
- Alias chains: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` as fallbacks
- Provider-specific defaults (model, API key resolution)

## Future Work

- [ ] **Streaming responses** — Stream agent reasoning and partial results
- [ ] **Mutation support** — Controlled writes with confirmation flows
- [ ] **Schema caching** — Reduce startup latency for repeated connections
- [ ] **Multi-API joins** — Query across multiple APIs in a single request
- [ ] **Recipe sharing** — Export/import learned recipes between instances

## References

- **Upstream:** [agoda-com/api-agent](https://github.com/agoda-com/api-agent)
- **Blog post:** [How to convert any API to MCP](https://medium.com/agoda-engineering/how-to-convert-any-api-to-mcp-with-zero-code-and-zero-deployments-using-apiagent-fa494de8eaee)
