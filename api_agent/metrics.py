"""OpenTelemetry metrics instrumentation.

Follows the same lazy-init / graceful-degradation pattern as ``tracing.py``:
instruments are no-ops until ``init_metrics()`` is called, and that function
is a no-op when no OTLP endpoint is configured.
"""

from __future__ import annotations

import structlog

from .config import get_settings

logger = structlog.get_logger(__name__)

_meter_provider = None
_meter = None
_prometheus_available = False

# --- Lazy instrument singletons ---
_query_counter = None
_token_counter = None
_latency_histogram = None
_turns_histogram = None
_duckdb_histogram = None
_schema_fetch_histogram = None
_toon_compressions_counter = None
_toon_fallbacks_counter = None
_toon_chars_saved_counter = None
_toon_ratio_histogram = None
_injection_detected_counter = None


def init_metrics() -> bool:
    """Initialize OTel MeterProvider.  No-op if already initialized.

    Returns True if a Prometheus metric reader is available (for ``/metrics``).
    """
    global _meter_provider, _meter, _prometheus_available
    if _meter_provider is not None:
        return _prometheus_available

    settings = get_settings()
    otlp_endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        readers: list = []

        # OTLP push reader
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

                exporter = OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics")
                readers.append(
                    PeriodicExportingMetricReader(
                        exporter,
                        export_interval_millis=settings.METRICS_EXPORT_INTERVAL_MS,
                    )
                )
            except ImportError:
                logger.warning(
                    "otlp_metric_exporter_unavailable",
                    hint="OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp "
                    "is not installed. Metrics will not be exported.",
                )

        # OTLP endpoint set but exporter unavailable — drop to no-op
        if otlp_endpoint and not readers:
            _meter_provider = True
            _meter = otel_metrics.get_meter("api_agent")
            return False

        # Optional Prometheus reader
        try:
            from opentelemetry.exporter.prometheus import (  # type: ignore[unresolved-import]
                PrometheusMetricReader,
            )

            readers.append(PrometheusMetricReader())
            _prometheus_available = True
        except ImportError:
            pass  # prometheus extras not installed

        if not readers and not otlp_endpoint:
            # No exporters and no endpoint — use global no-op meter.
            # Set _meter_provider to sentinel so repeated calls are idempotent.
            _meter_provider = True
            _meter = otel_metrics.get_meter("api_agent")
            return False

        resource = Resource.create({"service.name": settings.SERVICE_NAME})
        provider = MeterProvider(resource=resource, metric_readers=readers)
        otel_metrics.set_meter_provider(provider)
        _meter_provider = provider
        _meter = provider.get_meter("api_agent", version="0.1.0")
        logger.info("metrics_enabled", otlp=bool(otlp_endpoint), prometheus=_prometheus_available)
        return _prometheus_available

    except Exception as e:
        logger.warning("metrics_setup_failed", error=str(e), exc_info=True)
        try:
            from opentelemetry import metrics as otel_metrics

            _meter = otel_metrics.get_meter("api_agent")
        except ImportError:
            pass
        return False


def shutdown_metrics() -> None:
    """Flush and shut down the MeterProvider.  Safe to call if not initialized."""
    if _meter_provider is not None and _meter_provider is not True:
        try:
            _meter_provider.shutdown()
        except Exception as e:
            logger.warning("metrics_shutdown_failed", error=str(e))


def _get_meter():
    """Return the global meter, initializing a no-op if needed."""
    global _meter
    if _meter is None:
        try:
            from opentelemetry import metrics as otel_metrics

            _meter = otel_metrics.get_meter("api_agent")
        except ImportError:
            return None
    return _meter


# --- Instrument accessors (lazy singletons) ---


def _query_ctr():
    global _query_counter
    if _query_counter is None:
        m = _get_meter()
        if m:
            _query_counter = m.create_counter(
                "api_agent.requests.total",
                description="Total MCP query requests",
                unit="requests",
            )
    return _query_counter


def _token_ctr():
    global _token_counter
    if _token_counter is None:
        m = _get_meter()
        if m:
            _token_counter = m.create_counter(
                "api_agent.llm.tokens.total",
                description="Total LLM tokens consumed",
                unit="tokens",
            )
    return _token_counter


def _latency_hist():
    global _latency_histogram
    if _latency_histogram is None:
        m = _get_meter()
        if m:
            _latency_histogram = m.create_histogram(
                "api_agent.request.duration_ms",
                description="Request duration in milliseconds",
                unit="ms",
            )
    return _latency_histogram


def _turns_hist():
    global _turns_histogram
    if _turns_histogram is None:
        m = _get_meter()
        if m:
            _turns_histogram = m.create_histogram(
                "api_agent.llm.turns",
                description="Number of LLM turns per agent run",
                unit="turns",
            )
    return _turns_histogram


def _duckdb_hist():
    global _duckdb_histogram
    if _duckdb_histogram is None:
        m = _get_meter()
        if m:
            _duckdb_histogram = m.create_histogram(
                "api_agent.duckdb.duration_ms",
                description="DuckDB operation duration in milliseconds",
                unit="ms",
            )
    return _duckdb_histogram


def _schema_fetch_hist():
    global _schema_fetch_histogram
    if _schema_fetch_histogram is None:
        m = _get_meter()
        if m:
            _schema_fetch_histogram = m.create_histogram(
                "api_agent.schema.fetch_duration_ms",
                description="Schema fetch duration in milliseconds",
                unit="ms",
            )
    return _schema_fetch_histogram


# --- Public recording API ---


def record_request(protocol: str, status: str, duration_ms: float) -> None:
    """Record a completed query request."""
    ctr = _query_ctr()
    if ctr:
        ctr.add(1, {"protocol": protocol, "status": status})
    hist = _latency_hist()
    if hist:
        hist.record(duration_ms, {"protocol": protocol})


def record_token_usage(prompt_tokens: int, completion_tokens: int, provider_name: str) -> None:
    """Record LLM token usage from a completed agent run."""
    ctr = _token_ctr()
    if not ctr:
        return
    # Skip zero: some providers (openai-compat) omit usage fields, yielding 0
    if prompt_tokens:
        ctr.add(prompt_tokens, {"provider": provider_name, "token_type": "prompt"})
    if completion_tokens:
        ctr.add(completion_tokens, {"provider": provider_name, "token_type": "completion"})


def record_agent_turns(turns: int, agent_type: str) -> None:
    """Record how many LLM turns an agent run used."""
    hist = _turns_hist()
    if hist:
        hist.record(turns, {"agent_type": agent_type})


def record_duckdb_duration(duration_ms: float, operation: str) -> None:
    """Record DuckDB operation duration."""
    hist = _duckdb_hist()
    if hist:
        hist.record(duration_ms, {"operation": operation})


def record_schema_fetch(duration_ms: float, protocol: str) -> None:
    """Record schema fetch duration."""
    hist = _schema_fetch_hist()
    if hist:
        hist.record(duration_ms, {"protocol": protocol})


# --- TOON instrument accessors ---


def _toon_compressions_ctr():
    global _toon_compressions_counter
    if _toon_compressions_counter is None:
        m = _get_meter()
        if m:
            _toon_compressions_counter = m.create_counter(
                "api_agent.toon.compressions.total",
                description="Successful TOON encodings",
                unit="encodings",
            )
    return _toon_compressions_counter


def _toon_fallbacks_ctr():
    global _toon_fallbacks_counter
    if _toon_fallbacks_counter is None:
        m = _get_meter()
        if m:
            _toon_fallbacks_counter = m.create_counter(
                "api_agent.toon.fallbacks.total",
                description="Times JSON fallback was used (no gain, error, import missing)",
                unit="fallbacks",
            )
    return _toon_fallbacks_counter


def _toon_chars_saved_ctr():
    global _toon_chars_saved_counter
    if _toon_chars_saved_counter is None:
        m = _get_meter()
        if m:
            _toon_chars_saved_counter = m.create_counter(
                "api_agent.toon.chars_saved.total",
                description="Cumulative characters saved by TOON compression",
                unit="chars",
            )
    return _toon_chars_saved_counter


def _toon_ratio_hist():
    global _toon_ratio_histogram
    if _toon_ratio_histogram is None:
        m = _get_meter()
        if m:
            _toon_ratio_histogram = m.create_histogram(
                "api_agent.toon.compression_ratio",
                description="TOON compression ratio per encoding (toon_chars / original_chars)",
                unit="ratio",
            )
    return _toon_ratio_histogram


def _injection_detected_ctr():
    global _injection_detected_counter
    if _injection_detected_counter is None:
        m = _get_meter()
        if m:
            _injection_detected_counter = m.create_counter(
                "api_agent.schema_reduction.injection_detected.total",
                description="Suspected prompt injection detected in Haiku schema reduction output",
                unit="detections",
            )
    return _injection_detected_counter


def record_injection_detected() -> None:
    """Record a suspected prompt injection in Haiku schema reduction output."""
    ctr = _injection_detected_ctr()
    if ctr:
        ctr.add(1)


def record_toon_encoding(original_chars: int, toon_chars: int, applied: bool) -> None:
    """Record a TOON encoding attempt.

    Args:
        original_chars: Length of original JSON string.
        toon_chars: Length of TOON output (including header), or original_chars on fallback.
        applied: True if TOON was actually used, False for any fallback.
    """
    if applied:
        ctr = _toon_compressions_ctr()
        if ctr:
            ctr.add(1)
        saved = original_chars - toon_chars
        if saved > 0:
            chars_ctr = _toon_chars_saved_ctr()
            if chars_ctr:
                chars_ctr.add(saved)
    else:
        ctr = _toon_fallbacks_ctr()
        if ctr:
            ctr.add(1)

    if original_chars > 0:
        hist = _toon_ratio_hist()
        if hist:
            hist.record(toon_chars / original_chars)
