"""structlog configuration with OpenTelemetry trace_id/span_id injection.

Configures structlog for JSON output and routes standard library ``logging``
through structlog's ProcessorFormatter, so that both ``structlog.get_logger()``
and existing ``logging.getLogger(__name__)`` calls produce the same JSON format
with trace_id and span_id fields from the active OTel span.
"""

import logging
from typing import Any

import structlog
from opentelemetry import trace


def _add_otel_trace_ids(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor: inject trace_id and span_id from the current OTel span.

    If no valid span context is active (outside a trace), the keys are omitted
    rather than set to None, keeping log entries clean outside of a trace.
    """
    span = trace.get_current_span()
    span_context = span.get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = f"{span_context.trace_id:032x}"
        event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def setup_structlog(log_level: int = logging.INFO) -> None:
    """Configure structlog for JSON output with OTel trace injection.

    Also routes standard library ``logging`` through structlog's
    ProcessorFormatter so that existing ``logging.getLogger(__name__)`` calls
    produce the same JSON structure with trace_id/span_id.

    Args:
        log_level: The minimum log level (default: ``logging.INFO``).
    """
    # Shared processor chain — runs for both structlog and stdlib log records
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_otel_trace_ids,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog's ProcessorFormatter
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Remove existing handlers to avoid duplicate output
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)


def get_logger(name: str) -> Any:
    """Get a structlog logger bound to the given name.

    Usage:
        logger = get_logger(__name__)
        logger.info("message", key="value")
    """
    return structlog.get_logger(name)
