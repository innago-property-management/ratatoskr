# LLM Coupling Audit — api-agent → Ratatoskr

**Date:** 2026-02-20
**Auditor:** Sigyn

## Summary

The OpenAI Agents SDK (`openai-agents`) is deeply integrated — it's not just an HTTP client. It provides:

1. **The tool-calling loop** (`Runner.run()`) — the core LLM↔tool execution cycle
2. **Tool definitions** (`@function_tool` decorator) — converts Python functions to LLM-callable tools
3. **Agent configuration** (`Agent()`) — bundles model + instructions + tools + behavior
4. **Model abstraction** (`OpenAIChatCompletionsModel`) — wraps `AsyncOpenAI` client
5. **Run configuration** (`RunConfig`, `ModelSettings`) — per-run settings like reasoning effort
6. **Output interception** (`ToolsToFinalOutputResult`, `FunctionToolResult`) — controls when to short-circuit the loop
7. **Turn management** (`MaxTurnsExceeded`) — caps the tool-calling loop

## Import Map

### `api_agent/agent/model.py` (central config)
```python
from agents import ModelSettings, RunConfig, set_default_openai_api, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.run import CallModelData, ModelInputData
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
```
- Creates shared `AsyncOpenAI` client and `OpenAIChatCompletionsModel`
- Configures `RunConfig` with reasoning settings and turn injection hook
- The `_inject_turn` filter mutates instructions before each LLM call

### `api_agent/agent/rest_agent.py` + `graphql_agent.py`
```python
from agents import Agent, MaxTurnsExceeded, Runner, function_tool
```
- `Agent()` — creates agent with name, model, instructions, tools, `tool_use_behavior`
- `Runner.run()` — executes the agent loop (prompt → LLM → tool calls → execute → feed back → repeat)
- `@function_tool` — decorates Python functions as tools (handles schema extraction from type hints)
- `MaxTurnsExceeded` — caught to return partial results when loop exhausts turns

### `api_agent/recipe/extractor.py`
```python
from agents import Agent, Runner
```
- Uses Agent/Runner for recipe extraction (LLM call with no tools, just structured output)

### `api_agent/recipe/common.py`
```python
from agents import FunctionToolResult, RunContextWrapper
from agents.agent import ToolsToFinalOutputResult
```
- `_tools_to_final_output()` — callback checked after each tool execution
- Returns `ToolsToFinalOutputResult(is_final_output=True, ...)` to short-circuit the loop
- Used for `return_directly` feature (skip LLM processing, return tool output directly)

### `api_agent/agent/schema_search.py`
```python
from agents import function_tool
```
- `@function_tool` on `search_schema` — makes it available as an LLM tool

## What Runner.run() Actually Does

This is the critical piece. The Agents SDK `Runner.run()` manages:

1. Send messages + tool definitions to LLM
2. If LLM returns tool calls → execute each tool function
3. Feed tool results back as messages
4. Check `tool_use_behavior` callback — if it says "final output", stop
5. Repeat until LLM returns text-only response or `max_turns` exceeded
6. Return `result.final_output` (the LLM's final text response)

**We must reimplement this loop.** It's ~50 lines of logic but it's the heart of the system.

## What @function_tool Does

Converts a Python function into a tool definition by:
1. Extracting parameter names, types, and docstrings
2. Generating JSON Schema for tool parameters
3. Wrapping the function for async execution
4. Handling return value serialization

**We need our own tool definition system** that generates both OpenAI-format and Anthropic-format tool schemas from the same Python function.

## Provider-Specific Concerns

### Tool Call Wire Formats
- **OpenAI:** `message.tool_calls[].function.{name, arguments}` → response as `tool` role message
- **Anthropic:** `content[].type="tool_use"` blocks with `{id, name, input}` → `tool_result` content blocks

### System Prompts
- Both support system messages, but Anthropic uses a separate `system` parameter, not a system role message
- Existing prompts should work with both — they're SQL/API focused, not model-specific

### Reasoning/Thinking
- OpenAI: `reasoning.effort` in model settings
- Anthropic: `thinking` parameter with budget tokens
- OpenAI-compat: likely unsupported, ignore gracefully

## Recipe System Impact

The recipe system is **mostly LLM-agnostic** — it caches the execution trace (API calls + SQL), not LLM interactions. However:
- `recipe/extractor.py` uses `Agent`/`Runner` for extraction — needs provider swap
- Recipe storage/matching is pure Python, no LLM dependency

## Recommended Architecture

```
api_agent/llm/
├── __init__.py
├── provider.py      # ABC: LLMProvider with complete() and run_tool_loop()
├── types.py         # LLMResponse, ToolCall, ToolDefinition
├── tools.py         # @tool decorator (replaces @function_tool)
├── factory.py       # create_provider() from config
├── anthropic.py     # Anthropic provider
├── openai.py        # OpenAI provider (native SDK, not Agents SDK)
└── openai_compat.py # OpenAI-compatible provider
```

The key insight: **the tool-calling loop is provider-agnostic logic**. Only the wire format (how tool calls are sent/received) differs per provider. The loop itself (call LLM → parse tool calls → execute → feed back) should live in the base class.

## Estimated Effort

| Phase | Effort | Risk |
|-------|--------|------|
| Provider interface + types | 1 day | Low |
| Tool decorator replacement | 1 day | Medium (must match existing behavior) |
| OpenAI provider | 0.5 day | Low (closest to existing) |
| Anthropic provider | 1 day | Medium (format differences) |
| OpenAI-compat provider | 0.5 day | Low (reuses OpenAI) |
| Replace call sites (agents) | 1 day | High (most breakage risk) |
| Replace call sites (recipes) | 0.5 day | Low |
| Testing | 1 day | Medium |
| **Total** | **~6 days** | |

## Files That Need Changes

| File | Change |
|------|--------|
| `api_agent/agent/model.py` | Replace entirely — becomes provider factory |
| `api_agent/agent/rest_agent.py` | Replace Agent/Runner with provider.run_tool_loop() |
| `api_agent/agent/graphql_agent.py` | Same as rest_agent |
| `api_agent/agent/schema_search.py` | Replace @function_tool with @tool |
| `api_agent/recipe/extractor.py` | Replace Agent/Runner with provider.complete() |
| `api_agent/recipe/common.py` | Remove agents SDK imports, use our types |
| `api_agent/config.py` | Add PROVIDER, API_KEY, BASE_URL settings |
| `api_agent/tracing.py` | Remove OpenAI Agents instrumentor (optional) |
| `pyproject.toml` | Remove openai-agents, add anthropic SDK |
