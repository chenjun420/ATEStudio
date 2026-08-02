"""Tests for Neo4j FMEA knowledge graph service, seeder, and API.

Uses mocked Neo4j driver (via patching langchain_neo4j.Neo4jGraph) —
no real Neo4j instance required. The autouse ``_dev_mode_bypass``
fixture from conftest.py bypasses auth.
"""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ate_cloud.api.v1.faults import _get_graph_service
from ate_cloud.api.v1.faults import router as faults_router
from ate_cloud.services.kg_seeder import (
    CAT_ASSEMBLY,
    CAT_COMM,
    CAT_ENVIRONMENT,
    CAT_MIXED,
    CAT_PASSIVE,
    CAT_POWER,
    FaultRecord,
    KGSeeder,
)
from ate_cloud.services.neo4j_graph_service import Neo4jGraphService

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_graph() -> MagicMock:
    """A MagicMock simulating langchain_neo4j.Neo4jGraph.

    The ``query`` method is a sync MagicMock (Neo4jGraph.query is sync,
    bridged to async via asyncio.to_thread in Neo4jGraphService).
    """
    graph = MagicMock()
    graph.query.return_value = []
    return graph


@pytest.fixture
async def graph_service(mock_graph: MagicMock) -> Neo4jGraphService:
    """Create a Neo4jGraphService with a mocked internal Neo4jGraph.

    Patches the langchain_neo4j.Neo4jGraph constructor so __init__
    creates a mock instead of a real driver.
    """
    with patch("langchain_neo4j.Neo4jGraph") as mock_neo4j_cls:
        mock_neo4j_cls.return_value = mock_graph
        service = Neo4jGraphService(
            url="bolt://localhost:7687",
            password="test-password",
        )
    return service


@pytest.fixture
async def app_with_faults(graph_service: Neo4jGraphService) -> AsyncGenerator[FastAPI, None]:
    """Create a FastAPI app with faults_router and mocked graph service.

    Uses the same create_app() as production but overrides the
    _get_graph_service dependency to return the mocked service.
    """
    from ate_cloud.main import create_app

    app = create_app()
    # Override the graph service dependency
    app.dependency_overrides[_get_graph_service] = lambda: graph_service
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def faults_client(app_with_faults: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client targeting the faults API with mocked Neo4j."""
    transport = ASGITransport(app=app_with_faults)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Neo4jGraphService Tests ───────────────────────────────────────────────


class TestNeo4jGraphService:
    """Tests for Neo4jGraphService async graph operations."""

    async def test_query_returns_results(self, graph_service: Neo4jGraphService) -> None:
        """query() returns list of dicts from Neo4jGraph.query."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"name": "test", "count": 42}]
        result = await graph_service.query("MATCH (n) RETURN n.name AS name, count(n) AS count")
        assert result == [{"name": "test", "count": 42}]
        mock_graph.query.assert_called_once_with("MATCH (n) RETURN n.name AS name, count(n) AS count", {})

    async def test_query_with_params(self, graph_service: Neo4jGraphService) -> None:
        """query() passes params to Neo4jGraph.query."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"total": 5}]
        await graph_service.query("MATCH (n) RETURN count(n) AS total", {"limit": 5})
        mock_graph.query.assert_called_once_with("MATCH (n) RETURN count(n) AS total", {"limit": 5})

    async def test_query_empty_params_default(self, graph_service: Neo4jGraphService) -> None:
        """query() defaults params to empty dict when None."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = []
        await graph_service.query("MATCH (n) RETURN n")
        mock_graph.query.assert_called_once_with("MATCH (n) RETURN n", {})

    async def test_write_is_alias_for_query(self, graph_service: Neo4jGraphService) -> None:
        """write() delegates to query() with same semantics."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"created": 1}]
        result = await graph_service.write("CREATE (n:Test) RETURN count(n) AS created")
        assert result == [{"created": 1}]
        assert mock_graph.query.call_count == 1

    async def test_count_nodes(self, graph_service: Neo4jGraphService) -> None:
        """count_nodes() returns total node count from graph."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"total": 42}]
        count = await graph_service.count_nodes()
        assert count == 42

    async def test_count_nodes_empty_graph(self, graph_service: Neo4jGraphService) -> None:
        """count_nodes() returns 0 when graph is empty."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"total": 0}]
        count = await graph_service.count_nodes()
        assert count == 0

    async def test_count_nodes_empty_result(self, graph_service: Neo4jGraphService) -> None:
        """count_nodes() returns 0 when query returns no results."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = []
        count = await graph_service.count_nodes()
        assert count == 0

    async def test_count_relationships(self, graph_service: Neo4jGraphService) -> None:
        """count_relationships() returns total relationship count."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"total": 120}]
        count = await graph_service.count_relationships()
        assert count == 120

    async def test_create_constraints(self, graph_service: Neo4jGraphService) -> None:
        """create_constraints() executes 7 uniqueness constraint statements."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = []
        await graph_service.create_constraints()
        assert mock_graph.query.call_count == 7
        # Verify each call contains "CREATE CONSTRAINT" and "IF NOT EXISTS"
        for call_args in mock_graph.query.call_args_list:
            cypher: str = call_args[0][0]
            assert "CREATE CONSTRAINT" in cypher
            assert "IF NOT EXISTS" in cypher
            assert "IS UNIQUE" in cypher

    async def test_query_fault_causes(self, graph_service: Neo4jGraphService) -> None:
        """query_fault_causes() returns symptom→cause pairs."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [
            {"symptom": "I2C bus failure", "cause": "Pull-up too large"},
            {"symptom": "SPI mode error", "cause": "CPOL mismatch"},
        ]
        results = await graph_service.query_fault_causes(limit=5)
        assert len(results) == 2
        assert results[0]["symptom"] == "I2C bus failure"
        # Verify the Cypher contains the MATCH pattern and LIMIT
        call_args = mock_graph.query.call_args
        cypher: str = call_args[0][0]
        assert "HAS_CAUSE" in cypher
        assert call_args[0][1] == {"limit": 5}

    async def test_circuit_breaker_property(self, graph_service: Neo4jGraphService) -> None:
        """circuit_breaker property exposes the CircuitBreaker instance."""
        from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitState

        breaker = graph_service.circuit_breaker
        assert isinstance(breaker, CircuitBreaker)
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_query_failure_increments_breaker(self, graph_service: Neo4jGraphService) -> None:
        """A query failure increments the circuit breaker failure count."""
        mock_graph = graph_service._graph
        mock_graph.query.side_effect = RuntimeError("Neo4j connection lost")
        with pytest.raises(RuntimeError, match="Neo4j connection lost"):
            await graph_service.query("MATCH (n) RETURN n")
        assert graph_service.circuit_breaker.failure_count == 1

    async def test_query_failure_opens_circuit_after_threshold(
        self, graph_service: Neo4jGraphService,
    ) -> None:
        """After 5 failures, the circuit opens and rejects calls."""
        from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

        mock_graph = graph_service._graph
        mock_graph.query.side_effect = RuntimeError("Neo4j unreachable")
        # First 5 calls raise the underlying error
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await graph_service.query("MATCH (n) RETURN n")
        # 6th call is rejected by the open circuit
        with pytest.raises(CircuitBreakerOpenError):
            await graph_service.query("MATCH (n) RETURN n")

    async def test_constructor_patches_neo4j_graph(self) -> None:
        """Neo4jGraphService.__init__ creates a Neo4jGraph with correct params."""
        with patch("langchain_neo4j.Neo4jGraph") as mock_cls:
            mock_cls.return_value = MagicMock()
            service = Neo4jGraphService(
                url="bolt://test:7687",
                password="secret",
                username="neo4j",
                database="testdb",
            )
            mock_cls.assert_called_once_with(
                url="bolt://test:7687",
                username="neo4j",
                password="secret",
                database="testdb",
                refresh_schema=False,
            )
            assert service.circuit_breaker.failure_count == 0


# ── KGSeeder Tests ────────────────────────────────────────────────────────


class TestKGSeeder:
    """Tests for KGSeeder fault record generation and seeding."""

    def test_record_count_exceeds_100(self, graph_service: Neo4jGraphService) -> None:
        """KGSeeder provides 100+ fault records."""
        seeder = KGSeeder(graph_service)
        assert seeder.record_count >= 100

    def test_records_are_fault_record_type(self, graph_service: Neo4jGraphService) -> None:
        """All records are FaultRecord instances."""
        seeder = KGSeeder(graph_service)
        for record in seeder.records:
            assert isinstance(record, FaultRecord)

    def test_records_cover_all_six_categories(self, graph_service: Neo4jGraphService) -> None:
        """Records cover all 6 FMEA categories."""
        seeder = KGSeeder(graph_service)
        categories = {r.category for r in seeder.records}
        assert categories == {
            CAT_COMM, CAT_POWER, CAT_ASSEMBLY,
            CAT_PASSIVE, CAT_ENVIRONMENT, CAT_MIXED,
        }

    def test_each_category_has_at_least_15_records(self, graph_service: Neo4jGraphService) -> None:
        """Each of the 6 categories has at least 15 records."""
        seeder = KGSeeder(graph_service)
        from collections import Counter

        counts = Counter(r.category for r in seeder.records)
        for cat in [CAT_COMM, CAT_POWER, CAT_ASSEMBLY, CAT_PASSIVE, CAT_ENVIRONMENT, CAT_MIXED]:
            assert counts[cat] >= 15, f"Category {cat} has only {counts[cat]} records"

    def test_records_have_chinese_and_english_descriptions(self, graph_service: Neo4jGraphService) -> None:
        """Each record has non-empty Chinese and English symptom/cause/solution."""
        seeder = KGSeeder(graph_service)
        for record in seeder.records:
            assert record.symptom_zh, f"Empty symptom_zh for {record.symptom_en}"
            assert record.symptom_en, f"Empty symptom_en for {record.symptom_zh}"
            assert record.cause_zh, f"Empty cause_zh for {record.cause_en}"
            assert record.cause_en, f"Empty cause_en for {record.cause_zh}"
            assert record.solution_zh, f"Empty solution_zh for {record.solution_en}"
            assert record.solution_en, f"Empty solution_en for {record.solution_zh}"

    def test_records_have_error_codes(self, graph_service: Neo4jGraphService) -> None:
        """Each record has a non-empty error code."""
        seeder = KGSeeder(graph_service)
        for record in seeder.records:
            assert record.error_code, f"Empty error_code for {record.symptom_en}"

    def test_records_have_unique_error_codes(self, graph_service: Neo4jGraphService) -> None:
        """All error codes are unique across all records."""
        seeder = KGSeeder(graph_service)
        codes = [r.error_code for r in seeder.records]
        assert len(codes) == len(set(codes)), "Duplicate error codes found"

    def test_records_have_component_and_product(self, graph_service: Neo4jGraphService) -> None:
        """Each record has non-empty component, component_type, product_type, instrument."""
        seeder = KGSeeder(graph_service)
        for record in seeder.records:
            assert record.component, f"Empty component for {record.symptom_en}"
            assert record.component_type, f"Empty component_type for {record.symptom_en}"
            assert record.product_type, f"Empty product_type for {record.symptom_en}"
            assert record.instrument, f"Empty instrument for {record.symptom_en}"

    async def test_seed_all_creates_constraints_and_records(
        self, graph_service: Neo4jGraphService,
    ) -> None:
        """seed_all() creates constraints then MERGEs all fault records."""
        mock_graph = graph_service._graph
        # Track query calls to return appropriate counts
        call_count = [0]

        def mock_query(cypher: str, params: dict = None) -> list[dict[str, Any]]:
            call_count[0] += 1
            # Constraints (first 7 calls) return empty
            if "CREATE CONSTRAINT" in cypher:
                return []
            # count_nodes query
            if "count(n)" in cypher and "AS total" in cypher:
                return [{"total": 700}]
            # count_relationships query
            if "count(r)" in cypher and "AS total" in cypher:
                return [{"total": 600}]
            # MERGE seed queries return empty
            return []

        mock_graph.query.side_effect = mock_query

        seeder = KGSeeder(graph_service)
        result = await seeder.seed_all()

        # 7 constraints + N seed queries + 2 count queries
        expected_seed_queries = seeder.record_count
        total_queries = 7 + expected_seed_queries + 2
        assert mock_graph.query.call_count == total_queries
        assert result["nodes_created"] == 700
        assert result["relationships_created"] == 600

    async def test_seed_all_is_idempotent(self, graph_service: Neo4jGraphService) -> None:
        """seed_all() can be called multiple times safely (MERGE is idempotent)."""
        mock_graph = graph_service._graph
        mock_graph.query.return_value = [{"total": 100}]

        seeder = KGSeeder(graph_service)
        await seeder.seed_all()
        first_call_count = mock_graph.query.call_count
        await seeder.seed_all()
        second_call_count = mock_graph.query.call_count
        # Second run should make the same number of calls
        assert second_call_count == first_call_count * 2

    def test_seed_cypher_contains_all_node_types(self) -> None:
        """The seed Cypher creates all 7 FMEA node types."""
        from ate_cloud.services.kg_seeder import _SEED_CYPHER

        assert "FaultSymptom" in _SEED_CYPHER
        assert ":Cause" in _SEED_CYPHER
        assert ":Solution" in _SEED_CYPHER
        assert ":Component" in _SEED_CYPHER
        assert ":Product" in _SEED_CYPHER
        assert ":ErrorCode" in _SEED_CYPHER
        assert ":Instrument" in _SEED_CYPHER

    def test_seed_cypher_contains_all_relationships(self) -> None:
        """The seed Cypher creates all 6 relationship types."""
        from ate_cloud.services.kg_seeder import _SEED_CYPHER

        assert "HAS_CAUSE" in _SEED_CYPHER
        assert "HAS_SOLUTION" in _SEED_CYPHER
        assert "USES_INSTRUMENT" in _SEED_CYPHER
        assert "AFFECTS_COMPONENT" in _SEED_CYPHER
        assert "OCCURS_IN_PRODUCT" in _SEED_CYPHER
        assert "TRIGGERS_ERROR_CODE" in _SEED_CYPHER


# ── Faults API Tests ──────────────────────────────────────────────────────


class TestFaultsAPI:
    """Tests for POST /api/v1/faults/seed endpoint."""

    async def test_seed_endpoint_returns_counts(self, faults_client: AsyncClient) -> None:
        """POST /faults/seed returns nodes_created and relationships_created."""
        # The faults_client uses graph_service fixture which has a mock_graph
        # Get the graph_service from the app's dependency override
        response = await faults_client.post("/api/v1/faults/seed")
        assert response.status_code == 200
        data = response.json()
        assert "nodes_created" in data
        assert "relationships_created" in data
        assert isinstance(data["nodes_created"], int)
        assert isinstance(data["relationships_created"], int)

    async def test_seed_endpoint_returns_503_on_circuit_open(
        self, app_with_faults: FastAPI, graph_service: Neo4jGraphService,
    ) -> None:
        """POST /faults/seed returns 503 when circuit breaker is open."""
        # Force the circuit breaker open
        breaker = graph_service.circuit_breaker
        for _ in range(5):
            await breaker._on_failure()

        transport = ASGITransport(app=app_with_faults)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/seed")
            assert response.status_code == 503
            assert "circuit breaker" in response.json()["detail"].lower()

    async def test_seed_endpoint_returns_502_on_neo4j_error(
        self, app_with_faults: FastAPI, graph_service: Neo4jGraphService,
    ) -> None:
        """POST /faults/seed returns 502 on Neo4j operation failure."""
        mock_graph = graph_service._graph
        # Reset circuit breaker first
        await graph_service.circuit_breaker.reset()
        # Make query fail with a non-circuit-breaker error
        mock_graph.query.side_effect = RuntimeError("Neo4j query failed")

        transport = ASGITransport(app=app_with_faults)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/seed")
            assert response.status_code == 502
            assert "seed fault graph" in response.json()["detail"].lower()

    async def test_seed_endpoint_uses_mocked_graph_service(
        self, app_with_faults: FastAPI, graph_service: Neo4jGraphService,
    ) -> None:
        """The seed endpoint uses the dependency-overridden graph service."""
        mock_graph = graph_service._graph
        await graph_service.circuit_breaker.reset()

        # Configure mock to return counts
        def mock_query(cypher: str, params: dict = None) -> list[dict[str, Any]]:
            if "count(n)" in cypher:
                return [{"total": 728}]
            if "count(r)" in cypher:
                return [{"total": 624}]
            return []

        mock_graph.query.side_effect = mock_query

        transport = ASGITransport(app=app_with_faults)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/seed")
            assert response.status_code == 200
            data = response.json()
            assert data["nodes_created"] == 728
            assert data["relationships_created"] == 624


# ── Router Registration Tests ─────────────────────────────────────────────


class TestRouterRegistration:
    """Tests verifying the faults router is properly registered."""

    def test_faults_router_has_correct_prefix_and_tags(self) -> None:
        """The faults router has prefix=/faults and tags=['faults']."""
        assert faults_router.prefix == "/faults"
        assert "faults" in faults_router.tags

    def test_faults_router_has_seed_endpoint(self) -> None:
        """The faults router has a POST /seed route."""
        routes = {r.path: r for r in faults_router.routes}
        assert "/faults/seed" in routes
        seed_route = routes["/faults/seed"]
        assert "POST" in seed_route.methods  # type: ignore[union-attr]
