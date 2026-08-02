"""Faults API endpoints — FMEA knowledge graph seeding and diagnosis.

Provides:
- ``POST /api/v1/faults/seed`` — seed the Neo4j FMEA knowledge graph with
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
from ate_cloud.services.kg_evolution import KGEvolution
from ate_cloud.services.kg_seeder import KGSeeder
from ate_cloud.services.neo4j_graph_service import (
    CircuitBreakerOpenError,
    Neo4jGraphService,
)

router = APIRouter(prefix="/faults", tags=["faults"])


def _get_graph_service(request: Request) -> Neo4jGraphService:
    """Dependency: lazily create or retrieve Neo4jGraphService from app state.

    The service is constructed from ``settings.neo4j_url`` and
    ``settings.neo4j_password`` on first access and cached in
    ``app.state`` for reuse. This avoids modifying ``main.py`` (no
    lifespan wiring) while still pooling the Neo4j driver.

    Raises:
        HTTPException: 503 if Neo4jGraphService construction fails
            (invalid credentials, unreachable server).
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


def _get_kg_seeder(
    graph_service: Annotated[Neo4jGraphService, Depends(_get_graph_service)],
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
        )
    except (ValueError, Exception) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Embedding service unavailable: {e}",
        ) from e
    request.app.state.embedding_service = service
    return service


def _get_kg_evolution(
    graph_service: Annotated[Neo4jGraphService, Depends(_get_graph_service)],
    embedding_service: Annotated[EmbeddingService, Depends(_get_embedding_service)],
) -> KGEvolution:
    """Dependency: create a KGEvolution bound to graph and embedding services."""
    return KGEvolution(graph_service, embedding_service)


@router.post("/seed")
async def seed_fault_graph(
    seeder: Annotated[KGSeeder, Depends(_get_kg_seeder)],
) -> dict[str, int]:
    """POST /api/v1/faults/seed — seed the FMEA knowledge graph.

    Creates uniqueness constraints for all FMEA node types, then MERGEs
    100+ electronics fault records (nodes + relationships) into the Neo4j
    graph. Idempotent — re-running updates existing nodes without
    creating duplicates.

    Returns:
        Dict with ``nodes_created`` and ``relationships_created`` (total
        counts in the graph after seeding).

    Raises:
        HTTPException: 503 if the Neo4j circuit breaker is OPEN or
            the graph service is unavailable.
        HTTPException: 502 if a Neo4j operation fails during seeding.
    """
    try:
        result = await seeder.seed_all()
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Neo4j circuit breaker open: {e}",
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
        HTTPException: 503 if the Neo4j circuit breaker is OPEN or
            the embedding/graph service is unavailable.
        HTTPException: 502 if a Neo4j operation fails during evolution.
    """
    try:
        result = await evolution.process_feedback(request.model_dump())
    except CircuitBreakerOpenError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Neo4j circuit breaker open: {e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to evolve fault graph: {e}",
        ) from e
    return result
