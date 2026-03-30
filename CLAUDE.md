# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Ratatoskr** is a polyglot-LLM fork of [agoda-com/api-agent](https://github.com/agoda-com/api-agent). Upstream changes may need periodic cherry-picking.

## Commands

**Setup:**
```bash
uv sync --group dev
```

**Run server:**
```bash
uv run api-agent                    # Local dev (OpenAI default)
uv run api-agent --provider anthropic --api-key sk-ant-...  # Anthropic
uv run api-agent --provider openai-compat --base-url http://localhost:11434/v1 --model llama3  # Local
# Server starts on http://localhost:3000/mcp
```

**Tests:**
```bash
uv run pytest tests/ -v              # All tests (1412 passing)
uv run pytest tests/test_foo.py -v   # Single test file
uv run pytest tests/test_foo.py::test_bar -v  # Single test
```

**Linting & Formatting:**
```bash
uv run ruff check api_agent/
uv run ruff check --fix api_agent/   # Auto-fix
uv run ruff format api_agent/        # Format
uv run ty check                      # Type check
```

**Docker:**
```bash
docker build -t ratatoskr .
docker run -p 3000:3000 -e OPENAI_API_KEY="..." ratatoskr
```

## Architecture

**MCP Server (FastMCP)** receives NL queries + headers → routes to **Agents** (pluggable LLM providers) → agents call target APIs + DuckDB for SQL processing.

### Request Flow

1. **Client** sends MCP request w/ headers (`X-Target-URL`, `X-API-Type`, `X-Target-Headers`)
2. **middleware.py**: `DynamicToolNamingMiddleware` transforms tool names per session (e.g., `_query` → `flights_api_query` based on URL)
3. **context.py**: Extracts `RequestContext` from headers
4. **tools/query.py**: Routes to GraphQL or REST agent
5. **agent/graphql_agent.py** or **agent/rest_agent.py**:
   - Fetches schema (introspection or OpenAPI)
   - Creates agent w/ dynamic tools (`graphql_query`/`rest_call`, `sql_query`, `search_schema`)
   - Runs agent loop (max 30 turns) via `LLMProvider.run_tool_loop()`
   - Returns results
6. **executor.py**: DuckDB integration for SQL post-processing

### Key Modules

- **api_agent/**: Main package
  - **__main__.py**: Entry point, creates FastMCP app w/ middleware
  - **config.py**: Settings via `pydantic-settings` (env vars w/ `API_AGENT_` prefix)
  - **context.py**: Header parsing → `RequestContext`, tool name generation
  - **middleware.py**: Dynamic tool naming per session
  - **tracing.py**: OpenTelemetry tracing via OTLP

- **api_agent/llm/**: LLM provider abstraction (Ratatoskr addition)
  - **provider.py**: `LLMProvider` ABC with `complete()` and `run_tool_loop()`
  - **types.py**: `LLMResponse`, `ToolCall`, `ToolDefinition`, `ToolResult`
  - **openai_provider.py**: OpenAI native SDK provider
  - **anthropic_provider.py**: Anthropic native SDK provider
  - **openai_compat.py**: OpenAI-compatible provider (Ollama, LM Studio, vLLM) with retry-without-tools fallback

- **api_agent/tools/**: MCP tool implementations
  - **query.py**: `_query` tool (NL → agent)
  - **execute.py**: `_execute` tool (direct GraphQL/REST call)

- **api_agent/agent/**: Agent logic
  - **graphql_agent.py**: GraphQL agent w/ introspection, query building, SQL
  - **rest_agent.py**: REST agent w/ OpenAPI parsing, polling support
  - **model.py**: Lazy LLM provider singleton (`_provider` / `provider` — monkeypatch both in tests)
  - **prompts.py**: Shared system prompt fragments
  - **progress.py**: Turn tracking
  - **schema_search.py**: Grep-like schema search tool
  - **contextvar_utils.py**: Safe ContextVar access helpers

- **api_agent/recipe/**: Parameterized pipeline caching
  - **store.py**: `RecipeStore` (LRU in-memory cache, thread-safe)
  - **extractor.py**: Extract reusable recipes from agent runs
  - **runner.py**: Execute recipes outside agent context (for MCP tools)
  - **common.py**: Recipe validation, execution, parameter binding
  - **naming.py**: Tool name sanitization

- **api_agent/utils/**: Shared utilities
  - **csv.py**: CSV conversion via DuckDB (for recipe `return_directly` output)

- **api_agent/graphql/**: GraphQL client (httpx)
- **api_agent/rest/**: REST client (httpx) + OpenAPI loader
- **api_agent/executor.py**: DuckDB SQL execution, table extraction, context truncation

### Context Management

All outputs capped at ~32k chars (`MAX_TOOL_RESPONSE_CHARS`) to prevent LLM overflow:
- **Query results**: Truncate by char count, show complete rows that fit
- **Schema**: Truncate large schemas, use `search_schema()` for exploration
- **Single objects**: Return DuckDB schema summary instead of full data

Agents use **ContextVar** for request isolation: `_graphql_queries`, `_query_results`, `_last_result`, `_raw_schema`. Use mutable containers (lists/dicts) since `ContextVar.set()` in child tasks doesn't propagate to parent.

### Tool Naming

Tools have internal names (`_query`, `_execute`) transformed by middleware per session:
- **Format**: `{prefix}_query`, `{prefix}_execute` — prefix from hostname or `X-API-Name` header
- Skips generic parts: TLDs, `api`, `qa`, `dev`, `internal`
- **Recipe tools**: `r_{slug}` (not API-specific), max 60 chars; `send_tool_list_changed()` notifies clients

### Safety

- **GraphQL**: Mutations blocked (queries only). Partial success returns both `data` and `errors` per GraphQL spec.
- **REST**: POST/PUT/DELETE/PATCH blocked by default, enable via `X-Allow-Unsafe-Paths` header (glob patterns)

### Polling (REST only)

Set `X-Poll-Paths` header to enable `poll_until_done` tool:
- Auto-increments `polling.count` in body between polls
- Checks `done_field` (dot-path like `"status"`, `"trips.0.isCompleted"`) against `done_value`
- Max 20 polls (configurable), default 3s delay

### TOON Compression

- **Why**: JSON punctuation (braces, quotes, colons) creates noise tokens that dilute LLM attention. TOON strips this, concentrating attention on field names and values.
- **Where**: `api_agent/llm/toon_encoder.py` — `ToolResultEncoder` class
- **Integration**: Wired into `format_tool_response()` and `sql_query()` in `orchestrator.py`
- **Header**: TOON output carries `[success:true format:toon]\n` prefix
- **Config**: `TOON_TOOL_RESULTS_ENABLED` (default: true)
- **Fallback**: If toon_format not installed or output is larger, falls back to JSON silently
- **Size guard**: Includes `_TOON_HEADER` length in comparison to ensure TOON is always smaller

### Schema Reduction

3-layer pipeline in `api_agent/schema/reducer.py`: keyword ranking → TOON → AI reduction.

- **AIReductionLayer**: Provider-agnostic (replaces old Anthropic-only HaikuLayer). Uses `LLMProvider.complete()` with `tools=None` (security boundary).
- **Factory**: `api_agent/llm/factory.py` — `create_schema_reduction_provider()` builds a provider from `SCHEMA_REDUCTION_*` settings, falling back to main provider config. Timeout baked into HTTP client at construction.
- **Config**: `SCHEMA_REDUCTION_PROVIDER` (inherits `PROVIDER`), `SCHEMA_REDUCTION_MODEL` (inherits provider default), `SCHEMA_REDUCTION_API_KEY` (inherits `API_KEY`), `SCHEMA_REDUCTION_BASE_URL` (inherits `BASE_URL`)
- **Orchestrator**: Lazy singleton `_get_schema_reduction_provider()` in `orchestrator.py`, resettable via `_reset_schema_reduction_provider()` for tests

### PROFILE Preset

- **`PROFILE=local`** (or `API_AGENT_PROFILE=local`): Sets `BLOCK_PRIVATE_IPS=false`, `LOG_FORMAT=console`, `SCHEMA_REDUCTION_ENABLED=false`. One env var for local dev.
- **CLI**: `--profile local` flag in `__main__.py`
- **Implementation**: `model_validator(mode="before")` on `Settings` — injects defaults before field validation, explicit env vars always win
- **`_profile_overrides`**: Module-level list in `config.py`, populated by validator, read by `__main__.py` for startup logging

### Recipes

Caches parameterized API call + SQL pipelines from successful agent runs, exposed as MCP tools:

```
Query → Agent executes → Extractor LLM → Recipe stored → MCP tool `r_{name}` exposed
```

- **Storage**: LRU in-memory (default 64 entries), keyed by `(api_id, schema_hash)` - auto-invalidates on schema change
- **Deduplication**: Skips equivalent recipes, ensures unique tool names
- **Templating**: GraphQL `{{param}}`, REST `{"$param": "name"}`, SQL `{{param}}`
- **Config**: `ENABLE_RECIPES` (default: True), `RECIPE_CACHE_SIZE` (default: 64)

## Testing

1412 tests, pytest-asyncio. CI runs tests + linting + type checking on Python 3.11/3.12.

### Test Patterns

- **`FakeLLMProvider`** in `tests/conftest.py`: Returns canned `LLMResponse` objects. Monkeypatch at `api_agent.agent.model._provider` AND `api_agent.agent.model.provider`.
- **`fake_provider_factory`** fixture: Accepts either `(monkeypatch, responses)` or `(responses, monkeypatch)`.
- **Mock boundary**: Mock httpx transport and LLM provider. Use REAL ContextVar, RecipeStore, DuckDB executor.
- **Fixtures**: `tests/fixtures/` has recorded JSON responses and sample GraphQL schemas.
- **Helpers**: `make_text_response()`, `make_tool_call_response()` for building canned LLM responses.

### Test Coverage Map

| Area | Test Files |
|------|-----------|
| GraphQL orchestrator | `test_graphql_agent.py` (8 tests) |
| REST orchestrator | `test_rest_agent.py` (10 tests) |
| gRPC orchestrator | `test_grpc_agent.py` (47 tests) |
| GraphQL client | `test_graphql_client.py` (15 tests) |
| gRPC client | `test_grpc_client.py` (37 tests) |
| gRPC reflection | `test_grpc_reflection.py` (21 tests) |
| Execute tool | `test_execute_tool.py` (32 tests) |
| Recipe runner | `test_recipe_runner.py` (12 tests) |
| Config settings | `test_config.py` (93 tests) |
| OpenAI provider | `test_llm/test_openai_complete.py` (7 tests) |
| Anthropic provider | `test_llm/test_anthropic_complete.py` (8 tests) |
| OpenAI-compat provider | `test_llm/test_compat_complete.py` (9 tests) |
| Query tool routing | `test_query_tool.py` (8 tests) |
| gRPC recipes | `test_grpc_recipe.py` (28 tests) |
| Middleware | `test_middleware_routing.py` (8 tests) |
| Endpoint filtering | `test_filtering.py` (63 tests) |
| Allowlist integration | `test_endpoint_allowlist.py` (9 tests) |
| Schema reduction provider factory | `test_provider_factory.py` (16 tests) |
| AI reduction layer | `test_haiku_layer.py` (14 tests) |

