"""OpenTelemetry tracing setup."""

from contextlib import contextmanager
from typing import Any, Generator

import structlog

from .config import get_settings

logger = structlog.get_logger(__name__)

_tracer_ready = False
_using_metadata_fn = None


def init_tracing() -> None:
    """Initialize tracing if OTLP endpoint available."""
    global _tracer_ready, _using_metadata_fn

    otlp_endpoint = get_settings().OTEL_EXPORTER_OTLP_ENDPOINT
    if not otlp_endpoint:
        return

    try:
        from openinference.instrumentation import using_metadata
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        otlp_endpoint = otlp_endpoint.rstrip("/")
        resource = Resource.create({"service.name": get_settings().SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(provider)

        # Instrument OpenAI/Anthropic clients if available
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor  # noqa: I001  # type: ignore[unresolved-import]

            OpenAIInstrumentor().instrument(tracer_provider=provider)
        except ImportError:
            logger.debug("openai_instrumentation_unavailable")

        try:
            from openinference.instrumentation.anthropic import AnthropicInstrumentor  # noqa: I001  # type: ignore[unresolved-import]

            AnthropicInstrumentor().instrument(tracer_provider=provider)
        except ImportError:
            logger.debug("anthropic_instrumentation_unavailable")

        _using_metadata_fn = using_metadata
        _tracer_ready = True
        logger.info("tracing_enabled", endpoint=otlp_endpoint)
    except Exception as e:
        logger.warning("tracing_setup_failed", error=str(e), exc_info=True)


@contextmanager
def trace_metadata(metadata: dict[str, Any]) -> Generator[None, None, None]:
    """Context manager for span metadata. No-op if tracing disabled."""
    if _tracer_ready and _using_metadata_fn:
        with _using_metadata_fn(metadata):
            yield
    else:
        yield


def is_enabled() -> bool:
    """Check if tracing is enabled."""
    return _tracer_ready


@contextmanager
def trace_span(name: str, attributes: dict[str, Any] | None = None) -> Generator[Any, None, None]:
    """Create a span with optional attributes. No-op if tracing disabled.

    Args:
        name: Span name
        attributes: Optional attributes to add to span

    Yields:
        Span object (or None if tracing disabled)
    """
    if not _tracer_ready:
        yield None
        return

    try:
        from opentelemetry import trace

        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(name) as span:
            if attributes and span:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            yield span
    except Exception as e:
        logger.warning("span_creation_failed", error=str(e), exc_info=True)
        yield None
