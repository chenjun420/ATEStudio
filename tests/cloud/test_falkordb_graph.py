"""Tests for the FalkorDB FMEA knowledge graph service, seeder, and API.

Uses an in-memory FAKE FalkorDB double (FakeFalkorDB/FakeGraph) that mimics
the real ``falkordb.asyncio`` surface — positional ``QueryResult`` objects
(``header`` + ``result_set``), ``query``/``ro_query``/``execute_command``,
and a Redis ``connection.ping()`` — so no external FalkorDB/Redis is
required by default. A single ``@pytest.mark.integration`` test probes a
real FalkorDB at ``redis://localhost:6379`` and skips fast when absent.

The autouse ``_dev_mode_bypass`` fixture from conftest.py bypasses auth.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ate_cloud.api.v1.faults import _get_graph_service
from ate_cloud.api.v1.faults import router as faults_router
from ate_cloud.services.falkordb_graph_service import FalkorDBGraphService

# ── Fake FalkorDB double ──────────────────────────────────────────────────


class FakeQueryResult:
    """Mimics ``falkordb.asyncio.query_result.QueryResult``.

    FalkorDB results are POSITIONAL: ``header`` is a list of
    ``[column_name, type_code]`` pairs and ``result_set`` is a list of
    positional rows — never list[dict].
    """

    def __init__(self, header: list[list[Any]], result_set: list[list[Any]]) -> None:
        self.header: list[list[Any]] = header
        self.result_set: list[list[Any]] = result_set


class FakeGraph:
    """Mimics ``falkordb.asyncio.graph.AsyncGraph``."""

    def __init__(self, name: str) -> None:
        self.name = name
        # Recorded traffic for assertions.
        self.queries: list[tuple[str, dict[str, Any] | None, bool]] = []
        self.commands: list[tuple[Any, ...]] = []
        # Configurable behavior.
        self.side_effect: Exception | None = None
        self.duplicate_ddl_error: bool = False

    async def query(
        self, q: str, params: dict[str, Any] | None = None, timeout: int | None = None
    ) -> FakeQueryResult:
        return await self._run(q, params, read_only=False)

    async def ro_query(
        self, q: str, params: dict[str, Any] | None = None, timeout: int | None = None
    ) -> FakeQueryResult:
        return await self._run(q, params, read_only=True)

    async def execute_command(self, *args: Any) -> FakeQueryResult:
        if self.side_effect is not None:
            raise self.side_effect
        if self.duplicate_ddl_error and args[:2] == ("GRAPH.CONSTRAINT", "CREATE"):
            from redis.exceptions import ResponseError

            raise ResponseError("Constraint already exists")
        self.commands.append(args)
        return FakeQueryResult([], [])

    async def _run(self, q: str, params: dict[str, Any] | None, *, read_only: bool) -> FakeQueryResult:
        if self.side_effect is not None:
            raise self.side_effect
        if self.duplicate_ddl_error and "CREATE INDEX" in q:
            from redis.exceptions import ResponseError

            raise ResponseError("Index already exists")
        self.queries.append((q, params, read_only))
        # Canned count responses, mirroring the seeder's count queries.
        if "count(n)" in q and "AS total" in q:
            return FakeQueryResult([["total", 3]], [[700]])
        if "count(r)" in q and "AS total" in q:
            return FakeQueryResult([["total", 3]], [[600]])
        return FakeQueryResult([], [])


class FakeConnection:
    """Mimics the redis.asyncio client exposed as ``FalkorDB.connection``."""

    def __init__(self) -> None:
        self.ping_error: Exception | None = None

    async def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True


class FakeFalkorDB:
    """Mimics ``falkordb.asyncio.FalkorDB`` (RESP client, port 6379)."""

    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.graphs: dict[str, FakeGraph] = {}

    def select_graph(self, graph_id: str) -> FakeGraph:
        # Memoize by graph id: the service lazily selects the same key the
        # tests inspect, so both must resolve to one shared FakeGraph.
        if graph_id not in self.graphs:
            self.graphs[graph_id] = FakeGraph(graph_id)
        return self.graphs[graph_id]


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_db() -> FakeFalkorDB:
    return FakeFalkorDB()


@pytest.fixture
async def graph_service(fake_db: FakeFalkorDB) -> FalkorDBGraphService:
    """A FalkorDBGraphService bound to the in-memory fake client."""
    return FalkorDBGraphService(
        url="redis://localhost:6379",
        graph_name="fmea",
        client=fake_db,  # type: ignore[arg-type]
    )


@pytest.fixture
def fake_graph(graph_service: FalkorDBGraphService, fake_db: FakeFalkorDB) -> FakeGraph:
    """The FakeGraph the service selects on first use (lazy selection)."""
    return fake_db.select_graph("fmea")


@pytest.fixture
async def app_with_faults(graph_service: FalkorDBGraphService) -> AsyncGenerator[FastAPI, None]:
    """FastAPI app with faults_router and the fake-backed graph service."""
    from ate_cloud.main import create_app

    app = create_app()
    app.dependency_overrides[_get_graph_service] = lambda: graph_service
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def faults_client(app_with_faults: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app_with_faults)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── FalkorDBGraphService Tests ────────────────────────────────────────────


class TestFalkorDBGraphService:
    """Tests for FalkorDBGraphService async graph operations."""

    async def test_query_maps_positional_rows_to_dicts(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """read returning positional rows yields list[dict] keyed by header."""
        # Script a positional FalkorDB result: header [name,type] pairs + rows.
        async def scripted_query(
            q: str, params: dict[str, Any] | None = None, timeout: int | None = None
        ) -> FakeQueryResult:
            fake_graph.queries.append((q, params, True))
            return FakeQueryResult(
                [["symptom", 2], ["cause", 2]],
                [["I2C bus failure", "Pull-up too large"], ["SPI mode error", "CPOL mismatch"]],
            )

        fake_graph.ro_query = scripted_query  # type: ignore[method-assign]
        result = await graph_service.query(
            "MATCH (s:FaultSymptom)-[:HAS_CAUSE]->(c:Cause) "
            "RETURN s.name AS symptom, c.name AS cause"
        )
        assert result == [
            {"symptom": "I2C bus failure", "cause": "Pull-up too large"},
            {"symptom": "SPI mode error", "cause": "CPOL mismatch"},
        ]

    async def test_query_decodes_bytes_header(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """Bytes header names (decode_responses=False) are decoded to str."""
        async def scripted_query(
            q: str, params: dict[str, Any] | None = None, timeout: int | None = None
        ) -> FakeQueryResult:
            fake_graph.queries.append((q, params, True))
            return FakeQueryResult([[b"total", 3]], [[42]])

        fake_graph.ro_query = scripted_query  # type: ignore[method-assign]
        result = await graph_service.query("MATCH (n) RETURN count(n) AS total")
        assert result == [{"total": 42}]

    async def test_query_empty_result(self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph) -> None:
        """An empty header/result set maps to an empty list (never dict-key access)."""
        result = await graph_service.query("MATCH (n:Missing) RETURN n.name AS name")
        assert result == []

    async def test_query_passes_params_through(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """query() forwards bound params to the FalkorDB graph call."""
        await graph_service.query("RETURN 1 AS ok WHERE 1 = $one", {"one": 1})
        assert fake_graph.queries[0][1] == {"one": 1}

    async def test_read_uses_ro_query(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """query() dispatches to the read-only ro_query path."""
        await graph_service.query("MATCH (n) RETURN n LIMIT 1")
        assert fake_graph.queries[0][2] is True  # read_only=True

    async def test_write_uses_query_and_returns_rows(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """write() dispatches to the read-write query path and maps rows."""
        async def scripted_write(
            q: str, params: dict[str, Any] | None = None, timeout: int | None = None
        ) -> FakeQueryResult:
            fake_graph.queries.append((q, params, False))
            return FakeQueryResult([["created", 3]], [[7]])

        fake_graph.query = scripted_write  # type: ignore[method-assign]
        result = await graph_service.write("CREATE (n:Test) RETURN count(n) AS created")
        assert result == [{"created": 7}]
        assert fake_graph.queries[0][2] is False  # read_only=False

    async def test_count_nodes(self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph) -> None:
        """count_nodes() reads the positional count via the mapped dict row."""
        count = await graph_service.count_nodes()
        assert count == 700

    async def test_count_relationships(self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph) -> None:
        """count_relationships() reads the positional count via the mapped dict row."""
        count = await graph_service.count_relationships()
        assert count == 600

    async def test_create_constraints_emits_falkordb_ddl(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """create_constraints emits CREATE INDEX + GRAPH.CONSTRAINT CREATE, not Neo4j syntax."""
        await graph_service.create_constraints()

        index_stmts = [q for (q, _p, _ro) in fake_graph.queries if "CREATE INDEX" in q]
        # 11 ontology node types → 11 range indexes (all keyed on stable `id`).
        assert len(index_stmts) == 11
        for stmt in index_stmts:
            assert "CREATE INDEX" in stmt
            # Neo4j constraint syntax must never appear.
            assert "IS UNIQUE" not in stmt
            assert "REQUIRE" not in stmt
        # Labels covered (the ontology vocabulary; legacy FaultSymptom/ErrorCode gone):
        joined = " ".join(index_stmts)
        for label in (
            "Fault", "Symptom", "Cause", "Solution", "Component",
            "Product", "Instrument", "TestRequirement", "TestCase",
            "TestStep", "UUTResult",
        ):
            assert f":{label}" in joined
        # Every ontology node type is unique/indexed on the stable `id`.
        assert "e.id" in joined
        assert "FaultSymptom" not in joined
        assert "ErrorCode" not in joined

        # 11 uniqueness constraints via GRAPH.CONSTRAINT CREATE.
        constraint_calls = fake_graph.commands
        assert len(constraint_calls) == 11
        for args in constraint_calls:
            assert args[0] == "GRAPH.CONSTRAINT"
            assert args[1] == "CREATE"
            assert "UNIQUE" in args
            assert "NODE" in args
        # Graph key is forwarded to the DDL.
        assert all(call[2] == "fmea" for call in constraint_calls)

    async def test_create_constraints_is_idempotent(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """Re-running create_constraints tolerates already-exists DDL errors."""
        from redis.exceptions import ResponseError

        await graph_service.create_constraints()
        # Second run: server reports index/constraint already exist.
        fake_graph.duplicate_ddl_error = True
        await graph_service.create_constraints()  # must not raise

        # A non-exists error still propagates.
        fake_graph.duplicate_ddl_error = False
        fake_graph.side_effect = ResponseError("boom: real failure")
        with pytest.raises(ResponseError, match="boom"):
            await graph_service.create_constraints()

    async def test_health_ok_on_ping(self, graph_service: FalkorDBGraphService, fake_db: FakeFalkorDB) -> None:
        """health() returns healthy when Redis PING succeeds."""
        info = await graph_service.health()
        assert info == {"status": "healthy", "backend": "falkordb"}
        # PING goes through the Redis connection.
        assert fake_db.connection is not None

    async def test_health_down_on_connection_error(
        self, graph_service: FalkorDBGraphService, fake_db: FakeFalkorDB,
    ) -> None:
        """health() raises when PING fails (callers map to 503)."""
        fake_db.connection.ping_error = ConnectionError("connection refused")
        with pytest.raises(ConnectionError, match="connection refused"):
            await graph_service.health()

    async def test_circuit_breaker_property(self, graph_service: FalkorDBGraphService) -> None:
        """circuit_breaker property exposes the CircuitBreaker instance."""
        from ate_platform.common.circuit_breaker import CircuitBreaker, CircuitState

        breaker = graph_service.circuit_breaker
        assert isinstance(breaker, CircuitBreaker)
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_query_failure_increments_breaker(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """A query failure increments the circuit breaker failure count."""
        fake_graph.side_effect = RuntimeError("FalkorDB connection lost")
        with pytest.raises(RuntimeError, match="FalkorDB connection lost"):
            await graph_service.query("MATCH (n) RETURN n")
        assert graph_service.circuit_breaker.failure_count == 1

    async def test_circuit_opens_after_threshold(
        self, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """After failure_threshold failures the circuit opens and rejects calls."""
        from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

        fake_graph.side_effect = RuntimeError("FalkorDB unreachable")
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await graph_service.query("MATCH (n) RETURN n")
        # 6th call is rejected by the open circuit without touching the fake.
        calls_before = len(fake_graph.queries)
        with pytest.raises(CircuitBreakerOpenError):
            await graph_service.query("MATCH (n) RETURN n")
        assert len(fake_graph.queries) == calls_before

    async def test_construction_is_lazy(self) -> None:
        """Constructing the service does NOT connect (app boots without FalkorDB).

        The real client is created lazily on first use, so an unreachable
        server never breaks app import/boot — only graph calls fail.
        """
        service = FalkorDBGraphService(url="redis://unreachable:6379", graph_name="fmea")
        assert service._client is None
        assert service._graph is None

    async def test_uses_resp_6379_not_bolt(self) -> None:
        """The service imports falkordb.asyncio (RESP); no Bolt/7687 shim exists."""
        import falkordb.asyncio  # noqa: F401

        with pytest.raises(ModuleNotFoundError):
            import neo4j  # noqa: F401
        with pytest.raises(ModuleNotFoundError):
            import langchain_neo4j  # noqa: F401


# ── KGSeeder (ontology-aligned) is tested in test_kg_seeder.py ────────────
# The seeder now persists ontology entities/relationships via GraphService
# UNWIND/MERGE on stable ids; its data/idempotency/alignment tests live there
# against an in-memory fake GraphService. This module keeps the FalkorDB
# adapter tests and the /faults/seed endpoint contract tests below.


# ── Faults API Tests ──────────────────────────────────────────────────────


class TestFaultsAPI:
    """Tests for POST /api/v1/faults/seed endpoint."""

    async def test_seed_endpoint_returns_counts(self, faults_client: AsyncClient) -> None:
        response = await faults_client.post("/api/v1/faults/seed")
        assert response.status_code == 200
        data = response.json()
        assert "nodes_created" in data
        assert "relationships_created" in data
        assert isinstance(data["nodes_created"], int)
        assert isinstance(data["relationships_created"], int)

    async def test_seed_endpoint_returns_503_on_circuit_open(
        self, app_with_faults: FastAPI, graph_service: FalkorDBGraphService,
    ) -> None:
        """POST /faults/seed returns 503 when the circuit breaker is open."""
        breaker = graph_service.circuit_breaker
        for _ in range(5):
            await breaker._on_failure()

        transport = ASGITransport(app=app_with_faults)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/seed")
            assert response.status_code == 503
            assert "circuit breaker" in response.json()["detail"].lower()

    async def test_seed_endpoint_returns_502_on_graph_error(
        self, app_with_faults: FastAPI, graph_service: FalkorDBGraphService, fake_graph: FakeGraph,
    ) -> None:
        """POST /faults/seed returns 502 on a non-breaker FalkorDB operation failure."""
        await graph_service.circuit_breaker.reset()
        fake_graph.side_effect = RuntimeError("FalkorDB query failed")

        transport = ASGITransport(app=app_with_faults)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/faults/seed")
            assert response.status_code == 502
            assert "seed fault graph" in response.json()["detail"].lower()


# ── Router registration ───────────────────────────────────────────────────


class TestRouterRegistration:
    def test_faults_router_has_correct_prefix_and_tags(self) -> None:
        assert faults_router.prefix == "/faults"
        assert "faults" in faults_router.tags

    def test_faults_router_has_seed_endpoint(self) -> None:
        routes = {r.path: r for r in faults_router.routes}
        assert "/faults/seed" in routes
        seed_route = routes["/faults/seed"]
        assert "POST" in seed_route.methods  # type: ignore[union-attr]


# ── Real-service integration test (skipped without FalkorDB) ──────────────


@pytest.mark.integration
async def test_real_falkordb_integration() -> None:
    """Connect to a real FalkorDB at redis://localhost:6379 (RESP).

    Skipped fast when no server answers PING. Run with a local instance:
        docker run --rm -p 6379:6379 falkordb/falkordb
    """
    service = FalkorDBGraphService(url="redis://localhost:6379", graph_name="fmea_test")
    try:
        info = await service.health()
    except Exception as e:  # noqa: BLE001 - any connection failure → skip
        pytest.skip(f"no FalkorDB at redis://localhost:6379: {e}")
    assert info["backend"] == "falkordb"

    await service.create_constraints()
    probe_id = "fault:__probe__"
    await service.write(
        "MERGE (f:Fault {id: $id}) SET f.name = $name",
        {"id": probe_id, "name": "__probe_fault__"},
    )
    rows = await service.query(
        "MATCH (f:Fault) WHERE f.id = $id RETURN f.name AS name",
        {"id": probe_id},
    )
    assert rows and rows[0]["name"] == "__probe_fault__"
    assert await service.count_nodes() >= 1
