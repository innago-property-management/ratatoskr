"""Shared LLM provider configuration for API agents.

Provider creation is lazy so that CLI overrides (which set env vars
before import but after the settings singleton) take effect.
"""

from __future__ import annotations

from ..llm.provider import LLMProvider
from .progress import get_turn_context, increment_turn

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Get or create the shared LLM provider (lazy singleton).

    Defers creation until first use so that CLI argument overrides
    (env vars set in __main__.apply_cli_overrides) are picked up.
    """
    global _provider
    if _provider is None:
        import os

        from ..config import Settings
        from ..llm import create_provider
        from ..tracing import init_tracing

        # Reload settings to pick up any env var overrides from CLI
        s = Settings()
        init_tracing()

        # Resolve API key with provider awareness:
        # 1. Explicit API_AGENT_API_KEY (highest precedence, set by --api-key flag)
        # 2. Provider-specific key (ANTHROPIC_API_KEY or OPENAI_API_KEY)
        # 3. Generic fallback
        explicit_key = os.environ.get("API_AGENT_API_KEY", "")
        if explicit_key:
            api_key = explicit_key
        elif s.PROVIDER == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "") or s.API_KEY
        else:
            api_key = s.API_KEY or s.OPENAI_API_KEY

        base_url = s.BASE_URL or s.OPENAI_BASE_URL or None

        _provider = create_provider(
            provider=s.PROVIDER,
            model=s.MODEL_NAME or None,
            api_key=api_key,
            base_url=base_url,
        )
    return _provider


# Backward-compatible module-level access — lazy proxy
class _ProviderProxy:
    """Proxy that defers provider creation to first attribute access."""

    def __getattr__(self, name: str):
        return getattr(get_provider(), name)


provider: LLMProvider = _ProviderProxy()  # type: ignore[assignment]


def get_inject_instructions():
    """Return a callback that injects turn count into instructions before each LLM call."""

    def inject(instructions: str, turn: int) -> str:
        from ..config import settings

        increment_turn()
        turn_info = get_turn_context(settings.MAX_AGENT_TURNS)
        return f"{instructions}\n\n{turn_info}"

    return inject
