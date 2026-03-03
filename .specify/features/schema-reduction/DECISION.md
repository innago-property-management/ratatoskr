# Smart Schema Reduction via TOON + AI Pipeline

**Status:** Research Complete, Pending Delphi Review
**Date:** 2026-03-03
**Complexity:** 8 (new component with external dependency, async AI call, touches all 3 protocol agents + orchestrator)

## Use Case

Ratatoskr caps schema text at `MAX_SCHEMA_CHARS` (32,000 chars) before passing it to the
agent's system prompt. When a schema exceeds this limit, the current truncation strategy is:

```python
context = schema_text[:MAX_SCHEMA_CHARS] + "\n[SCHEMA TRUNCATED - use search_schema() to explore]"
```

**Alphabetical wins.** For a 600-endpoint REST API, endpoints starting with A-M fill the
context window while N-Z endpoints (which may be exactly what the user asked about) are
silently cut. The agent doesn't know what was cut, so it either hallucinates endpoints or
burns turns on `search_schema()` to find what was dropped.

### Concrete Pain Points

1. **Wrong endpoint selection** — agent picks an alphabetically early endpoint that partially
   fits instead of the correct one that was truncated.
2. **Extra `search_schema()` turns** — agent discovers it doesn't have full information and
   spends turns recovering. Inflates cost and latency.
3. **Silent truncation** — the agent has no way to know whether its schema view is complete
   or not. It cannot ask "was anything cut?" because the cut happens before it is invoked.

## Business Value

- **Higher first-turn accuracy** — query-relevant endpoints appear in context; irrelevant
  ones are deprioritized. Fewer wasted turns.
- **Lower token cost per query** — smaller, focused schema = shorter system prompt = cheaper
  completion.
- **Transparent fallback** — `search_schema()` remains available as the raw-schema escape
  hatch; the agent can always search for what was excluded from the summary.

## Solution: TOON + AI-Mediated Schema Reduction

### Pipeline Overview

```
Parsed schema object (already in memory)
    |
    v
JSON serialize (json.dumps)
    |
    v
TOON encode (toon_format.encode) — lossless compression, ~30-45% token reduction
    |
    v
Token estimate: len(toon_text) / 4
    |
    +-- Under threshold (MAX_SCHEMA_CHARS)? -----> Use TOON text as-is (no AI call)
    |
    +-- Over threshold? -------------------------> Haiku reduction:
                                                   "Given query '{question}',
                                                    reduce this schema to the
                                                    most relevant endpoints/types"
                                                   Result: focused TOON/text schema
    |
    v
schema_text (compact, query-relevant) enters ProtocolConfig.schema_text
    |
    v
Orchestrator builds augmented_query = schema_text + "\n\nQuestion: " + question
    |
    v
search_schema() tool always has full raw schema — escape hatch unchanged
```

### Why TOON First, AI Second

TOON is lossless and synchronous. For many large schemas it achieves enough compression
to stay under threshold without any AI call. The AI call (Haiku) is only triggered when
TOON isn't enough — roughly when the schema is more than 2-3x the threshold.

This ordering is important:
- No AI latency on schemas that fit after compression
- Haiku operates on a compressed (TOON) input — smaller input = cheaper Haiku call
- Haiku output is already in TOON format by instruction — maintained compression downstream

### What the Existing Code Does (Unchanged by This Feature)

`search_schema()` always operates on the full raw schema stored in `ctx_vars.raw_schema`.
The reduction pipeline touches only `schema_text` (what goes into the agent's initial
context). `search_schema()` is the intentional "give me everything" escape hatch and
must not be affected.

## Scope Decisions

### In Scope

- New `api_agent/schema/reducer.py` module: `reduce_schema(schema_text, question, threshold)` → `str`
- TOON encoding layer: `toon_format` package from PyPI (`pip install git+https://github.com/toon-format/toon-python.git`)
- Haiku reduction layer: uses existing `anthropic` SDK already in dependencies
- Integration at the 3 truncation points (REST, GraphQL, gRPC) — before `schema_text` enters `ProtocolConfig`
- New config settings: `SCHEMA_REDUCTION_MODEL`, `SCHEMA_REDUCTION_TIMEOUT_MS`, `SCHEMA_REDUCTION_ENABLED`
- Unit tests + integration tests using `FakeLLMProvider`-style mocking

### Out of Scope

- Changing `search_schema()` behavior (always full raw schema)
- Changing how schemas are fetched or parsed (TOON operates on the already-built DSL text)
- Custom Haiku prompt per protocol (one prompt handles all three)
- Caching reduced schemas (each query has different intent; caching adds complexity for uncertain gain)

### Deferred

- **Schema ranking without AI**: The endpoint-allowlist Skeptic suggested priority-ranked
  truncation (e.g., keep endpoints matching query keywords first). This is a valid
  alternative for the non-AI path. Deferred to post-Delphi consideration.
- **TOON decode in search_schema()**: The agent could decode TOON before searching for
  human-readable output. Deferred — requires schema_search.py changes.

## Technology Evaluation

### TOON Format Package (`toon_format`)

| Property | Value |
|----------|-------|
| PyPI package name | `toon_format` (not the unrelated `toon` neuroscience package) |
| Install | `pip install git+https://github.com/toon-format/toon-python.git` (beta; not yet stable on PyPI) |
| Latest PyPI version | `0.9.0b1` (2025-11-08) — beta status |
| GitHub activity | Last commit 2025-12-02; 667 stars, 53 forks |
| Python version support | 3.8+ (project requires 3.11+, fully compatible) |
| Dependencies | `typing-extensions` (optional, Python < 3.10 only — irrelevant for us) |
| License | MIT |
| Status | Development Status :: 4 - Beta; API may change before 1.0.0 |
| Compression on homogeneous arrays | ~30-45% token reduction (per README benchmark) |
| Compression on heterogeneous structures | Can produce larger output (-10% to -40%) |

**Red Flag Assessment**: Beta status (v0.9.0b1) and install-from-git are yellow flags.
The package is actively maintained (commits within 90 days of today), MIT licensed, and
used in production by the mcp-langchain-bridge TypeScript project which uses the equivalent
`@toon-format/toon` npm package. Risk mitigation: implement with a size-check guard —
if TOON output is larger than JSON input, discard TOON and use the original JSON string.
This means TOON is always "best effort, no harm done."

### Haiku Model for Reduction

| Property | Value |
|----------|-------|
| Model | `claude-haiku-4-5` (cheapest Anthropic model as of 2026-03) |
| SDK | `anthropic>=0.83.0` already in `pyproject.toml` dependencies |
| Trigger condition | `len(toon_text) / 4 > MAX_SCHEMA_CHARS / 4` (token estimate over threshold) |
| API key | Reuse `API_KEY` from settings when provider is Anthropic; configurable separately |
| Timeout | Default 30s (same as bridge); configurable via `SCHEMA_REDUCTION_TIMEOUT_MS` |
| Availability guard | If API key is absent or provider is not Anthropic, skip Haiku layer silently |

**Alternative considered**: Use the main configured provider for reduction. Rejected because:
reduction is a cheap classification task, not a reasoning task. Using Haiku saves ~10x on
cost vs. using Sonnet. The provider config controls the agent; reduction should use the
cheapest available model for this auxiliary task.

### Alternative: Priority-Ranked Truncation (No AI)

Keep the current character-budget truncation but rank endpoints before truncating:
- Score each endpoint by keyword match against the user's question
- Put highest-scoring endpoints first in the schema text
- Truncate by character budget as before

Pros: No AI call, no external dependency, deterministic, works offline.
Cons: Keyword matching is brittle (semantic mismatch, plural/synonym problems).
Does not handle the "schema is 10x the limit" case well — even ranked, a lot is cut.

**Panel question to explore in Delphi**: Should we implement ranked truncation as the
fallback-when-no-AI-key before implementing the full TOON+AI pipeline?

## Resolved Questions

### Why not jq filtering like the TypeScript bridge?

The bridge uses jq as Layer 1 to filter fields from raw JSON responses. Ratatoskr
already has the schema in a structured Python object (dict) before DSL serialization.
We don't need jq — we can operate directly on the structured object. The TOON layer
replaces both jq and JSON re-serialization.

### Why not change the DSL builder to produce less output?

`build_schema_context()` (REST), `_build_schema_context()` (GraphQL), and
`build_schema_text()` (gRPC) each have protocol-specific serialization logic.
Modifying them to do query-aware reduction would require passing the question into
schema-building, which happens at different layers. TOON+Haiku is cleanly layered
at the point where schema_text is handed off to ProtocolConfig — no DSL builder
changes needed.

### Where in the call stack does reduction happen?

Each protocol agent currently truncates `schema_text` before passing it to
`ProtocolConfig`. The reducer will replace those truncation lines. The orchestrator
receives `schema_text` through `ProtocolConfig.schema_text` — no orchestrator changes
needed.

## Recommended Next Steps

1. Write `DESIGN.md` with module structure and interface contracts
2. Write `PLAN.md` with implementation tasks
3. Run Delphi panel review
4. Implement (feature branch from main)
