"""Configuration settings for API Agent MCP server."""

import re

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

    # Server
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 3000
    TRANSPORT: str = "streamable-http"
    CORS_ALLOWED_ORIGINS: str = "*"

    # Recipes (in-process reuse)
    ENABLE_RECIPES: bool = True
    RECIPE_CACHE_SIZE: int = 64

    # Security — SSRF protection
    ALLOWED_URL_SCHEMES: str = "http,https,grpc,grpcs"
    BLOCK_PRIVATE_IPS: bool = True
    BLOCKED_HOSTS: str = "169.254.169.254,metadata.google.internal"
    ALLOWED_TARGET_HOSTS: str = ""  # empty = allow all non-blocked; comma-separated allowlist

    # gRPC mutation safety
    GRPC_UNSAFE_METHOD_PATTERNS: str = (
        "Create*,Delete*,Remove*,Update*,Set*,Put*,"
        "Destroy*,Drop*,Insert*,Add*,Modify*,Patch*,Upsert*,Write*"
    )


settings = Settings()
