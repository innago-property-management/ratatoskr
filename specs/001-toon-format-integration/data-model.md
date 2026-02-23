# Data Model: TOON Format Integration

**Feature**: 001-toon-format-integration  
**Date**: 2026-02-23  
**Phase**: 1 (Design & Contracts)

## Overview

This document defines the data structures, types, and relationships for TOON format integration in Ratatoskr. The model focuses on: encoding abstraction, configuration, observability metadata, and enhanced tool result types.

---

## Core Entities

### 1. TOONEncoder

**Purpose**: Wrapper around `toon-format` library providing error handling, fallback logic, token counting, and observability.

**Module**: `api_agent/llm/toon_encoder.py`

#### Attributes

```python
@dataclass
class TOONEncoder:
    """TOON encoding wrapper with error handling and observability."""
    
    config: TOONConfig
    """Configuration for TOON features (loaded from env vars and request headers)."""
    
    _tiktoken_encoding: Encoding | None = field(default=None, init=False)
    """Cached tiktoken encoder (lazy-loaded on first use)."""
    
    _encoding_failures: int = field(default=0, init=False)
    """Count of encoding failures this request (for per-request disable threshold)."""
```

#### Methods

```python
def encode(self, data: Any) -> str | None:
    """
    Encode Python data structure to TOON format.
    
    Args:
        data: Python dict, list, or primitive to encode.
    
    Returns:
        TOON-formatted string, or None if encoding fails.
    
    Behavior:
        - Calls toon_format.encode(data)
        - Catches all exceptions (returns None on error)
        - Enforces 50ms timeout (returns None if exceeded)
        - Logs errors with context (data sample, error message)
        - Increments self._encoding_failures on failure
    
    Observability:
        - Creates OTel span "toon.encode"
        - Span attributes: toon.input_size_bytes, toon.encoding_time_ms, toon.error
        - Increments metric: toon.encoding_errors (if failed)
    """

def estimate_tokens(self, data: Any) -> tuple[int, int]:
    """
    Estimate token count for JSON vs TOON encoding.
    
    Args:
        data: Python data structure.
    
    Returns:
        Tuple of (json_tokens, toon_tokens).
    
    Behavior:
        - Encodes data as JSON and TOON
        - Uses tiktoken (cl100k_base) to count tokens
        - If TOON encoding fails, returns (json_tokens, json_tokens)
    
    Note:
        Token counts are estimates using OpenAI's tokenizer.
        Anthropic uses a different tokenizer (~5-10% variance).
    """

def compare_formats(self, data: Any) -> ComparisonResult:
    """
    Compare JSON vs TOON encoding with detailed metrics.
    
    Args:
        data: Python data structure.
    
    Returns:
        ComparisonResult with side-by-side comparison.
    
    Use Case:
        Debugging, documentation, benchmarking.
    """

@property
def tiktoken_encoding(self) -> Encoding:
    """
    Lazy-load tiktoken encoder (cached after first access).
    
    Returns:
        tiktoken Encoding object (cl100k_base).
    
    Raises:
        ImportError: If tiktoken is not installed.
    """
```

#### Error Handling

**Exception Handling**:
- Catch `toon_format` exceptions: `TOONEncodingError`, `TOONDataError`, etc.
- Catch generic `Exception` as fallback (TOON library may raise unexpected errors)
- **Never propagate exceptions** — always return `None` on failure

**Timeout Handling**:
- Use `signal.SIGALRM` (Unix) or `threading.Timer` (cross-platform) for timeout
- Default timeout: 50ms (configurable via `config.encoding_timeout_ms`)
- On timeout: Log warning, return `None`

**Logging**:
```python
logger.warning(
    f"TOON encoding failed",
    extra={
        "error": str(e),
        "data_type": type(data).__name__,
        "data_size_bytes": sys.getsizeof(data),
        "data_sample": str(data)[:500],  # First 500 chars
    }
)
```

#### Observability

**OTel Span** (created for each `encode()` call):
```python
with tracer.start_as_current_span("toon.encode") as span:
    span.set_attribute("toon.input_size_bytes", sys.getsizeof(data))
    span.set_attribute("toon.input_type", type(data).__name__)
    
    # After encoding
    span.set_attribute("toon.encoding_time_ms", latency_ms)
    span.set_attribute("toon.output_size_chars", len(result) if result else 0)
    
    # On error
    span.set_attribute("toon.error", str(e))
    span.set_status(Status(StatusCode.ERROR, str(e)))
```

**Metrics**:
- `toon.encoding_errors` (counter): Incremented on any encoding failure
- `toon.encoding_timeouts` (counter): Incremented when timeout exceeded
- `toon.encoding_latency` (histogram): Distribution of encoding times (ms)

---

### 2. TOONConfig

**Purpose**: Configuration object for TOON feature flags, loaded from environment variables and request headers.

**Module**: `api_agent/llm/toon_encoder.py` (or `api_agent/config.py`)

#### Attributes

```python
@dataclass
class TOONConfig:
    """Configuration for TOON encoding features."""
    
    enabled: bool = False
    """Global toggle for TOON features (default: False for beta opt-in)."""
    
    tool_results: bool = False
    """Encode tool results (GraphQL, REST API responses) as TOON."""
    
    schema_context: bool = False
    """Encode API schemas (GraphQL introspection, OpenAPI specs) as TOON."""
    
    sql_results: bool = False
    """Encode DuckDB SQL query results as TOON."""
    
    mcp_output: bool = False
    """Support X-Output-Format: toon header for MCP responses."""
    
    fallback_on_error: bool = True
    """Fall back to JSON on TOON encoding errors (recommended: True)."""
    
    encoding_timeout_ms: int = 50
    """Maximum encoding time before fallback (milliseconds)."""
    
    format_hint_enabled: bool = False
    """Add TOON format hint to system prompts (default: False, enable if LLM accuracy <95%)."""
    
    @classmethod
    def from_env(cls) -> "TOONConfig":
        """Load configuration from environment variables."""
    
    def merge_request_headers(self, headers: dict[str, str]) -> "TOONConfig":
        """
        Create new config with request headers applied.
        
        Args:
            headers: Request headers (case-insensitive dict).
        
        Returns:
            New TOONConfig with header overrides applied.
        
        Supported Headers:
            - X-Enable-TOON: true|false (overrides global enabled)
            - X-TOON-Tool-Results: true|false
            - X-TOON-Schema-Context: true|false
            - X-TOON-SQL-Results: true|false
        """
```

#### Loading from Environment Variables

```python
# Environment variable mapping
API_AGENT_ENABLE_TOON=true                # → enabled
API_AGENT_TOON_TOOL_RESULTS=true          # → tool_results
API_AGENT_TOON_SCHEMA_CONTEXT=true        # → schema_context
API_AGENT_TOON_SQL_RESULTS=true           # → sql_results
API_AGENT_TOON_MCP_OUTPUT=true            # → mcp_output
API_AGENT_TOON_FALLBACK_ON_ERROR=true     # → fallback_on_error
API_AGENT_TOON_ENCODING_TIMEOUT_MS=50     # → encoding_timeout_ms
API_AGENT_TOON_FORMAT_HINT=false          # → format_hint_enabled
```

#### Request Header Override Logic

```python
def merge_request_headers(self, headers: dict[str, str]) -> "TOONConfig":
    """Request headers override env vars."""
    return TOONConfig(
        enabled=_parse_bool_header(headers, "X-Enable-TOON", self.enabled),
        tool_results=_parse_bool_header(headers, "X-TOON-Tool-Results", self.tool_results),
        schema_context=_parse_bool_header(headers, "X-TOON-Schema-Context", self.schema_context),
        sql_results=_parse_bool_header(headers, "X-TOON-SQL-Results", self.sql_results),
        mcp_output=self.mcp_output,  # Not overridable per-request
        fallback_on_error=self.fallback_on_error,  # Not overridable
        encoding_timeout_ms=self.encoding_timeout_ms,  # Not overridable
        format_hint_enabled=self.format_hint_enabled,  # Not overridable
    )
```

---

### 3. ToolResult (Enhanced)

**Purpose**: Extend existing `ToolResult` dataclass with format metadata for observability.

**Module**: `api_agent/llm/types.py`

#### Current Definition (Pre-TOON)

```python
@dataclass
class ToolResult:
    """Result of executing a tool."""
    
    tool_call_id: str
    """ID of the tool call this result corresponds to."""
    
    name: str
    """Name of the tool that was executed."""
    
    content: str
    """Serialized result string (JSON or TOON)."""
```

#### Enhanced Definition (Post-TOON)

```python
@dataclass
class ToolResult:
    """Result of executing a tool."""
    
    tool_call_id: str
    name: str
    content: str
    
    # New attributes for TOON integration
    format: Literal["json", "toon"] = "json"
    """Format used for content serialization."""
    
    tokens_json: int | None = None
    """Estimated token count if encoded as JSON (for comparison)."""
    
    tokens_toon: int | None = None
    """Estimated token count (if TOON was used)."""
    
    @property
    def compression_ratio(self) -> float | None:
        """
        Calculate compression ratio (0.0 to 1.0).
        
        Returns:
            (tokens_json - tokens_toon) / tokens_json, or None if no token data.
        
        Example:
            JSON: 1000 tokens, TOON: 400 tokens → compression_ratio = 0.60 (60% savings)
        """
        if self.tokens_json and self.tokens_toon:
            return (self.tokens_json - self.tokens_toon) / self.tokens_json
        return None
```

#### Backward Compatibility

**New attributes are optional** (have default values):
- Existing code that constructs `ToolResult` without format metadata continues to work
- Default: `format="json"`, `tokens_json=None`, `tokens_toon=None`
- Only code that explicitly accesses new attributes needs updates

**Migration Path**:
```python
# Old code (still works)
result = ToolResult(tool_call_id="123", name="query", content=json.dumps(data))

# New code (with TOON metadata)
result = ToolResult(
    tool_call_id="123",
    name="query",
    content=toon_str,
    format="toon",
    tokens_json=1000,
    tokens_toon=400,
)
```

---

### 4. ComparisonResult

**Purpose**: Output of `TOONEncoder.compare_formats()` for debugging and benchmarking.

**Module**: `api_agent/llm/toon_encoder.py`

#### Definition

```python
@dataclass
class ComparisonResult:
    """Side-by-side comparison of JSON vs TOON encoding."""
    
    data_sample: str
    """First 200 characters of input data (for context)."""
    
    json_output: str
    """Full JSON-encoded output."""
    
    toon_output: str | None
    """Full TOON-encoded output (None if encoding failed)."""
    
    json_tokens: int
    """Token count for JSON output (via tiktoken)."""
    
    toon_tokens: int | None
    """Token count for TOON output (None if encoding failed)."""
    
    json_size_chars: int
    """Character count of JSON output."""
    
    toon_size_chars: int | None
    """Character count of TOON output (None if encoding failed)."""
    
    compression_ratio: float | None
    """(json_tokens - toon_tokens) / json_tokens, or None if TOON failed."""
    
    encoding_time_ms: float
    """Time spent encoding to TOON (milliseconds)."""
    
    error: str | None
    """Error message if TOON encoding failed, otherwise None."""
    
    def to_table(self) -> str:
        """
        Format as human-readable comparison table.
        
        Returns:
            Markdown table comparing JSON vs TOON.
        
        Example:
            Format Comparison
            ────────────────────────────────────────────────
            Format    Tokens    Size (chars)    Compression
            JSON      1000      3200            -
            TOON      400       1100            60.0%
            ────────────────────────────────────────────────
            Savings: 600 tokens (60.0%)
        """
```

#### Use Cases

1. **Debugging**: Investigate why TOON encoding failed or produced unexpected output
2. **Documentation**: Generate examples for user guide
3. **Benchmarking**: Measure token savings across sample data

---

## Supporting Types

### 5. TOON_FORMAT_HINT (Constant)

**Purpose**: System prompt hint for TOON format (used only if LLM accuracy <95%).

**Module**: `api_agent/agent/prompts.py`

```python
TOON_FORMAT_HINT = """
Tool results may be formatted in TOON (Tabular Object Oriented Notation) for compactness:
- Objects use `key: value` pairs with indentation (like YAML)
- Arrays of uniform objects use tabular format: `[count,]{field1,field2}:` followed by data rows
- Parse TOON the same way you parse JSON — extract fields, aggregate data, and generate queries.

Example TOON format:
```
users[3,]{id,name,age}:
1,Alice,30
2,Bob,25
3,Charlie,35
```

This is equivalent to JSON:
```json
{"users": [{"id": 1, "name": "Alice", "age": 30}, {"id": 2, "name": "Bob", "age": 25}, {"id": 3, "name": "Charlie", "age": 35}]}
```
""".strip()
```

**Configuration**:
```python
# In graphql_agent.py, rest_agent.py
def build_system_prompt() -> str:
    prompt = BASE_SYSTEM_PROMPT
    
    if settings.TOON_FORMAT_HINT_ENABLED:
        prompt += "\n\n" + TOON_FORMAT_HINT
    
    return prompt
```

---

## OpenTelemetry Attributes

**Purpose**: Standardized span attributes for TOON observability.

**Module**: `api_agent/tracing.py`

```python
# TOON-specific span attribute keys
class OTelAttributes:
    """OpenTelemetry span attribute keys."""
    
    # Encoding metadata
    TOON_FORMAT_USED = "toon.format_used"           # "json" | "toon"
    TOON_TOKENS_JSON = "toon.tokens_json"           # int
    TOON_TOKENS_TOON = "toon.tokens_toon"           # int | None
    TOON_COMPRESSION_RATIO = "toon.compression_ratio"  # float (0.0-1.0)
    TOON_ENCODING_TIME_MS = "toon.encoding_time_ms"    # float
    
    # Error tracking
    TOON_FALLBACK = "toon.fallback"                 # bool (true if fallback occurred)
    TOON_ERROR = "toon.error"                       # str (error message)
    TOON_FALLBACKS_THIS_TURN = "toon.fallbacks_this_turn"  # int
    TOON_REQUEST_DISABLED = "toon.request_disabled" # bool (disabled mid-request)
    
    # Input metadata
    TOON_INPUT_SIZE_BYTES = "toon.input_size_bytes"  # int
    TOON_INPUT_TYPE = "toon.input_type"              # str (type name)
```

**Usage Example**:
```python
with tracer.start_as_current_span("llm.tool_loop") as span:
    # Encode tool results
    result = toon_encoder.encode(data)
    
    # Record metadata
    span.set_attribute(OTelAttributes.TOON_FORMAT_USED, "toon" if result else "json")
    span.set_attribute(OTelAttributes.TOON_TOKENS_JSON, json_tokens)
    span.set_attribute(OTelAttributes.TOON_TOKENS_TOON, toon_tokens)
    span.set_attribute(OTelAttributes.TOON_COMPRESSION_RATIO, compression_ratio)
```

---

## State Transitions

### ToolResult Format State Machine

```
┌──────────────────────────────────────────────────────────┐
│ Initial State: ToolResult does not exist yet             │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
         ╔═══════════════════════════════╗
         ║   Tool Execution Complete     ║
         ║   (data ready to serialize)   ║
         ╚═══════════════╦═══════════════╝
                         │
          ┌──────────────┴──────────────┐
          │                             │
  ┌───────▼──────────┐         ┌────────▼─────────┐
  │  TOON Disabled   │         │  TOON Enabled    │
  │  (config check)  │         │  (config check)  │
  └───────┬──────────┘         └────────┬─────────┘
          │                             │
          ▼                             ▼
  ┌─────────────┐              ┌────────────────┐
  │ Format:     │              │ Try TOON       │
  │   JSON      │              │ Encoding       │
  └─────────────┘              └────────┬───────┘
                                        │
                       ┌────────────────┴────────────────┐
                       │                                 │
              ┌────────▼───────┐              ┌──────────▼─────────┐
              │ Encoding       │              │ Encoding Success   │
              │ Failed/Timeout │              │                    │
              └────────┬───────┘              └──────────┬─────────┘
                       │                                 │
                       ▼                                 ▼
              ┌─────────────┐              ┌────────────────────┐
              │ Fallback to │              │ Format: TOON       │
              │ JSON        │              │ tokens_json: 1000  │
              │             │              │ tokens_toon: 400   │
              └─────────────┘              └────────────────────┘
```

### Per-Request TOON Disable Logic

```
Request starts with TOON enabled
         │
         ▼
╔════════════════════╗
║  Tool Execution    ║
║  Turn 1            ║
╚════════╦═══════════╝
         │
    ┌────┴────┐
    │ TOON OK │ → Continue
    └────┬────┘
         │
    ┌────▼────────┐
    │ TOON Failed │
    └────┬────────┘
         │
         ▼
  _encoding_failures += 1
         │
    ┌────▼────────────────────┐
    │ failures < 3?           │
    │ Yes → Try TOON again    │
    │ No  → Disable for       │
    │       entire request    │
    └─────────────────────────┘
```

---

## Relationships

```
┌─────────────────────┐
│  LLMProvider        │
│  (provider.py)      │
│                     │
│  - run_tool_loop()  │
│  - _execute_tools() │
└──────────┬──────────┘
           │
           │ Uses
           ▼
┌─────────────────────┐
│  TOONEncoder        │
│  (toon_encoder.py)  │
│                     │
│  - encode()         │
│  - estimate_tokens()│
└──────────┬──────────┘
           │
           │ Produces
           ▼
┌─────────────────────┐
│  ToolResult         │
│  (types.py)         │
│                     │
│  - content: str     │
│  - format: "toon"   │
│  - tokens_json: int │
│  - tokens_toon: int │
└─────────────────────┘
```

---

## Validation Rules

### ToolResult Validation

```python
def validate_tool_result(result: ToolResult) -> None:
    """Validate ToolResult constraints."""
    
    # Format must be "json" or "toon"
    assert result.format in ("json", "toon"), f"Invalid format: {result.format}"
    
    # If format is "toon", tokens_toon must be set
    if result.format == "toon":
        assert result.tokens_toon is not None, "TOON format requires tokens_toon"
    
    # If tokens are set, tokens_json >= tokens_toon (TOON should save tokens)
    if result.tokens_json and result.tokens_toon:
        assert result.tokens_json >= result.tokens_toon, \
            f"TOON tokens ({result.tokens_toon}) should not exceed JSON tokens ({result.tokens_json})"
```

### TOONConfig Validation

```python
def validate_toon_config(config: TOONConfig) -> None:
    """Validate TOONConfig constraints."""
    
    # Encoding timeout must be positive
    assert config.encoding_timeout_ms > 0, "encoding_timeout_ms must be positive"
    
    # If any feature is enabled, global enabled must be True
    if config.tool_results or config.schema_context or config.sql_results or config.mcp_output:
        assert config.enabled, "TOON features require enabled=True"
```

---

## Data Flow Example

**Scenario**: Agent executes GraphQL query with TOON enabled

```
1. Request arrives → Load TOONConfig from env + headers
   
2. Agent calls run_tool_loop()
   ↓
   Execute tool: query_users()
   ↓
   Result: [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
   ↓
   TOONEncoder.encode(result)
   ↓
   TOON output: "users[2,]{id,name}:\n1,Alice\n2,Bob"
   ↓
   TOONEncoder.estimate_tokens(result)
   ↓
   tokens_json=45, tokens_toon=18 (60% compression)
   ↓
   Build ToolResult:
     - content: "users[2,]{id,name}:\n1,Alice\n2,Bob"
     - format: "toon"
     - tokens_json: 45
     - tokens_toon: 18
   ↓
   Format tool result message for LLM:
     {"role": "tool", "tool_call_id": "123", "content": "<TOON string>"}
   ↓
   LLM receives TOON-formatted tool result
   ↓
   LLM parses TOON, extracts user data, generates next query

3. OTel span attributes recorded:
   - toon.format_used: "toon"
   - toon.tokens_json: 45
   - toon.tokens_toon: 18
   - toon.compression_ratio: 0.60
```

---

## Summary

**Key Data Structures**:
1. `TOONEncoder` — Encoding wrapper with error handling
2. `TOONConfig` — Feature flags loaded from env + headers
3. `ToolResult` (enhanced) — Format metadata for observability
4. `ComparisonResult` — Debugging/benchmarking output
5. `OTelAttributes` — Standardized span attribute keys

**Design Principles**:
- Backward compatible (new attributes optional)
- Graceful degradation (JSON fallback on error)
- Observable (OTel spans + metrics)
- Configurable (global + per-request toggles)

**Next Steps**:
- Phase 1.2: Create interface contracts (`contracts/toon_encoder.md`)
- Phase 1.3: Create user guide (`quickstart.md`)
- Phase 2.1: Implement `TOONEncoder` class
