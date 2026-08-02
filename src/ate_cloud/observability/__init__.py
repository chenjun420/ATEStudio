"""Observability package: OpenTelemetry tracing/metrics + structlog logging.

Public API:
    setup_telemetry — initialize OTel TracerProvider + MeterProvider with OTLP gRPC exporters
    setup_structlog — configure structlog JSON output with trace_id/span_id injection
    instrument_app  — auto-instrument a FastAPI application
    instrument_httpx — auto-instrument httpx clients
    shutdown_telemetry — flush + shutdown OTel providers
    get_logger — get a structlog logger
    inject_context / extract_context — NATS message trace context propagation
"""

from ate_cloud.observability.logging import get_logger, setup_structlog
from ate_cloud.observability.telemetry import (
    extract_context,
    inject_context,
    instrument_app,
    instrument_httpx,
    setup_telemetry,
    shutdown_telemetry,
)

__all__ = [
    "extract_context",
    "get_logger",
    "inject_context",
    "instrument_app",
    "instrument_httpx",
    "setup_structlog",
    "setup_telemetry",
    "shutdown_telemetry",
]
