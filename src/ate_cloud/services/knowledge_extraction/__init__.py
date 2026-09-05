"""Deterministic knowledge extraction (task 12).

Builds the traceability chain **TestRequirement → TestCase → DSL step →
(recorded) UUT result** from STRUCTURED sources:

* DSL YAML plans — parsed with the real edge DSL parser
  (:class:`ate_platform.dsl.parser.YamlParser`); one ``TestRequirement`` per
  plan/product and one ``TestCase`` per test step, linked to the DSL step id.
* ATML IEEE 1671 TestDescription imports — driven through the task-11
  :class:`~ate_cloud.services.atml_importer.ATMLImporter` (XML is never
  re-parsed here).
* recordings JSONL — executed step outcomes (the
  :class:`~ate_platform.simulation.recording.RecordingInterceptor`
  ``step_started``/``step_completed``/``step_failed`` events) become
  ``UUTResult`` instances linked to the DSL step/case.

Persistence is TWO layers, both idempotent: ORM rows
(:mod:`ate_cloud.models.knowledge`, upserted by natural keys) and knowledge
graph nodes/relationships written through the
:class:`~ate_cloud.services.graph_service.GraphService` with Cypher
``UNWIND ... MERGE`` on stable ids (the task-8 seed-writer idiom). Vectors to
Qdrant are best-effort and owned by the task-7 ``kg_pipeline`` (optional LLM
enrichment only); structured extraction never calls an LLM.

Graceful degrade: with no GraphService the ORM layer still succeeds, and the
app boots with no FalkorDB/Qdrant (consistent with tasks 7/8). Semantica is
never imported by this package.
"""

from __future__ import annotations

from ate_cloud.services.knowledge_extraction.service import (
    ExtractionResult,
    ExtractionSummary,
    KnowledgeExtractionService,
    RecordingsResult,
)

__all__ = [
    "ExtractionResult",
    "ExtractionSummary",
    "KnowledgeExtractionService",
    "RecordingsResult",
]
