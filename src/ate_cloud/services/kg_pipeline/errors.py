"""Controlled exceptions raised across the KG pipeline boundary.

Callers (routers / lazy factories) map :class:`KGPipelineUnavailable` to HTTP
503 — mirroring the existing degrade in the graph/embedding factories — while
the application itself boots normally and non-graph endpoints keep working.
"""

from __future__ import annotations


class KGPipelineError(Exception):
    """Base class for pipeline failures crossing the service boundary."""


class KGPipelineUnavailable(KGPipelineError):  # noqa: N818 — boundary vocabulary (503)
    """The Semantica pipeline cannot be constructed or reached.

    Raised when Semantica fails to import, when a required stage cannot be
    built, or when the backing graph service is unusable at construction
    time. Callers translate this to a 503 response; it never crashes app
    startup.
    """


__all__ = ["KGPipelineError", "KGPipelineUnavailable"]
