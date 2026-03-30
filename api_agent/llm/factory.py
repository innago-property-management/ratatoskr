"""Provider factory — creates the right LLM provider from config."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from .provider import LLMProvider

if TYPE_CHECKING:
    from ..config import Settings

logger = structlog.get_logger(__name__)

# Default models per provider
_DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "openai-compat": "gpt-4o",
}


def create_provider(
    provider: str = "openai",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """Create an LLM provider instance.

    Args:
        provider: Provider name — "openai", "anthropic", or "openai-compat"
        model: Model name (defaults to provider-specific default)
        api_key: API key (falls back to env vars)
        base_url: Custom endpoint URL
    """
    resolved_model = model or _DEFAULT_MODELS.get(provider, "gpt-4o")

    if provider == "openai":
        from .openai_provider import OpenAIProvider

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        resolved_url = base_url or os.environ.get("OPENAI_BASE_URL")
        return OpenAIProvider(model=resolved_model, api_key=resolved_key, base_url=resolved_url)

    elif provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        return AnthropicProvider(model=resolved_model, api_key=resolved_key, base_url=base_url)

    elif provider == "openai-compat":
        from .openai_compat import OpenAICompatProvider

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "not-needed")
        resolved_url = base_url or os.environ.get("OPENAI_BASE_URL")
        if not resolved_url:
            raise ValueError(
                "openai-compat provider requires base_url "
                "(set API_AGENT_BASE_URL or OPENAI_BASE_URL)"
            )
        return OpenAICompatProvider(
            model=resolved_model, api_key=resolved_key, base_url=resolved_url
        )

    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. Supported: openai, anthropic, openai-compat"
        )


def create_schema_reduction_provider(settings: Settings) -> LLMProvider | None:
    """Build an LLMProvider for schema reduction, or None to skip AI layer.

    Resolution: each setting falls back to the main provider setting.
    Timeout is baked into the HTTP client at construction time (S-1).
    """
    provider_type = (settings.SCHEMA_REDUCTION_PROVIDER or settings.PROVIDER).lower()
    api_key = settings.SCHEMA_REDUCTION_API_KEY or settings.API_KEY
    model = settings.SCHEMA_REDUCTION_MODEL  # "" = provider default
    base_url = settings.SCHEMA_REDUCTION_BASE_URL or settings.BASE_URL
    timeout_s = settings.SCHEMA_REDUCTION_TIMEOUT_MS / 1000

    # Cloud providers require an API key
    if not api_key and provider_type in ("openai", "anthropic"):
        logger.info(
            "schema_reduction_provider_skipped",
            reason="no API key for cloud provider",
            provider=provider_type,
        )
        return None

    if provider_type == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=model or "claude-haiku-4-5-20251001",
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_s,
        )
    elif provider_type == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            model=model or "gpt-4o-mini",
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_s,
        )
    elif provider_type == "openai-compat":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            model=model or _DEFAULT_MODELS.get("openai-compat", "gpt-4o"),
            api_key=api_key or "not-needed",
            base_url=base_url or None,
            timeout=timeout_s,
        )
    else:
        logger.warning("schema_reduction_unknown_provider", provider=provider_type)
        return None
