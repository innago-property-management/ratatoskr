# Ratatoskr Phase 1: Completion Report

**Agent:** Sigyn (subagent)  
**Date:** 2026-02-20 16:02 EST  
**Task:** Phase 1 - Audit LLM coupling in api-agent fork  
**Status:** ✅ **COMPLETE**

---

## Executive Summary

Phase 1 audit of Agoda's `api-agent` codebase is **complete**. The repo has been cloned, analyzed, and documented.

**TL;DR:** The fork is **highly feasible** with **low risk**. The codebase has excellent separation of concerns. Only ~800 lines of code (out of 6000) need modification. Estimated effort: **3-5 days**.

---

## What Was Delivered

### 1. Full Technical Audit
**File:** `docs/llm-coupling-audit.md` (19KB, 500+ lines)

Comprehensive analysis including:
- Dependency surface mapping
- Usage pattern analysis for OpenAI Agents SDK
- Prompt template analysis (all provider-agnostic!)
- Tool calling format comparison (OpenAI vs Anthropic)
- Configuration surface review
- Migration complexity assessment
- File-by-file inventory
- Recommended abstraction boundary

### 2. Phase Summary
**File:** `docs/PHASE1_COMPLETE.md` (7KB)

Quick-reference summary with:
- Key findings
- Complexity matrix
- Files that need changes
- Implementation strategy
- Risk assessment
- Next actions

### 3. Project Roadmap
**File:** `RATATOSKR_ROADMAP.md` (7KB)

7-phase implementation plan:
- ✅ Phase 1: Audit (COMPLETE)
- 🔲 Phase 2: Define provider interface
- 🔲 Phase 3: Implement providers
- 🔲 Phase 4: Provider factory + config
- 🔲 Phase 5: Replace Agents SDK call sites
- 🔲 Phase 6: Testing
- 🔲 Phase 7: CLI enhancement + docs

### 4. Repository
**Location:** `/Users/christopheranderson/clawd/ratatoskr/`

Cloned from `https://github.com/agoda-com/api-agent.git`

---

## Key Findings

### ✅ Good News

1. **Clean Architecture**
   - LLM coupling isolated to 6 files
   - Clear abstraction boundaries
   - Provider-agnostic prompts

2. **Already Designed for Extension**
   - Supports `OPENAI_BASE_URL` override
   - Model name is configurable
   - Uses standard tool-calling patterns

3. **Minimal SDK Usage**
   The OpenAI Agents SDK is only doing:
   - `@function_tool` decorator (generate tool schemas)
   - `Runner.run()` loop (tool-calling loop)
   - `OpenAIChatCompletionsModel` wrapper (thin layer)
   
   All easily replaceable.

4. **No Prompt Lock-In**
   All system prompts are provider-agnostic. They focus on:
   - Tool usage instructions
   - SQL syntax guidance
   - Schema notation
   - Workflow steps
   
   No OpenAI-specific features or formatting.

### 🔍 What Needs Work

| Component | Complexity | LOC | Notes |
|-----------|-----------|-----|-------|
| Tool schema generation | Medium | ~100 | Replace `@function_tool` |
| Runner loop | High | ~200 | Implement per-provider tool-calling loop |
| Provider factory | Low | ~50 | Simple config-based factory |
| Model initialization | Medium | ~100 | Replace OpenAIChatCompletionsModel |
| Tracing | High | ~150 | Provider-specific OTel instrumentation |
| Config | Low | ~50 | Add `API_AGENT_PROVIDER` env var |
| Call site updates | Medium | ~200 | Replace Agents SDK imports |

**Total:** ~800 LOC to modify (out of ~6000 total)

### 🎯 Critical Insight

The Agents SDK is doing **far less** than expected. The core logic is:

```
1. LLM returns tool calls
2. Execute tools
3. Feed results back to LLM
4. Repeat (max_turns)
5. Return final answer
```

This is **standard agentic loop logic**. Only the **wire format** differs between providers.

Our abstraction just needs to normalize:
- OpenAI: `tool_calls` array with `function.name` + `function.arguments` (JSON string)
- Anthropic: `content` blocks with `tool_use` type + `name` + `input` (object)

---

## Recommended Approach

### Start with Anthropic (Cleanest API)

1. Implement Anthropic provider first
2. Use as reference for abstraction design
3. Then add OpenAI (preserve current behavior exactly)
4. Finally add OpenAI-compatible (just base_url override)

### Thin Provider Layer

Don't over-abstract. We need:

```python
class LLMProvider(ABC):
    async def complete(messages, tools, ...) -> LLMResponse:
        """Single completion."""
        ...
    
    async def complete_with_tools(messages, tools, max_turns, ...) -> LLMResponse:
        """Completion with tool-calling loop."""
        ...
```

That's it. The provider handles:
- Tool schema conversion (our format → provider format)
- Tool-calling loop (provider-specific wire format)
- Response parsing (provider format → our format)

### Preserve Key Behaviors

1. **Turn injection** - `call_model_input_filter` hook (add turn count to each prompt)
2. **Return directly** - `tool_use_behavior` callback (skip LLM for large results)
3. **MaxTurnsExceeded** - Exception when agent hits turn limit (return partial results)
4. **Dynamic tool generation** - Recipe tools created at runtime via `create_model()`

All of these are provider-agnostic and should be preserved in the abstraction.

---

## Files to Modify

### LLM-Coupled Files (6 files, ~800 LOC)

1. **`api_agent/agent/model.py`**
   - Replace `OpenAIChatCompletionsModel` with provider factory
   - Keep turn injection hook

2. **`api_agent/agent/rest_agent.py`**
   - Replace `Runner.run()` with `UniversalRunner.run()`
   - Replace `@function_tool` with `@universal_tool`
   - Keep all prompt logic

3. **`api_agent/agent/graphql_agent.py`**
   - Same as rest_agent.py

4. **`api_agent/recipe/extractor.py`**
   - Replace `Runner.run()` (simple single-turn completion)

5. **`api_agent/recipe/common.py`**
   - Update tool callback types (keep interface, change imports)

6. **`api_agent/agent/schema_search.py`**
   - Replace `@function_tool` with `@universal_tool`

### Configuration (1 file, ~50 LOC)

7. **`api_agent/config.py`**
   - Add `API_AGENT_PROVIDER` env var
   - Add backward-compatible aliases for `OPENAI_*` vars

### New Files to Create (~500 LOC)

```
api_agent/llm/
├── __init__.py
├── provider.py          # Abstract base class (~100 LOC)
├── anthropic.py         # Anthropic provider (~150 LOC)
├── openai.py            # OpenAI provider (~150 LOC)
├── openai_compat.py     # OpenAI-compatible provider (~50 LOC)
├── factory.py           # create_provider() (~50 LOC)
└── tool_decorator.py    # @universal_tool (~100 LOC)
```

---

## Risk Assessment

### Low Risk ✅

- Clean architecture with isolated coupling
- Small code surface to modify
- Provider-agnostic prompts
- No proprietary OpenAI features used
- Standard tool-calling patterns
- Comprehensive test suite exists

### Medium Risk 🟡

- Tool calling format differences (mitigated by normalization layer)
- Prompt tuning may be needed for Claude (start with existing, tune if needed)
- Tracing instrumentation requires provider-specific packages

### High Risk ❌

- None identified

**Overall Risk:** **LOW** ✅

---

## Effort Estimate

| Phase | Effort | Confidence |
|-------|--------|-----------|
| Phase 2: Define interface | 0.5 days | High |
| Phase 3: Implement providers | 1.5 days | High |
| Phase 4: Factory + config | 0.5 days | High |
| Phase 5: Replace call sites | 1.0 days | Medium |
| Phase 6: Testing | 1.0 days | Medium |
| Phase 7: CLI + docs | 0.5 days | High |

**Total:** 5 days (with buffer)  
**Range:** 3-5 days for experienced Python developer

---

## Next Steps

### Option A: Continue to Phase 2 (Recommended)

**Action items:**
1. Review audit findings
2. Create feature branch: `git checkout -b feature/polyglot-llm`
3. Start Phase 2: Define provider interface
4. Create `api_agent/llm/provider.py`
5. Implement base classes and types

**Timeline:** Can start immediately

### Option B: Iterate on Audit

**If you need:**
- Deeper analysis of specific components
- Prototype a provider implementation first
- Different abstraction approach

**Action items:**
1. Provide feedback on audit
2. Request specific deep-dives
3. Re-run audit with new focus

---

## Witness Anchor

**PHASE-1-AUDIT-COMPLETE** ✅

Phase 1 is complete. All deliverables ready for review:
- Full audit: `docs/llm-coupling-audit.md`
- Summary: `docs/PHASE1_COMPLETE.md`
- Roadmap: `RATATOSKR_ROADMAP.md`
- This report: `PHASE1_REPORT.md`

Repository cloned to: `/Users/christopheranderson/clawd/ratatoskr/`

Ready to proceed to Phase 2 on your signal.

---

**Questions or feedback?** Review the full audit in `docs/llm-coupling-audit.md` for technical details.
