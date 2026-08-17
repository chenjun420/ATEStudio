"""Neo4j Graph Service — FMEA knowledge graph operations via LangChain Neo4jGraph.

Wraps the LangChain ``Neo4jGraph`` integration (deepagents framework) with a
CircuitBreaker for resilience against transient Neo4j failures. Manages
connection authentication and provides async Cypher query/write operations.

The FMEA (Failure Mode and Effects Analysis) knowledge graph schema:

    Node types:
        FaultSymptom  — observable test failure symptom
        Cause         — root cause of a symptom
        Solution      — repair/diagnosis solution for a cause
        Component     — affected electronic component
        Product       — product type where the symptom occurs
        ErrorCode     — error code triggered by the symptom
        Instrument    — instrument used for diagnosis/repair

    Relationships:
        (FaultSymptom)-[:HAS_CAUSE]->(Cause)
        (Cause)-[:HAS_SOLUTION]->(Solution)
        (Solution)-[:USES_INSTRUMENT]->(Instrument)
        (FaultSymptom)-[:AFFECTS_COMPONENT]->(Component)
        (FaultSymptom)-[:OCCURS_IN_PRODUCT]->(Product)
        (FaultSymptom)-[:TRIGGERS_ERROR_CODE]->(ErrorCode)

Per AGENTS.md section 7: if Neo4j is configured but unreachable, the
CircuitBreaker opens and ``CircuitBreakerOpenError`` propagates — no
silent degradation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

# Default Neo4j credentials (username is always "neo4j" per docker-compose convention).
_DEFAULT_USERNAME: str = "neo4j"
_DEFAULT_DATABASE: str = "neo4j"


class Neo4jGraphService:
    """Async Neo4j graph service backed by LangChain ``Neo4jGraph``.

    Uses LangChain's ``Neo4jGraph`` wrapper (handles auth, driver lifecycle,
    and Cypher execution) plus a CircuitBreaker for cascading-failure
    protection. All operations are async — the sync ``Neo4jGraph.query``
    is bridged via ``asyncio.to_thread``.

    Args:
        url: Neo4j Bolt connection URL (e.g. ``bolt://localhost:7687``).
        password: Neo4j database password.
        username: Neo4j username (default ``neo4j``).
        database: Neo4j database name (default ``neo4j``).
    """

    def __init__(
        self,
        url: str,
        password: str,
        username: str = _DEFAULT_USERNAME,
        database: str = _DEFAULT_DATABASE,
    ) -> None:
        from langchain_neo4j import Neo4jGraph

        self._graph = Neo4jGraph(
            url=url,
            username=username,
            password=password,
            database=database,
            refresh_schema=False,
        )
        self._breaker = CircuitBreaker(
            failure_threshold=5,
            timeout=30.0,
            name="neo4j-graph",
        )

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Underlying CircuitBreaker instance (for inspection/reset)."""
        return self._breaker

    async def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query and return results.

        Runs the sync ``Neo4jGraph.query`` in a thread to avoid blocking
        the event loop. Protected by the CircuitBreaker.

        Args:
            cypher: Cypher query string.
            params: Optional query parameters.

        Returns:
            List of result dictionaries.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN after repeated failures.
            Exception: Any Neo4j query error not suppressed by the breaker.
        """
        async def _do_query() -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                self._graph.query, cypher, params or {}
            )

        return await self._breaker.call(_do_query)

    async def write(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a Cypher write statement (CREATE/MERGE/DELETE).

        Functionally identical to :meth:`query` — Neo4jGraph dispatches
        all Cypher through the same transaction. Provided as a separate
        method for semantic clarity.

        Args:
            cypher: Cypher write statement.
            params: Optional query parameters.

        Returns:
            List of result dictionaries (may be empty for pure writes).

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
            Exception: Any Neo4j write error.
        """
        return await self.query(cypher, params)

    async def create_constraints(self) -> None:
        """Create uniqueness constraints for all FMEA node types.

        Idempotent — uses ``IF NOT EXISTS``. Must be called before seeding
        to ensure MERGE operations are performant and correct.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
        """
        constraints = [
            "CREATE CONSTRAINT fault_symptom_unique IF NOT EXISTS "
            "FOR (s:FaultSymptom) REQUIRE s.name IS UNIQUE",
            "CREATE CONSTRAINT cause_unique IF NOT EXISTS "
            "FOR (c:Cause) REQUIRE c.name IS UNIQUE",
            "CREATE CONSTRAINT solution_unique IF NOT EXISTS "
            "FOR (sol:Solution) REQUIRE sol.name IS UNIQUE",
            "CREATE CONSTRAINT component_unique IF NOT EXISTS "
            "FOR (comp:Component) REQUIRE comp.name IS UNIQUE",
            "CREATE CONSTRAINT product_unique IF NOT EXISTS "
            "FOR (p:Product) REQUIRE p.name IS UNIQUE",
            "CREATE CONSTRAINT error_code_unique IF NOT EXISTS "
            "FOR (e:ErrorCode) REQUIRE e.code IS UNIQUE",
            "CREATE CONSTRAINT instrument_unique IF NOT EXISTS "
            "FOR (i:Instrument) REQUIRE i.name IS UNIQUE",
        ]
        for stmt in constraints:
            await self.write(stmt)
        logger.info("Created FMEA knowledge graph constraints (7 node types)")

    async def count_nodes(self) -> int:
        """Count total nodes in the graph.

        Returns:
            Total node count across all labels.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
        """
        results = await self.query("MATCH (n) RETURN count(n) AS total")
        if results:
            total: int = int(results[0].get("total", 0))
            return total
        return 0

    async def count_relationships(self) -> int:
        """Count total relationships in the graph.

        Returns:
            Total relationship count across all types.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
        """
        results = await self.query("MATCH ()-[r]->() RETURN count(r) AS total")
        if results:
            total: int = int(results[0].get("total", 0))
            return total
        return 0

    async def query_fault_causes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Query sample fault symptom → cause paths for verification.

        Args:
            limit: Maximum number of results.

        Returns:
            List of dicts with ``symptom`` and ``cause`` keys.

        Raises:
            CircuitBreakerOpenError: If the circuit is OPEN.
        """
        cypher = (
            "MATCH (s:FaultSymptom)-[:HAS_CAUSE]->(c:Cause) "
            "RETURN s.name AS symptom, c.name AS cause "
            "LIMIT $limit"
        )
        return await self.query(cypher, {"limit": limit})


__all__ = ["Neo4jGraphService", "CircuitBreakerOpenError"]
