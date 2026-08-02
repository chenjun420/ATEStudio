"""Diagnosis API endpoints - AI-assisted fault diagnosis via hybrid RAG + LLM.

Provides:
- ``POST /api/v1/diagnose`` - diagnose a test failure using hybrid retrieval
  (Qdrant + Neo4j) and LLM analysis.
- ``POST /api/v1/diagnose/{diagnosis_id}/feedback`` - record operator
  feedback (confirm/reject) for a diagnosis, enabling knowledge graph
  evolution.

The diagnosis pipeline: receive failure info -> HybridRetriever searches
Qdrant (semantic) + Neo4j (causal) -> RRF fusion -> LLM analyzes with
retrieved context -> returns structured diagnosis with root cause,
confidence, evidence citations, and repair steps.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ate_cloud.config import settings
from ate_cloud.services.diagnosis_service import DiagnosisService
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_cloud.services.neo4j_graph_service import (
    CircuitBreakerOpenError,
    Neo4jGraphService,
)

router = APIRouter(prefix="/diagnose", tags=["diagnosis"])


def _get_graph_service(request: Request) -> Neo4jGraphService:
    """Dependency: lazily create or retrieve Neo4jGraphService from app state.

    Mirrors the pattern in faults.py - caches on app.state for reuse.
    """
    service: Neo4jGraphService | None = getattr(request.app.state, "neo4j_graph_service", None)
    if service is not None:
        return service
    try:
        service = Neo4jGraphService(
            url=settings.neo4j_url,
            password=settings.neo4j_password,
        )
    except (ValueError, Exception) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Neo4j graph service unavailable: {e}",
        ) from e
    request.app.state.neo4j_graph_service = service
    return service


def _get_embedding_service(request: Request) -> EmbeddingService:
    """Dependency: lazily create or retrieve EmbeddingService from app state."""
    service: EmbeddingService | None = getattr(request.app.state, "embedding_service", None)
    if service is not None:
        return service
    try:
        service = EmbeddingService(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
        )
    except (ValueError, Exception) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Embedding service unavailable: {e}",
        ) from e
    request.app.state.embedding_service = service
    return service


def _get_qdrant_client(request: Request) -> Any:
    """Dependency: retrieve Qdrant client from app state.

    The Qdrant client is created in main.py lifespan and stored on
    app.state. If not present (e.g. Qdrant init failed), returns 503.
    """
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
    graph_service: Annotated[Neo4jGraphService, Depends(_get_graph_service)],
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
        neo4j_service=graph_service,
        qdrant_client=qdrant_client,
    )
    request.app.state.hybrid_retriever = retriever
    return retriever


def _get_diagnosis_service(
    retriever: Annotated[HybridRetriever, Depends(_get_hybrid_retriever)],
) -> DiagnosisService:
    """Dependency: create a DiagnosisService bound to the hybrid retriever.

    A new DiagnosisService is created per request (the feedback_store is
    in-memory; caching the service on app.state would share feedback
    across requests, which is acceptable for production but complicates
    testing). The underlying HybridRetriever is cached on app.state.
    """
    return DiagnosisService(
        hybrid_retriever=retriever,
        api_key=settings.openai_api_key,
    )


# ── Request/Response schemas ───────────────────────────────────────────────


class DiagnoseRequest(BaseModel):
    """Request body for POST /api/v1/diagnose."""

    product_type: str = Field(..., description="Product type identifier")
    failed_test: str = Field(..., description="Name/description of the failed test")
    error_code: str = Field(default="", description="Error code if available")
    log_snippet: str = Field(default="", description="Log fragment from the failed execution")


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
        description="Corrected root cause (when feedback='rejected')",
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
    service: Annotated[DiagnosisService, Depends(_get_diagnosis_service)],
) -> DiagnoseResponse:
    """POST /api/v1/diagnose - diagnose a test failure.

    Receives product type, failed test name, error code, and log snippet.
    Performs hybrid retrieval (Qdrant + Neo4j) to find similar past
    failures, then calls an LLM with the retrieved context to produce a
    structured diagnosis with root cause, confidence, evidence citations,
    and repair steps.

    Returns:
        DiagnoseResponse with diagnosis_id (for feedback), root_cause,
        confidence, evidence_citations, repair_steps, and retrieved_cases.

    Raises:
        HTTPException: 503 if the LLM circuit breaker is OPEN or services
            are unavailable. 502 if the LLM call fails.
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
    return DiagnoseResponse(**result)


@router.post(
    "/{diagnosis_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_200_OK,
)
async def record_feedback(
    diagnosis_id: str,
    request_body: FeedbackRequest,
    service: Annotated[DiagnosisService, Depends(_get_diagnosis_service)],
) -> FeedbackResponse:
    """POST /api/v1/diagnose/{diagnosis_id}/feedback - record operator feedback.

    Records operator confirmation or rejection of a diagnosis. When
    rejected with a correction, the correction can be used to evolve
    the knowledge graph via POST /api/v1/faults/evolve.

    Args:
        diagnosis_id: The diagnosis ID returned by POST /diagnose.
        request_body: Feedback ('confirmed' or 'rejected') and optional
            correction text.

    Returns:
        FeedbackResponse confirming the feedback was recorded.
    """
    if request_body.feedback not in ("confirmed", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feedback must be 'confirmed' or 'rejected'",
        )
    result = service.record_feedback(
        diagnosis_id=diagnosis_id,
        feedback=request_body.feedback,
        correction=request_body.correction,
    )
    return FeedbackResponse(**result)
