# API Agent

**Turn any API into an MCP server. Query in English. Get results—even when the API can't.**

Point at any GraphQL or REST API. Ask questions in natural language. The agent fetches data, stores it in DuckDB, and runs SQL post-processing. Rankings, filters, JOINs work **even if the API doesn't support them**.

## What Makes It Different

**🎯 Zero config.** No custom MCP code per API. Point at a GraphQL endpoint or OpenAPI spec — schema introspected automatically.

**✨ SQL post-processing.** API returns 10,000 unsorted rows? Agent ranks top 10. No GROUP BY? Agent aggregates. Need JOINs across endpoints? Agent combines.

**🔒 Safe by default.** Read-only. Mutations blocked unless explicitly allowed.

**🧠 Recipe learning.** Successful queries become cached pipelines. Reuse instantly without LLM reasoning.

## Quick Start

**1. Run (choose one):**

```bash
# OpenAI (default)
OPENAI_API_KEY=your_key uv run api-agent

# Anthropic (Claude)
uv run api-agent --provider anthropic --api-key sk-ant-your_key

# Local model (Ollama, LM Studio, vLLM)
uv run api-agent --provider openai-compat --base-url http://localhost:11434/v1 --model llama3

# Or Docker (OpenAI)
docker build -t api-agent .
docker run -p 3000:3000 -e OPENAI_API_KEY=your_key api-agent
```

**2. Add to any MCP client:**
```json
{
  "mcpServers": {
    "rickandmorty": {
      "url": "http://localhost:3000/mcp",
      "headers": {
        "X-Target-URL": "https://rickandmortyapi.com/graphql",
        "X-API-Type": "graphql"
      }
    }
  }
}
```

**3. Ask questions:**
- *"Show characters from Earth, only alive ones, group by species"*
- *"Top 10 characters by episode count"*
- *"Compare alive vs dead by species, only species with 10+ characters"*

That's it. Agent introspects schema, generates queries, runs SQL post-processing.

## More Examples

**REST API (Petstore):**
```json
{
  "mcpServers": {
    "petstore": {
      "url": "http://localhost:3000/mcp",
      "headers": {
        "X-Target-URL": "https://petstore3.swagger.io/api/v3/openapi.json",
        "X-API-Type": "rest"
      }
    }
  }
}
```

**Your own API with auth:**
```json
{
  "mcpServers": {
    "myapi": {
      "url": "http://localhost:3000/mcp",
      "headers": {
        "X-Target-URL": "https://api.example.com/graphql",
        "X-API-Type": "graphql",
        "X-Target-Headers": "{\"Authorization\": \"Bearer YOUR_TOKEN\"}"
      }
    }
  }
}
```

---

## Reference

### Headers

| Header                 | Required | Description                                                |
| ---------------------- | -------- | ---------------------------------------------------------- |
| `X-Target-URL`         | Yes      | GraphQL endpoint OR OpenAPI spec URL                       |
| `X-API-Type`           | Yes      | `graphql` or `rest`                                        |
| `X-Target-Headers`     | No       | JSON auth headers, e.g. `{"Authorization": "Bearer xxx"}`  |
| `X-API-Name`           | No       | Override tool name prefix (default: auto-generated)        |
| `X-Base-URL`           | No       | Override base URL for REST API calls                       |
| `X-Allow-Unsafe-Paths` | No       | JSON array of glob patterns for POST/PUT/DELETE/PATCH      |
| `X-Poll-Paths`         | No       | JSON array of paths requiring polling (enables poll tool)  |
| `X-Include-Result`     | No       | Include full uncapped `result` field in output             |

### MCP Tools

**Core tools** (2 per API):

| Tool               | Input                                                          | Output                          |
| ------------------ | -------------------------------------------------------------- | ------------------------------- |
| `{prefix}_query`   | Natural language question                                      | `{ok, data, queries/api_calls}` |
| `{prefix}_execute` | GraphQL: `query`, `variables` / REST: `method`, `path`, params | `{ok, data}`                    |

Tool names auto-generated from URL (e.g., `example_query`). Override with `X-API-Name`.

**Recipe tools** (dynamic, added as recipes are learned):

| Tool               | Input                              | Output |
| ------------------ | ---------------------------------- | ------ |
| `r_{recipe_slug}`  | flat recipe-specific params, `return_directly` (bool) | CSV or `{ok, data, executed_queries/calls}` |

Cached pipelines, no LLM reasoning. Appear after successful queries. Clients notified via `tools/list_changed`.

### CLI Arguments

| Argument       | Description                                              |
| -------------- | -------------------------------------------------------- |
| `--provider`   | LLM provider: `openai`, `anthropic`, or `openai-compat`  |
| `--model`      | Model name (default: provider-specific)                  |
| `--api-key`    | API key (overrides env vars)                             |
| `--base-url`   | Custom LLM endpoint (required for `openai-compat`)       |
| `--port`       | Server port (default: 3000)                              |
| `--host`       | Server host (default: 0.0.0.0)                           |
| `--transport`  | MCP transport: `http`, `streamable-http`, `sse`           |
| `--debug`      | Enable debug logging                                     |

CLI arguments override environment variables.

### Configuration (env vars)

| Variable                       | Required | Default                   | Description                        |
| ------------------------------ | -------- | ------------------------- | ---------------------------------- |
| `API_AGENT_PROVIDER`           | No       | `openai`                  | LLM provider (`openai`, `anthropic`, `openai-compat`) |
| `API_AGENT_API_KEY`            | **Yes**  | -                         | API key (also accepts `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) |
| `API_AGENT_BASE_URL`           | No*      | -                         | Custom LLM endpoint (*required for `openai-compat`) |
| `API_AGENT_MODEL_NAME`         | No       | (provider default)        | Model name                         |
| `API_AGENT_PORT`               | No       | 3000                      | Server port                        |
| `API_AGENT_ENABLE_RECIPES`     | No       | true                      | Enable recipe learning & caching   |
| `API_AGENT_RECIPE_CACHE_SIZE`  | No       | 64                        | Max cached recipes (LRU eviction)  |
| `OTEL_EXPORTER_OTLP_ENDPOINT`  | No       | -                         | OpenTelemetry tracing endpoint     |

**Provider defaults:**

| Provider        | Default model               | API key env var     |
| --------------- | --------------------------- | ------------------- |
| `openai`        | `gpt-4o`                    | `OPENAI_API_KEY`    |
| `anthropic`     | `claude-sonnet-4-20250514`  | `ANTHROPIC_API_KEY` |
| `openai-compat` | `gpt-4o`                    | (optional)          |

---

## How It Works

```mermaid
sequenceDiagram
    participant U as User
    participant M as MCP Server
    participant A as Agent
    participant G as Target API

    U->>M: Question + Headers
    M->>G: Schema introspection
    G-->>M: Schema
    M->>A: Schema + question
    A->>G: API call
    G-->>A: Data → stored in DuckDB
    A->>A: SQL post-processing
    A-->>M: Summary
    M-->>U: {ok, data, queries[]}
```

## Architecture

```mermaid
flowchart TB
    subgraph Client["MCP Client"]
        H["Headers: X-Target-URL, X-API-Type"]
    end

    subgraph MCP["MCP Server (FastMCP)"]
        Q["{prefix}_query"]
        E["{prefix}_execute"]
        R["r_{recipe} (dynamic)"]
    end

    subgraph Agent["Agents (Polyglot LLM)"]
        GA["GraphQL Agent"]
        RA["REST Agent"]
    end

    subgraph Exec["Executors"]
        HTTP["HTTP Client"]
        Duck["DuckDB"]
    end

    Client -->|NL + headers| MCP
    Q -->|graphql| GA
    Q -->|rest| RA
    E --> HTTP
    R -->|"no LLM"| HTTP
    R --> Duck
    GA --> HTTP
    RA --> HTTP
    GA --> Duck
    RA --> Duck
    HTTP --> API[Target API]
```

**Stack:** [FastMCP](https://github.com/jlowin/fastmcp) • [OpenAI](https://platform.openai.com/docs) / [Anthropic](https://docs.anthropic.com) / OpenAI-compatible • [DuckDB](https://duckdb.org)

---

## Recipe Learning

Agent learns reusable patterns from successful queries:

1. **Executes** — API calls + SQL via LLM reasoning
2. **Extracts** — LLM converts trace into parameterized template
3. **Caches** — Stores recipe keyed by (API, schema hash)
4. **Exposes** — Recipe becomes MCP tool (`r_{name}`) callable without LLM

```mermaid
flowchart LR
    subgraph First["First Query via {prefix}_query"]
        Q1["'Top 5 users by age'"]
        A1["Agent reasons"]
        E1["API + SQL"]
        R1["Recipe extracted"]
    end

    subgraph Tools["MCP Tools"]
        T["r_get_top_users<br/>params: {limit}"]
    end

    subgraph Reuse["Direct Call"]
        Q2["r_get_top_users({limit: 10})"]
        X["Execute directly"]
    end

    Q1 --> A1 --> E1 --> R1 --> T
    Q2 --> T --> X
```

Recipes auto-expire on schema changes. Disable with `API_AGENT_ENABLE_RECIPES=false`.

---

## Providers

This fork supports multiple LLM providers through a thin abstraction layer.

### OpenAI (default)

```bash
OPENAI_API_KEY=sk-... uv run api-agent
```

### Anthropic (Claude)

```bash
# Via CLI
uv run api-agent --provider anthropic --api-key sk-ant-...

# Via env vars
API_AGENT_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... uv run api-agent

# Custom model
uv run api-agent --provider anthropic --model claude-opus-4-20250514
```

### Local Models (Ollama, LM Studio, vLLM)

```bash
# Ollama
uv run api-agent --provider openai-compat \
  --base-url http://localhost:11434/v1 \
  --model llama3

# LM Studio
uv run api-agent --provider openai-compat \
  --base-url http://localhost:1234/v1 \
  --model local-model

# vLLM
uv run api-agent --provider openai-compat \
  --base-url http://gpu-server:8000/v1 \
  --model mistral-7b
```

> **Note:** Local models must support tool/function calling for full functionality.
> If an endpoint doesn't support tools, the agent will retry without them (graceful degradation).

---

## Development

```bash
git clone https://github.com/agoda-com/api-agent.git
cd api-agent
uv sync --group dev
uv run pytest tests/ -v      # Tests
uv run ruff check api_agent/  # Lint
uv run ty check               # Type check
```

## Observability

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable OpenTelemetry tracing. Works with Jaeger, Zipkin, Grafana Tempo, Arize Phoenix.
