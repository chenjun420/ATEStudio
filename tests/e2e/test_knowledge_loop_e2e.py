"""End-to-end closed loop A — failure event → KG evolution → hybrid retrieval
→ LLM diagnosis → persistence + feedback (task 29).

This drives the REAL production services wired together, faking ONLY the
external infrastructure (no live FalkorDB / Qdrant / OpenAI / SQLite file):

    STEP_FAILED event
      → FailureIndexer (real) ──upsert──▶ in-memory Qdrant (failures collection)
      → FailureEvolutionTrigger (real, lazy resolver like ate_cloud.main)
      → real kg_pipeline.build_pipeline (pattern extractors, no LLM key)
          ├─ MERGE ontology/evolved nodes+edges → in-memory GraphService
          └─ uuid5 entity vectors → in-memory Qdrant (KG collection)
      → HybridRetriever (real: query-rewrite → embed → Qdrant vector branch
        + ontology-KG graph branch → RRF fusion → rerank)
      → DiagnosisService (real prompt + real parser; ONLY the ChatOpenAI.ainvoke
        network call is stubbed with a deterministic grounded JSON answer)
      → DiagnosisStore.persist_diagnosis / record_feedback (real) → in-memory
        SQLite via the real ORM.

The fakes implement the real production protocols (GraphService, the async
embedding surface, the sync Qdrant surface, ChatModel.ainvoke) — no unit under
test is mocked. Everything runs in the default suite (no integration marker,
no skips, no live services).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import zlib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.models import Base
from ate_cloud.models.knowledge import Diagnosis
from ate_cloud.services.diagnosis_service import DiagnosisService
from ate_cloud.services.diagnosis_store import (
    HELPFUL_BY_FEEDBACK,
    build_symptom,
    persist_diagnosis,
    record_feedback,
)
from ate_cloud.services.failure_evolution import FailureEvolutionTrigger
from ate_cloud.services.failure_indexer import FailureIndexer
from ate_cloud.services.hybrid_retriever import HybridRetriever
from ate_cloud.services.kg_pipeline import PipelineConfig, build_pipeline
from ate_cloud.services.kg_seeder import KGSeeder
from shared.events import Event, EventType

#: Shared embedding dimensionality (kept small & CPU-free; must match every
#: collection size and every embedder/retriever in the loop).
DIM = 64

FAILURES_COLLECTION = "ate_failures"
KG_COLLECTION = "ate_kg_entities"

#: A rich, domain-vocabulary failure: the key-free pattern extractor
#: recognizes Symptom/Cause/Component/Instrument/TestStep/Measurement spans.
FAILURE_TEXT = (
    "Power supply PSU-1 exhibits excessive ripple on the 3.3V rail; "
    "the root cause is a degraded capacitor C12; replacing capacitor C12 "
    "resolves the fault; instrument DMM-42 measures the 3.3V rail during "
    "the in-circuit test"
)


# ---------------------------------------------------------------------------
# Deterministic bag-of-words embedder (async surface like EmbeddingService)
# ---------------------------------------------------------------------------


class HashEmbedder:
    """Deterministic token-bucket embedder: shared vocabulary ⇒ high cosine.

    No network. Each token hashes to a dimension (crc32 — stable across
    processes, unlike Python's salted ``hash``); vectors are L2-normalized so
    Qdrant-style cosine ranking reflects shared words. This is what lets the
    failure vector be genuinely *retrieved* for a same-topic diagnosis query.
    """

    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim
        self.calls: list[str] = []

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in set(re.findall(r"[a-z0-9]+", text.lower())):
            if len(token) < 2:
                continue
            vec[zlib.crc32(token.encode("utf-8")) % self._dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if not text.strip():
            return [0.0] * self._dim
        return self._vec(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_batch(texts)

    @property
    def dimensions(self) -> int:
        return self._dim


# ---------------------------------------------------------------------------
# In-memory Qdrant (sync surface; real cosine search)
# ---------------------------------------------------------------------------


class _Point:
    def __init__(self, point_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        self.id = point_id
        self.vector = vector
        self.payload = payload


class InMemoryQdrant:
    """Shared by the failure index AND the KG entity writer; COSINE search."""

    def __init__(self) -> None:
        self.collections: dict[str, int] = {}
        self.points: dict[str, list[_Point]] = {}

    def get_collections(self) -> Any:
        return SimpleNamespace(
            collections=[SimpleNamespace(name=n) for n in self.collections]
        )

    def create_collection(self, collection_name: str, vectors_config: Any = None, **_: Any) -> None:
        if collection_name not in self.collections:
            self.collections[collection_name] = getattr(vectors_config, "size", DIM)
            self.points[collection_name] = []

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        store = self.points.setdefault(collection_name, [])
        for point in points:
            store[:] = [p for p in store if p.id != point.id]  # uuid5 ⇒ idempotent
            store.append(_Point(str(point.id), list(point.vector), dict(point.payload or {})))

    def search(
        self,
        collection_name: str,
        query_vector: list[float] | None = None,
        limit: int = 10,
        with_payload: bool = True,  # noqa: ARG002 — mirrors Qdrant kwarg
        **_: Any,
    ) -> list[Any]:
        store = self.points.get(collection_name, [])
        q = query_vector or []
        qn = math.sqrt(sum(v * v for v in q)) or 1.0

        def cosine(p: _Point) -> float:
            pn = math.sqrt(sum(v * v for v in p.vector)) or 1.0
            return sum(a * b for a, b in zip(q, p.vector, strict=False)) / (qn * pn)

        ranked = sorted(store, key=cosine, reverse=True)[:limit]
        return [
            SimpleNamespace(id=p.id, score=cosine(p), payload=dict(p.payload)) for p in ranked
        ]


# ---------------------------------------------------------------------------
# In-memory GraphService — interprets BOTH seed (id-MERGE) and pipeline
# (name-MERGE) Cypher shapes AND the two kg_retrieval read shapes.
# ---------------------------------------------------------------------------


class InMemoryGraph:
    """Backend-agnostic GraphService fake with a tiny shape interpreter.

    Nodes live in ``self.nodes`` keyed by a stable internal key: id-keyed
    (ontology seed, ``MERGE (n:Label {id: row.id})``) use the entity id;
    name-keyed (auto-evolution pipeline, ``MERGE (n:Label {name: row.name})``)
    use ``"name::<lower>"``. Edges remember whether their endpoints are id- or
    name-keyed, so seed Fault traversal and evolved edges coexist in one graph.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.constraints_created = 0
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    # ── write path ──────────────────────────────────────────────────────
    async def create_constraints(self) -> None:
        self.constraints_created += 1

    async def write(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        rows = params.get("rows", [])
        if "{id: row.id}" in statement:
            label = self._label(statement)
            for row in rows:
                self._merge_id_node(label, row["id"], row.get("name"), row.get("props", {}))
        elif "{name: row.name}" in statement:
            label = self._label(statement)
            for row in rows:
                node = self._merge_name_node(label, row["name"])
                node["props"].update(
                    {k: row[k] for k in ("etype", "doc_id", "source") if k in row}
                )
        elif "row.src" in statement and "row.dst" in statement:  # seed edges (id endpoints)
            rel = self._rel(statement)
            for row in rows:
                self._add_edge("id", row["src"], rel, "id", row["dst"])
        elif "row.subject" in statement and "row.object" in statement:  # pipeline edges
            rel = self._rel(statement)
            for row in rows:
                sk = self._merge_name_node(None, row["subject"])["_key"]
                ok = self._merge_name_node(None, row["object"])["_key"]
                self._add_edge("name", row["subject"], rel, "name", row["object"],
                               doc_id=row.get("doc_id"))
                _ = (sk, ok)
        # CREATE INDEX / anything else: best-effort no-op (matches FalkorDB).
        return []

    @staticmethod
    def _label(statement: str) -> str:
        match = re.search(r"MERGE\s*\(\s*\w+:(\w+)", statement)
        return match.group(1) if match else "Entity"

    @staticmethod
    def _rel(statement: str) -> str:
        match = re.search(r"\[\s*\w+:(\w+)\s*\]", statement)
        return match.group(1) if match else "RELATED"

    def _merge_id_node(
        self, label: str | None, node_id: str, name: str | None, props: dict[str, Any]
    ) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        if node is None:
            node = {"_key": node_id, "id": node_id, "name": name or "", "label": label,
                    "props": {}}
            self.nodes[node_id] = node
        if name:
            node["name"] = name
        if label:
            node["label"] = label
        node["props"].update(props or {})
        if node.get("name"):
            self.nodes.setdefault(f"name::{node['name'].strip().lower()}", node)
        return node

    def _merge_name_node(self, label: str | None, name: str) -> dict[str, Any]:
        key = f"name::{name.strip().lower()}"
        node = self.nodes.get(key)
        if node is None:
            node = {"_key": key, "id": None, "name": name, "label": label, "props": {}}
            self.nodes[key] = node
        if label and node.get("label") is None:
            node["label"] = label
        return node

    def _add_edge(
        self,
        sk: str, s: str, rel: str, dk: str, d: str, *, doc_id: str | None = None,
    ) -> None:
        for edge in self.edges:
            if (
                edge["sk"] == sk and edge["s"] == s and edge["rel"] == rel
                and edge["dk"] == dk and edge["d"] == d
            ):
                return
        self.edges.append({"sk": sk, "s": s, "rel": rel, "dk": dk, "d": d,
                           "doc_id": doc_id})

    # ── read path (kg_retrieval shapes) ─────────────────────────────────
    async def query(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.queries.append((statement, params))
        params = params or {}
        if "count(n)" in statement:
            return [{"total": await self.count_nodes()}]
        if "count(r)" in statement:
            return [{"total": await self.count_relationships()}]
        if "CONTAINS toLower" in statement:
            return self._keyword_rows(str(params.get("keyword", "")))
        if "coalesce(f.name" in statement:
            return self._enrich_rows(list(params.get("ids", [])), int(params.get("limit", 10)))
        return []

    async def count_nodes(self) -> int:
        return len({id(n) for n in self.nodes.values()})

    async def count_relationships(self) -> int:
        return len(self.edges)

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "backend": "in-memory-e2e"}

    # ── traversal helpers ───────────────────────────────────────────────
    def _resolve(self, kind: str, value: str) -> dict[str, Any] | None:
        if kind == "id":
            return self.nodes.get(value)
        return self.nodes.get(f"name::{value.strip().lower()}")

    def _out(self, node: dict[str, Any], rel: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        nid = node.get("id")
        nname = (node.get("name") or "").strip().lower()
        for edge in self.edges:
            if edge["rel"] != rel:
                continue
            src = None
            if edge["sk"] == "id" and nid is not None and edge["s"] == nid:
                src = node
            elif edge["sk"] == "name" and nname and edge["s"].strip().lower() == nname:
                src = node
            if src is not None:
                target = self._resolve(edge["dk"], edge["d"])
                if target is not None:
                    out.append(target)
        return out

    def _fault_nodes(self) -> list[dict[str, Any]]:
        return [n for n in self.nodes.values() if n.get("label") == "Fault" and n.get("id")]

    def _keyword_rows(self, keyword: str) -> list[dict[str, Any]]:
        kw = keyword.lower().strip()
        if not kw:
            return []
        rows: list[dict[str, Any]] = []
        for fault in self._fault_nodes():
            haystack = [
                str(fault["props"].get("error_code", "")),
                str(fault.get("name", "")),
                str(fault["props"].get("description_en", "")),
            ]
            for symptom in self._out(fault, "HAS_SYMPTOM"):
                haystack.append(str(symptom.get("name", "")))
                for cause in self._out(symptom, "HAS_CAUSE"):
                    haystack.append(str(cause.get("name", "")))
            if any(kw in text.lower() for text in haystack):
                rows.append({"fault_id": fault["id"]})
        return rows

    def _enrich_rows(self, fault_ids: list[str], limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for fid in fault_ids[:limit]:
            fault = self.nodes.get(fid)
            if fault is None or fault.get("label") != "Fault":
                continue
            symptom = next(iter(self._out(fault, "HAS_SYMPTOM")), None)
            cause = next(iter(self._out(symptom, "HAS_CAUSE")), None) if symptom else None
            solution = next(iter(self._out(cause, "HAS_SOLUTION")), None) if cause else None
            component = next(iter(self._out(fault, "AFFECTS_COMPONENT")), None)
            product = next(iter(self._out(fault, "OCCURS_IN_PRODUCT")), None)
            instrument = next(iter(self._out(fault, "DIAGNOSED_WITH")), None)
            rows.append({
                "fault_id": fid,
                "error_code": fault["props"].get("error_code", ""),
                "fault_kind": fault["props"].get("fault_kind", ""),
                "symptom": (symptom or {}).get("name", "") or fault.get("name", ""),
                "cause": (cause or {}).get("name", ""),
                "solution": (solution or {}).get("name", ""),
                "component": (component or {}).get("name", ""),
                "product": (product or {}).get("name", "")
                or (product or {}).get("props", {}).get("product_type", ""),
                "instrument": (instrument or {}).get("name", "")
                or (instrument or {}).get("props", {}).get("instrument_kind", ""),
            })
        return rows

    # ── test introspection ──────────────────────────────────────────────
    def name_node(self, name: str) -> dict[str, Any] | None:
        return self.nodes.get(f"name::{name.strip().lower()}")

    def evolved_edges(self) -> list[dict[str, Any]]:
        return [e for e in self.edges if str(e.get("doc_id") or "").startswith("auto-fail:")]


# ---------------------------------------------------------------------------
# Grounded fake chat model (replaces ONLY the network LLM call)
# ---------------------------------------------------------------------------


class _GroundedChatLLM:
    """Captures the real prompt and returns a deterministic grounded JSON."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ainvoke(self, messages: list[Any]) -> Any:
        self.prompts.append(str(messages[0]) if messages else "")
        content = json.dumps({
            "root_cause": (
                "Excessive 3.3V rail ripple caused by a degraded capacitor C12 "
                "(seed fault PWR_5V_RIPPLE)."
            ),
            "confidence": 0.87,
            "evidence_citations": [
                "indexed failure: excessive ripple / degraded capacitor C12",
                "ontology fault PWR_5V_RIPPLE symptom chain",
            ],
            "repair_steps": [
                "Replace capacitor C12 on the 3.3V rail",
                "Re-measure rail ripple with DMM-42 after replacement",
            ],
        })
        return SimpleNamespace(content=content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failure_event(run_id: str = "run-e2e-001") -> Event:
    return Event(
        type=EventType.STEP_FAILED,
        data={
            "step_id": "power_rail_test",
            "failed_step_id": "power_rail_test",
            "failed_step_name": "power rail ripple test",
            "error_message": FAILURE_TEXT,
            "variable_snapshot": {"voltage": 3.28, "rail": "3.3V"},
            "run_id": run_id,
            "plan_name": "power_supply_selftest",
        },
    )


async def _wait_for(pred: Any, description: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for: {description}")


def _build_services(graph: InMemoryGraph, qdrant: InMemoryQdrant, embedder: HashEmbedder) -> Any:
    """Wire the real services exactly like ate_cloud.main does (lazy resolver)."""
    pipeline = build_pipeline(
        graph_service=graph,
        embedding_service=embedder,
        qdrant_client=qdrant,
        config=PipelineConfig(
            llm_api_key=None,  # key-free pattern extractors still evolve the KG
            embedding_dim=DIM,
            vector_collection=KG_COLLECTION,
        ),
    )

    def _resolve() -> Any:
        return pipeline  # cached-singleton semantics of main._resolve_failure_pipeline

    indexer = FailureIndexer(
        qdrant_client=qdrant,
        embedding_service=embedder,
        collection_name=FAILURES_COLLECTION,
        embedding_dim=DIM,
    )
    indexer.set_evolution_trigger(FailureEvolutionTrigger(resolve=_resolve).evolve_from_failure)

    retriever = HybridRetriever(
        embedding_service=embedder,
        graph_service=graph,  # type: ignore[arg-type]
        qdrant_client=qdrant,
        collection_name=FAILURES_COLLECTION,
        api_key="",  # no rewrite LLM → deterministic dictionary expansion only
        embedding_dim=DIM,
    )
    return indexer, pipeline, retriever


# ---------------------------------------------------------------------------
# The closed loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_evolves_kg_and_flows_into_diagnosis_and_persistence() -> None:
    """Chain A: one real failure closes the whole index→evolve→retrieve→diagnose→store loop."""
    graph = InMemoryGraph()
    qdrant = InMemoryQdrant()
    embedder = HashEmbedder()

    # 0. Seed the ontology FMEA graph via the REAL seeder (idempotent MERGE).
    seed_result = await KGSeeder(graph).seed_all()  # type: ignore[arg-type]
    assert seed_result["facts_seeded"] == 104
    seeded_nodes = await graph.count_nodes()
    assert seeded_nodes > 100

    indexer, pipeline, retriever = _build_services(graph, qdrant, embedder)
    await indexer.ensure_collection()

    # 1. A real STEP_FAILED event enters through the public indexer entry point.
    indexer.index_failure(_failure_event())

    # The failure point lands in the RAG failures collection...
    await _wait_for(
        lambda: bool(qdrant.points.get(FAILURES_COLLECTION)),
        "failure point indexed",
    )
    failure_points = qdrant.points[FAILURES_COLLECTION]
    assert len(failure_points) == 1
    assert "excessive ripple" in failure_points[0].payload["error_message"]

    # ...and auto-evolution ran through the REAL pattern pipeline.
    def _evolved() -> bool:
        return (
            graph.name_node("degraded capacitor") is not None
            and bool(qdrant.points.get(KG_COLLECTION))
        )

    await _wait_for(_evolved, "KG evolved (graph nodes + entity vectors)")
    assert pipeline.extraction_mode == "pattern"
    # Evolved, name-MERGED ontology entities from the failure text:
    assert graph.name_node("excessive ripple") is not None  # Symptom
    assert graph.name_node("degraded capacitor") is not None  # Cause
    assert graph.name_node("capacitor c12") is not None  # Component
    # Evolved relationship edges carry the deterministic auto-fail doc id:
    assert graph.evolved_edges(), "pipeline MERGEd evolved relationship edges"
    # KG entity vectors were upserted (uuid5 point ids):
    assert qdrant.points[KG_COLLECTION], "KG entity vectors persisted"
    # The seed graph is untouched in count by name-keyed evolution adds:
    assert await graph.count_nodes() > seeded_nodes

    # 2. Hybrid retrieval fuses the vector failure branch with the ontology KG.
    results = await retriever.search(
        "power rail ripple test | error code: PWR_5V_RIPPLE | "
        "excessive ripple degraded capacitor C12 3.3V",
        top_k=10,
        rerank=True,
        error_code="PWR_5V_RIPPLE",
    )
    assert results, "hybrid retrieval must return evidence"
    sources = {str(r.get("source")) for r in results}
    # Graph branch: the seeded fault enriched via the ontology traversal...
    graph_hits = [r for r in results if r.get("entity_id") == "fault:pwr_5v_ripple"]
    assert graph_hits, "ontology seed fault reached retrieval via the graph branch"
    assert graph_hits[0].get("cause"), "graph fault enriched with cause/solution"
    # Vector branch: the just-indexed failure (no entity_id ⇒ stays qdrant-sourced).
    vector_hits = [
        r for r in results
        if "ripple" in str(r.get("error_message", "")).lower()
        or r.get("failed_step_name") == "power rail ripple test"
    ]
    assert vector_hits, "the indexed failure reached retrieval via the vector branch"
    assert "graph" in sources and ("qdrant" in sources or "fused" in sources)

    # 3. DiagnosisService with the REAL prompt builder + parser; only the LLM
    #    network call is a grounded stub.
    service = DiagnosisService(retriever, api_key="sk-test", model="fake-diag-model")
    fake_llm = _GroundedChatLLM()
    service._llm = fake_llm  # type: ignore[assignment]
    service._prompt = SimpleNamespace(  # capture the real rendered prompt text
        format_messages=lambda **kw: [kw["diagnosis_info"]]
    )
    service._initialized = True  # skip LangChain construction (no network)

    result = await service.diagnose(
        product_type="COMM-PWR-001",
        failed_test="power rail ripple test",
        error_code="PWR_5V_RIPPLE",
        log_snippet="excessive ripple on 3.3V rail, degraded capacitor C12",
    )

    assert result["retrieval_only"] is False
    assert result["llm_model"] == "fake-diag-model"
    assert "degraded capacitor C12" in result["root_cause"]
    assert result["confidence"] > 0.5
    assert result["repair_steps"], "grounded repair steps parsed from LLM JSON"
    assert len(result["retrieved_cases"]) >= 2
    # The REAL prompt the LLM received carried the retrieved failure evidence:
    assert fake_llm.prompts, "LLM was invoked with the rendered diagnosis prompt"
    assert "ripple" in fake_llm.prompts[0].lower()
    assert "PWR_5V_RIPPLE" in fake_llm.prompts[0]

    # 4. Persist the diagnosis + record operator feedback (real store + ORM).
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            symptom = build_symptom(
                failed_test="power rail ripple test",
                error_code="PWR_5V_RIPPLE",
                log_snippet="excessive ripple on 3.3V rail",
                product_type="COMM-PWR-001",
            )
            row = await persist_diagnosis(
                db,
                diagnosis_id=result["diagnosis_id"],
                symptom=symptom,
                result=result,
                run_id="run-e2e-001",
                session_id="edge-session-1",
            )
            await db.flush()
            assert row.run_id == "run-e2e-001"
            assert row.session_id == "edge-session-1"
            assert row.llm_model == "fake-diag-model"
            assert "power rail ripple test" in row.symptom
            assert "degraded capacitor C12" in (row.conclusion or "")
            assert row.helpful is None  # no feedback yet
            assert "retrieved case" in (row.context_summary or "")

            # Feedback: confirmed ⇒ helpful True; rejected ⇒ helpful False + note.
            confirmed = await record_feedback(
                db, diagnosis_id=result["diagnosis_id"],
                helpful=HELPFUL_BY_FEEDBACK["confirmed"],
            )
            await db.flush()
            assert confirmed is not None and confirmed.helpful is True

            rejected = await record_feedback(
                db, diagnosis_id=result["diagnosis_id"],
                helpful=HELPFUL_BY_FEEDBACK["rejected"],
                note="Actual cause: cold solder at C12",
            )
            await db.flush()
            assert rejected is not None and rejected.helpful is False
            assert rejected.feedback_note == "Actual cause: cold solder at C12"

            fetched = (
                await db.execute(
                    select(Diagnosis).where(Diagnosis.id == result["diagnosis_id"])
                )
            ).scalar_one()
            assert fetched.helpful is False
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Idempotency — re-ingesting the identical failure never duplicates KG state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_re_evolving_same_failure_is_idempotent_in_graph_and_vectors() -> None:
    graph = InMemoryGraph()
    qdrant = InMemoryQdrant()
    embedder = HashEmbedder()
    await KGSeeder(graph).seed_all()  # type: ignore[arg-type]
    _indexer, pipeline, _retriever = _build_services(graph, qdrant, embedder)

    metadata = dict(_failure_event().data)

    # Drive the trigger twice with the SAME failure metadata.
    trigger = FailureEvolutionTrigger(pipeline=pipeline)
    ok1 = await trigger.evolve_from_failure(metadata)
    nodes_after_first = await graph.count_nodes()
    kg_vectors_after_first = len(qdrant.points.get(KG_COLLECTION, []))
    evolved_edges_after_first = len(graph.evolved_edges())

    ok2 = await trigger.evolve_from_failure(metadata)
    assert ok1 is True and ok2 is True

    # Deterministic doc id (uuid5) + name-MERGE ⇒ no duplicated ontology state.
    assert await graph.count_nodes() == nodes_after_first
    assert len(graph.evolved_edges()) == evolved_edges_after_first
    # VectorWriter uses uuid5(doc_id, name) point ids ⇒ upsert, not append.
    assert len(qdrant.points.get(KG_COLLECTION, [])) == kg_vectors_after_first


# ---------------------------------------------------------------------------
# Best-effort — evolution can NEVER break failure indexing
# ---------------------------------------------------------------------------


class _BoomPipeline:
    """Pipeline double whose ingest always raises (evolution fault injection)."""

    def __init__(self) -> None:
        self.ingest_calls = 0

    async def ingest(self, document: Any) -> Any:
        self.ingest_calls += 1
        raise RuntimeError("kg pipeline exploded")


@pytest.mark.asyncio
async def test_evolution_failures_never_block_indexing() -> None:
    qdrant = InMemoryQdrant()
    embedder = HashEmbedder()

    # (a) No pipeline wired at all (resolver returns None) → logged skip.
    assert await FailureEvolutionTrigger().evolve_from_failure(
        {"error_message": "x", "run_id": "r-none"}
    ) is False

    # (b) Resolver raising → swallowed inside the trigger, returns False.
    def _raising_resolver() -> Any:
        raise RuntimeError("pipeline construction failed")

    assert await FailureEvolutionTrigger(resolve=_raising_resolver).evolve_from_failure(
        {"error_message": "x", "run_id": "r-resolver-boom"}
    ) is False

    # (c) Pipeline ingest raising → swallowed by the trigger.
    boom = _BoomPipeline()
    assert await FailureEvolutionTrigger(pipeline=boom).evolve_from_failure(
        dict(_failure_event().data)
    ) is False
    assert boom.ingest_calls == 1

    # (d) The indexer's backstop: even a hook that RAISES never breaks indexing.
    indexer = FailureIndexer(
        qdrant_client=qdrant,
        embedding_service=embedder,
        collection_name=FAILURES_COLLECTION,
        embedding_dim=DIM,
    )

    async def _raising_hook(_metadata: dict[str, Any]) -> bool:
        raise RuntimeError("evolution hook exploded")

    indexer.set_evolution_trigger(_raising_hook)
    await indexer.ensure_collection()
    indexer.index_failure(_failure_event(run_id="run-best-effort"))
    await _wait_for(
        lambda: bool(qdrant.points.get(FAILURES_COLLECTION)),
        "failure indexed despite exploding evolution hook",
    )
    assert qdrant.points[FAILURES_COLLECTION], "indexing survives a raising hook"
