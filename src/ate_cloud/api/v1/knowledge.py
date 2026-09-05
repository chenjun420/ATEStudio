"""Knowledge API: task-12 extraction trigger plus shared graph factories.

``POST /api/v1/knowledge/extract`` triggers deterministic extraction of
test requirements / test cases / recorded results from STRUCTURED,
server-side sources — DSL YAML plans and recordings JSONL (ATML import has
its own raw-XML endpoint at ``/api/v1/atml/import-test-description``).

The READ endpoints for the knowledge-graph browse (frontend task 25) and the
requirement↔case traceability matrix (frontend task 26) —
``GET /knowledge/requirements|cases|traceability|graph`` — are registered on
the SAME router object by :mod:`ate_cloud.api.v1.knowledge_reads`, imported
for its registration side effect at the bottom of this module. Keeping them in
a separate module honors the 250-LOC ceiling while adding NO new router mount,
so the mount-level JWT guard and the auth sentinel (protected==27 /
anonymous==5) are unchanged.

The path arguments to extract are server-local filesystem paths (no file
upload; python-multipart is not installed). The endpoint is mount-level JWT
protected like the other knowledge routers.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.schemas.knowledge import (
    KnowledgeExtractRequest,
    KnowledgeExtractSummary,
    SourceExtractCounts,
)
from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.knowledge_extraction import KnowledgeExtractionService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
)

DBSession = Annotated[AsyncSession, Depends(get_db)]


def get_graph_service(request: Request) -> GraphService | None:
    """Lazily build/cache the GraphService; ``None`` when unavailable.

    Construction is cheap/lazy (the FalkorDB client opens no socket until
    the first graph command), so a failure here means degradation rather
    than a hard error — the ORM layer of extraction still runs.
    """
    existing: GraphService | None = getattr(request.app.state, "graph_service", None)
    if existing is not None:
        return existing
    try:
        from ate_cloud.services.falkordb_graph_service import FalkorDBGraphService

        service = FalkorDBGraphService(
            url=settings.falkordb_url,
            graph_name=settings.falkordb_graph,
            password=settings.falkordb_password or None,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to ORM-only extraction
        logger.warning("Graph service unavailable for extraction: %s", exc)
        return None
    request.app.state.graph_service = service
    return service


def get_extraction_service(
    request: Request,
) -> KnowledgeExtractionService:
    """Factory dependency (overridable in tests via dependency_overrides)."""
    return KnowledgeExtractionService(graph=get_graph_service(request))


def require_graph_service(request: Request) -> GraphService:
    """Graph dependency for endpoints whose data lives ONLY in the graph.

    Unlike :func:`get_graph_service` (which returns ``None`` so extraction can
    degrade to ORM-only), the graph-browse endpoint has no relational fallback:
    a missing/down backend is an honest 503. Construction itself is still lazy
    and cached on ``app.state.graph_service`` (shared with faults/diagnose/
    health), so the app boots fine with no reachable FalkorDB.
    """
    graph = get_graph_service(request)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Knowledge graph unavailable: no graph backend configured",
        )
    return graph


@router.post("/extract", response_model=KnowledgeExtractSummary)
async def extract_knowledge(
    payload: KnowledgeExtractRequest,
    db: DBSession,
    extractor: Annotated[KnowledgeExtractionService, Depends(get_extraction_service)],
) -> KnowledgeExtractSummary:
    """POST /api/v1/knowledge/extract — run deterministic extraction.

    Idempotent: re-running MERGEs/upserts on stable ids and natural keys.
    Malformed recordings are skipped with a warning; a down graph backend
    degrades to ORM-only persistence (``graph_status == "degraded"``).
    """
    try:
        summary = await extractor.extract_sources(
            db,
            product_code=payload.product_code,
            dsl_paths=payload.dsl_paths,
            recording_paths=payload.recording_paths,
        )
    except Exception as exc:  # noqa: BLE001 - breaker/graph failures -> 502/503
        from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

        if isinstance(exc, CircuitBreakerOpenError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Knowledge graph unavailable: {exc}",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Knowledge extraction failed: {exc}",
        ) from exc
    await db.commit()
    return KnowledgeExtractSummary(
        product_code=summary.product_code,
        requirements=SourceExtractCounts(
            created=summary.requirements.created, updated=summary.requirements.updated
        ),
        cases=SourceExtractCounts(
            created=summary.cases.created, updated=summary.cases.updated
        ),
        recordings_read=summary.recordings.files_read,
        results_written=summary.recordings.results_written,
        recording_events_skipped=summary.recordings.skipped_events,
        recordings_skipped=summary.recordings.skipped_files,
        unmatched_steps=summary.recordings.unmatched_steps,
        graph_status=summary.graph_status,
    )


# Register the READ endpoints (requirements/cases/traceability/graph) on the
# SAME router. Imported last for its side effect: knowledge_reads imports
# ``router``/factories from this module, so this avoids a circular import.
from ate_cloud.api.v1 import knowledge_reads  # noqa: E402,F401  (route registration)

__all__ = [
    "get_extraction_service",
    "get_graph_service",
    "require_graph_service",
    "router",
]
