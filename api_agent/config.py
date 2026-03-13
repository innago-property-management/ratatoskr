"""Configuration settings for API Agent MCP server."""

import re
from typing import Literal

from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings with API_AGENT_ env prefix. OPENAI_* also accepts unprefixed."""

    model_config = SettingsConfigDict(env_prefix="API_AGENT_", env_file=".env", extra="ignore")

    # MCP Server
    MCP_NAME: str = "API Agent"
    SERVICE_NAME: str = "api-agent"

    @computed_field
    @property
    def MCP_SLUG(self) -> str:
        """Slugified MCP_NAME for identifiers."""
        return re.sub(r"[^a-z0-9]+", "_", self.MCP_NAME.lower()).strip("_")

    # LLM Provider
    PROVIDER: str = "openai"  # "openai", "anthropic", or "openai-compat"
    API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("API_AGENT_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
    )
    BASE_URL: str = Field(
        default="",
        validation_alias=AliasChoices("API_AGENT_BASE_URL", "OPENAI_BASE_URL"),
    )
    MODEL_NAME: str = ""  # empty = provider default
    REASONING_EFFORT: str = ""  # "low", "medium", "high" - empty = disabled

    # Legacy aliases for backward compatibility
    OPENAI_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("API_AGENT_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    OPENAI_BASE_URL: str = Field(
        default="",
        validation_alias=AliasChoices("API_AGENT_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )

    # Agent limits
    MAX_AGENT_TURNS: int = 30
    MAX_RESPONSE_CHARS: int = 50000
    MAX_SCHEMA_CHARS: int = 32000
    MAX_PREVIEW_ROWS: int = 10  # Rows to show before suggesting pagination
    MAX_TOOL_RESPONSE_CHARS: int = 32000  # ~8K tokens, cap tool responses for LLM context

    # Polling limits
    MAX_POLLS: int = 20  # Max poll attempts
    DEFAULT_POLL_DELAY_MS: int = 3000  # Default delay if agent doesn't specify

    # Logging
    LOG_FORMAT: Literal["json", "console"] = "json"

    # Server
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 3000
    TRANSPORT: str = "streamable-http"
    CORS_ALLOWED_ORIGINS: str = (
        "*"  # Set specific origins in production; '*' with credentials is invalid per CORS spec
    )

    # Recipes (in-process reuse)
    ENABLE_RECIPES: bool = True
    RECIPE_CACHE_SIZE: int = 64
    RECIPE_MIN_SIMILARITY: int = Field(
        default=30,
        ge=0,
        le=100,
        description="Minimum similarity score (0-100) to return a recipe match",
    )

    # Security — SSRF protection
    ALLOWED_URL_SCHEMES: str = "http,https,grpc,grpcs,mcp"
    BLOCK_PRIVATE_IPS: bool = True
    BLOCKED_HOSTS: str = "169.254.169.254,metadata.google.internal"
    ALLOWED_TARGET_HOSTS: str = ""  # empty = allow all non-blocked; comma-separated allowlist

    # gRPC mutation safety
    GRPC_UNSAFE_METHOD_PATTERNS: str = (
        "Create*,Delete*,Remove*,Update*,Set*,Put*,"
        "Destroy*,Drop*,Insert*,Add*,Modify*,Patch*,Upsert*,Write*"
    )

    # Default target (optional — fallback when headers omitted, useful for single-API instances)
    DEFAULT_TARGET_URL: str = (
        ""  # e.g. "https://swapi-graphql.netlify.app/.netlify/functions/graphql"
    )
    DEFAULT_API_TYPE: str = ""  # "graphql", "rest", or "grpc"
    DEFAULT_BASE_URL: str = ""  # REST base URL override
    DEFAULT_TARGET_HEADERS: str = ""  # JSON object, e.g. '{"Accept": "application/json"}'

    # Endpoint allowlist (empty = all endpoints exposed, backwards compatible)
    ALLOW_ENDPOINTS_REST: str = ""  # CSV: "GET /users/*,GET /orders/*"
    ALLOW_ENDPOINTS_GRAPHQL: str = ""  # CSV: "Query.user*,Query.orders"
    ALLOW_ENDPOINTS_GRPC: str = ""  # CSV: "helloworld.Greeter/*"
    ALLOW_ENDPOINTS_MCP: str = ""  # CSV: "read_*,list_*"

    # MCP facade
    MCP_DISCOVERY_COMMAND: str = ""  # e.g. "mcp-proxy list"
    MCP_TARGET_TRANSPORT: str = ""  # "stdio" or "http"
    MCP_TARGET_COMMAND: str = ""  # command for stdio transport
    MCP_TARGET_ARGS: str = ""  # shell-quoted args for stdio transport (parsed with shlex)
    MCP_TARGET_ENV: str = "{}"  # JSON object of env vars for stdio transport

    # Agent concurrency
    MAX_CONCURRENT_AGENTS: int = 10  # Semaphore bound for concurrent agent orchestrations

    # DuckDB resource limits
    MAX_CONCURRENT_QUERIES: int = 10  # Semaphore bound for concurrent DuckDB ops
    TEMP_FILE_MAX_BYTES: int = 50 * 1024 * 1024  # 50 MB per temp file

    # Connection pool
    HTTP_POOL_MAX_CONNECTIONS: int = 20
    HTTP_POOL_MAX_KEEPALIVE: int = 10
    HTTP_POOL_TIMEOUT: float = 30.0

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(
        default="",
        validation_alias=AliasChoices(
            "API_AGENT_OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"
        ),
    )
    METRICS_EXPORT_INTERVAL_MS: int = 30_000

    # Schema reduction pipeline
    SCHEMA_REDUCTION_ENABLED: bool = True
    SCHEMA_REDUCTION_MODEL: str = "claude-haiku-4-5-20251001"
    SCHEMA_REDUCTION_TIMEOUT_MS: int = 30_000
    SCHEMA_REDUCTION_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("API_AGENT_SCHEMA_REDUCTION_API_KEY", "ANTHROPIC_API_KEY"),
    )
    SCHEMA_REDUCTION_MAX_INPUT_CHARS: int = 100_000
    SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS: int = 8192


settings = Settings()


def get_settings() -> Settings:
    """Return the current Settings instance.

    The module-level ``settings`` singleton is created at import time from
    environment variables and ``.env``.  This function returns that same
    instance and exists primarily as a seam for testing (monkeypatch the
    return value) and for future refactoring of import sites.
    """
    return settings
