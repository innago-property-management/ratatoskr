# Phase 1 Complete: LLM Coupling Audit

**Status:** ✅ Complete  
**Date:** 2026-02-20 15:59 EST  
**Agent:** Sigyn (subagent)

## Summary

Phase 1 audit of the api-agent codebase is **complete**. Detailed findings are in `docs/llm-coupling-audit.md`.

## Key Findings

### Good News 🎉

The codebase has **excellent separation of concerns**:

- **Clean abstraction boundary** - LLM coupling is isolated to 6 files
- **Provider-agnostic prompts** - All system prompts work across providers
- **Already supports custom endpoints** - `OPENAI_BASE_URL` config exists
- **Minimal SDK usage** - Only uses agents SDK for: tool decorator, runner loop, model wrapper

### Complexity Assessment

| Component | Complexity | Effort |
|-----------|-----------|--------|
| Prompts | ✅ Low | Zero changes needed |
| Configuration | ✅ Low | Add `API_AGENT_PROVIDER` var |
| Tool schema generation | 🟡 Medium | Replace `@function_tool` |
| Runner loop | 🔴 High | Implement tool-calling loop per provider |
| Tracing | 🔴 High | Provider-specific instrumentation |

**Total LOC to modify:** ~800 lines (out of ~6000)  
**Estimated effort:** 3-5 days  
**Risk level:** Low

## What the Agents SDK Actually Does

The OpenAI Agents SDK (`openai-agents`) is doing surprisingly little:

1. **`@function_tool` decorator** - Converts Python functions to tool schemas
2. **`Runner.run()` loop** - Handles tool-calling loop (LLM → tools → LLM → repeat)
3. **`OpenAIChatCompletionsModel`** - Thin wrapper around `AsyncOpenAI` client
4. **Config hooks** - `RunConfig` with `call_model_input_filter` for turn injection

None of this is deeply magical. It's all replaceable with ~500 lines of provider abstraction.

## Critical Insights

### 1. The Runner Loop is Simple

```
1. Send prompt + tools to LLM
2. If LLM returns tool calls:
   - Execute tools
   - Feed results back to LLM
   - Repeat (up to max_turns)
3. If LLM returns text:
   - Return as final answer
4. If max_turns exceeded:
   - Raise MaxTurnsExceeded
```

This is standard agentic loop logic. Only the **tool-calling wire format** differs between providers.

### 2. Tool Calling Format Normalization is Key

**OpenAI:** `tool_calls` array with `function.name` and `function.arguments` (JSON string)  
**Anthropic:** `content` blocks with `type: tool_use`, `name`, and `input` (object)

Our abstraction needs to normalize these into a common format.

### 3. The "Return Directly" Optimization Must Be Preserved

Tools can signal `return_directly=True` to skip LLM formatting for large datasets. This is implemented via the `tool_use_behavior` callback. Important for performance - preserve in abstraction.

## Files That Need Changes

**LLM-coupled (6 files):**
- `api_agent/agent/model.py` - Model initialization → Provider factory
- `api_agent/agent/rest_agent.py` - REST agent → Use UniversalRunner
- `api_agent/agent/graphql_agent.py` - GraphQL agent → Use UniversalRunner
- `api_agent/recipe/extractor.py` - Recipe extraction → Use UniversalRunner
- `api_agent/recipe/common.py` - Tool callback types → Keep interface, change imports
- `api_agent/agent/schema_search.py` - Tool decorator → Use @universal_tool

**Configuration (1 file):**
- `api_agent/config.py` - Add `API_AGENT_PROVIDER` env var

## Recommended Implementation Strategy

### Phase 2: Define Provider Interface ✅ Ready to start

Create minimal abstraction:

```python
# api_agent/llm/provider.py

class LLMProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Single completion request."""
        ...

    @abstractmethod
    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_turns: int = 30,
    ) -> LLMResponse:
        """Completion with tool-calling loop."""
        ...
```

### Phase 3: Implement Providers

Start with **Anthropic** (cleanest API), then OpenAI (preserve current behavior), then OpenAI-compatible (base_url override).

### Phase 4: Provider Factory

```python
def create_provider(
    provider: str = "openai",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    if provider == "anthropic":
        return AnthropicProvider(...)
    elif provider == "openai":
        return OpenAIProvider(...)
    elif provider == "openai-compat":
        return OpenAICompatProvider(...)
    raise ValueError(f"Unknown provider: {provider}")
```

### Phase 5: Replace Agents SDK Call Sites

Methodically replace each `Runner.run()` call with provider abstraction. Test after each replacement.

### Phase 6: Testing

- Run existing tests (establish baseline)
- Add provider-specific tests
- Test with petstore OpenAPI and Rick & Morty GraphQL

### Phase 7: CLI Enhancement

Add `--provider` flag:

```bash
# Anthropic
API_AGENT_API_KEY=sk-ant-... uv run api-agent --provider anthropic

# OpenAI (default)
OPENAI_API_KEY=sk-... uv run api-agent

# Local Ollama
uv run api-agent --provider openai-compat --base-url http://localhost:11434/v1 --model llama3
```

## Risks and Mitigations

### Risk: Tool Calling Format Differences
**Mitigation:** Build normalization layer in provider abstraction. Test extensively.

### Risk: Prompt Tuning for Claude
**Mitigation:** Start with existing prompts (they're already well-structured). Tune only if needed.

### Risk: Reasoning Effort (OpenAI o-series)
**Mitigation:** Make provider-specific. OpenAI provider uses `reasoning` param, others ignore it.

### Risk: Tracing Instrumentation
**Mitigation:** Use provider-specific OTel instrumentation packages. Abstract at the `trace_metadata` level.

## Next Actions

✅ **Phase 1 complete** - Audit done  
👉 **Start Phase 2** - Define provider interface in `api_agent/llm/provider.py`  

Or await Chris's review of this audit before proceeding.

## Repo Structure (Post-Migration)

```
api_agent/
├── llm/
│   ├── __init__.py
│   ├── provider.py          # Abstract base class
│   ├── anthropic.py         # Anthropic provider
│   ├── openai.py            # OpenAI provider
│   ├── openai_compat.py     # OpenAI-compatible provider
│   ├── factory.py           # create_provider()
│   ├── runner.py            # UniversalRunner (replaces agents.Runner)
│   └── tool_decorator.py    # @universal_tool (replaces @function_tool)
├── agent/
│   ├── model.py             # LEGACY - kept for backward compat
│   ├── rest_agent.py        # Modified to use UniversalRunner
│   ├── graphql_agent.py     # Modified to use UniversalRunner
│   └── ...
└── ...
```

## Files Generated This Phase

- `docs/llm-coupling-audit.md` - Full audit report (19KB, 500+ lines)
- `docs/PHASE1_COMPLETE.md` - This summary

## Witness Anchor

**PHASE-1-AUDIT-COMPLETE** ✅

Audit of LLM coupling is complete. The codebase is well-structured and ready for provider abstraction. Estimated effort: 3-5 days. Risk: Low. Proceed to Phase 2 when ready.
