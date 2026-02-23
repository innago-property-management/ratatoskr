"""Provider factory — creates the right LLM provider from config."""

from __future__ import annotations

import os

from .provider import LLMProvider

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
            f"Unknown provider: {provider!r}. "
            f"Supported: openai, anthropic, openai-compat"
        )
