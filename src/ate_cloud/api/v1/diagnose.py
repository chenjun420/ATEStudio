"""Diagnosis API endpoints - AI-assisted fault diagnosis via hybrid RAG + LLM.

- ``POST /api/v1/diagnose`` - vector retrieval over the fault-case index and
  LLM analysis; every diagnosis is persisted to the ``diagnoses`` ORM table
  (task 15), linked to the run/session when supplied.
- ``POST /api/v1/diagnose/{diagnosis_id}/feedback`` - record operator
  feedback, updating the row's ``helpful`` / ``feedback_note`` columns.

EmbeddingService, HybridRetriever and DiagnosisService are
each lazily built once and cached on ``app.state`` (mirrors faults.py), so
requests reuse one shared DiagnosisService.
"""

import logging
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
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/diagnose", tags=["diagnosis"])

# Type alias for async DB session dependency (avoids B008 ruff warning).
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_embedding_service(request: Request) -> EmbeddingService:
    """Lazily create/cache EmbeddingService on app.state (503 on failure).

    The not-configured case is separated from the broken case, and it names the
    variable to set. Both distinctions matter:

    * A missing deployment credential is not the same failure as a service that
      is configured but cannot start. Collapsing them into one message meant an
      operator saw the OpenAI SDK's own "Missing credentials" text and had no
      idea it was a deployment step, not a code fault.
    * The SDK's message leaks library internals and names nothing actionable.
      Whoever reads it still cannot tell what to do.

    Deliberately still a 503: the service genuinely cannot serve this
    capability, and the code says so honestly. What changed is that the reason
    is stated instead of implied.
    """
    service: EmbeddingService | None = getattr(request.app.state, "embedding_service", None)
    if service is not None:
        return service

    if not settings.openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "故障诊断未启用: 缺少 OPENAI_API_KEY。症状/故障向量的语义检索依赖它, "
                "未配置时本能力不可用(检索退化, 不影响产测执行与其他功能)。"
            ),
        )

    try:
        service = EmbeddingService(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    except Exception as e:  # noqa: BLE001 — construction can fail many ways
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"嵌入服务初始化失败: {type(e).__name__}: {e}。"
                "请检查 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_EMBEDDING_MODEL 是否可达。"
            ),
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
    qdrant_client: Annotated[Any, Depends(_get_qdrant_client)],
) -> HybridRetriever:
    """Dependency: create or retrieve HybridRetriever from app state.

    Caches on app.state for reuse across requests.

    No ``graph_service``: the knowledge-graph leg is gone, and the retriever's
    RRF degrades to the single vector list it was always able to run on its own
    (see ``hybrid_fusion`` — a pure function over two ranked lists, of which one
    is now always empty).
    """
    retriever: HybridRetriever | None = getattr(request.app.state, "hybrid_retriever", None)
    if retriever is not None:
        return retriever
    retriever = HybridRetriever(
        embedding_service=embedding_service,
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


@router.get("/readiness")
async def diagnose_readiness(request: Request) -> dict[str, Any]:
    """GET /api/v1/diagnose/readiness — what this capability can actually do.

    Added because four capabilities in this project were built, wired, and
    deployed, and had never once run: the SPA was never served, the
    ``aterag:import`` scope was granted to nobody, the diagnosis path had no
    credentials, and the knowledge-graph backend was never provisioned. Every
    one of them passed every check that existed, because every check was an API
    probe rather than a walk through the flow.

    So the rule this endpoint exists to enforce: **a capability is not available
    until it has been observed to work end to end.** A menu entry, a registered
    route and a build stamp are not evidence. This reports what is configured
    right now, which is the input to that judgement — the walk itself lives in
    ``scripts/verify_flow_http.py``.

    Deliberately unauthenticated-free of side effects and cheap: it touches no
    network service beyond reading ``app.state``, so the UI can call it on every
    page load and grey out what is not live.
    """
    state = request.app.state
    embedding = getattr(state, "embedding_service", None) is not None
    qdrant = getattr(state, "qdrant_client", None) is not None

    # Retrieval is the part that must work for a suggestion to exist at all.
    # Without embeddings the fault vectors carry no meaning, so retrieval
    # silently returns nothing useful — which reads as "no similar past faults"
    # rather than as "this feature is off".
    retrieval = embedding and qdrant
    llm = bool(settings.openai_api_key)

    blockers: list[str] = []
    if not embedding:
        blockers.append("缺少 OPENAI_API_KEY —— 症状/故障向量无法嵌入, 检索退化")
    if not qdrant:
        blockers.append("Qdrant 不可用 —— 故障向量库无法读取")
    # Set by the lifespan when the collection's vector width disagreed with the
    # configured model. The client and the collection can both be present while
    # nothing has ever been indexed, so presence checks alone would report this
    # capability as available and every future diagnosis would return "no similar
    # past faults" — indistinguishable from a line that has never failed.
    blockers.extend(getattr(state, "failure_index_blockers", []) or [])

    available = retrieval and not any(b.startswith("Qdrant collection") for b in blockers)

    # Configured is not the same as useful. An index holding zero points answers
    # every query with "no similar past faults" — indistinguishable from a line
    # that has genuinely never failed, so the operator concludes either that the
    # tool is broken or that nothing is wrong. The count lets the UI say which.
    #
    # Reported as None when it cannot be read rather than as 0, because "the
    # count failed" and "there are no cases" lead to opposite advice.
    indexed_cases: int | None = None
    if qdrant:
        try:
            info = state.qdrant_client.get_collection(settings.qdrant_collection_failures)
            indexed_cases = int(getattr(info, "points_count", 0) or 0)
        except Exception as exc:  # noqa: BLE001 — a count failure is not a readiness failure
            logger.debug("readiness: could not read indexed case count: %s", exc)

    return {
        # `available` is deliberately narrower than "the route exists".
        "available": available,
        "capabilities": {
            "retrieval": available,
            # A suggestion needs retrieval; an LLM only writes it up.
            "suggestion": available,
            "llm_writeup": available and llm,
        },
        "blockers": blockers,
        # Not a blocker: the pipeline is fine, there is simply nothing in it yet.
        # Reported separately so "no similar past faults" is legible as a fact
        # about the line rather than as a malfunction.
        "indexed_cases": indexed_cases,
        "has_history": bool(indexed_cases),
        "detail": (
            "故障诊断未启用"
            if not retrieval
            else (
                "故障索引不可用, 诊断无法检索历史"
                if not available
                else (
                    f"可用, 但故障案例库为空 ({indexed_cases} 条) —— 诊断只会依据规格"
                    "与通用失效模式, 不含本线历史"
                    if not indexed_cases
                    else (
                        "诊断建议可用 (无 LLM 时仅返回检索结果)"
                        if not llm
                        else f"诊断建议与 LLM 归纳均可用 (已索引 {indexed_cases} 条案例)"
                    )
                )
            )
        ),
    }


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
    ``feedback_note`` on the persisted diagnosis.

    A rejection used to be able to drive knowledge-graph evolution through
    ``POST /api/v1/faults/evolve``. That endpoint is gone with the subsystem, so
    feedback now only records what the operator said — it does not change any
    knowledge. Turning a rejected diagnosis into a fault case is a person's
    edit in the station case base, deliberately: an automatic write would let a
    bad diagnosis author its own correction.

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
