"""OpenTelemetry tracer/meter provider configuration.

Configures BatchSpanProcessor (never SimpleSpanProcessor — it blocks the event loop)
with OTLP gRPC exporters for both traces and metrics. Health-check, metrics, and
docs endpoints are excluded from FastAPI auto-instrumentation.
"""

import logging

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_logger = logging.getLogger(__name__)

# URLs excluded from FastAPI auto-instrumentation (health, metrics, docs)
_EXCLUDED_URLS = "healthz,metrics,docs,/api/v1/health"

# BatchSpanProcessor tuning for async workloads — never use SimpleSpanProcessor
# (it exports synchronously on every span, blocking the event loop).
_BATCH_MAX_QUEUE_SIZE = 2048
_BATCH_SCHEDULE_DELAY_MS = 2000

# Periodic metric export interval
_METRIC_EXPORT_INTERVAL_MS = 10000

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def setup_telemetry(
    otlp_endpoint: str = "localhost:4317",
    service_name: str = "ate-cloud",
    service_version: str = "0.1.0",
) -> TracerProvider:
    """Initialize OTel TracerProvider + MeterProvider with OTLP gRPC exporters.

    Uses BatchSpanProcessor (max_queue_size=2048, schedule_delay_millis=2000)
    to avoid blocking the event loop on every span export.

    Args:
        otlp_endpoint: OTLP gRPC collector endpoint (host:port).
        service_name: Service name for the OTel resource.
        service_version: Service version for the OTel resource.

    Returns:
        The configured TracerProvider (also set as the global provider).
    """
    global _tracer_provider, _meter_provider

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
        }
    )

    # TracerProvider with BatchSpanProcessor + OTLP gRPC span exporter
    _tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    span_processor = BatchSpanProcessor(
        span_exporter,
        max_queue_size=_BATCH_MAX_QUEUE_SIZE,
        schedule_delay_millis=_BATCH_SCHEDULE_DELAY_MS,
    )
    _tracer_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(_tracer_provider)

    # MeterProvider with PeriodicExportingMetricReader + OTLP gRPC metric exporter
    metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=_METRIC_EXPORT_INTERVAL_MS,
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(_meter_provider)

    return _tracer_provider


def instrument_app(app: FastAPI) -> None:
    """Auto-instrument a FastAPI application (traces + metrics).

    Excludes health-check, metrics, and docs endpoints from tracing.
    Must be called AFTER setup_telemetry() and AFTER all routes are registered.
    """
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=_EXCLUDED_URLS,
    )


def instrument_httpx() -> None:
    """Auto-instrument httpx clients for distributed tracing.

    Instruments all httpx.AsyncClient and httpx.Client instances created after
    this call. Existing clients are not affected.
    """
    HTTPXClientInstrumentor().instrument()


def shutdown_telemetry() -> None:
    """Flush pending spans/metrics and shut down OTel providers.

    Called during application shutdown. Performs force_flush + shutdown on
    both the tracer and meter providers. Errors during shutdown are logged
    but not re-raised (best-effort cleanup during shutdown).
    """
    global _tracer_provider, _meter_provider

    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush()
            _tracer_provider.shutdown()
        except Exception as e:
            _logger.warning("Error during tracer shutdown: %s", e)
        finally:
            _tracer_provider = None

    if _meter_provider is not None:
        try:
            _meter_provider.force_flush()
            _meter_provider.shutdown()
        except Exception as e:
            _logger.warning("Error during meter shutdown: %s", e)
        finally:
            _meter_provider = None


# --- NATS context propagation helpers ---
#
# Usage in NATS publishers:
#   headers: dict[str, str] = {}
#   inject_context(headers)
#   await nc.publish("ate.tasks.run", payload, headers=headers)
#
# Usage in NATS subscribers:
#   ctx = extract_context(dict(msg.headers))
#   with tracer.start_as_current_span("process_task", context=ctx):
#       ...


def inject_context(headers: dict[str, str]) -> None:
    """Inject current trace context into a dict for NATS message headers.

    Uses W3C Trace Context propagation format (traceparent + tracestate headers).
    The carrier dict is mutated in place.
    """
    inject(headers)


def extract_context(headers: dict[str, str]) -> Context:
    """Extract trace context from NATS message headers.

    Returns an opentelemetry.context.Context that can be passed to
    tracer.start_as_current_span(..., context=ctx) to continue a trace
    across NATS message boundaries.
    """
    return extract(headers)
