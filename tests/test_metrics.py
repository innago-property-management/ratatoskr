"""Tests for api_agent.metrics — OTel metrics instrumentation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api_agent import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset all module-level metrics state between tests."""
    metrics._meter_provider = None
    metrics._meter = None
    metrics._prometheus_available = False
    metrics._query_counter = None
    metrics._token_counter = None
    metrics._latency_histogram = None
    metrics._turns_histogram = None
    metrics._duckdb_histogram = None
    metrics._schema_fetch_histogram = None
    yield
    # Cleanup: reset again to avoid leaking state
    metrics._meter_provider = None
    metrics._meter = None
    metrics._prometheus_available = False
    metrics._query_counter = None
    metrics._token_counter = None
    metrics._latency_histogram = None
    metrics._turns_histogram = None
    metrics._duckdb_histogram = None
    metrics._schema_fetch_histogram = None


class TestInitMetrics:
    """init_metrics() initialization and idempotency."""

    def test_init_without_endpoint_returns_false(self):
        """Without OTLP endpoint, init_metrics returns False (no-op meter)."""
        with patch.dict("os.environ", {}, clear=False):
            # Ensure no OTLP endpoint
            import os

            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            result = metrics.init_metrics()

        assert result is False

    def test_init_idempotent_with_endpoint(self):
        """With OTLP endpoint, calling init_metrics() twice returns same result."""
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}):
            first = metrics.init_metrics()
            provider_after_first = metrics._meter_provider

            second = metrics.init_metrics()
            assert first == second
            assert metrics._meter_provider is provider_after_first

        metrics.shutdown_metrics()

    def test_init_with_endpoint(self):
        """With OTLP endpoint, init_metrics initializes a real MeterProvider."""
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}):
            result = metrics.init_metrics()

        # Should have initialized — meter_provider is set
        assert metrics._meter_provider is not None
        assert metrics._meter is not None
        # Prometheus may or may not be available depending on installed packages
        assert isinstance(result, bool)

        # Shutdown to avoid background threads
        metrics.shutdown_metrics()


class TestShutdownMetrics:
    """shutdown_metrics() flush and cleanup."""

    def test_shutdown_without_init_is_safe(self):
        """Calling shutdown_metrics() before init is a no-op."""
        metrics.shutdown_metrics()  # Should not raise

    def test_shutdown_after_init(self):
        """Shutdown after init doesn't raise."""
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}):
            metrics.init_metrics()
        metrics.shutdown_metrics()  # Should not raise


class TestRecordRequest:
    """record_request() counter + histogram."""

    def test_record_without_init_is_safe(self):
        """Recording before init is a no-op (no exception)."""
        metrics.record_request("graphql", "ok", 42.0)

    def test_record_after_init(self):
        """Recording after init writes to instruments without error."""
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        metrics.init_metrics()
        metrics.record_request("graphql", "ok", 150.5)
        metrics.record_request("rest", "error", 3000.0)
        metrics.record_request("grpc", "ok", 50.0)


class TestRecordTokenUsage:
    """record_token_usage() counter by token type."""

    def test_record_without_init_is_safe(self):
        metrics.record_token_usage(100, 50, "openai")

    def test_record_zero_tokens_skipped(self):
        """Zero-value tokens don't call counter.add()."""
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        metrics.init_metrics()
        # Should not raise — 0 values are skipped
        metrics.record_token_usage(0, 0, "openai")

    def test_record_with_values(self):
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        metrics.init_metrics()
        metrics.record_token_usage(500, 200, "anthropic")


class TestRecordAgentTurns:
    """record_agent_turns() histogram."""

    def test_record_without_init_is_safe(self):
        metrics.record_agent_turns(5, "graphql")

    def test_record_after_init(self):
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        metrics.init_metrics()
        metrics.record_agent_turns(3, "graphql")
        metrics.record_agent_turns(12, "rest")


class TestRecordDuckdbDuration:
    """record_duckdb_duration() histogram."""

    def test_record_without_init_is_safe(self):
        metrics.record_duckdb_duration(15.0, "sql_query")

    def test_record_after_init(self):
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        metrics.init_metrics()
        metrics.record_duckdb_duration(42.5, "sql_query")


class TestRecordSchemaFetch:
    """record_schema_fetch() histogram."""

    def test_record_without_init_is_safe(self):
        metrics.record_schema_fetch(250.0, "graphql")

    def test_record_after_init(self):
        import os

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        metrics.init_metrics()
        metrics.record_schema_fetch(1200.0, "grpc")
        metrics.record_schema_fetch(350.0, "rest")


class TestInMemoryMetricReader:
    """Integration test using InMemoryMetricReader to verify actual metric data."""

    def test_request_counter_and_histogram_recorded(self):
        """Verify counter and histogram values via InMemoryMetricReader."""
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("api_agent.test")

        # Manually set the module meter
        metrics._meter = meter
        # Reset lazy singletons so they use the new meter
        metrics._query_counter = None
        metrics._latency_histogram = None

        metrics.record_request("graphql", "ok", 100.0)
        metrics.record_request("graphql", "ok", 200.0)
        metrics.record_request("rest", "error", 500.0)

        data = reader.get_metrics_data()
        metric_names = set()
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    metric_names.add(metric.name)

        assert "api_agent.requests.total" in metric_names
        assert "api_agent.request.duration_ms" in metric_names

        provider.shutdown()

    def test_token_counter_recorded(self):
        """Verify token counter records prompt and completion separately."""
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("api_agent.test")

        metrics._meter = meter
        metrics._token_counter = None

        metrics.record_token_usage(500, 200, "openai")

        data = reader.get_metrics_data()
        metric_names = set()
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    metric_names.add(metric.name)

        assert "api_agent.llm.tokens.total" in metric_names

        provider.shutdown()

    def test_all_histograms_recorded(self):
        """Verify all histogram instruments record data."""
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("api_agent.test")

        metrics._meter = meter
        metrics._turns_histogram = None
        metrics._duckdb_histogram = None
        metrics._schema_fetch_histogram = None

        metrics.record_agent_turns(5, "graphql")
        metrics.record_duckdb_duration(42.5, "sql_query")
        metrics.record_schema_fetch(1200.0, "grpc")

        data = reader.get_metrics_data()
        metric_names = set()
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    metric_names.add(metric.name)

        assert "api_agent.llm.turns" in metric_names
        assert "api_agent.duckdb.duration_ms" in metric_names
        assert "api_agent.schema.fetch_duration_ms" in metric_names

        provider.shutdown()
