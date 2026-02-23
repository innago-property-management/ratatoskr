"""Shared LLM provider configuration for API agents."""

from __future__ import annotations

from ..config import settings
from ..llm import create_provider
from ..llm.provider import LLMProvider
from ..tracing import init_tracing
from ..tracing import is_enabled as tracing_enabled
from .progress import get_turn_context, increment_turn

# Initialize tracing
init_tracing()

# Resolve API key: prefer new API_KEY, fall back to legacy OPENAI_API_KEY
_api_key = settings.API_KEY or settings.OPENAI_API_KEY
_base_url = settings.BASE_URL or settings.OPENAI_BASE_URL or None

# Create the shared provider instance
provider: LLMProvider = create_provider(
    provider=settings.PROVIDER,
    model=settings.MODEL_NAME or None,
    api_key=_api_key,
    base_url=_base_url,
)


def get_inject_instructions():
    """Return a callback that injects turn count into instructions before each LLM call."""

    def inject(instructions: str, turn: int) -> str:
        increment_turn()
        turn_info = get_turn_context(settings.MAX_AGENT_TURNS)
        return f"{instructions}\n\n{turn_info}"

    return inject
