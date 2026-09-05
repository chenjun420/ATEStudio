"""FalkorDB Graph Service — FMEA knowledge graph operations over RESP/6379.

Backend-agnostic replacement for the former Neo4j adapter: implements the
:class:`~ate_cloud.services.graph_service.GraphService` protocol using the
async FalkorDB client (``falkordb.asyncio``), which speaks the Redis RESP
protocol on the default Redis port 6379 (NOT the Neo4j Bolt/7687 shim).

Key FalkorDB quirks handled here:

* Query results are POSITIONAL — ``QueryResult.header`` is a list of
  ``[column_name, type_code]`` pairs and ``QueryResult.result_set`` is a
  list of positional rows (``list[list]``), never ``list[dict]``.
  :meth:`_rows_to_dicts` zips header names onto each row to produce the
  ``list[dict]`` shape the GraphService protocol promises. There is no
  dict-key access on raw positional FalkorDB results.
* Uniqueness is a two-step DDL: a RANGE index (``CREATE INDEX FOR ...``)
  must exist before ``GRAPH.CONSTRAINT CREATE ... UNIQUE ...`` (FalkorDB
  rejects uniqueness without the backing index). Neo4j's
  ``CREATE CONSTRAINT ... REQUIRE ... IS UNIQUE`` is not valid FalkorDB.
* Health is a Redis ``PING`` on the underlying connection.

Per AGENTS.md section 7: if FalkorDB is configured but unreachable, the
CircuitBreaker opens and ``CircuitBreakerOpenError`` propagates — the app
still boots (the client is created lazily on first use) and non-graph
endpoints keep working; callers translate graph failures into 503.
"""

from __future__ import annotations

import logging
from typing import Any

from ate_cloud.services.graph_service import GraphService
from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

_DEFAULT_GRAPH: str = "fmea"

# Ontology node label → unique property. The task-8 seed writer and the
# task-12 extraction writer MERGE every node on its stable ``id`` (e.g.
# ``fault:<slug(error_code)>``, ``instrument:<kind>`` ...), so uniqueness is
# keyed on ``id`` for every ontology label. (This replaces the retired
# ``FaultSymptom``/``ErrorCode`` labels, which keyed uniqueness on ``name``/
# ``code``.)
_UNIQUE_NODES: tuple[tuple[str, str], ...] = (
    ("Fault", "id"),
    ("Symptom", "id"),
    ("Cause", "id"),
    ("Solution", "id"),
    ("Component", "id"),
    ("Product", "id"),
    ("Instrument", "id"),
    ("TestRequirement", "id"),
    ("TestCase", "id"),
    ("TestStep", "id"),
    ("UUTResult", "id"),
)


def _column_name(descriptor: Any) -> str:
    """Extract a column name from a FalkorDB header descriptor.

    With ``decode_responses=True`` (the ``from_url`` default) each header
    entry is ``[name: str, type_code: int]``; without it ``name`` is bytes.
    Defensively tolerate a bare scalar name too.
    """
    name = descriptor[0] if isinstance(descriptor, (list, tuple)) else descriptor
    return name.decode() if isinstance(name, bytes) else str(name)


def _is_already_exists(error: Exception) -> bool:
    """True when a DDL error reports an existing index/constraint (idempotent re-run)."""
    message = str(error).lower()
    return "already exist" in message or "duplicate" in message


class FalkorDBGraphService(GraphService):
    """Async FalkorDB graph service backed by ``falkordb.asyncio``.

    Implements the backend-agnostic :class:`GraphService` protocol —
    consumers (KG seeder/evolution, hybrid retriever) depend on the
    protocol, not on this class.

    The client is created lazily on first graph use, so constructing the
    service (e.g. at app import / dependency wiring) never opens a socket;
    an unreachable server only fails the actual graph call, which the
    CircuitBreaker tracks.

    Args:
        url: FalkorDB/Redis connection URL (RESP, e.g. ``redis://localhost:6379``).
        graph_name: FalkorDB graph key holding the FMEA knowledge graph.
        password: Optional Redis password (ignored when embedded in ``url``).
        client: Optional pre-built ``falkordb.asyncio.FalkorDB`` (tests inject a fake).
    """

    def __init__(
        self,
        url: str,
        graph_name: str = _DEFAULT_GRAPH,
        password: str | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self._url = url
        self._graph_name = graph_name
        self._password = password or None
        self._client: Any | None = client
        self._graph: Any | None = None
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="falkordb-graph",
        )

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    def _get_client(self) -> Any:
        """Lazily build the async FalkorDB client (RESP; no connection until a command runs)."""
        if self._client is None:
            from falkordb.asyncio import FalkorDB

            kwargs: dict[str, Any] = {}
            if self._password:
                kwargs["password"] = self._password
            self._client = FalkorDB.from_url(self._url, **kwargs)
        return self._client

    def _get_graph(self) -> Any:
        """Lazily select the FMEA graph handle from the client."""
        if self._graph is None:
            self._graph = self._get_client().select_graph(self._graph_name)
        return self._graph

    @staticmethod
    def _rows_to_dicts(result: Any) -> list[dict[str, Any]]:
        """Map positional FalkorDB results to ``list[dict]`` keyed by header names."""
        columns = [_column_name(h) for h in result.header]
        return [dict(zip(columns, row, strict=False)) for row in result.result_set]

    async def query(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a read query (Cypher/GQL) via the read-only path.

        Args:
            statement: Query string (GraphService protocol's opaque ``statement``).
            params: Optional bound query parameters.

        Returns:
            Result rows as dicts keyed by column/alias name.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN after repeated failures.
            Exception: Any FalkorDB query error not suppressed by the breaker.
        """
        graph = self._get_graph()

        async def _do_query() -> list[dict[str, Any]]:
            result = await graph.ro_query(statement, params)
            return self._rows_to_dicts(result)

        return await self._breaker.call(_do_query)

    async def write(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a write statement (CREATE/MERGE/DELETE) via the read-write path.

        Returns:
            Result rows as dicts (may be empty for pure writes).

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
            Exception: Any FalkorDB write error.
        """
        graph = self._get_graph()

        async def _do_write() -> list[dict[str, Any]]:
            result = await graph.query(statement, params)
            return self._rows_to_dicts(result)

        return await self._breaker.call(_do_write)

    async def create_constraints(self) -> None:
        """Create FalkorDB range indexes + uniqueness constraints for FMEA nodes.

        Idempotent: re-running tolerates "already exists" DDL errors while
        genuine failures propagate (breaker-protected). A uniqueness
        constraint requires a backing RANGE index on the same property, so
        each node type gets ``CREATE INDEX FOR (e:Label) ON (e.prop)``
        followed by ``GRAPH.CONSTRAINT CREATE <graph> UNIQUE NODE <Label>
        PROPERTIES 1 <prop>``.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
        """
        graph = self._get_graph()
        graph_key = self._graph_name

        for label, prop in _UNIQUE_NODES:
            index_stmt = f"CREATE INDEX FOR (e:{label}) ON (e.{prop})"

            async def _ensure_index(stmt: str = index_stmt) -> None:
                from redis.exceptions import ResponseError

                try:
                    await graph.query(stmt)
                except ResponseError as e:
                    if not _is_already_exists(e):
                        raise

            async def _ensure_constraint(lbl: str = label, prp: str = prop) -> None:
                from redis.exceptions import ResponseError

                try:
                    await graph.execute_command(
                        "GRAPH.CONSTRAINT",
                        "CREATE",
                        graph_key,
                        "UNIQUE",
                        "NODE",
                        lbl,
                        "PROPERTIES",
                        1,
                        prp,
                    )
                except ResponseError as e:
                    if not _is_already_exists(e):
                        raise

            await self._breaker.call(_ensure_index)
            await self._breaker.call(_ensure_constraint)

        logger.info("Created FMEA knowledge graph indexes+constraints (%d node types)", len(_UNIQUE_NODES))

    async def count_nodes(self) -> int:
        """Return the total node count in the graph."""
        results = await self.query("MATCH (n) RETURN count(n) AS total")
        return int(results[0]["total"]) if results else 0

    async def count_relationships(self) -> int:
        """Return the total relationship count in the graph."""
        results = await self.query("MATCH ()-[r]->() RETURN count(r) AS total")
        return int(results[0]["total"]) if results else 0

    async def query_fault_causes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Query sample Fault -> Symptom -> Cause paths for verification.

        Traverses the ontology fault chain (the task-8 seed labels/rels):
        ``Fault -HAS_SYMPTOM-> Symptom -HAS_CAUSE-> Cause``.
        """
        cypher = (
            "MATCH (f:Fault)-[:HAS_SYMPTOM]->(s:Symptom)-[:HAS_CAUSE]->(c:Cause) "
            "RETURN f.error_code AS error_code, s.name AS symptom, c.name AS cause "
            "LIMIT $limit"
        )
        return await self.query(cypher, {"limit": limit})

    async def health(self) -> dict[str, Any]:
        """Check FalkorDB reachability via a breaker-protected Redis PING.

        Returns:
            ``{"status": "healthy", "backend": "falkordb"}`` when PING answers.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN after failures.
            Exception: Any connection error (callers map this to a 503 —
                the app still boots without a reachable graph).
        """
        client = self._get_client()

        async def _do_ping() -> dict[str, Any]:
            await client.connection.ping()
            return {"status": "healthy", "backend": "falkordb"}

        return await self._breaker.call(_do_ping)


__all__ = ["FalkorDBGraphService", "CircuitBreakerOpenError"]
