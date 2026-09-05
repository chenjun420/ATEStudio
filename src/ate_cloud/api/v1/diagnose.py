"""Diagnosis API endpoints - AI-assisted fault diagnosis via hybrid RAG + LLM.

- ``POST /api/v1/diagnose`` - hybrid retrieval (Qdrant + ontology KG) and
  LLM analysis; every diagnosis is persisted to the ``diagnoses`` ORM table
  (task 15), linked to the run/session when supplied.
- ``POST /api/v1/diagnose/{diagnosis_id}/feedback`` - record operator
  feedback, updating the row's ``helpful`` / ``feedback_note`` columns.

GraphService, EmbeddingService, HybridRetriever and DiagnosisService are
each lazily built once and cached on ``app.state`` (mirrors faults.py), so
requests reuse one shared DiagnosisService.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.config import settings
from ate_cloud.db import get_db
from ate_cloud.services.diagnosis_service import DiagnosisService
from ate_cloud.services.diagnosis_store import (
    HELPFUL_BY_FEEDBACK,
    build_symptom,
    persist_diagnosis,
)
from ate_cloud.services.diagnosis_store import (
    record_feedback as persist_feedback,
)
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.falkordb_graph_service import (
    CircuitBreakerOpenError,
    FalkorDBGraphService,
)
from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.hybrid_retriever import HybridRetriever

router = APIRouter(prefix="/diagnose", tags=["diagnosis"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_graph_service(request: Request) -> GraphService:
    """Lazily create/cache the GraphService on app.state (mirrors faults.py).

    Construction is lazy/cheap (no socket until first graph command).
    Raises HTTPException 503 if construction fails.
    """
    service: GraphService | None = getattr(request.app.state, "graph_service", None)
    if service is not None:
        return service
    try:
        service = FalkorDBGraphService(
            url=settings.falkordb_url,
            graph_name=settings.falkordb_graph,
            password=settings.falkordb_password or None,
        )
    except (ValueError, Exception) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Graph service unavailable: {e}",
        ) from e
    request.app.state.graph_service = service
    return service


def _get_embedding_service(request: Request) -> EmbeddingService:
    """Lazily create/cache EmbeddingService on app.state (503 on failure)."""
    service: EmbeddingService | None = getattr(request.app.state, "embedding_service", None)
    if service is not None:
        return service
    try:
        service = EmbeddingService(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    except (ValueError, Exception) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Embedding service unavailable: {e}",
        ) from e
    request.app.state.embedding_service = service
    return service


def _get_qdrant_client(request: Request) -> Any:
    """Retrieve the lifespan-created Qdrant client from app.state (503 if absent)."""
    client: Any = getattr(request.app.state, "qdrant_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant client not initialized",
        )
    return client


def _get_hybrid_retriever(
    request: Request,
    embedding_service: Annotated[EmbeddingService, Depends(_get_embedding_service)],
    graph_service: Annotated[GraphService, Depends(_get_graph_service)],
    qdrant_client: Annotated[Any, Depends(_get_qdrant_client)],
) -> HybridRetriever:
    """Dependency: create or retrieve HybridRetriever from app state.

    Caches on app.state for reuse across requests.
    """
    retriever: HybridRetriever | None = getattr(request.app.state, "hybrid_retriever", None)
    if retriever is not None:
        return retriever
    retriever = HybridRetriever(
        embedding_service=embedding_service,
        graph_service=graph_service,
        qdrant_client=qdrant_client,
    )
    request.app.state.hybrid_retriever = retriever
    return retriever


def _get_diagnosis_service(
    request: Request,
    retriever: Annotated[HybridRetriever, Depends(_get_hybrid_retriever)],
) -> DiagnosisService:
    """Lazily create/cache the shared DiagnosisService on app.state.

    The service is stateless apart from its LLM client/circuit breaker
    (persistence is DB-backed via diagnosis_store), so one instance serves
    every request.
    """
    service: DiagnosisService | None = getattr(
        request.app.state, "diagnosis_service", None
    )
    if service is not None:
        return service
    service = DiagnosisService(
        hybrid_retriever=retriever,
        api_key=settings.openai_api_key,
    )
    request.app.state.diagnosis_service = service
    return service


# ── Request/Response schemas ───────────────────────────────────────────────


class DiagnoseRequest(BaseModel):
    """Request body for POST /api/v1/diagnose."""

    product_type: str = Field(..., description="Product type identifier")
    failed_test: str = Field(..., description="Name/description of the failed test")
    error_code: str = Field(default="", description="Error code if available")
    log_snippet: str = Field(default="", description="Log fragment from the failed execution")
    run_id: str | None = Field(default=None, description="Execution run id to link")
    session_id: str | None = Field(default=None, description="Edge/NATS session reference")


class DiagnoseResponse(BaseModel):
    """Response for POST /api/v1/diagnose."""

    diagnosis_id: str = Field(..., description="Unique diagnosis ID for feedback")
    root_cause: str = Field(default="", description="Primary root cause explanation")
    confidence: float = Field(default=0.0, description="Confidence score (0.0-1.0)")
    evidence_citations: list[str] = Field(
        default_factory=list,
        description="Citations referencing retrieved cases",
    )
    repair_steps: list[str] = Field(
        default_factory=list,
        description="Actionable repair steps",
    )
    retrieved_cases: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Raw retrieved failure cases (for transparency)",
    )


class FeedbackRequest(BaseModel):
    """Request body for POST /api/v1/diagnose/{id}/feedback."""

    feedback: str = Field(
        ...,
        description="Feedback: 'confirmed' or 'rejected'",
    )
    correction: str = Field(
        default="",
        description="Corrected root cause / note (when feedback='rejected')",
    )


class FeedbackResponse(BaseModel):
    """Response for POST /api/v1/diagnose/{id}/feedback."""

    diagnosis_id: str
    feedback: str
    correction: str
    recorded: bool


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("", response_model=DiagnoseResponse, status_code=status.HTTP_200_OK)
async def diagnose_fault(
    request_body: DiagnoseRequest,
    db: DBSession,
    service: Annotated[DiagnosisService, Depends(_get_diagnosis_service)],
) -> DiagnoseResponse:
    """POST /api/v1/diagnose - diagnose a test failure and persist it.

    Retrieval-only (no LLM key) results are still persisted.

    Raises:
        HTTPException: 503 if the LLM circuit breaker is OPEN; 502 on
            LLM/retrieval failure.
    """
    try:
        result = await service.diagnose(
            product_type=request_body.product_type,
            failed_test=request_body.failed_test,
            error_code=request_body.error_code,
            log_snippet=request_body.log_snippet,
        )
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"LLM circuit breaker open: {e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Diagnosis failed: {e}",
        ) from e

    symptom = build_symptom(
        failed_test=request_body.failed_test,
        error_code=request_body.error_code,
        log_snippet=request_body.log_snippet,
        product_type=request_body.product_type,
    )
    await persist_diagnosis(
        db,
        diagnosis_id=str(result["diagnosis_id"]),
        symptom=symptom,
        result=result,
        run_id=request_body.run_id,
        session_id=request_body.session_id,
    )
    return DiagnoseResponse(**result)


@router.post(
    "/{diagnosis_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_200_OK,
)
async def record_feedback(
    diagnosis_id: str,
    request_body: FeedbackRequest,
    db: DBSession,
) -> FeedbackResponse:
    """POST /api/v1/diagnose/{diagnosis_id}/feedback - record operator feedback.

    Updates ``helpful`` (confirmed -> True, rejected -> False) and
    ``feedback_note`` on the persisted diagnosis. A rejected diagnosis with
    a correction can later drive knowledge-graph evolution via
    ``POST /api/v1/faults/evolve``.

    Raises:
        HTTPException: 400 if feedback is not 'confirmed'/'rejected';
            404 if no diagnosis exists for the id.
    """
    helpful = HELPFUL_BY_FEEDBACK.get(request_body.feedback)
    if helpful is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feedback must be 'confirmed' or 'rejected'",
        )
    row = await persist_feedback(
        db,
        diagnosis_id=diagnosis_id,
        helpful=helpful,
        note=request_body.correction,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Diagnosis {diagnosis_id} not found",
        )
    return FeedbackResponse(
        diagnosis_id=diagnosis_id,
        feedback=request_body.feedback,
        correction=request_body.correction,
        recorded=True,
    )
