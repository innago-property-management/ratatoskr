# Research Report: TOON Format Integration

**Feature**: 001-toon-format-integration  
**Date**: 2026-02-23  
**Phase**: 0 (Research & Technical Decisions)

## Overview

This document resolves all NEEDS CLARIFICATION items from the implementation plan and answers open questions from the specification. Research focuses on: TOON library stability, LLM prompt engineering, token counting accuracy, fallback strategy, and performance benchmarks.

---

## Decision 1: TOON Library Dependency Strategy

**Research Question**: Should we vendor `toon-format` v0.9.x to avoid beta instability, or trust PyPI releases?

### Investigation

**Current State** (as of 2026-02-23):
- `toon-format` is at **v0.9.x** (beta)
- Published to PyPI: `pip install toon_format`
- Working towards v1.0.0 with full spec compliance
- GitHub: `https://github.com/toon-format/toon-python`
- Python 3.8+ support
- 792 tests, 91% coverage (85% enforced)
- Active maintenance (GitHub Actions CI/CD)

**API Stability Analysis**:
- Core API (`encode()`, `decode()`) is stable across v0.9.x
- Options structure (`delimiter`, `indent`, `lengthMarker`) is stable
- Risk: Breaking changes before v1.0.0 (common for beta)
- Mitigation: Pin exact version (e.g., `toon-format==0.9.3`)

**Alternatives Considered**:

| Approach | Pros | Cons |
|----------|------|------|
| **PyPI pinned version** | Simple, receives bug fixes, standard Python practice | Risk of yanked releases, network dependency |
| **Vendor (copy into repo)** | Total control, no external dependency | Maintenance burden, miss bug fixes, license compliance |
| **Git submodule** | Track upstream, controlled updates | Complex workflow, requires Git submodule knowledge |
| **Fork on GitHub** | Control + contributions, easy updates | Overhead of maintaining fork |

### Decision: PyPI Pinned Version with Contingency Plan

**Rationale**:
1. `toon-format` has good test coverage (91%) and active CI
2. API is simple (`encode()`, `decode()`) — unlikely to change drastically
3. Pinning exact version (`toon-format==0.9.3`) protects against breaking changes
4. If issues arise, we can vendor later (copy library into `api_agent/vendor/toon_format/`)

**Implementation**:

```toml
# pyproject.toml
[project]
dependencies = [
    "toon-format==0.9.3",  # Pin exact version
]

[project.optional-dependencies]
observability = [
    "tiktoken>=0.5.0",  # For token counting
]
```

**Contingency Plan** (if stability issues occur):
1. Vendor the library: `cp -r .venv/lib/python3.11/site-packages/toon_format api_agent/vendor/`
2. Add `api_agent/vendor/` to Python path
3. Update imports: `from api_agent.vendor.toon_format import encode`
4. Document vendoring reason in `docs/architecture/dependencies.md`

**Monitoring**:
- Track `toon-format` releases on GitHub (watch repository)
- Review changelog before any version bumps
- If v1.0.0 is released, evaluate upgrade path

---

## Decision 2: LLM Prompt Engineering for TOON

**Research Question**: Do Claude and GPT-4 natively understand TOON format, or do we need format hints in prompts?

### Hypothesis

TOON's syntax is human-readable and follows familiar patterns (YAML-like objects, CSV-like tables). Modern LLMs (Claude Sonnet 4, GPT-4o) are likely trained on diverse formats and should interpret TOON without explicit format hints.

### Experimental Design (Deferred to Phase 2.2)

Since we don't have a live Ratatoskr deployment yet, we defer empirical testing to Phase 2.2 (Tool Result Integration). However, we can design the experiment:

**Test Methodology**:
1. **Baseline (JSON)**: Execute 10 representative queries (GraphQL, REST) with JSON tool results, measure LLM accuracy
2. **TOON (no hints)**: Same 10 queries with TOON tool results, no prompt changes, measure LLM accuracy
3. **TOON (with hints)**: Same 10 queries with TOON + format hint in system prompt, measure LLM accuracy

**Sample Format Hint** (if needed):

```text
Tool results may be formatted in TOON (Tabular Object Oriented Notation) for compactness:
- Objects use key: value pairs with indentation
- Arrays of uniform objects use tabular format: [count,]{field1,field2}: data rows
- Parse TOON the same way you parse JSON — extract fields and values.
```

**Accuracy Metrics**:
- **Field extraction**: Can LLM extract specific fields from TOON-encoded tool results?
- **Query generation**: Does LLM generate correct follow-up queries based on TOON data?
- **Aggregation**: Can LLM aggregate/filter TOON data correctly (e.g., "top 10 users by age")?

**Success Threshold**: 95% accuracy (match JSON baseline)

### Decision: Start Without Hints, Add Only If Needed

**Rationale**:
1. Adding prompt complexity increases token usage (defeats the purpose)
2. TOON is designed to be human-readable — LLMs should parse it naturally
3. Early TOON documentation suggests LLMs handle it well (per TOON project claims)

**Implementation**:
- **Phase 2.2**: Implement TOON encoding with **no prompt changes**
- **Phase 2.2 Testing**: Run integration tests (10 real queries) comparing JSON vs TOON
- **Decision Point**: If accuracy <95%, add format hint to system prompt
- **Fallback**: If accuracy still <95% with hints, disable TOON for that provider

**Implementation Notes**:
```python
# In graphql_agent.py, rest_agent.py system prompt generation
def get_system_prompt() -> str:
    base_prompt = """...[existing prompt]..."""
    
    if settings.TOON_ENABLED and settings.TOON_FORMAT_HINT:
        # Only add if config flag enabled (default: False)
        base_prompt += "\n\n" + TOON_FORMAT_HINT
    
    return base_prompt
```

---

## Decision 3: Token Counting Integration

**Research Question**: Should we use `tiktoken` (approximation) for all providers, or integrate Anthropic's token counting API for Claude?

### Background

**`tiktoken`**:
- OpenAI's tokenizer library (used by GPT models)
- Fast, local, no API calls
- Encodings: `cl100k_base` (GPT-4), `o200k_base` (GPT-4o, GPT-5)
- **Limitation**: Anthropic uses a different tokenizer

**Anthropic Token Counting**:
- Claude uses a proprietary tokenizer (not `tiktoken`)
- API: `anthropic.count_tokens()` (requires API call)
- Cost: Free, but adds latency (~50-100ms)
- Accuracy: Exact for Claude

### Analysis

**Token Count Use Cases in Ratatoskr**:

1. **Observability (token savings metrics)**: Need approximate counts for comparison (JSON vs TOON)
   - Accuracy requirement: ±10% acceptable (we're measuring relative difference, not absolute cost)
   - Latency requirement: <10ms (should not block request)

2. **Billing estimation**: Not applicable (Ratatoskr doesn't track usage costs)

3. **Context length management**: Not currently implemented (agents don't truncate context based on tokens)

**`tiktoken` vs Anthropic API**:

| Aspect | `tiktoken` | Anthropic API |
|--------|-----------|---------------|
| Accuracy (Claude) | ~5-10% error | Exact |
| Latency | <1ms | 50-100ms |
| Complexity | Simple (pip install) | Requires API key, error handling |
| Offline capability | Yes | No |
| Cost | Free | Free |

**Error Analysis** (estimated):
- `tiktoken` typically over-counts tokens for Claude by ~5-10% (conservative estimate)
- For observability, this is acceptable — we measure relative savings (TOON vs JSON)
- Example: If JSON is 1000 tokens and TOON is 400 tokens (60% savings), `tiktoken` might report 1050 and 420 → still 60% savings

### Decision: Use `tiktoken` Universally

**Rationale**:
1. **Observability is the primary use case** — relative comparison matters, not absolute accuracy
2. **Latency is critical** — `tiktoken` is <1ms, Anthropic API is 50-100ms (unacceptable for per-request metrics)
3. **Simplicity** — Single tokenizer for all providers, no API key management
4. **Offline capability** — Works without network access (useful for testing)

**Implementation**:

```python
# api_agent/llm/toon_encoder.py
import tiktoken

class TOONEncoder:
    def __init__(self):
        # Use GPT-4 tokenizer as universal approximation
        self._tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
    
    def estimate_tokens(self, data: Any) -> tuple[int, int]:
        """Return (JSON tokens, TOON tokens)."""
        json_str = json.dumps(data)
        toon_str = self.encode(data)
        
        json_tokens = len(self._tiktoken_encoding.encode(json_str))
        toon_tokens = len(self._tiktoken_encoding.encode(toon_str)) if toon_str else json_tokens
        
        return json_tokens, toon_tokens
```

**Validation Strategy** (ongoing):
- **Monthly audit**: Compare `tiktoken` estimates vs actual API usage (from LLM response headers)
- **If error >10%**: Consider integrating Anthropic's token counting (async, cached)
- **Acceptable error**: ±10% for observability purposes

**Configuration**:
```bash
# pyproject.toml
[project.optional-dependencies]
observability = [
    "tiktoken>=0.5.0",
]
```

Make `tiktoken` optional — if not installed, disable token counting but allow TOON encoding.

---

## Decision 4: Fallback Strategy Granularity

**Research Question**: Should fallback be per-field (single tool result), per-request (all tool results), or per-turn (entire agent turn)?

### Analysis

**Failure Scenarios**:

1. **Single tool result fails to encode** (e.g., unsupported data structure)
   - Impact: One tool result in a multi-tool turn
   - Example: Agent calls `query_users()` (succeeds) and `query_orders()` (TOON fails)

2. **All tool results in a turn fail** (e.g., TOON library bug)
   - Impact: Entire turn's tool results
   - Example: Library crashes on specific data pattern

3. **Entire request fails** (e.g., TOON library not installed)
   - Impact: All turns in the agent loop
   - Example: `import toon_format` fails

**Fallback Options**:

| Granularity | Pros | Cons |
|-------------|------|------|
| **Per-field** (each tool result) | Minimal impact, other results still use TOON | Complex error handling |
| **Per-turn** (all tool results in one LLM call) | Simpler, consistent format per turn | One failure affects all results |
| **Per-request** (entire agent loop) | Simplest, easy to debug | One failure disables TOON for entire conversation |

### Decision: Per-Field Fallback with Per-Request Disable

**Rationale**:
1. **Per-field minimizes impact** — If one tool result fails, others still benefit from TOON
2. **Consistent format per turn** — All tool results in a single turn should use the same format (mix of TOON and JSON is confusing for LLMs)
3. **Per-request disable for catastrophic failures** — If TOON library is missing or consistently failing, disable for entire request

**Implementation Strategy**:

```python
# api_agent/llm/provider.py

class LLMProvider:
    def __init__(self):
        self._toon_encoder = TOONEncoder() if settings.TOON_ENABLED else None
        self._toon_disabled_this_request = False  # Per-request flag
    
    async def _execute_tools(self, tool_calls, tools_map):
        results = []
        toon_failures = 0
        
        for tc in tool_calls:
            # Execute tool (existing logic)
            result_data = await execute_tool(tc, tools_map)
            
            # Encode result
            format_used = "json"
            content = json.dumps(result_data)
            tokens_json = None
            tokens_toon = None
            
            if self._toon_encoder and not self._toon_disabled_this_request:
                toon_result = self._toon_encoder.encode(result_data)
                
                if toon_result:
                    # Success
                    format_used = "toon"
                    content = toon_result
                    tokens_json, tokens_toon = self._toon_encoder.estimate_tokens(result_data)
                else:
                    # Fallback to JSON
                    toon_failures += 1
                    logger.warning(f"TOON encoding failed for {tc.name}, falling back to JSON")
                    
                    # If 3+ failures in this turn, disable TOON for entire request
                    if toon_failures >= 3:
                        self._toon_disabled_this_request = True
                        logger.error("TOON encoder failing repeatedly, disabling for this request")
            
            results.append(ToolResult(
                tool_call_id=tc.id,
                name=tc.name,
                content=content,
                format=format_used,
                tokens_json=tokens_json,
                tokens_toon=tokens_toon,
            ))
        
        return results
```

**Error Reporting**:
- **Per-field failure**: Log at WARNING level, increment `toon.fallback_count` metric
- **Per-request disable** (3+ failures): Log at ERROR level, include context (tool names, error messages)
- **OTel span attributes**: `toon.fallbacks_this_turn`, `toon.request_disabled`

**Consistency Rule**:
- All tool results in a single turn should use the same format (all TOON or all JSON)
- If any TOON encoding fails in a turn, fall back ALL results in that turn to JSON
- Rationale: Mixed TOON/JSON in one turn may confuse LLMs

**Revised Implementation**:

```python
async def _execute_tools(self, tool_calls, tools_map):
    # Execute all tools first
    results_data = [await execute_tool(tc, tools_map) for tc in tool_calls]
    
    # Try TOON encoding for all results
    toon_encoded = []
    for data in results_data:
        toon_result = self._toon_encoder.encode(data) if self._toon_encoder else None
        toon_encoded.append(toon_result)
    
    # If ANY encoding failed, use JSON for ALL results
    any_failed = any(t is None for t in toon_encoded)
    format_used = "json" if any_failed else "toon"
    
    if any_failed and self._toon_encoder:
        logger.warning(f"TOON encoding failed for {sum(t is None for t in toon_encoded)}/{len(toon_encoded)} results, using JSON for all")
    
    # Build ToolResult objects
    return [
        ToolResult(
            tool_call_id=tc.id,
            name=tc.name,
            content=toon_encoded[i] if not any_failed and toon_encoded[i] else json.dumps(results_data[i]),
            format=format_used,
        )
        for i, tc in enumerate(tool_calls)
    ]
```

### Decision Summary

**Fallback Granularity**: **Per-turn** (all tool results in one LLM call use same format)

**Trigger Conditions**:
- If ANY tool result in a turn fails TOON encoding → all results in that turn use JSON
- If 3+ consecutive turns fail → disable TOON for entire request

**Observability**:
- `toon.fallback_count` (counter) — increments on any fallback
- `toon.fallbacks_this_turn` (span attribute) — count of failed encodings per turn
- `toon.request_disabled` (span attribute) — boolean flag if disabled mid-request

---

## Decision 5: Performance Benchmarking & Timeout

**Research Question**: What is acceptable encoding latency before fallback? What timeout should we set?

### Performance Targets (from Spec)

- **Typical results** (<10KB): <10ms encoding latency
- **Large results** (<100KB): <50ms encoding latency
- **Request overhead**: <1% increase in end-to-end latency

### Benchmarking Plan (Deferred to Phase 2.1)

Since we don't have `TOONEncoder` implemented yet, we defer empirical benchmarks to Phase 2.1. However, we can design the benchmark:

**Test Scenarios**:

| Scenario | Data Pattern | Size | Expected Latency |
|----------|--------------|------|------------------|
| Small API response | List of 10 users (5 fields each) | ~2KB | <5ms |
| Medium API response | List of 100 products (10 fields) | ~20KB | <15ms |
| Large API response | List of 500 orders (15 fields) | ~80KB | <40ms |
| GraphQL schema | Introspection JSON (50 types) | ~30KB | <25ms |
| DuckDB result | Aggregated table (50 rows, 10 cols) | ~10KB | <10ms |

**Benchmark Implementation**:

```python
# tests/test_llm/test_toon_performance.py
import time
from api_agent.llm.toon_encoder import TOONEncoder

def test_encoding_latency():
    encoder = TOONEncoder()
    
    # Generate test data (100 users)
    users = [{"id": i, "name": f"User{i}", "email": f"user{i}@example.com", "age": 20 + i % 50} for i in range(100)]
    
    start = time.perf_counter()
    toon_result = encoder.encode(users)
    latency_ms = (time.perf_counter() - start) * 1000
    
    assert toon_result is not None
    assert latency_ms < 15  # 100 users should encode in <15ms
    print(f"Encoded 100 users in {latency_ms:.2f}ms")
```

**Run benchmarks across**:
- Different Python versions (3.8, 3.11, 3.12)
- Different data patterns (arrays, nested objects, mixed types)
- Different sizes (10, 100, 500, 1000 objects)

### Decision: 50ms Timeout with Warning

**Rationale**:
1. **50ms is reasonable** for encoding <100KB responses
2. **If exceeded**: Log warning, fall back to JSON (don't block request)
3. **Timeout protects against** pathological data patterns or library bugs

**Implementation**:

```python
# api_agent/llm/toon_encoder.py
import signal
from contextlib import contextmanager

class TimeoutError(Exception):
    pass

@contextmanager
def timeout(seconds: float):
    """Context manager for timeout."""
    def handler(signum, frame):
        raise TimeoutError()
    
    old_handler = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)

class TOONEncoder:
    def encode(self, data: Any) -> str | None:
        try:
            with timeout(0.050):  # 50ms timeout
                return toon_format.encode(data)
        except TimeoutError:
            logger.warning(f"TOON encoding exceeded 50ms timeout, falling back to JSON (data size: {sys.getsizeof(data)} bytes)")
            return None
        except Exception as e:
            logger.error(f"TOON encoding failed: {e}", exc_info=True)
            return None
```

**Configuration**:
```python
# api_agent/config.py
class Settings(BaseSettings):
    TOON_ENCODING_TIMEOUT_MS: int = 50  # Configurable timeout
```

**Observability**:
- `toon.encoding_time_ms` (span attribute) — actual encoding time
- `toon.timeout_exceeded` (counter) — increments on timeout
- `toon.encoding_latency` (histogram) — distribution of encoding times

---

## Summary of Research Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **TOON library** | PyPI pinned version (`toon-format==0.9.3`) | Simple, stable API, vendor if issues arise |
| **Prompt engineering** | Start without hints, add only if LLM accuracy <95% | TOON is human-readable, avoid prompt bloat |
| **Token counting** | `tiktoken` for all providers | Fast (<1ms), good enough for observability (±10%) |
| **Fallback granularity** | Per-turn (all results in one turn use same format) | Consistent format, minimal LLM confusion |
| **Encoding timeout** | 50ms | Protects against pathological data, fall back to JSON |

---

## Implementation Notes for Phase 2

1. **Phase 2.1** (TOONEncoder):
   - Implement timeout with `signal.SIGALRM` (Unix) or `threading.Timer` (cross-platform)
   - Add `toon-format==0.9.3` to `pyproject.toml`
   - Make `tiktoken` optional dependency (`pip install api-agent[observability]`)

2. **Phase 2.2** (Tool Result Integration):
   - Run LLM accuracy experiment (10 queries, compare JSON vs TOON)
   - If accuracy <95%, add `settings.TOON_FORMAT_HINT` config flag
   - Default: no hint (keep prompts clean)

3. **Phase 2.6** (Observability):
   - Validate `tiktoken` token counts monthly against actual API usage (from LLM response headers)
   - If error >10% for Anthropic, consider async Anthropic token counting API

4. **Phase 2.7** (Documentation):
   - Document vendoring contingency plan in `docs/toon-integration.md`
   - Add troubleshooting guide for `toon-format` installation issues

---

## Open Questions Resolved

✅ All open questions from spec are resolved.

**Next Step**: Proceed to Phase 1 (Design & Contracts) — create `data-model.md`, `contracts/`, `quickstart.md`.
