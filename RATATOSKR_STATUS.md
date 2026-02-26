# Ratatoskr — Multi-Provider LLM Support

**Fork of:** [agoda-com/api-agent](https://github.com/agoda-com/api-agent)  
**Goal:** Replace hardcoded OpenAI Agents SDK with provider-abstracted LLM layer

## Status: ~95% Complete

### ✅ Completed

**Phase 1: Audit (2026-02-20)**
- Full coupling analysis documented in `docs/llm-coupling-audit.md`
- Identified all Agents SDK touchpoints
- Designed provider abstraction architecture

**Phases 2-4: Provider Layer (2026-02-20)**
- `api_agent/llm/` package created with:
  - `provider.py` — Abstract base class with tool-calling loop
  - `types.py` — Normalized LLMResponse, ToolCall, ToolDefinition
  - `tools.py` — `@tool` decorator (replacement for Agents SDK `@function_tool`)
  - `factory.py` — `create_provider()` from config
  - `openai_provider.py` — Native OpenAI SDK (no Agents SDK)
  - `anthropic_provider.py` — Native Anthropic SDK with tool_use blocks
  - `openai_compat.py` — OpenAI-compatible endpoints (Ollama, LM Studio, vLLM)

**Phase 5: Replace Call Sites (2026-02-20)**
- `api_agent/agent/model.py` — Replaced entirely, now creates shared provider
- `api_agent/agent/rest_agent.py` — Rewired to `provider.run_tool_loop()`
- `api_agent/agent/graphql_agent.py` — Rewired to `provider.run_tool_loop()`
- `api_agent/agent/schema_search.py` — Uses new `@tool` decorator
- `api_agent/recipe/extractor.py` — Uses `provider.complete()` for extraction
- `api_agent/recipe/common.py` — Removed Agents SDK types
- `api_agent/config.py` — Added `PROVIDER`, `API_KEY`, `BASE_URL` settings (backward compatible)
- `pyproject.toml` — Removed `openai-agents`, added `openai` and `anthropic` SDKs
- `api_agent/tracing.py` — Replaced OpenAI Agents instrumentation with OpenAI/Anthropic

**Phase 7: CLI (Already Existed)**
- `--provider`, `--model`, `--api-key`, `--base-url` flags already present in `__main__.py`

### ✅ Verified

**Provider Creation & Completion:**
```bash
uv run python test_providers.py
```
- ✓ OpenAI provider: works
- ⊘ Anthropic: skipped (no API key in test env)
- ⊘ OpenAI-compat: skipped (no local endpoint)

### 🟡 Not Fully Tested

**Phase 6: Integration Testing**
- Basic provider layer works (unit tests pass)
- Full REST/GraphQL agent flow not tested against real APIs
- Existing REST client has URL construction bug (pre-existing, not related to provider changes)

### 📋 TODO (Optional)

1. **Real Integration Tests**
   - Test against petstore OpenAPI spec
   - Test against Rick & Morty GraphQL
   - Verify recipe extraction still works

2. **Documentation**
   - Update main README.md with provider selection examples
   - Add provider-specific notes (Anthropic tool_use format, etc.)

3. **Performance Testing**
   - Compare token usage across providers
   - Verify recipe caching works with all providers

## Usage

### OpenAI (default)
```bash
uv run api-agent --provider openai --model gpt-4o
```

### Anthropic
```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run api-agent --provider anthropic --model claude-sonnet-4-20250514
```

### OpenAI-Compatible (Ollama, etc.)
```bash
uv run api-agent --provider openai-compat \
  --base-url http://localhost:11434/v1 \
  --model llama3
```

## Architecture

**Key Decision:** The tool-calling loop is provider-agnostic. Only wire format (how tool calls are sent/received) differs per provider.

**Provider Interface:**
- `complete(messages, tools, ...) -> LLMResponse` — Single completion call
- `run_tool_loop(instructions, user_message, tool_defs, ...) -> RunResult` — Full agent loop
- `format_tools()`, `format_tool_results()`, `format_assistant_tool_calls()` — Wire format translation

**Tool Decorator:**
- Old: `@function_tool` from Agents SDK
- New: `@tool` returns `ToolDefinition` with name, description, parameters, function

**Providers:**
- OpenAI: Standard tool_calls on assistant messages
- Anthropic: tool_use content blocks + tool_result content blocks
- OpenAI-compat: Same as OpenAI (may fallback to no-tools if unsupported)

## Key Files Changed

| File | Change |
|------|--------|
| `api_agent/llm/*.py` | **NEW** — Provider abstraction layer |
| `api_agent/agent/model.py` | **REPLACED** — Provider factory |
| `api_agent/agent/rest_agent.py` | **REWIRED** — Uses provider.run_tool_loop() |
| `api_agent/agent/graphql_agent.py` | **REWIRED** — Uses provider.run_tool_loop() |
| `api_agent/recipe/extractor.py` | **REWIRED** — Uses provider.complete() |
| `api_agent/config.py` | **UPDATED** — Added PROVIDER, API_KEY, BASE_URL |
| `pyproject.toml` | **UPDATED** — Swapped dependencies |
| `api_agent/tracing.py` | **UPDATED** — New instrumentation |

## Estimated Effort

| Phase | Estimated | Actual |
|-------|-----------|--------|
| Phase 1: Audit | 1 day | 0.5 day |
| Phase 2-4: Provider layer | 3 days | 1 day |
| Phase 5: Replace call sites | 1.5 days | 1 day |
| Phase 6: Testing | 1 day | 0.5 day (partial) |
| **Total** | **6.5 days** | **~3 days** |

## Next Steps

If continuing:
1. Fix REST client URL construction bug (pre-existing)
2. Add full integration tests against real APIs
3. Test recipe extraction with each provider
4. Update main README with provider examples

---

*Sigyn — 2026-02-20 to 2026-02-26*
