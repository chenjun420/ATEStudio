"""Direct-call / fake-based coverage tests for the plan's new backend modules.

The HTTP-level suites (test_fmea_api / test_diagnose / test_knowledge_read_apis)
drive these surfaces through the ASGI router wrapper and prove behavior, but
that wrapper obscures per-line coverage attribution for the wrapped handlers
(Starlette's ``_IncludedRouter`` records the endpoint body as covered
unevenly). These tests call the thin plan-code UNITS directly — handler
functions with a real in-memory session, factory functions with a fake
Request/app.state, pure fakes for the LLM/graph/NATS seams — so every branch
(404s, 503s, the LLM rewrite path, the reconnect loop) is deterministically
exercised without any live FalkorDB/Qdrant/NATS/LLM service.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from ate_cloud.models import Base

# ── Shared fixtures / fakes ────────────────────────────────────────────────


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """One StaticPool in-memory engine shared across sessions within a test."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class _FakeRequest:
    """Minimal FastAPI Request stand-in carrying an ``app.state`` namespace."""

    def __init__(self, **state: Any) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(**state))


async def _make_fmea(session: AsyncSession, **overrides: Any) -> Any:
    from ate_cloud.models.knowledge import FMEA

    defaults: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "component_code": "PSU_MAIN",
        "failure_mode": "OVP",
        "effects": "reset",
        "cause": "drift",
        "severity": 7,
        "occurrence": 4,
        "detection": 3,
        "recommended_action": "replace",
    }
    defaults.update(overrides)
    row = FMEA(**defaults)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ── FMEA handlers (api/v1/fmea.py) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fmea_list_filters_and_404_paths(session_factory: Any) -> None:
    """list_fmeas applies both filters; get/update/delete 404 on a missing id."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import fmea as fmea_api
    from ate_cloud.schemas.knowledge import FMEAUpdate

    async with session_factory() as s:
        await _make_fmea(s, component_code="PSU", fault_code="over_voltage")
        await _make_fmea(s, component_code="DMM", fault_code="signal_loss")

        # list with BOTH filters present (skip/limit are FastAPI Query defaults
        # in the app, so pass concrete ints when calling the handler directly).
        listing = await fmea_api.list_fmeas(s, "PSU", "over_voltage", 0, 100)
        assert listing["total"] == 1
        # list with no filters
        all_listing = await fmea_api.list_fmeas(s, None, None, 0, 100)
        assert all_listing["total"] == 2

        # get one missing -> 404
        with pytest.raises(HTTPException) as exc:
            await fmea_api.get_fmea("missing", s)
        assert exc.value.status_code == 404

        # update missing -> 404
        with pytest.raises(HTTPException) as exc:
            await fmea_api.update_fmea("missing", FMEAUpdate(severity=2), s)
        assert exc.value.status_code == 404

        # delete missing -> 404
        with pytest.raises(HTTPException) as exc:
            await fmea_api.delete_fmea("missing", s)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_fmea_update_and_delete_happy_paths(session_factory: Any) -> None:
    """update_fmea recomputes rpn server-side; delete_fmea removes the row."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import fmea as fmea_api
    from ate_cloud.schemas.knowledge import FMEAUpdate

    async with session_factory() as s:
        row = await _make_fmea(s)
        fid = row.id

        updated = await fmea_api.update_fmea(fid, FMEAUpdate(severity=10), s)
        assert updated.severity == 10
        assert updated.rpn == 120  # 10*4*3, recomputed server-side

        await fmea_api.delete_fmea(fid, s)
        with pytest.raises(HTTPException) as exc:
            await fmea_api.get_fmea(fid, s)
        assert exc.value.status_code == 404


# ── QueryRewriter LLM path (services/query_rewrite.py) ─────────────────────


class _FakeBreaker:
    """CircuitBreaker stand-in: awaits the coroutine, optionally raises open."""

    def __init__(self, *, open_: bool = False) -> None:
        self._open = open_

    async def call(self, fn: Any) -> Any:
        from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

        if self._open:
            raise CircuitBreakerOpenError("open")
        return await fn()


class _FakePrompt:
    def format_messages(self, **_kw: Any) -> list[object]:
        return [{"role": "user", "content": "rewritten"}]


class _FakeLLM:
    def __init__(self, *, content: str = "I2C bus fault rewritten", raises: bool = False) -> None:
        self._content = content
        self._raises = raises

    async def ainvoke(self, _messages: Any) -> Any:
        if self._raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=self._content)


def _make_rewriter(*, open_: bool = False, content: str = "I2C bus fault rewritten",
                  raises: bool = False) -> Any:
    from ate_cloud.services.query_rewrite import QueryRewriter

    rewriter = QueryRewriter(api_key="k", model="m", breaker=_FakeBreaker(open_=open_))
    rewriter._prompt = _FakePrompt()
    rewriter._llm = _FakeLLM(content=content, raises=raises)
    rewriter._initialized = True
    return rewriter


@pytest.mark.asyncio
async def test_query_rewriter_uses_llm_result_when_configured() -> None:
    """With an api_key + a healthy LLM, rewrite() returns the LLM text."""
    rewriter = _make_rewriter()
    out = await rewriter.rewrite("I2C timeout")
    assert out == "I2C bus fault rewritten"


@pytest.mark.asyncio
async def test_query_rewriter_empty_llm_falls_back_to_dictionary() -> None:
    """An empty/whitespace LLM answer falls back to the dictionary expansion."""
    rewriter = _make_rewriter(content="   ")
    out = await rewriter.rewrite("I2C timeout")
    assert "Inter-Integrated Circuit" in out


@pytest.mark.asyncio
async def test_query_rewriter_breaker_open_falls_back() -> None:
    """An open circuit breaker returns the dictionary-expanded query."""
    rewriter = _make_rewriter(open_=True)
    out = await rewriter.rewrite("SPI failure")
    assert "Serial Peripheral Interface" in out


@pytest.mark.asyncio
async def test_query_rewriter_llm_exception_falls_back() -> None:
    """Any LLM exception is swallowed and the dictionary query is returned."""
    rewriter = _make_rewriter(raises=True)
    out = await rewriter.rewrite("ESD damage")
    assert "Electrostatic Discharge" in out


@pytest.mark.asyncio
async def test_query_rewriter_no_key_skips_llm() -> None:
    """Without an api_key the LLM is never touched; dictionary expansion wins."""
    from ate_cloud.services.query_rewrite import QueryRewriter

    rewriter = QueryRewriter(api_key=None, model=None, breaker=_FakeBreaker())
    rewriter._api_key = ""  # force retrieval-only regardless of ambient settings
    rewriter._llm = _FakeLLM(raises=True)  # would blow up if ever called
    rewriter._initialized = True
    out = await rewriter.rewrite("JTAG chain broken")
    assert "Joint Test Action Group" in out


# ── Diagnose factories + feedback (api/v1/diagnose.py) ─────────────────────


def test_diagnose_factories_cache_hits_and_qdrant_503() -> None:
    """Cached services return as-is; a missing qdrant client is a 503."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import diagnose as diag

    sentinel = object()
    assert diag._get_graph_service(_FakeRequest(graph_service=sentinel)) is sentinel
    assert diag._get_embedding_service(_FakeRequest(embedding_service="emb")) == "emb"
    assert diag._get_qdrant_client(_FakeRequest(qdrant_client="qd")) == "qd"
    assert diag._get_hybrid_retriever(_FakeRequest(hybrid_retriever="hr"), "e", "g", "q") == "hr"
    assert diag._get_diagnosis_service(_FakeRequest(diagnosis_service="ds"), "r") == "ds"

    with pytest.raises(HTTPException) as exc:
        diag._get_qdrant_client(_FakeRequest())
    assert exc.value.status_code == 503


def test_diagnose_graph_factory_construction_failure_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FalkorDBGraphService construction failure is mapped to a 503."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import diagnose as diag

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("bad url")

    monkeypatch.setattr(diag, "FalkorDBGraphService", _boom)
    with pytest.raises(HTTPException) as exc:
        diag._get_graph_service(_FakeRequest())
    assert exc.value.status_code == 503


def test_diagnose_embedding_factory_construction_failure_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EmbeddingService construction failure is mapped to a 503."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import diagnose as diag

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ValueError("no key")

    monkeypatch.setattr(diag, "EmbeddingService", _boom)
    with pytest.raises(HTTPException) as exc:
        diag._get_embedding_service(_FakeRequest())
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_diagnose_feedback_handler_400_and_404(session_factory: Any) -> None:
    """record_feedback: invalid feedback -> 400; unknown id -> 404."""
    from fastapi import HTTPException

    from ate_cloud.api.v1.diagnose import FeedbackRequest, record_feedback

    async with session_factory() as s:
        # invalid feedback word -> 400 (before touching DB)
        with pytest.raises(HTTPException) as exc:
            await record_feedback("x", FeedbackRequest(feedback="maybe"), s)
        assert exc.value.status_code == 400

        # valid word but unknown id -> 404
        with pytest.raises(HTTPException) as exc:
            await record_feedback("no-such-id", FeedbackRequest(feedback="confirmed"), s)
        assert exc.value.status_code == 404


# ── Knowledge factories + extract error mapping (api/v1/knowledge.py) ──────


class _BoomGraph:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        raise ValueError("graph down")


def test_knowledge_graph_factory_degrade_and_require_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_graph_service caches and degrades to None; require_graph_service 503s."""
    from fastapi import HTTPException

    import ate_cloud.services.falkordb_graph_service as fg
    from ate_cloud.api.v1 import knowledge as know

    sentinel = object()
    assert know.get_graph_service(_FakeRequest(graph_service=sentinel)) is sentinel

    monkeypatch.setattr(fg, "FalkorDBGraphService", _BoomGraph)
    req = _FakeRequest()
    assert know.get_graph_service(req) is None  # degrade for ORM-only extraction
    with pytest.raises(HTTPException) as exc:
        know.require_graph_service(req)
    assert exc.value.status_code == 503


def test_knowledge_get_extraction_service_builds_with_graph() -> None:
    """get_extraction_service wires the (possibly None) graph into the service."""
    from ate_cloud.api.v1 import knowledge as know
    from ate_cloud.services.knowledge_extraction import KnowledgeExtractionService

    svc = know.get_extraction_service(_FakeRequest(graph_service=object()))
    assert isinstance(svc, KnowledgeExtractionService)


@pytest.mark.asyncio
async def test_knowledge_extract_breaker_open_maps_to_503() -> None:
    """A CircuitBreakerOpenError from extraction maps to a 503 response."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import knowledge as know
    from ate_cloud.schemas.knowledge import KnowledgeExtractRequest
    from ate_platform.common.circuit_breaker import CircuitBreakerOpenError

    class _BoomExtractor:
        async def extract_sources(self, *_a: Any, **_k: Any) -> Any:
            raise CircuitBreakerOpenError("graph breaker open")

    with pytest.raises(HTTPException) as exc:
        await know.extract_knowledge(
            KnowledgeExtractRequest(product_code="P"),
            None,  # db unused before the raise
            _BoomExtractor(),
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_knowledge_extract_generic_error_maps_to_502() -> None:
    """A non-breaker extraction failure maps to a 502 response."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import knowledge as know
    from ate_cloud.schemas.knowledge import KnowledgeExtractRequest

    class _BoomExtractor:
        async def extract_sources(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

    with pytest.raises(HTTPException) as exc:
        await know.extract_knowledge(
            KnowledgeExtractRequest(product_code="P"), None, _BoomExtractor()
        )
    assert exc.value.status_code == 502


# ── kg_pipeline factory + semantica adapter (pure fakes) ──────────────────


def test_build_pipeline_uses_explicit_config_and_default() -> None:
    """build_pipeline passes an explicit config through and builds one otherwise."""
    from ate_cloud.services.kg_pipeline import build_pipeline
    from ate_cloud.services.kg_pipeline.models import PipelineConfig

    explicit = PipelineConfig(llm_api_key=None, llm_model="m", llm_base_url=None, embedding_dim=4)
    pipe = build_pipeline(graph_service=object(), config=explicit)
    assert pipe._config.embedding_dim == 4

    # Default config branch (reads settings; semantica is installed so the
    # GraphBuilder stage constructs without network).
    pipe_default = build_pipeline(graph_service=object())
    assert pipe_default._config is not None


def test_build_merged_graph_non_dict_raises() -> None:
    """A GraphBuilder that returns a non-dict triggers the defensive TypeError."""
    from ate_cloud.services.kg_pipeline import _semantica

    class _BadBuilder:
        def build(self, _payload: Any) -> Any:
            return ["not", "a", "dict"]

    with pytest.raises(TypeError):
        _semantica.build_merged_graph(_BadBuilder(), [], [])


class _Entity:
    def __init__(self, text: str, label: str = "Component") -> None:
        self.text = text
        self.label = label
        self.confidence = 1.0


class _Relation:
    def __init__(self, subject: _Entity, predicate: str, object: _Entity) -> None:
        self.subject = subject
        self.predicate = predicate
        self.object = object
        self.confidence = 1.0


def _install_fake_semantica(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow ``semantica.semantic_extract`` with a fake extractor module."""

    class _FakeNER:
        def __init__(self, **_kw: Any) -> None:
            pass

        def extract(self, _text: str) -> list[_Entity]:
            return [_Entity("R12", "Component"), _Entity("overheat", "Symptom")]

    class _FakeRel:
        def __init__(self, **_kw: Any) -> None:
            pass

        def extract(self, _text: str, entities: list[_Entity]) -> list[_Relation]:
            return [_Relation(entities[0], "exhibits", entities[1])]

    class _FakeTriplet:
        def __init__(self, **_kw: Any) -> None:
            pass

        def extract(self, text: str, entities: list[Any], relations: list[Any]) -> list[Any]:
            return [("triplet", text, len(entities), len(relations))]

    fake_se = types.ModuleType("semantica.semantic_extract")
    fake_se.NERExtractor = _FakeNER  # type: ignore[attr-defined]
    fake_se.RelationExtractor = _FakeRel  # type: ignore[attr-defined]
    fake_se.TripletExtractor = _FakeTriplet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "semantica.semantic_extract", fake_se)


def test_semantica_llm_extractor_maps_to_plain_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """SemanticaLLMExtractor maps Entity/Relation objects to plain dicts and
    relations_to_triplets drives the pattern TripletExtractor."""
    from ate_cloud.services.kg_pipeline import _semantica

    _install_fake_semantica(monkeypatch)

    # base_url set -> covers the kwargs branch.
    ext = _semantica.SemanticaLLMExtractor(api_key="k", model="m", base_url="http://x")
    out = ext.extract("R12 overheats")
    assert {e["name"] for e in out["entities"]} == {"R12", "overheat"}
    assert out["relationships"][0]["source"] == "R12"
    assert out["relationships"][0]["type"] == "EXHIBITS"

    # base_url unset -> covers the no-base-url branch.
    ext_no_url = _semantica.SemanticaLLMExtractor(api_key="k")
    out2 = ext_no_url.extract("R12 overheats")
    assert len(out2["entities"]) == 2

    # Triplet pattern path.
    triplets = _semantica.relations_to_triplets("R12 overheats", [_Entity("R12")], [])
    assert triplets and triplets[0][0] == "triplet"


# ── Edge worker run loop (ate_platform/scheduler/edge_worker.py) ──────────


@dataclass
class _FakeNatsConn:
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


class _ScriptedEdgeWorker:
    """JetStreamWorker stand-in scripted to exercise every run() branch."""

    worker_id = "edge-test"

    def __init__(self) -> None:
        self.events: list[str] = []
        self.start_calls = 0
        self.pull_calls = 0
        self.stop_calls = 0

    async def start(self, nc: Any = None) -> None:
        self.start_calls += 1
        self.events.append("start")
        if self.start_calls == 1:
            raise RuntimeError("start boom")  # -> reconnect branch

    async def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("stop")

    async def pull_and_process_one(self, timeout: float = 30.0) -> None:
        self.pull_calls += 1
        self.events.append("pull")
        if self.pull_calls == 1:
            raise RuntimeError("pull boom")  # -> mid-run reconnect branch
        raise asyncio.CancelledError()  # -> clean stop + re-raise


@pytest.mark.asyncio
async def test_edge_worker_run_retries_connect_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() retries a failed connect, reconnects after start/loop errors, and
    stops cleanly on cancellation."""
    from ate_platform.scheduler import edge_worker

    monkeypatch.setattr(edge_worker, "_RECONNECT_DELAY_SECONDS", 0)

    worker = _ScriptedEdgeWorker()
    connect_calls = {"n": 0}
    closed: list[_FakeNatsConn] = []

    async def _connector(url: str) -> Any:
        connect_calls["n"] += 1
        if connect_calls["n"] == 1:
            raise OSError("connection refused")  # -> connect-retry branch
        conn = _FakeNatsConn()
        closed.append(conn)
        return conn

    with pytest.raises(asyncio.CancelledError):
        await edge_worker.run(worker=worker, connector=_connector)

    # connect: fail(1) + start-fail(2) + loop-error(3) + cancel(4)
    assert connect_calls["n"] == 4
    assert worker.start_calls == 3
    assert worker.pull_calls == 2
    assert worker.stop_calls == 2  # one after loop error, one on cancel
    assert closed[0].closed  # the start-failed connection was closed


@pytest.mark.asyncio
async def test_edge_worker_connect_nats_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect_nats surfaces a connect failure so run() can log and retry."""
    from ate_platform.scheduler import edge_worker

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("dead server")

    monkeypatch.setattr(edge_worker.nats, "connect", _boom)
    with pytest.raises((OSError, TimeoutError)):
        await edge_worker.connect_nats("nats://127.0.0.1:49999")


# ── Knowledge READ handlers (api/v1/knowledge_reads.py) ────────────────────


async def _seed_requirements_and_cases(session_factory: Any) -> str:
    """Seed one requirement with a linked case and one orphan (unlinked) case."""
    from ate_cloud.models.knowledge import TestCase, TestRequirement

    async with session_factory() as s:
        req = TestRequirement(
            id=str(uuid.uuid4()),
            product_code="PSU-A",
            requirement_code="REQ-1",
            title="Output voltage in range",
            source="dsl",
        )
        s.add(req)
        await s.flush()
        linked = TestCase(
            id=str(uuid.uuid4()),
            requirement_id=req.id,
            case_code="TC-1",
            title="Measure 5V rail",
            step_id="seq1:step1",
        )
        orphan = TestCase(
            id=str(uuid.uuid4()),
            requirement_id=None,
            case_code="TC-2",
            title="Ingested before its requirement",
        )
        s.add_all([linked, orphan])
        await s.commit()
        return str(req.id)


@pytest.mark.asyncio
async def test_knowledge_reads_paged_lists_and_filters(session_factory: Any) -> None:
    """list_requirements / list_cases apply filters and denormalize product."""
    from ate_cloud.api.v1 import knowledge_reads as kr

    req_id = await _seed_requirements_and_cases(session_factory)

    async with session_factory() as s:
        # requirements: unfiltered / product filter / source filter / miss.
        assert (await kr.list_requirements(s, None, None, 0, 100)).total == 1
        assert (await kr.list_requirements(s, "PSU-A", None, 0, 100)).total == 1
        assert (await kr.list_requirements(s, None, "dsl", 0, 100)).total == 1
        assert (await kr.list_requirements(s, "NOPE", None, 0, 100)).total == 0

        # cases: unfiltered sees both; product filter (inner join) drops orphan.
        all_cases = await kr.list_cases(s, None, None, 0, 100)
        assert all_cases.total == 2
        by_product = await kr.list_cases(s, None, "PSU-A", 0, 100)
        assert by_product.total == 1
        assert by_product.items[0].product_code == "PSU-A"
        assert by_product.items[0].requirement_code == "REQ-1"

        # requirement_id filter isolates the linked case.
        by_req = await kr.list_cases(s, req_id, None, 0, 100)
        assert by_req.total == 1
        assert by_req.items[0].case_code == "TC-1"


@pytest.mark.asyncio
async def test_knowledge_reads_traceability_tree(session_factory: Any) -> None:
    """get_traceability nests linked cases and surfaces unlinked cases."""
    from ate_cloud.api.v1 import knowledge_reads as kr

    await _seed_requirements_and_cases(session_factory)

    async with session_factory() as s:
        tree = await kr.get_traceability(s, None)
        assert len(tree.requirements) == 1
        assert tree.requirements[0].requirement_code == "REQ-1"
        assert len(tree.requirements[0].cases) == 1
        assert tree.requirements[0].cases[0].case_code == "TC-1"
        assert len(tree.unlinked_cases) == 1
        assert tree.unlinked_cases[0].case_code == "TC-2"

        # product filter that matches nothing yields an empty tree.
        empty = await kr.get_traceability(s, "NO-SUCH-PRODUCT")
        assert empty.requirements == []
        assert empty.unlinked_cases == []


class _FakeBrowseGraph:
    """GraphService stand-in for the graph-browse handler."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def query(self, stmt: str, params: Any = None) -> list[dict[str, Any]]:
        if self._fail:
            raise RuntimeError("graph down")
        if "MATCH (n)" in stmt:  # node scan
            return [
                {"id": "r1", "labels": ["Component"], "name": "PSU", "properties": {"name": "PSU"}},
                {"id": "s1", "labels": ["Symptom"], "name": "OVP", "properties": {}},
                {"id": None, "labels": ["Ghost"], "name": "dropped", "properties": {}},
            ]
        # edge scan
        return [
            {"source": "r1", "target": "s1", "type": "EXHIBITS"},
            {"source": "r1", "target": "ghost", "type": None},  # missing type -> dropped
        ]


@pytest.mark.asyncio
async def test_knowledge_graph_browse_happy_and_filter() -> None:
    """browse_knowledge_graph projects nodes/edges and honors a label filter."""
    from ate_cloud.api.v1 import knowledge_reads as kr

    graph = _FakeBrowseGraph()
    result = await kr.browse_knowledge_graph(graph, 100, None)
    assert {n.id for n in result.nodes} == {"r1", "s1"}  # no-id row skipped
    assert len(result.edges) == 1
    assert result.edges[0].type == "EXHIBITS"

    # With a label filter, edges incident to returned nodes are kept.
    filtered = await kr.browse_knowledge_graph(graph, 100, "Component")
    assert len(filtered.edges) == 1


@pytest.mark.asyncio
async def test_knowledge_graph_browse_error_maps_to_503() -> None:
    """A graph-backend error during browse is mapped to an honest 503."""
    from fastapi import HTTPException

    from ate_cloud.api.v1 import knowledge_reads as kr

    with pytest.raises(HTTPException) as exc:
        await kr.browse_knowledge_graph(_FakeBrowseGraph(fail=True), 100, None)
    assert exc.value.status_code == 503
