# Implementation Plan: TOON Format Integration

**Branch**: `001-toon-format-integration` | **Date**: 2026-02-23 | **Spec**: [spec.md](./spec.md)  
**Input**: Feature specification from `/specs/001-toon-format-integration/spec.md`

## Summary

Integrate TOON (Tabular Object Oriented Notation) serialization format into Ratatoskr's LLM agent loop to reduce token consumption by 30-60%. TOON combines YAML-like indentation for objects with CSV-like tabular arrays, optimizing the exact data patterns API agents produce (lists of uniform objects). 

Primary integration points:
1. **Tool results** (P1, highest impact): Encode GraphQL/REST API responses as TOON before feeding to LLM
2. **Schema context** (P2): Compress GraphQL introspection and OpenAPI specs in system prompts
3. **DuckDB results** (P3): Format SQL post-processing results as TOON tabular arrays
4. **MCP output** (P4): Allow MCP clients to request TOON via header

Technical approach: Wrap `toon-format` (v0.9.x beta) with error handling and JSON fallback, integrate with `tiktoken` for token counting, export OpenTelemetry metrics for A/B testing, and provide per-request and global configuration toggles.

## Technical Context

**Language/Version**: Python 3.11 (minimum 3.8 per toon-format requirement)  
**Primary Dependencies**: 
- `toon-format` v0.9.x (beta, from PyPI or GitHub)
- `tiktoken` (optional, for token counting)
- `opentelemetry-api` (already present for tracing)
- `pydantic` (already present for config)

**Storage**: N/A (no persistent storage for TOON; encoding is per-request)  

**Testing**: `pytest` (already present)

**Target Platform**: Linux/macOS server (MCP server via stdio/HTTP)

**Project Type**: Library/service (MCP server that wraps APIs)

**Performance Goals**: 
- TOON encoding <10ms for typical API responses (<10KB)
- TOON encoding <50ms for large responses (<100KB)
- <1% increase in end-to-end request latency

**Constraints**:
- Must not break existing JSON-based workflows (opt-in, default disabled)
- Must fall back to JSON on any TOON encoding error (100% reliability)
- Must work with Anthropic, OpenAI, and OpenAI-compatible providers
- Must provide observability for A/B testing (token counts, compression ratios)

**Scale/Scope**:
- Encoding ~100-1000 tool results per day (depending on agent usage)
- Typical API responses: 10-100 objects, 5-20 fields each
- GraphQL schemas: 10K-50K tokens (introspection JSON)
- OpenAPI specs: 20K-100K tokens

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

**Note**: Constitution file is currently a template with placeholders. Based on Ratatoskr's existing architecture (from Phase 1 audit), we can infer principles:

### Inferred Principles (from codebase patterns)

1. **Provider abstraction**: LLM providers are isolated behind `LLMProvider` interface ✅  
   - TOON integration respects this: encoding happens in shared tool loop, not per-provider

2. **Graceful degradation**: System must not fail on LLM/tool errors ✅  
   - TOON integration includes JSON fallback on any encoding error

3. **Observability-first**: All LLM operations traced via OpenTelemetry ✅  
   - TOON integration adds span attributes, metrics for token savings

4. **Configuration-driven**: Features controlled via env vars and config ✅  
   - TOON integration uses `API_AGENT_ENABLE_TOON`, `API_AGENT_TOON_*` env vars

5. **Test coverage**: Existing test suite has >80% coverage ✅  
   - TOON integration requires unit tests (encoder), integration tests (agent loop)

**Compliance**: All gates pass. No violations to justify.

## Project Structure

### Documentation (this feature)

```text
specs/001-toon-format-integration/
├── plan.md              # This file
├── research.md          # Phase 0 output (TOON library investigation)
├── data-model.md        # Phase 1 output (data structures for TOON integration)
├── quickstart.md        # Phase 1 output (user guide for enabling TOON)
├── contracts/           # Phase 1 output (TOON encoding API contract)
│   └── toon_encoder.md  # TOONEncoder interface specification
├── checklists/
│   └── requirements.md  # Requirements validation (already created)
└── tasks.md             # Phase 2 output (NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
api_agent/
├── llm/
│   ├── provider.py          # [MODIFY] Add TOON encoding to tool loop
│   ├── types.py             # [MODIFY] Add format metadata to ToolResult
│   ├── toon_encoder.py      # [NEW] TOON encoding wrapper with fallback
│   ├── anthropic_provider.py
│   ├── openai_provider.py
│   └── openai_compat.py
├── agent/
│   ├── graphql_agent.py     # [MODIFY] TOON-encode schema context
│   └── rest_agent.py        # [MODIFY] TOON-encode schema context
├── executor.py              # [MODIFY] TOON-encode DuckDB results
├── config.py                # [MODIFY] Add TOON feature flags
└── tracing.py               # [MODIFY] Add TOON OTel attributes

tests/
├── test_llm/
│   ├── test_toon_encoder.py       # [NEW] Unit tests for TOONEncoder
│   ├── test_toon_integration.py   # [NEW] Integration tests (agent loop)
│   └── test_toon_observability.py # [NEW] OTel metrics tests
├── test_graphql_toon.py           # [NEW] GraphQL schema TOON encoding
├── test_rest_toon.py              # [NEW] REST schema TOON encoding
└── test_executor.py               # [MODIFY] Add DuckDB TOON tests

docs/
├── toon-integration.md            # [NEW] User guide for TOON feature
└── configuration.md               # [MODIFY] Add TOON env vars

.env.example                       # [MODIFY] Add TOON config examples
pyproject.toml                     # [MODIFY] Add toon-format, tiktoken deps
```

**Structure Decision**: Single project structure (Option 1). Ratatoskr is a monolithic Python package with submodules for agents, LLM providers, GraphQL/REST clients, and execution. TOON integration follows this pattern: new `toon_encoder.py` module in `api_agent/llm/`, modifications to existing agent and executor modules.

## Complexity Tracking

No constitution violations. This section is empty.

---

## Phase 0: Research & Technical Decisions

**Goal**: Resolve all NEEDS CLARIFICATION items from Technical Context and answer open questions from spec.

### Research Tasks

1. **TOON library stability investigation**
   - Audit `toon-format` v0.9.x API stability
   - Check for open issues related to encoding failures
   - Review changelog for breaking changes between v0.9.0 and latest
   - Decision: PyPI pinned version vs vendoring

2. **LLM prompt engineering for TOON**
   - Test whether Claude and GPT-4 natively understand TOON format
   - Prototype: Send TOON-encoded tool results, measure LLM interpretation accuracy
   - Compare: JSON baseline vs TOON (same queries, same data)
   - Decision: Whether to add TOON format hints to system prompts

3. **Token counting integration**
   - Validate `tiktoken` accuracy for OpenAI models
   - Compare `tiktoken` vs Anthropic's token counting API for Claude
   - Measure error rate: is `tiktoken` accurate enough for Anthropic? (target <10%)
   - Decision: Use `tiktoken` universally or integrate Anthropic's API

4. **Fallback strategy granularity**
   - Design: Should fallback be per-field (single tool result) or per-request?
   - Trade-off: Per-field minimizes impact but adds complexity
   - Decision: Fallback scope and error reporting strategy

5. **Performance benchmarking**
   - Measure TOON encoding latency for typical API responses (10, 50, 100, 500 objects)
   - Identify encoding bottlenecks (serialization, string formatting, schema analysis)
   - Decision: Acceptable latency threshold before fallback

### Research Deliverable: `research.md`

Document findings in `specs/001-toon-format-integration/research.md` using format:

```markdown
## Decision: [Topic]

**Rationale**: [Why this choice]

**Alternatives Considered**: [What else we evaluated]

**Implementation Notes**: [How to implement this choice]
```

**Research Output Preview (anticipated decisions)**:

- **TOON library**: Pin to v0.9.x on PyPI, monitor for stability, vendor if breaking changes occur
- **Prompt engineering**: Start with no prompt changes, add TOON format hints only if LLM interpretation <95% accurate
- **Token counting**: Use `tiktoken` for all providers as approximation, validate monthly against actual API usage
- **Fallback**: Per-field fallback (each tool result independently), log errors with context
- **Performance**: Set 50ms encoding timeout, fall back to JSON if exceeded

---

## Phase 1: Design & Contracts

**Prerequisites**: `research.md` complete

### 1.1 Data Model (`data-model.md`)

**Entities to define**:

#### TOONEncoder

Wrapper around `toon_format` library with error handling and observability.

**Attributes**:
- `config: TOONConfig` — Feature flags and options
- `tiktoken_encoding: Encoding | None` — Cached tiktoken encoder (lazy-loaded)

**Methods**:
- `encode(data: Any) -> str | None` — Encode to TOON, return None on error
- `estimate_tokens(data: Any) -> tuple[int, int]` — Return (JSON tokens, TOON tokens)
- `compare_formats(data: Any) -> ComparisonResult` — Compare JSON vs TOON with metrics

**Error Handling**:
- Catch all `toon_format` exceptions
- Log error with data sample (first 500 chars)
- Return `None` on error (triggers fallback to JSON)

**Observability**:
- OTel span for each `encode()` call
- Span attributes: `toon.encoding_time_ms`, `toon.input_size_bytes`, `toon.output_size_chars`
- Metrics: `toon.encoding_errors` (counter), `toon.encoding_latency` (histogram)

#### TOONConfig

Configuration for TOON feature flags.

**Attributes**:
- `enabled: bool` — Global toggle (default: False for beta)
- `tool_results: bool` — Encode tool results (default: False)
- `schema_context: bool` — Encode schemas (default: False)
- `sql_results: bool` — Encode DuckDB results (default: False)
- `mcp_output: bool` — Support MCP output format header (default: False)
- `fallback_on_error: bool` — Fall back to JSON on error (default: True)
- `encoding_timeout_ms: int` — Max encoding time before fallback (default: 50)

**Loaded from**:
- Env vars: `API_AGENT_ENABLE_TOON`, `API_AGENT_TOON_TOOL_RESULTS`, etc.
- Request headers: `X-Enable-TOON`, `X-TOON-Tool-Results`, etc.

#### ToolResult (enhanced)

Existing dataclass in `api_agent/llm/types.py`, add format metadata.

**New Attributes**:
- `format: Literal["json", "toon"]` — Format used for `content`
- `tokens_json: int | None` — Estimated tokens if encoded as JSON
- `tokens_toon: int | None` — Actual tokens (if TOON used)

**Backward Compatibility**:
- New attributes are optional (default `format="json"`, `tokens_json=None`, `tokens_toon=None`)
- Existing code that doesn't read these fields continues to work

#### ComparisonResult

Output of `TOONEncoder.compare_formats()`.

**Attributes**:
- `data_sample: str` — First 200 chars of input data
- `json_output: str` — JSON-encoded output
- `toon_output: str` — TOON-encoded output
- `json_tokens: int`
- `toon_tokens: int`
- `compression_ratio: float` — (json_tokens - toon_tokens) / json_tokens
- `encoding_time_ms: float`

### 1.2 Interface Contracts (`contracts/toon_encoder.md`)

Document the public API contract for `TOONEncoder`:

**Guarantees**:
- `encode()` never raises exceptions (returns `None` on error)
- `encode()` returns valid TOON string or `None`
- `estimate_tokens()` returns conservative estimates (may over-count by <10%)
- Encoding latency <50ms for inputs <100KB (P95)

**Failure Modes**:
- Unsupported data structure (e.g., circular references) → return `None`
- TOON library bug → return `None`, log error
- Encoding timeout exceeded → return `None`, log warning

**Observability Contract**:
- Every `encode()` call creates an OTel span
- Errors are logged at ERROR level with context
- Metrics are exported via OpenTelemetry

### 1.3 Quickstart Guide (`quickstart.md`)

User-facing documentation for enabling TOON:

**Sections**:
1. **What is TOON?** — Brief explanation, token savings promise
2. **Enabling TOON** — Env var configuration
3. **Per-Request Control** — Header-based toggles
4. **Measuring Impact** — How to query OTel metrics
5. **Troubleshooting** — Common issues (library not installed, encoding errors)
6. **Beta Caveats** — Warning about v0.9.x beta, fallback behavior

**Example Configuration**:

```bash
# Enable TOON globally (opt-in beta)
export API_AGENT_ENABLE_TOON=true
export API_AGENT_TOON_TOOL_RESULTS=true
export API_AGENT_TOON_SCHEMA_CONTEXT=true

# Optional: Install toon-format and tiktoken
uv add toon-format tiktoken

# Run Ratatoskr
uv run api-agent --provider anthropic
```

**Example Per-Request Toggle**:

```bash
# Disable TOON for this request (if globally enabled)
curl -X POST http://localhost:3000/mcp \
  -H "X-Enable-TOON: false" \
  -d '{"query": "..."}'
```

### 1.4 Agent Context Update

Run `.specify/scripts/bash/update-agent-context.sh` to add TOON integration context to Claude's project context (if using Cline or similar).

**Context to add**:
- TOON integration status (in progress)
- Key files: `toon_encoder.py`, modified `provider.py`, `config.py`
- Configuration env vars
- Testing approach

---

## Phase 2: Implementation Phases

**Note**: Implementation phases are detailed in `tasks.md` (created by `/speckit.tasks` command). This section provides high-level sequencing.

### Phase 2.1: TOONEncoder Core (P1)

**Goal**: Build `toon_encoder.py` with error handling and observability.

**Dependencies**: `research.md` decisions on fallback strategy and performance.

**Deliverables**:
- `api_agent/llm/toon_encoder.py` — `TOONEncoder` class
- `tests/test_llm/test_toon_encoder.py` — Unit tests (encode, estimate_tokens, error handling)
- OTel span integration
- Config loading from env vars

**Success Criteria**:
- `encode()` handles all edge cases from spec (null, empty, unsupported types)
- `estimate_tokens()` within 10% of `tiktoken` baseline
- Error handling tested (mock TOON library exceptions)

### Phase 2.2: Tool Result Integration (P1)

**Goal**: TOON-encode tool results in `run_tool_loop()`.

**Dependencies**: Phase 2.1 complete.

**Deliverables**:
- Modify `api_agent/llm/provider.py` — Use `TOONEncoder` in `_execute_tools()`
- Modify `api_agent/llm/types.py` — Add format metadata to `ToolResult`
- `tests/test_llm/test_toon_integration.py` — Integration tests (GraphQL, REST agents)
- OTel span attributes for tool results

**Success Criteria**:
- Tool results encoded as TOON when enabled
- LLM receives TOON-formatted tool result messages
- Token count metrics exported
- JSON fallback on encoding error

### Phase 2.3: Schema Context Compression (P2)

**Goal**: TOON-encode GraphQL introspection and OpenAPI specs.

**Dependencies**: Phase 2.1 complete (can run in parallel with 2.2).

**Deliverables**:
- Modify `api_agent/agent/graphql_agent.py` — TOON-encode introspection result
- Modify `api_agent/agent/rest_agent.py` — TOON-encode OpenAPI schema
- `tests/test_graphql_toon.py`, `tests/test_rest_toon.py` — Integration tests

**Success Criteria**:
- Schema context in system prompt is TOON-formatted
- Agents correctly query APIs using TOON-encoded schema
- Token savings measured for real schemas

### Phase 2.4: DuckDB Result Formatting (P3)

**Goal**: TOON-encode SQL query results.

**Dependencies**: Phase 2.1 complete.

**Deliverables**:
- Modify `api_agent/executor.py` — TOON-encode DuckDB query results
- Modify `tests/test_executor.py` — Add TOON formatting tests

**Success Criteria**:
- SQL results formatted as TOON tabular arrays
- LLM correctly interprets SQL results in TOON format

### Phase 2.5: MCP Output Format (P4)

**Goal**: Support `X-Output-Format: toon` header.

**Dependencies**: Phase 2.1 complete.

**Deliverables**:
- Modify MCP server (likely in `api_agent/server.py` or main entry point)
- Parse `X-Output-Format` header, apply TOON encoding to final response
- HTTP 400 for unsupported formats

**Success Criteria**:
- MCP clients can request TOON output
- Default remains JSON (backward compatible)

### Phase 2.6: Observability & A/B Testing (Co-P1)

**Goal**: Complete observability for token tracking and A/B testing.

**Dependencies**: Runs in parallel with 2.1-2.5, integrated throughout.

**Deliverables**:
- OTel span attributes: `toon.tokens_json`, `toon.tokens_toon`, `toon.compression_ratio`, `toon.format_used`
- Metrics: `toon.tokens_saved`, `toon.fallback_count`, `toon.encoding_errors`
- `tests/test_llm/test_toon_observability.py` — Verify metrics exported
- Dashboard query examples in `docs/toon-integration.md`

**Success Criteria**:
- Metrics queryable in OTel backend (e.g., Grafana)
- Token savings visible within 1 day of deployment
- A/B testing enabled via per-request header

### Phase 2.7: Documentation & Rollout

**Goal**: User-facing docs and deployment guide.

**Dependencies**: All phases 2.1-2.6 complete.

**Deliverables**:
- `docs/toon-integration.md` — Comprehensive user guide
- Update `README.md` — Mention TOON feature in features section
- Update `.env.example` — Add TOON config examples
- Deployment guide — Beta opt-in, monitoring checklist

---

## Open Questions (to be resolved in Phase 0 research)

1. **Prompt engineering for TOON**: Do LLMs natively understand TOON, or do we need format hints?
   - **Suggested approach**: Start without hints, test with real queries, add hints only if accuracy <95%
   - **Research task**: Phase 0, Task 2

2. **Vendor vs PyPI dependency**: Should we vendor `toon-format` to avoid beta instability?
   - **Suggested approach**: Start with PyPI pinned version, vendor only if we hit breaking changes
   - **Research task**: Phase 0, Task 1

3. **Token counting for Anthropic**: Should we use `tiktoken` (approximation) or Anthropic's API?
   - **Suggested approach**: Use `tiktoken` initially, validate monthly, integrate Anthropic API if error >10%
   - **Research task**: Phase 0, Task 3

4. **Fallback strategy granularity**: Per-field or per-request fallback?
   - **Suggested approach**: Per-field (minimize impact), log errors with context
   - **Research task**: Phase 0, Task 4

---

## Risk Mitigation Summary

| Risk | Severity | Mitigation | Status |
|------|----------|------------|--------|
| LLM misinterprets TOON | High | Phase 0 research, integration tests vs JSON baseline | Phase 0 |
| TOON library instability | High | Pin version, comprehensive error handling, JSON fallback | Phase 2.1 |
| Performance degradation | Medium | Benchmark in Phase 0, set timeout, fall back if slow | Phase 0, 2.1 |
| Token counting inaccuracy | Medium | Validate `tiktoken` vs actual API usage | Phase 0, ongoing |
| MCP client incompatibility | Low | Default to JSON, require explicit header for TOON | Phase 2.5 |

---

## Success Metrics (from Spec)

Track these post-deployment:

- **Token reduction**: 30-60% for tool results, 30-50% for schemas/SQL
- **Encoding latency**: <10ms for typical results, <50ms for large results
- **Reliability**: <0.1% TOON-related errors, 100% fallback success
- **LLM accuracy**: 100% correct interpretation (match JSON baseline)
- **Observability**: 100% of requests export token metrics

---

## Next Steps

1. **Review this plan** — Validate technical decisions, project structure
2. **Execute Phase 0** — Run research tasks, document findings in `research.md`
3. **Execute Phase 1** — Create data-model.md, contracts/, quickstart.md
4. **Transition to `/speckit.tasks`** — Break Phase 2 implementation into concrete tasks
5. **Begin implementation** — Start with Phase 2.1 (TOONEncoder core)

**Ready for**: Phase 0 research kick-off ✅
