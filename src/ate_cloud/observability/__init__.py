"""Observability package: OpenTelemetry tracing/metrics + structlog logging.

Public API:
    setup_telemetry — initialize OTel TracerProvider + MeterProvider with OTLP gRPC exporters
    setup_structlog — configure structlog JSON output with trace_id/span_id injection
    instrument_app  — auto-instrument a FastAPI application
    instrument_httpx — auto-instrument httpx clients
    shutdown_telemetry — flush + shutdown OTel providers
    get_logger — get a structlog logger
    inject_context / extract_context — NATS message trace context propagation

``get_logger`` and ``setup_structlog`` need nothing beyond structlog, which is a
declared dependency, so they are imported eagerly. Everything else lives in
``observability.telemetry``, which requires the ``opentelemetry`` SDK — not a
declared dependency, and installed in no environment, including the deployed
board (see TRANSITIVE_BY_DESIGN in tests/cloud/test_declared_dependencies.py).

Those names are resolved by the module ``__getattr__`` below rather than by an
import at module scope, and that distinction is the entire reason this file
exists in this shape. A bare ``from ...telemetry import ...`` here made
``import ate_cloud.observability`` raise ModuleNotFoundError on every host that
has not installed the SDK — which was every host. It stayed invisible only
because nothing in src/ imports this package, so the one line that could fail was
never executed by the service. Importing the package has to work; asking for a
telemetry function without the SDK has to fail loudly and name opentelemetry.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from ate_cloud.observability.logging import get_logger, setup_structlog

if TYPE_CHECKING:
    # Imported for type checkers only. At runtime these names come from
    # __getattr__ below, which is what keeps the SDK off the import path.
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

#: Names served by the lazy loader rather than by this module's globals. Kept
#: explicit so that a typo raises AttributeError here instead of being forwarded
#: to telemetry and reported as a missing attribute of an unrelated module.
_TELEMETRY_EXPORTS = frozenset(
    {
        "extract_context",
        "inject_context",
        "instrument_app",
        "instrument_httpx",
        "setup_telemetry",
        "shutdown_telemetry",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve a telemetry symbol on first use (PEP 562).

    Propagates the ImportError from importing the SDK, whose message names
    opentelemetry. That is the useful answer to the question the caller actually
    asked, so it is not rewritten into something vaguer.
    """
    if name in _TELEMETRY_EXPORTS:
        module = import_module(f"{__name__}.telemetry")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
