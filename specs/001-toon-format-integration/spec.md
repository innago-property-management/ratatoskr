# Feature Specification: TOON Format Integration for LLM Context Compression

**Feature Branch**: `001-toon-format-integration`  
**Created**: 2026-02-23  
**Status**: Draft  
**Input**: Integrate TOON format for LLM context compression with 30-60% token reduction. Support tool results, schema context, DuckDB results, and optional MCP output format. Include observability for token counting and A/B testing capability.

## User Scenarios & Testing

### User Story 1 - Tool Result Compression in Agent Loop (Priority: P1)

**Scenario**: When Ratatoskr's agent executes API calls (GraphQL or REST) and feeds results back to the LLM in the tool-calling loop, those results are encoded as TOON instead of JSON, reducing token consumption by 30-60% on every turn.

**Why this priority**: This is the highest-value integration point. Every agent turn consumes tokens proportional to tool result size. API responses are typically arrays of uniform objects (e.g., list of users, products, orders) — exactly the data pattern TOON optimizes with tabular arrays. This is where 80% of the token savings will come from.

**Independent Test**: Can be fully tested by executing a single GraphQL or REST query through the agent, comparing JSON vs TOON token counts in the tool result message to the LLM, and verifying the LLM can still correctly interpret the response.

**Acceptance Scenarios**:

1. **Given** a GraphQL query returns a list of 100 users, **When** the agent encodes the result as TOON, **Then** the token count is 30-60% lower than JSON equivalent
2. **Given** a REST API returns a paginated response with 50 products, **When** the result is encoded as TOON, **Then** the LLM correctly extracts product attributes in subsequent tool calls
3. **Given** a tool result contains nested objects and arrays, **When** TOON encoding is enabled, **Then** the format preserves all data structure and the LLM interprets it correctly
4. **Given** TOON encoding is disabled via configuration, **When** tool results are returned, **Then** they are formatted as JSON (fallback behavior)

---

### User Story 2 - Schema Context Compression (Priority: P2)

**Scenario**: API schemas (GraphQL introspection, OpenAPI specs) passed in system prompts are TOON-encoded, reducing the initial context size before any tool calls happen.

**Why this priority**: Schema context is fixed overhead in every agent conversation. GraphQL introspection can be 10K+ tokens for a complex API. OpenAPI specs are often 20K+ tokens. TOON compression here provides immediate savings on every request, though less frequently refreshed than tool results.

**Independent Test**: Can be tested by loading a GraphQL schema, encoding it as TOON, passing it in the system prompt, and verifying the agent can still correctly query the API using the compressed schema.

**Acceptance Scenarios**:

1. **Given** a GraphQL schema with 50 types and 200 fields, **When** the schema is TOON-encoded, **Then** the token count is 30-60% lower than JSON introspection
2. **Given** a TOON-encoded GraphQL schema in system prompt, **When** the agent generates a query, **Then** it correctly references types, fields, and arguments from the schema
3. **Given** an OpenAPI spec with 100 endpoints, **When** the schema is TOON-encoded, **Then** the REST agent correctly constructs API requests
4. **Given** schema search is used to retrieve partial schema, **When** results are TOON-encoded, **Then** the agent correctly interprets the filtered schema context

---

### User Story 3 - DuckDB Result Formatting (Priority: P3)

**Scenario**: SQL post-processing results (executed via DuckDB) are formatted as TOON tabular arrays before feeding back to the LLM, compressing aggregated data.

**Why this priority**: DuckDB is used for SQL transformations on API responses. Results are often aggregated tables (e.g., "top 10 products by revenue") — perfect for TOON's tabular format. However, this is lower priority because SQL results tend to be smaller than raw API responses (already filtered/aggregated).

**Independent Test**: Can be tested by executing a SQL query on API data, formatting the result as TOON, and verifying the LLM correctly interprets the tabular output.

**Acceptance Scenarios**:

1. **Given** a SQL query returns a 10-row result set with 5 columns, **When** formatted as TOON, **Then** the output uses tabular array notation and is 30-50% smaller than JSON
2. **Given** a SQL query with nested aggregations, **When** the result is TOON-encoded, **Then** the LLM correctly extracts aggregated values
3. **Given** a SQL result with mixed data types (numbers, strings, nulls), **When** TOON-encoded, **Then** type information is preserved and the LLM interprets it correctly

---

### User Story 4 - MCP Output Format Option (Priority: P4)

**Scenario**: MCP clients can optionally request TOON-formatted output via a header (e.g., `X-Output-Format: toon`), allowing clients that support TOON to receive compressed responses.

**Why this priority**: This is lowest priority because most MCP clients expect JSON. This is future-proofing for when MCP clients gain TOON support, but doesn't provide immediate value to Ratatoskr's core use case (internal agent loop).

**Independent Test**: Can be tested by sending an MCP request with the `X-Output-Format: toon` header and verifying the response is TOON-encoded and valid.

**Acceptance Scenarios**:

1. **Given** an MCP client sends a request with `X-Output-Format: toon`, **When** the tool returns results, **Then** the response is TOON-encoded
2. **Given** an MCP client sends a request without the header, **When** the tool returns results, **Then** the response is JSON (default)
3. **Given** an MCP client sends an invalid output format, **When** the request is processed, **Then** the server returns a 400 error with supported formats

---

### User Story 5 - Observability and Token Tracking (Priority: P1)

**Scenario**: Operators can measure token savings from TOON integration via OpenTelemetry spans, logs, and metrics, and can A/B test TOON vs JSON per-request or globally.

**Why this priority**: This is co-P1 with tool result compression because observability is mandatory for validating the feature's value. Without metrics, we can't prove TOON is saving tokens or detect regressions.

**Independent Test**: Can be tested by executing a request with TOON enabled, checking OTel span attributes for token counts, and comparing to a baseline JSON request.

**Acceptance Scenarios**:

1. **Given** TOON encoding is enabled, **When** tool results are encoded, **Then** OTel span attributes include `toon.tokens_json`, `toon.tokens_toon`, `toon.compression_ratio`
2. **Given** TOON is enabled globally via config, **When** any agent turn executes, **Then** logs show format used (JSON or TOON) at each stage (tool result, schema, SQL)
3. **Given** TOON is toggled per-request via header `X-Enable-TOON: false`, **When** the request is processed, **Then** JSON is used and metrics reflect the format choice
4. **Given** TOON encoding fails, **When** the error occurs, **Then** the system falls back to JSON, logs the error with context, and increments a `toon.fallback_count` metric
5. **Given** a monitoring dashboard queries OTel metrics, **When** viewing TOON performance, **Then** metrics show tokens saved, compression ratio, and error rate over time

---

### Edge Cases

- **What happens when TOON encoding fails?** (e.g., unsupported data structure, library bug)
  - System MUST fall back to JSON encoding
  - Error MUST be logged with full context (data sample, error message)
  - Metric `toon.fallback_count` MUST increment
  - Request MUST NOT fail (graceful degradation)

- **What happens when TOON decoding fails?** (if MCP client sends TOON input)
  - System MUST return HTTP 400 with clear error message
  - Error MUST specify which part of TOON input failed to parse
  - Request MUST NOT crash the server

- **How does the system handle mixed TOON/JSON scenarios?**
  - Tool results: configurable per-provider or globally
  - Schema: configurable independently of tool results
  - SQL results: configurable independently of tool results
  - Configuration MUST be clear and documented

- **What happens with empty or null results?**
  - TOON encoder MUST handle null, empty arrays, empty objects correctly
  - Result MUST be valid TOON or fall back to JSON if encoder returns None

- **What happens with very large results (>100KB)?**
  - TOON encoding MUST complete within reasonable time (<100ms)
  - If encoding takes longer, MUST fall back to JSON with warning log
  - Metric `toon.encoding_time_ms` MUST track encoding latency

- **What if TOON library is not installed?**
  - Ratatoskr MUST start successfully with TOON features disabled
  - Logs MUST warn that TOON is unavailable
  - Configuration validation MUST fail if TOON is explicitly enabled but library missing

## Requirements

### Functional Requirements

#### Core Integration (P1-P2)

- **FR-001**: System MUST support encoding tool results (GraphQL, REST) as TOON format
- **FR-002**: System MUST support encoding API schemas (GraphQL introspection, OpenAPI) as TOON format
- **FR-003**: System MUST support encoding DuckDB SQL results as TOON format
- **FR-004**: System MUST preserve all data structure and types when encoding to TOON
- **FR-005**: LLM providers (Anthropic, OpenAI, OpenAI-compatible) MUST correctly receive and interpret TOON-encoded tool results

#### Configuration & Control

- **FR-006**: System MUST support global TOON enable/disable via configuration (`API_AGENT_ENABLE_TOON=true|false`)
- **FR-007**: System MUST support per-request TOON control via header (`X-Enable-TOON: true|false`)
- **FR-008**: System MUST support independent TOON toggles for: tool results, schema context, SQL results, MCP output
- **FR-009**: Configuration MUST default to TOON disabled (opt-in for beta) to ensure backward compatibility

#### Fallback & Error Handling

- **FR-010**: System MUST fall back to JSON encoding if TOON encoding fails
- **FR-011**: System MUST NOT fail requests when TOON encoding errors occur
- **FR-012**: System MUST log TOON encoding failures with sufficient context for debugging
- **FR-013**: System MUST validate TOON library availability on startup and disable TOON features if unavailable

#### Observability

- **FR-014**: System MUST track token count for both JSON and TOON formats on every encoding operation
- **FR-015**: System MUST export OpenTelemetry span attributes: `toon.tokens_json`, `toon.tokens_toon`, `toon.compression_ratio`, `toon.format_used`, `toon.encoding_time_ms`
- **FR-016**: System MUST increment metrics: `toon.fallback_count` (counter), `toon.encoding_errors` (counter), `toon.tokens_saved` (gauge)
- **FR-017**: System MUST log format used at each encoding stage (tool result, schema, SQL) when debug logging enabled
- **FR-018**: System MUST provide comparison utility to measure token savings across sample data

#### MCP Output Format (P4)

- **FR-019**: System MUST support MCP output format selection via `X-Output-Format: toon|json` header
- **FR-020**: System MUST default to JSON output if no `X-Output-Format` header is provided
- **FR-021**: System MUST return HTTP 400 if `X-Output-Format` specifies an unsupported format

### Key Entities

- **TOONEncoder**: Abstraction for encoding Python data structures to TOON format
  - Methods: `encode(data) -> str | None`, `estimate_tokens(data) -> tuple[int, int]` (JSON tokens, TOON tokens)
  - Wraps `toon_format` library with error handling and fallback
  - Integrates with `tiktoken` for token counting

- **FormatConfig**: Configuration for TOON feature flags
  - Attributes: `enabled` (global toggle), `tool_results` (bool), `schema_context` (bool), `sql_results` (bool), `mcp_output` (bool), `fallback_on_error` (bool)
  - Loaded from environment variables and request headers

- **ToolResult (enhanced)**: Add format metadata to existing `ToolResult` type
  - New attributes: `format` ("json" | "toon"), `tokens_json` (int), `tokens_toon` (int | None)

- **OTelAttributes**: OpenTelemetry span attribute keys for TOON observability
  - Constants: `TOON_FORMAT_USED`, `TOON_TOKENS_JSON`, `TOON_TOKENS_TOON`, `TOON_COMPRESSION_RATIO`, `TOON_ENCODING_TIME_MS`, `TOON_FALLBACK`, `TOON_ERROR`

## Success Criteria

### Measurable Outcomes

- **SC-001**: Tool results encoded as TOON MUST achieve 30-60% token reduction vs JSON for typical API responses (lists of 10+ uniform objects)
- **SC-002**: GraphQL schema introspection encoded as TOON MUST achieve 30-50% token reduction vs JSON introspection
- **SC-003**: DuckDB SQL results encoded as TOON MUST achieve 30-50% token reduction vs JSON for tabular results (5+ rows)
- **SC-004**: TOON encoding latency MUST be <10ms for typical tool results (<10KB), <50ms for large results (<100KB)
- **SC-005**: TOON fallback to JSON MUST succeed 100% of the time (no request failures due to TOON errors)
- **SC-006**: LLM agents MUST correctly interpret TOON-encoded tool results with 100% accuracy (same query results as JSON baseline)
- **SC-007**: OpenTelemetry metrics MUST capture token savings on 100% of TOON-enabled requests
- **SC-008**: Operators MUST be able to A/B test TOON vs JSON and measure impact within 1 day of deployment
- **SC-009**: System MUST maintain <1% increase in request latency when TOON encoding is enabled (encoding overhead)
- **SC-010**: TOON-related errors MUST constitute <0.1% of total requests (high reliability of fallback mechanism)

## Assumptions

1. **TOON library stability**: `toon-format` v0.9.x is sufficiently stable for beta testing. We assume the API won't drastically change before 1.0.0, but we will vendor the library if needed.

2. **LLM compatibility**: We assume that Claude, GPT-4, and OpenAI-compatible models can interpret TOON format with minimal or no prompt engineering. Initial tests suggest this is true for tabular arrays, but we may need to add TOON format hints to system prompts.

3. **Token counting accuracy**: We assume `tiktoken` (used by TOON library) provides accurate token counts for OpenAI and compatible models. For Anthropic, we assume token counts are close enough for comparison purposes (Claude uses a different tokenizer, but differences are <5%).

4. **Performance**: We assume TOON encoding latency is negligible (<10ms) for typical API responses. If encoding becomes a bottleneck, we can cache encoded results or parallelize encoding.

5. **Backward compatibility**: We assume TOON integration is fully opt-in and does not affect existing JSON-based workflows. All TOON features default to disabled.

6. **MCP client support**: We assume most MCP clients currently expect JSON output. The `X-Output-Format` header is future-proofing for when clients add TOON support.

## Dependencies

### External Libraries

- **toon-format** (v0.9.x beta from PyPI or GitHub)
  - Risk: Beta library may have bugs or breaking API changes before 1.0.0
  - Mitigation: Pin exact version, vendor library if stability issues arise, comprehensive error handling with JSON fallback

- **tiktoken** (optional, for token counting)
  - Risk: Required for observability features
  - Mitigation: Make optional; if missing, disable token counting but allow TOON encoding

### Internal Dependencies

- **api_agent/llm/provider.py**: Tool-calling loop where tool results are formatted
- **api_agent/llm/types.py**: `ToolResult` type needs format metadata
- **api_agent/agent/graphql_agent.py**: GraphQL schema formatting
- **api_agent/agent/rest_agent.py**: REST response formatting
- **api_agent/executor.py**: DuckDB SQL result formatting
- **api_agent/config.py**: Configuration for TOON feature flags

## Out of Scope

- **TOON input parsing**: MCP clients sending TOON-encoded requests (P4, defer to future)
- **Streaming TOON encoding**: Real-time encoding during LLM streaming responses (not needed for tool results)
- **Custom TOON dialects**: Ratatoskr uses standard TOON spec with default delimiters
- **TOON schema validation**: We trust TOON library's internal validation; no custom schema enforcement
- **TOON performance profiling**: Beyond latency tracking, detailed profiling deferred to post-launch optimization

## Open Questions

1. **Prompt engineering for TOON**: Do we need to add TOON format hints to system prompts, or do LLMs natively understand the format?
   - Suggestion: Start with no prompt changes, monitor LLM output quality, add hints only if needed

2. **Vendor vs PyPI dependency**: Should we vendor `toon-format` to avoid beta instability, or trust PyPI releases?
   - Suggestion: Start with PyPI pinned version, vendor only if we hit breaking changes

3. **Token counting for Anthropic**: Anthropic uses a different tokenizer than OpenAI. Should we integrate Anthropic's token counting API?
   - Suggestion: Use `tiktoken` as approximation initially, validate against Anthropic's API, and add native support if error >10%

4. **Fallback strategy granularity**: Should fallback be per-field (e.g., fall back only the failed tool result) or per-request?
   - Suggestion: Per-field fallback to minimize impact, with per-request fallback as last resort

## Risks

### High Priority Risks

1. **LLM misinterpretation of TOON format**
   - Impact: Agent generates incorrect queries or fails to extract data from TOON-encoded results
   - Mitigation: Comprehensive integration tests with real LLM calls, comparison against JSON baseline, add TOON format hints to prompts if needed
   - Contingency: Disable TOON for affected model/provider, investigate prompt engineering

2. **TOON library bugs or breaking changes**
   - Impact: Encoding failures, incorrect output, or crashes
   - Mitigation: Pin exact version, comprehensive error handling with JSON fallback, vendor library if needed
   - Contingency: Roll back to JSON-only, wait for library fix, or fork/patch library

### Medium Priority Risks

3. **Performance degradation from encoding overhead**
   - Impact: Request latency increases, affecting user experience
   - Mitigation: Benchmark encoding latency, set timeout for encoding (<100ms), fall back to JSON if slow
   - Contingency: Disable TOON encoding, optimize encoding (e.g., caching, parallelization)

4. **Token counting inaccuracy**
   - Impact: Metrics don't reflect actual token savings, leading to incorrect conclusions
   - Mitigation: Validate token counts against actual LLM API usage, compare `tiktoken` vs Anthropic tokenizer
   - Contingency: Use actual API token counts from LLM responses instead of pre-computed estimates

### Low Priority Risks

5. **MCP client incompatibility**
   - Impact: Clients expecting JSON fail to parse TOON output
   - Mitigation: Default to JSON, require explicit `X-Output-Format: toon` header
   - Contingency: Remove MCP output format support, focus only on internal agent loop

## Next Steps

1. **Technical Planning** (`/speckit.plan`): Create detailed implementation plan with phases
2. **Prototype TOON encoder**: Build `TOONEncoder` wrapper with error handling and token counting
3. **Integration testing**: Test TOON-encoded tool results with Anthropic/OpenAI providers
4. **Observability setup**: Implement OTel span attributes and metrics
5. **Configuration design**: Define env vars and request header schema
6. **Documentation**: Update README with TOON feature guide and configuration options
