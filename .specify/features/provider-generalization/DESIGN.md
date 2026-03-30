# DESIGN.md — provider-generalization

**Feature:** Provider Generalization (Schema Reduction LLM + PROFILE=local Preset)
**Version:** 1.0
**Date:** 2026-03-30
**Status:** Ready for implementation
**Spec:** [SPEC.md](SPEC.md) v1.1 (Post-Delphi Round 1)

---

## Module Change Map

| File | Change Type | Summary |
|------|------------|---------|
| `api_agent/config.py` | Modify | Add `PROFILE`, `SCHEMA_REDUCTION_PROVIDER`, `SCHEMA_REDUCTION_BASE_URL`; change `SCHEMA_REDUCTION_API_KEY` alias, `SCHEMA_REDUCTION_MODEL` default; add `model_validator` |
| `api_agent/llm/factory.py` | **New** | `create_schema_reduction_provider()` factory with timeout injection |
| `api_agent/schema/reducer.py` | Modify | Rename `HaikuLayer` → `AIReductionLayer`; replace `anthropic` import with `LLMProvider`; update `reduce_schema()` signature |
| `api_agent/agent/orchestrator.py` | Modify | Replace `api_key`/`model`/`timeout_ms` args with `provider` from factory |
| `api_agent/__main__.py` | Modify | Add `--profile` CLI flag; add startup log for profile overrides |
| `tests/test_config.py` | Modify | Tests for `PROFILE`, validation error, new field defaults |
| `tests/test_schema/test_reducer.py` | Modify | Tests for `AIReductionLayer` with `FakeLLMProvider` |
| `tests/test_schema/test_provider_factory.py` | **New** | Tests for factory resolution logic + timeout injection |
| `tests/test_main.py` | Modify | Tests for `--profile` flag + startup logging |

---

## 1. Config Changes (`api_agent/config.py`)

### 1.1 New Fields

```python
# Profile preset
PROFILE: Literal["local", ""] = ""

# Schema reduction provider overrides
SCHEMA_REDUCTION_PROVIDER: str = ""   # empty = inherit from PROVIDER
SCHEMA_REDUCTION_BASE_URL: str = ""   # empty = inherit from BASE_URL
```

### 1.2 Changed Fields

```python
# Was: validation_alias=AliasChoices("API_AGENT_SCHEMA_REDUCTION_API_KEY", "ANTHROPIC_API_KEY")
# Now: no ANTHROPIC_API_KEY alias — defaults to "" (resolved at factory level)
SCHEMA_REDUCTION_API_KEY: str = Field(
    default="",
    validation_alias=AliasChoices("API_AGENT_SCHEMA_REDUCTION_API_KEY"),
)

# Was: default="claude-haiku-4-5-20251001"
# Now: default="" (provider decides)
SCHEMA_REDUCTION_MODEL: str = ""
```

### 1.3 Model Validator — PROFILE + Custom Error (U-3)

```python
from pydantic import model_validator

class Settings(BaseSettings):
    # ... fields ...

    @model_validator(mode="before")
    @classmethod
    def apply_profile_defaults(cls, data: dict) -> dict:
        profile = data.get("PROFILE", data.get("API_AGENT_PROFILE", ""))
        if isinstance(profile, str):
            profile = profile.strip().lower()

        if not profile:
            return data

        # Custom validation error (U-3) — before Literal validation
        if profile != "local":
            raise ValueError(
                f"Invalid PROFILE '{profile}'. "
                "Supported profiles: 'local'. Leave empty for default behavior."
            )

        # Inject defaults only for keys not already present
        _LOCAL_DEFAULTS = {
            "BLOCK_PRIVATE_IPS": False,
            "LOG_FORMAT": "console",
            "SCHEMA_REDUCTION_ENABLED": False,
        }

        applied = []
        for key, value in _LOCAL_DEFAULTS.items():
            prefixed = f"API_AGENT_{key}"
            if key not in data and prefixed not in data:
                data[key] = value
                applied.append(f"{key}={value}")

        # Stash applied overrides for startup logging (U-1)
        data["_profile_applied"] = applied

        return data
```

**Note on `_profile_applied`:** This is a transient field not declared on `Settings`
(which uses `extra="ignore"`). The startup code in `__main__.py` reads it from the raw
dict before pydantic drops it. Alternative: store on a module-level variable in config.py
set during validation.

**Implementation detail:** Since `extra="ignore"` drops unknown fields, we'll use a
module-level list in `config.py` instead:

```python
# Module-level in config.py
_profile_overrides: list[str] = []

# Inside the validator, append to _profile_overrides instead of data["_profile_applied"]
```

### 1.4 Field Placement

New fields are grouped with existing schema reduction fields. `PROFILE` goes near the
top, after the server section (logically it's a meta-setting that affects other fields).

---

## 2. Provider Factory (`api_agent/llm/factory.py`)

### 2.1 Factory Function

```python
"""Factory for constructing schema-reduction LLM providers."""

from __future__ import annotations

import httpx
import structlog

from ..config import Settings
from .provider import LLMProvider

logger = structlog.get_logger(__name__)


def create_schema_reduction_provider(settings: Settings) -> LLMProvider | None:
    """Build an LLMProvider for schema reduction, or None to skip AI layer.

    Timeout is baked into the HTTP client at construction time (S-1).
    The LLMProvider.complete() interface is unchanged.
    """
    # Resolve effective values (fall back to main settings)
    provider_type = settings.SCHEMA_REDUCTION_PROVIDER or settings.PROVIDER
    api_key = settings.SCHEMA_REDUCTION_API_KEY or settings.API_KEY
    model = settings.SCHEMA_REDUCTION_MODEL  # "" = provider default
    base_url = settings.SCHEMA_REDUCTION_BASE_URL or settings.BASE_URL
    timeout = httpx.Timeout(settings.SCHEMA_REDUCTION_TIMEOUT_MS / 1000)

    # Cloud providers require an API key
    if not api_key and provider_type.lower() in ("openai", "anthropic"):
        logger.info(
            "schema_reduction_provider_skipped",
            reason="no API key for cloud provider",
            provider=provider_type,
        )
        return None

    provider_type_lower = provider_type.lower()

    if provider_type_lower == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(
            model=model or "claude-haiku-4-5-20251001",
            api_key=api_key,
            timeout=timeout,
        )
    elif provider_type_lower == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(
            model=model,
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
        )
    elif provider_type_lower == "openai-compat":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(
            model=model,
            api_key=api_key or "not-needed",
            base_url=base_url or None,
            timeout=timeout,
        )
    else:
        logger.warning(
            "schema_reduction_unknown_provider",
            provider=provider_type,
        )
        return None
```

### 2.2 Timeout Injection (S-1)

Each provider subclass already constructs an HTTP client internally. The factory passes
`timeout: httpx.Timeout` to the provider constructor. This requires a small addition to
each provider's `__init__`:

- **`AnthropicProvider.__init__`**: Already accepts/constructs `httpx.Timeout` for
  `AsyncAnthropic(timeout=...)`. Add optional `timeout` kwarg.
- **`OpenAIProvider.__init__`**: Add optional `timeout` kwarg, pass to
  `AsyncOpenAI(timeout=...)`.
- **`OpenAICompatProvider.__init__`**: Add optional `timeout` kwarg, pass to
  `AsyncOpenAI(timeout=...)`.

The `timeout` parameter is **optional** with a sensible default (e.g., `httpx.Timeout(60.0)`)
so existing non-factory construction paths are unaffected.

### 2.3 Provider Singleton

The orchestrator calls the factory once and caches the result. Two options:

**Option A (chosen) — Module-level lazy singleton in orchestrator:**
```python
# api_agent/agent/orchestrator.py
_schema_reduction_provider: LLMProvider | None = None
_schema_reduction_provider_initialized = False

def _get_schema_reduction_provider() -> LLMProvider | None:
    global _schema_reduction_provider, _schema_reduction_provider_initialized
    if not _schema_reduction_provider_initialized:
        _schema_reduction_provider = create_schema_reduction_provider(settings)
        _schema_reduction_provider_initialized = True
    return _schema_reduction_provider
```

This mirrors the existing `_provider` / `provider` pattern in `api_agent/agent/model.py`.

**Option B — Pass through from startup.** Rejected — would require threading the provider
through too many layers.

---

## 3. AIReductionLayer (`api_agent/schema/reducer.py`)

### 3.1 Class Rename and Constructor

```python
class AIReductionLayer:
    """Provider-agnostic AI reduction layer (replaces HaikuLayer).

    Only instantiated when a provider is available.
    Never raises — returns (original, False) on ANY failure.
    """

    def __init__(self, provider: LLMProvider, max_output_tokens: int = 8192):
        self.provider = provider
        self.max_output_tokens = max_output_tokens
```

### 3.2 `reduce()` Method

```python
async def reduce(self, schema_text: str, question: str) -> tuple[str, bool]:
    try:
        prompt = _HAIKU_PROMPT.replace("{schema_text}", schema_text).replace(
            "{question}", question
        )
        messages = [{"role": "user", "content": prompt}]

        # S-2: Explicit max_tokens mapping from max_output_tokens
        response = await self.provider.complete(
            messages,
            tools=None,
            max_tokens=self.max_output_tokens,
        )

        reduced = response.content  # LLMResponse.content is already str

        # Strip markdown fences (S-3: works for all providers)
        fence_match = _FENCE_RE.match(reduced)
        if fence_match:
            reduced = fence_match.group(1)

        # ... existing sanity checks, injection detection unchanged ...

        return reduced, True
    except Exception:
        logger.warning("ai_reduction_error", exc_info=True)
        return schema_text, False
```

Key changes from `HaikuLayer.reduce()`:
1. Uses `self.provider.complete()` instead of `self.client.messages.create()`
2. Reads `response.content` (str) instead of iterating `response.content` blocks
3. Passes `max_tokens=self.max_output_tokens` explicitly **(S-2)**
4. All safety checks (empty, too short, longer than input, injection) preserved

### 3.3 Removed Code

- `import anthropic` — removed
- `import httpx` — removed (was only for `httpx.Timeout` in `HaikuLayer`)
- `_get_haiku_layer()` LRU cache — removed (provider singleton lives in orchestrator)
- `_get_api_key()` helper — removed (resolved in factory)
- `HaikuLayer` class — replaced by `AIReductionLayer`

### 3.4 `reduce_schema()` Changes

```python
async def reduce_schema(
    schema_text: str,
    question: str,
    threshold: int,
    provider: LLMProvider | None = None,   # was: api_key, model, timeout_ms
    enabled: bool = True,
    max_input_chars: int = 100_000,
    max_output_tokens: int = 8192,
    ai_reduction_threshold: int = 0,
) -> ReductionResult:
```

Layer 2 changes:
```python
# Layer 2: AI reduction
ai_applied = False
effective_ai_threshold = ai_reduction_threshold or threshold
if (
    provider is not None
    and question
    and original_chars >= effective_ai_threshold
    and len(current_text) <= max_input_chars
):
    ai_layer = AIReductionLayer(provider, max_output_tokens)
    reduced_text, ai_applied = await ai_layer.reduce(current_text, question)
    if ai_applied:
        current_text = reduced_text
```

---

## 4. Orchestrator Update (`api_agent/agent/orchestrator.py`)

### 4.1 Call-Site Change

**Before:**
```python
reduction = await reduce_schema(
    schema_text=schema_for_reduction,
    question=question,
    threshold=settings.MAX_SCHEMA_CHARS,
    api_key=settings.SCHEMA_REDUCTION_API_KEY,
    model=settings.SCHEMA_REDUCTION_MODEL,
    timeout_ms=settings.SCHEMA_REDUCTION_TIMEOUT_MS,
    enabled=settings.SCHEMA_REDUCTION_ENABLED,
    max_input_chars=settings.SCHEMA_REDUCTION_MAX_INPUT_CHARS,
    max_output_tokens=settings.SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS,
    ai_reduction_threshold=settings.SCHEMA_AI_REDUCTION_THRESHOLD,
)
```

**After:**
```python
reduction = await reduce_schema(
    schema_text=schema_for_reduction,
    question=question,
    threshold=settings.MAX_SCHEMA_CHARS,
    provider=_get_schema_reduction_provider() if settings.SCHEMA_REDUCTION_ENABLED else None,
    enabled=settings.SCHEMA_REDUCTION_ENABLED,
    max_input_chars=settings.SCHEMA_REDUCTION_MAX_INPUT_CHARS,
    max_output_tokens=settings.SCHEMA_REDUCTION_MAX_OUTPUT_TOKENS,
    ai_reduction_threshold=settings.SCHEMA_AI_REDUCTION_THRESHOLD,
)
```

Removed args: `api_key`, `model`, `timeout_ms` (all absorbed into the provider).

---

## 5. CLI Flag (`api_agent/__main__.py`)

### 5.1 `--profile` Argument (U-2)

```python
parser.add_argument(
    "--profile",
    choices=("local",),
    default=None,
    help="Configuration profile. 'local' enables local-dev defaults "
         "(BLOCK_PRIVATE_IPS=false, LOG_FORMAT=console, SCHEMA_REDUCTION_ENABLED=false).",
)
```

### 5.2 `apply_cli_overrides()` Addition

```python
if args.profile:
    os.environ["API_AGENT_PROFILE"] = args.profile
```

### 5.3 Startup Logging (U-1)

After reloading settings in `main()`:

```python
from .config import _profile_overrides

if _profile_overrides:
    logger.info(
        "profile_applied",
        profile=reloaded.PROFILE,
        overrides=",".join(_profile_overrides),
    )
```

---

## 6. Dependency Graph

```
Step 1: Config fields + PROFILE validator
   ↓
Step 2: Provider factory (depends on config fields)
   ↓
Step 3: AIReductionLayer (depends on LLMProvider interface, no factory dependency)
   ↓
Step 4: reduce_schema() signature (depends on AIReductionLayer)
   ↓
Step 5: Orchestrator call-site (depends on factory + reduce_schema signature)
   ↓
Step 6: CLI flag + startup logging (depends on config fields)
```

Steps 2 and 3 are independent of each other and can be implemented in parallel.
Step 6 is independent of steps 2-5 and can also be parallelized.

---

## 7. Backward Compatibility

| Scenario | Before | After | Observable Change |
|----------|--------|-------|-------------------|
| `ANTHROPIC_API_KEY` set, `PROVIDER=openai` | Schema reduction uses Anthropic via env fallback | Schema reduction skipped (no key for main provider) | **Breaking** — must set `SCHEMA_REDUCTION_PROVIDER=anthropic` |
| `PROVIDER=anthropic`, no overrides | Uses Anthropic Haiku for reduction | Same — main provider is Anthropic, key inherited | None |
| `PROVIDER=openai-compat`, no overrides | Schema reduction skipped (no Anthropic key) | Schema reduction uses same openai-compat provider | **Improvement** — reduction now works |
| `PROFILE` unset | N/A | All defaults unchanged | None |
| `PROFILE=invalid` | N/A | Custom `ValueError` at startup | New behavior (U-3) |

---

## 8. Test Design Notes

### FakeLLMProvider for AIReductionLayer

Tests construct `AIReductionLayer(provider=FakeLLMProvider(...))` directly. The
`FakeLLMProvider` returns canned `LLMResponse` objects. No monkeypatching needed —
the layer accepts the provider as a constructor argument (ISP: consumer controls interface).

### Fence-stripping test (S-3)

```python
async def test_ai_reduction_strips_fences():
    """S-3: Fence stripping works regardless of provider."""
    fenced = "```json\n<types>\ntype Foo { id: ID! }\n</types>\n```"
    provider = FakeLLMProvider(responses=[make_text_response(fenced)])
    layer = AIReductionLayer(provider=provider, max_output_tokens=8192)
    result, applied = await layer.reduce("original " * 100, "find Foo")
    assert applied
    assert not result.startswith("```")
```

### Timeout injection test (S-1)

```python
def test_factory_injects_timeout():
    """S-1: Factory bakes SCHEMA_REDUCTION_TIMEOUT_MS into the provider."""
    settings = Settings(
        PROVIDER="anthropic",
        API_KEY="sk-test",
        SCHEMA_REDUCTION_TIMEOUT_MS=5000,
    )
    provider = create_schema_reduction_provider(settings)
    assert provider is not None
    # Verify timeout was set on the underlying client
    assert provider.client.timeout.connect == 5.0  # 5000ms → 5.0s
```
