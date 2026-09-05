"""Faults API endpoints — FMEA knowledge graph seeding and diagnosis.

Provides:
- ``POST /api/v1/faults/seed`` — seed the FalkorDB FMEA knowledge graph with
  100+ electronics fault records.
- ``POST /api/v1/faults/evolve`` — evolve the knowledge graph from new
  diagnosis feedback (synonym detection + entity creation + edge degradation).

The knowledge graph enables AI-assisted fault diagnosis by mapping
fault symptoms to causes, solutions, affected components, product types,
error codes, and diagnostic instruments.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ate_cloud.config import settings
from ate_cloud.services.embedding_service import EmbeddingService
from ate_cloud.services.falkordb_graph_service import (
    CircuitBreakerOpenError,
    FalkorDBGraphService,
)
from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.kg_evolution import KGEvolution
from ate_cloud.services.kg_seeder import KGSeeder

router = APIRouter(prefix="/faults", tags=["faults"])


def _get_graph_service(request: Request) -> GraphService:
    """Dependency: lazily create or retrieve the GraphService from app state.

    FalkorDB (Redis RESP protocol, default port 6379) is the current
    GraphService implementation; alternative backends slot in behind the
    same protocol. The service is constructed from ``settings.falkordb_url``
    on first access and cached on ``app.state.graph_service`` for reuse —
    construction is lazy/cheap (the client opens no socket until first
    graph command), so the app boots fine without a reachable graph.

    Raises:
        HTTPException: 503 if the graph service construction fails.
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


def _get_kg_seeder(
    graph_service: Annotated[GraphService, Depends(_get_graph_service)],
) -> KGSeeder:
    """Dependency: create a KGSeeder bound to the graph service."""
    return KGSeeder(graph_service)


def _get_embedding_service(request: Request) -> EmbeddingService:
    """Dependency: lazily create or retrieve EmbeddingService from app state.

    The service is constructed from ``settings.openai_api_key`` and
    ``settings.openai_embedding_model`` on first access and cached in
    ``app.state`` for reuse.

    Raises:
        HTTPException: 503 if EmbeddingService construction fails
            (missing API key, unreachable API).
    """
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


def _get_kg_evolution(
    request: Request,
    graph_service: Annotated[GraphService, Depends(_get_graph_service)],
    embedding_service: Annotated[EmbeddingService, Depends(_get_embedding_service)],
) -> KGEvolution:
    """Dependency: create a KGEvolution bound to graph and embedding services.

    The Qdrant client (set on ``app.state`` by the app lifespan) is passed
    when available for synonym nearest-neighbor; when Qdrant is not
    initialized (``app.state.qdrant_client`` absent), ``None`` is passed and
    KGEvolution degrades dedup gracefully while graph evolution proceeds.
    """
    qdrant_client: object | None = getattr(request.app.state, "qdrant_client", None)
    return KGEvolution(
        graph_service,
        embedding_service,
        qdrant_client=qdrant_client,
    )


@router.post("/seed")
async def seed_fault_graph(
    seeder: Annotated[KGSeeder, Depends(_get_kg_seeder)],
) -> dict[str, int]:
    """POST /api/v1/faults/seed — seed the FMEA knowledge graph.

    Creates uniqueness constraints for all FMEA node types, then MERGEs
    100+ electronics fault records (nodes + relationships) into the
    FalkorDB graph. Idempotent — re-running updates existing nodes without
    creating duplicates.

    Returns:
        Dict with ``nodes_created`` and ``relationships_created`` (total
        counts in the graph after seeding).

    Raises:
        HTTPException: 503 if the graph circuit breaker is OPEN or
            the graph service is unavailable.
        HTTPException: 502 if a graph operation fails during seeding.
    """
    try:
        result = await seeder.seed_all()
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Graph circuit breaker open: {e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed fault graph: {e}",
        ) from e
    return result


class EvolutionRequest(BaseModel):
    """Request body for POST /api/v1/faults/evolve.

    Attributes:
        fault_symptom: Observable test failure symptom description.
        root_cause: Diagnosed root cause of the symptom.
        error_code: Error code triggered by the symptom.
        product_type: Product type where the symptom occurs.
    """

    fault_symptom: str
    root_cause: str
    error_code: str
    product_type: str


@router.post("/evolve")
async def evolve_fault_graph(
    request: EvolutionRequest,
    evolution: Annotated[KGEvolution, Depends(_get_kg_evolution)],
) -> dict[str, str | int]:
    """POST /api/v1/faults/evolve — evolve the FMEA knowledge graph.

    Receives diagnosis feedback, embeds the fault symptom, checks for
    synonyms (cosine similarity >= 0.85 against existing FaultSymptom
    embeddings), creates new entities if novel, and degrades stale edges.

    Returns:
        Dict with ``action`` ("created" or "skipped"), ``nodes_created``,
        and ``edges_created``.

    Raises:
        HTTPException: 503 if the graph circuit breaker is OPEN or
            the embedding/graph service is unavailable.
        HTTPException: 502 if a graph operation fails during evolution.
    """
    try:
        result = await evolution.process_feedback(request.model_dump())
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Graph circuit breaker open: {e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to evolve fault graph: {e}",
        ) from e
    return result
