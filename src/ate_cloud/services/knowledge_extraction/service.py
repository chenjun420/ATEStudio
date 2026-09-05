"""Knowledge extraction orchestration (task 12).

Ties the deterministic sources together and persists BOTH layers:

1. ORM rows — :class:`~ate_cloud.models.knowledge.TestRequirement` /
   :class:`~ate_cloud.models.knowledge.TestCase`, upserted on natural keys by
   :mod:`orm_store` (``(product_code, requirement_code)`` / ``case_code``) so
   re-running extraction creates no duplicates.
2. Knowledge-graph nodes/relationships — built by :mod:`graph_build` and
   written through the :class:`~ate_cloud.services.graph_service.GraphService`
   via :func:`kg_writer.write_knowledge_graph` (Cypher ``UNWIND ... MERGE`` on
   stable ids from :mod:`ids`).

ATML XML is never re-parsed: :meth:`extract_atml` drives the task-11
:class:`~ate_cloud.services.atml_importer.ATMLImporter`. Recordings never
crash the run: an unreadable file or a lifecycle event missing ``step_id``
is skipped with a warning.

LLM use is strictly opt-in enrichment: an optional task-7 pipeline may be
injected and is only ever constructed by the caller when an API key is
present; structured extraction makes no LLM call. With ``graph=None`` the
ORM layer still succeeds (graceful degrade, app boots with no FalkorDB).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.knowledge import SOURCE_DSL, TestCase, TestRequirement
from ate_cloud.services.atml_importer import ATMLImporter, ImportCounts, ImportResult
from ate_cloud.services.graph_service import GraphService
from ate_cloud.services.knowledge_extraction import graph_build, orm_store
from ate_cloud.services.knowledge_extraction.dsl_extract import extract_plan
from ate_cloud.services.knowledge_extraction.kg_writer import (
    KGEdge,
    KGNode,
    write_knowledge_graph,
)
from ate_cloud.services.knowledge_extraction.recordings import read_recording

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Outcome of one DSL/ATML extraction pass."""

    product_code: str
    source: str
    requirements: ImportCounts
    cases: ImportCounts


@dataclass(frozen=True, slots=True)
class RecordingsResult:
    """Outcome of ingesting a batch of recording files."""

    files_read: int = 0
    results_written: int = 0
    skipped_events: int = 0
    unmatched_steps: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExtractionSummary:
    """Aggregated outcome of one extraction trigger (DSL batch + recordings)."""

    product_code: str
    requirements: ImportCounts
    cases: ImportCounts
    recordings: RecordingsResult
    graph_status: str


class KnowledgeExtractionService:
    """Extract requirements/cases/results from structured sources into ORM+KG."""

    def __init__(
        self,
        graph: GraphService | None,
        *,
        atml_importer: ATMLImporter | None = None,
        enrichment_pipeline: Any | None = None,
    ) -> None:
        """Args:
        graph: GraphService backend (FalkorDB in prod, fake in tests); ``None``
            degrades to ORM-only persistence with no graph writes.
        atml_importer: Overridable task-11 importer seam (tests inject fakes).
        enrichment_pipeline: Optional task-7 KGPipeline for LLM enrichment.
        """
        self._graph = graph
        self._atml = atml_importer or ATMLImporter()
        self._pipeline = enrichment_pipeline

    # ── DSL YAML ──────────────────────────────────────────────────────────

    async def extract_dsl_yaml(
        self, db: AsyncSession, path: str | Path, *, product_code: str
    ) -> ExtractionResult:
        """Parse one DSL YAML plan and upsert its requirement + step cases."""
        plan = extract_plan(path)
        if plan is None:
            return ExtractionResult(product_code, SOURCE_DSL, ImportCounts(), ImportCounts())

        req_counts, req_id = await orm_store.upsert_dsl_requirement(db, product_code, plan)
        case_counts = await orm_store.upsert_dsl_cases(db, plan, req_id)
        await db.flush()
        await self._write_graph(graph_build.dsl_graph(product_code, plan))
        await self._enrich(f"dsl-plan:{plan.plan_name}", plan.title)
        return ExtractionResult(product_code, SOURCE_DSL, req_counts, case_counts)

    async def extract_sources(
        self,
        db: AsyncSession,
        *,
        product_code: str,
        dsl_paths: Sequence[str | Path] | None = None,
        recording_paths: Sequence[str | Path] | None = None,
    ) -> ExtractionSummary:
        """Run a full deterministic extraction pass (DSL plans + recordings).

        Unparseable DSL files / unreadable recordings are counted and skipped.
        """
        req_total = ImportCounts()
        case_total = ImportCounts()
        for path in dsl_paths or []:
            result = await self.extract_dsl_yaml(db, path, product_code=product_code)
            req_total = ImportCounts(
                req_total.created + result.requirements.created,
                req_total.updated + result.requirements.updated,
            )
            case_total = ImportCounts(
                case_total.created + result.cases.created,
                case_total.updated + result.cases.updated,
            )
        recordings = await self.extract_recordings(db, list(recording_paths or []))
        await db.flush()
        return ExtractionSummary(
            product_code, req_total, case_total, recordings,
            "ok" if self._graph is not None else "degraded",
        )

    # ── ATML TestDescription (drive task-11 importer, no XML re-parse) ─────

    async def extract_atml(self, db: AsyncSession, xml: str | bytes) -> ExtractionResult:
        """Import an IEEE 1671 TestDescription via task-11 importer + sync KG."""
        result: ImportResult = await self._atml.import_test_description(db, xml)
        await self._sync_atml_graph(db, result.product_code)
        await self._enrich(
            f"atml:{result.product_code}", f"ATML TestDescription {result.product_code}"
        )
        return ExtractionResult(result.product_code, "atml", result.requirements, result.cases)

    async def _sync_atml_graph(self, db: AsyncSession, product_code: str) -> None:
        reqs = (
            await db.execute(
                select(TestRequirement).where(TestRequirement.product_code == product_code)
            )
        ).scalars().all()
        req_tuples: list[tuple[str, str, str, str | None]] = []
        case_tuples: list[tuple[str, str, str, str, str | None]] = []
        for req in reqs:
            req_tuples.append((req.requirement_code, req.title, req.source, req.atml_ref))
            cases = (
                await db.execute(select(TestCase).where(TestCase.requirement_id == req.id))
            ).scalars().all()
            for case in cases:
                case_tuples.append(
                    (case.case_code, case.title, req.requirement_code, case.status, case.atml_ref)
                )
        nodes, edges = graph_build.atml_graph(product_code, req_tuples, case_tuples)
        await self._write_graph((nodes, edges))

    # ── Recordings → UUT results ──────────────────────────────────────────

    async def extract_recordings(
        self, db: AsyncSession, paths: list[str | Path]
    ) -> RecordingsResult:
        """Ingest recording files; executed step outcomes become UUTResult nodes."""
        files_read = skipped_events = 0
        skipped_files: list[str] = []
        unmatched: list[str] = []
        nodes: list[KGNode] = []
        edges: list[KGEdge] = []

        for path in paths:
            try:
                results, skipped = read_recording(path)
            except OSError as exc:
                logger.warning("Recording %s unreadable; skipping file: %s", path, exc)
                skipped_files.append(str(path))
                continue
            files_read += 1
            skipped_events += skipped
            for recorded in results:
                case = (
                    await db.execute(
                        select(TestCase).where(TestCase.step_id == recorded.step_id).limit(1)
                    )
                ).scalars().first()
                if case is None:
                    unmatched.append(recorded.step_id)
                    continue
                product_code = await orm_store.product_for_case(db, case)
                result_nodes, result_edges = graph_build.result_graph(
                    case.case_code, product_code, recorded
                )
                nodes.extend(result_nodes)
                edges.extend(result_edges)

        await self._write_graph((nodes, edges))
        return RecordingsResult(
            files_read=files_read,
            results_written=len(nodes),
            skipped_events=skipped_events,
            unmatched_steps=unmatched,
            skipped_files=skipped_files,
        )

    # ── Persistence / enrichment ───────────────────────────────────────────

    async def _write_graph(self, payload: tuple[list[KGNode], list[KGEdge]]) -> None:
        """Persist nodes/edges when a graph backend is configured (degrade None)."""
        if self._graph is None:
            return
        nodes, edges = payload
        if nodes:
            await write_knowledge_graph(self._graph, nodes, edges)

    async def _enrich(self, doc_id: str, text: str) -> None:
        """Best-effort LLM enrichment via the task-7 pipeline (only if injected)."""
        if self._pipeline is None:
            return
        try:
            from ate_cloud.services.kg_pipeline import Document

            await self._pipeline.ingest(
                Document(doc_id=doc_id, text=text, source="knowledge_extraction")
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must never break extraction
            logger.warning("LLM enrichment skipped for %s: %s", doc_id, exc)


__all__ = [
    "ExtractionResult",
    "ExtractionSummary",
    "KnowledgeExtractionService",
    "RecordingsResult",
]
