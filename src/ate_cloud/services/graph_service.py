"""GraphService — backend-agnostic protocol for FMEA knowledge graph operations.

Defines the seam that graph consumers (KG seeding, KG evolution, hybrid
retrieval, diagnosis) depend on. The current production implementation is
:class:`~ate_cloud.services.falkordb_graph_service.FalkorDBGraphService`
(Cypher/GQL over the FalkorDB async client, RESP/6379); other backends
implement the same protocol and slot in without touching consumers.

The first parameter of :meth:`query` / :meth:`write` is named ``statement``
and carries the backend's query dialect (Cypher/GQL for FalkorDB) —
consumers treat it as an opaque query string.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GraphService(Protocol):
    """Async graph-database service used by the FMEA knowledge graph.

    All methods are async. Implementations are expected to protect calls
    with a CircuitBreaker (see FalkorDBGraphService) so transient backend
    failures surface as ``CircuitBreakerOpenError`` after the threshold.
    """

    async def query(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a read query (Cypher/GQL) and return result rows.

        Args:
            statement: Query string in the backend's dialect.
            params: Optional bound query parameters.

        Returns:
            List of result rows, each a dict keyed by column/alias name.
        """
        ...

    async def write(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a write statement (CREATE/MERGE/DELETE); returns result rows."""
        ...

    async def create_constraints(self) -> None:
        """Create backend uniqueness constraints/indexes for FMEA node types (idempotent)."""
        ...

    async def count_nodes(self) -> int:
        """Return the total node count in the graph."""
        ...

    async def count_relationships(self) -> int:
        """Return the total relationship count in the graph."""
        ...

    async def health(self) -> dict[str, Any]:
        """Return backend health info; raise when the backend is unreachable.

        Returns:
            A small dict (e.g. ``{"status": "healthy", "backend": "neo4j"}``).

        Raises:
            Exception: When the graph backend does not respond. Callers
                (health endpoints / lazy factories) translate failures into
                503; the app must still boot without a reachable backend.
        """
        ...


__all__ = ["GraphService"]
