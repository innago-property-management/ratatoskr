# Quickstart Guide: TOON Format Integration

**Feature**: 001-toon-format-integration  
**Date**: 2026-02-23  
**Phase**: 1 (Design & Contracts)

## 1. What is TOON?

**TOON** (Tabular Object Oriented Notation) is a compact, human-readable serialization format designed specifically for LLM contexts. It can reduce token consumption by **30-60%** compared to JSON, especially for the kind of structured data that API agents handle every day: lists of objects.

By enabling TOON in Ratatoskr, you can:
- **Reduce LLM API Costs**: Fewer tokens per request means lower costs.
- **Increase Context Window**: Fit more data (like larger API responses or schemas) into the LLM's context window.
- **Improve Agent Performance**: Smaller contexts can sometimes lead to faster LLM response times.

Ratatoskr integrates TOON at multiple levels, from tool results to API schemas, to maximize token savings throughout the agent's lifecycle.

## 2. Installation

TOON integration requires two optional dependencies. You can install them with the `[toon]` extra:

```bash
# Install Ratatoskr with TOON dependencies
pip install ratatoskr[toon]

# Or, if you have Ratatoskr already installed:
pip install toon-format>=0.9.3 tiktoken>=0.5.0
```

If these dependencies are not installed, Ratatoskr will function normally but all TOON-related features will be disabled, regardless of configuration.

## 3. Enabling TOON

TOON integration is an **opt-in beta feature**. It is disabled by default to ensure backward compatibility.

You can enable it globally using environment variables.

### Basic Configuration (Recommended)

This configuration enables TOON for the two most impactful areas: tool results and schema context.

```bash
# .env file

# --- TOON Integration (Beta) ---

# Step 1: Globally enable the TOON feature
API_AGENT_ENABLE_TOON=true

# Step 2: Enable TOON for specific integration points
API_AGENT_TOON_TOOL_RESULTS=true
API_AGENT_TOON_SCHEMA_CONTEXT=true
```

### Advanced Configuration

You can control each TOON integration point separately.

```bash
# .env file

# --- TOON Integration (Advanced) ---

# Global toggle. MUST be true to enable any sub-feature.
API_AGENT_ENABLE_TOON=true

# Encode API tool results (GraphQL/REST) as TOON. (Default: false)
# Highest impact on token savings.
API_AGENT_TOON_TOOL_RESULTS=true

# Encode API schemas (GraphQL/OpenAPI) as TOON. (Default: false)
# Reduces initial context size.
API_AGENT_TOON_SCHEMA_CONTEXT=true

# Encode DuckDB SQL query results as TOON. (Default: false)
API_AGENT_TOON_SQL_RESULTS=false

# Allow MCP clients to request TOON output via 'X-Output-Format: toon' header. (Default: false)
API_AGENT_TOON_MCP_OUTPUT=false

# Add a hint to the system prompt explaining the TOON format to the LLM.
# Recommended: false. Only enable if you notice the LLM struggling to interpret TOON.
API_AGENT_TOON_FORMAT_HINT=false

# Timeout in milliseconds for TOON encoding. If exceeded, Ratatoskr falls back to JSON.
API_AGENT_TOON_ENCODING_TIMEOUT_MS=50
```

## 4. Per-Request Control (A/B Testing)

For debugging or A/B testing, you can override the global configuration for a specific MCP request by sending HTTP headers.

**Note**: Headers can only override features that are globally enabled. They cannot enable a feature that is disabled in the environment configuration.

### Supported Headers

- `X-Enable-TOON`: `true` or `false`. Temporarily enables or disables all TOON features for this request.
- `X-TOON-Tool-Results`: `true` or `false`. Overrides the setting for tool results.
- `X-TOON-Schema-Context`: `true` or `false`. Overrides the setting for schema context.
- `X-TOON-SQL-Results`: `true` or `false`. Overrides the setting for SQL results.

### Example: A/B Testing

Imagine `API_AGENT_ENABLE_TOON` is `true` in your environment.

**Scenario A: Run with TOON (Default Behavior)**
```bash
curl -X POST http://localhost:3000/mcp \
  -d '{"query": "Find the top 5 users"}'
```
*(This request will use TOON encoding as configured in the environment.)*

**Scenario B: Run with JSON (Temporarily Disable TOON)**
```bash
curl -X POST http://localhost:3000/mcp \
  -H "X-Enable-TOON: false" \
  -d '{"query": "Find the top 5 users"}'
```
*(This request will use JSON, allowing you to compare token usage and LLM performance against the TOON-enabled request.)*

## 5. Measuring the Impact (Observability)

The key reason to use TOON is to save tokens. We provide detailed OpenTelemetry (OTel) metrics and span attributes to measure the impact.

### OpenTelemetry Span Attributes

In your tracing backend (like Jaeger or Datadog), inspect the spans for `llm.tool_loop` and `toon.encode`. You will find the following attributes on TOON-enabled requests:

- `toon.format_used`: "json" or "toon"
- `toon.tokens_json`: (int) Estimated token count if the data were JSON.
- `toon.tokens_toon`: (int) The actual token count of the TOON output.
- `toon.compression_ratio`: (float) The percentage of tokens saved. (e.g., `0.6` for 60% savings).
- `toon.fallback`: (bool) `True` if TOON encoding failed and the system fell back to JSON.
- `toon.error`: (string) The error message if a fallback occurred.

### Metrics

We export the following metrics to your OTel collector:

- **`ratatoskr.toon.encoding.errors`** (Counter): Tracks the number of times TOON encoding failed. Use this to monitor the health of the integration.
- **`ratatoskr.toon.tokens_saved`** (Counter): The cumulative number of tokens saved by using TOON.
- **`ratatoskr.toon.encoding.latency`** (Histogram): The time taken for TOON encoding operations.

## 6. Troubleshooting

### My requests are still using JSON, even though I enabled TOON.

1.  **Check Dependencies**: Make sure you installed the optional dependencies: `pip install ratatoskr[toon]`. If `toon-format` or `tiktoken` are not found, TOON features will be silently disabled.
2.  **Check Global Flag**: Ensure `API_AGENT_ENABLE_TOON=true` is set. If this is `false`, all other `API_AGENT_TOON_*` flags are ignored.
3.  **Check Logs**: Look for warnings or errors related to TOON in the Ratatoskr server logs. The system will log failures during initialization or encoding.

### I'm seeing fallback warnings in the logs.

The log message `TOON encoding failed, falling back to JSON` means the system is working as designed. This can happen if:
- An API response contains a data structure the `toon-format` library doesn't support.
- The encoding process took longer than the configured timeout (`API_AGENT_TOON_ENCODING_TIMEOUT_MS`).

If you see this frequently for a specific tool, investigate the data structure of that tool's response. The system will continue to function correctly by using JSON for that result.

### The LLM seems to be misunderstanding the TOON format.

This is rare, but possible with some models or complex data.
1.  **Enable the Format Hint**: Try setting `API_AGENT_TOON_FORMAT_HINT=true`. This adds a detailed explanation of the TOON syntax to the system prompt, which can improve the LLM's understanding.
2.  **Report an Issue**: If the problem persists, please open an issue on the Ratatoskr GitHub repository with a sample of the TOON data that is being misinterpreted.
