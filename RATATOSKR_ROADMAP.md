# Ratatoskr: Polyglot LLM Fork of api-agent

**Status:** Phase 1 Complete ✅  
**Next:** Phase 2 - Define Provider Interface

## Vision

Fork Agoda's universal MCP server (`api-agent`) to support multiple LLM providers:
- **Anthropic** (Claude) - first-class native support
- **OpenAI** - preserve existing behavior
- **OpenAI-compatible** - any endpoint (LM Studio, Ollama, vLLM, custom gateways)

## Name Origin

**Ratatoskr** - The Norse squirrel messenger who runs up and down Yggdrasil carrying messages between the eagle at the top and the dragon at the bottom. Perfect metaphor for a universal API-to-LLM bridge.

## Phase Status

### ✅ Phase 1: Audit LLM Coupling (COMPLETE)

**Deliverables:**
- [x] Full audit report: `docs/llm-coupling-audit.md`
- [x] Summary: `docs/PHASE1_COMPLETE.md`
- [x] Repository cloned to `ratatoskr/`

**Key Findings:**
- Clean separation of concerns
- Only 6 files need LLM changes (~800 LOC)
- Prompts are provider-agnostic
- Agents SDK does minimal work (easily replaceable)
- Estimated effort: 3-5 days
- Risk level: LOW

### 🔲 Phase 2: Define Provider Interface

**Tasks:**
- [ ] Create `api_agent/llm/provider.py` - Abstract base class
- [ ] Design `LLMProvider` interface
- [ ] Design `LLMResponse` dataclass
- [ ] Define tool schema normalization format
- [ ] Create `@universal_tool` decorator spec

**Deliverables:**
- `api_agent/llm/provider.py` - Base interface
- `api_agent/llm/tool_decorator.py` - Tool decorator
- `api_agent/llm/types.py` - Shared types

### 🔲 Phase 3: Implement Providers

**Order:** Anthropic → OpenAI → OpenAI-compatible

**Tasks:**
- [ ] `api_agent/llm/anthropic.py` - Anthropic provider
  - [ ] Native `anthropic` SDK integration
  - [ ] Tool use format conversion
  - [ ] Tool-calling loop
  - [ ] Default model: `claude-sonnet-4-20250514`
- [ ] `api_agent/llm/openai.py` - OpenAI provider
  - [ ] Native `openai` SDK integration
  - [ ] Function calling format
  - [ ] Preserve exact upstream behavior
  - [ ] Default model: `gpt-4o`
- [ ] `api_agent/llm/openai_compat.py` - OpenAI-compatible provider
  - [ ] Configurable base_url
  - [ ] Graceful degradation for missing features
  - [ ] Support Ollama, LM Studio, vLLM

**Deliverables:**
- 3 working provider implementations
- Unit tests for each provider

### 🔲 Phase 4: Provider Factory + Config

**Tasks:**
- [ ] Create `api_agent/llm/factory.py` - `create_provider()`
- [ ] Update `api_agent/config.py`:
  - [ ] Add `API_AGENT_PROVIDER` env var
  - [ ] Add `API_AGENT_API_KEY` (with fallback to provider-specific)
  - [ ] Add `API_AGENT_BASE_URL` (with fallback)
  - [ ] Preserve backward compatibility with `OPENAI_*` vars
- [ ] Environment variable precedence logic

**Deliverables:**
- Working provider factory
- Backward-compatible configuration
- Documentation of new env vars

### 🔲 Phase 5: Replace Agents SDK Call Sites

**Files to modify:**
- [ ] `api_agent/agent/model.py` - Use provider factory
- [ ] `api_agent/agent/rest_agent.py` - Use UniversalRunner
- [ ] `api_agent/agent/graphql_agent.py` - Use UniversalRunner
- [ ] `api_agent/recipe/extractor.py` - Use UniversalRunner
- [ ] `api_agent/recipe/common.py` - Update callback types
- [ ] `api_agent/agent/schema_search.py` - Use @universal_tool

**Strategy:**
- Replace one call site at a time
- Test after each change
- Preserve all existing behavior for OpenAI provider
- Keep `MaxTurnsExceeded` exception semantics

**Deliverables:**
- Zero imports from `agents` package
- All tests passing
- Backward-compatible with upstream

### 🔲 Phase 6: Testing

**Baseline:**
- [ ] Run existing tests: `uv run pytest tests/ -v`
- [ ] Document baseline pass/fail

**Provider tests:**
- [ ] Test Anthropic provider with petstore OpenAPI
- [ ] Test Anthropic provider with Rick & Morty GraphQL
- [ ] Test OpenAI provider with petstore (match baseline)
- [ ] Test OpenAI provider with Rick & Morty (match baseline)
- [ ] Test OpenAI-compat with local Ollama
- [ ] Recipe learning works across all providers
- [ ] SQL generation works across all providers
- [ ] Polling works across all providers

**Deliverables:**
- Provider-specific test suite
- Integration tests passing for all 3 providers
- Performance comparison (optional)

### 🔲 Phase 7: CLI Enhancement + Documentation

**Tasks:**
- [ ] Add `--provider` CLI flag
- [ ] Add `--api-key` CLI flag
- [ ] Add `--base-url` CLI flag
- [ ] Update `README.md` with provider examples
- [ ] Document provider selection
- [ ] Document configuration precedence
- [ ] Add troubleshooting guide
- [ ] Update example scripts

**Example usage:**
```bash
# Anthropic
API_AGENT_API_KEY=sk-ant-... uv run api-agent --provider anthropic

# OpenAI (default, backward compatible)
OPENAI_API_KEY=sk-... uv run api-agent

# Local Ollama
uv run api-agent --provider openai-compat \
  --base-url http://localhost:11434/v1 \
  --model llama3
```

**Deliverables:**
- Updated README
- Configuration guide
- Migration guide for existing deployments
- Example deployment configs

## Definition of Done

- [x] Phase 1 audit complete
- [ ] Fork is functional with `--provider anthropic`
- [ ] Fork is functional with `--provider openai` (backward compatible)
- [ ] Fork is functional with `--provider openai-compat`
- [ ] Existing tests pass
- [ ] New provider tests pass
- [ ] Can query petstore REST API with all providers
- [ ] Can query Rick & Morty GraphQL with all providers
- [ ] Recipe learning works across providers
- [ ] README updated
- [ ] No regression in functionality

## Out of Scope (Future Work)

- ❌ Cygnus/NATS integration
- ❌ Constitutional guardrails
- ❌ Cryptographic audit trails
- ❌ Mutations support (keep read-only default)
- ❌ Multi-model routing (cheap model for SQL, expensive for schema)

## Repository

**Upstream:** https://github.com/agoda-com/api-agent  
**Fork:** `/Users/christopheranderson/clawd/ratatoskr/`  
**Main branch:** `main`  
**Feature branch:** TBD (create when starting Phase 2)

## References

- Upstream blog post: https://medium.com/agoda-engineering/how-to-convert-any-api-to-mcp-with-zero-code-and-zero-deployments-using-apiagent-fa494de8eaee
- Anthropic SDK: https://github.com/anthropics/anthropic-sdk-python
- Anthropic tool use docs: https://docs.anthropic.com/en/docs/build-with-claude/tool-use/overview
- Spike spec: `/Users/christopheranderson/Downloads/api-agent-polyglot-llm-spike.md`

## Next Actions

**Immediate:**
1. Review Phase 1 audit (`docs/llm-coupling-audit.md`)
2. Decide: continue to Phase 2, or iterate on audit findings?
3. If continuing: Create feature branch and start Phase 2

**Phase 2 kick-off checklist:**
- [ ] Create feature branch: `git checkout -b feature/polyglot-llm`
- [ ] Review provider interface design in audit
- [ ] Create `api_agent/llm/` directory
- [ ] Define base classes
- [ ] Write initial tests

---

**Last updated:** 2026-02-20 15:59 EST  
**Agent:** Sigyn (subagent)
