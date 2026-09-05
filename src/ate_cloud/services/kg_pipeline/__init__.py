"""kg_pipeline — Semantica GraphRAG pipeline isolated behind a service boundary.

This package is the ONLY place in the application that imports Semantica.
The rest of the cloud app (routers, seeding, retrieval, diagnosis) depends on
the facade exported here — :class:`KGPipeline` / :func:`build_pipeline` — and
never on ``semantica`` directly::

    Sources → ingest → (domain parse) → normalize/split
            → extract (pattern NER/Relation/Triplet when no LLM key;
                       LLM-backed extractors when OPENAI_API_KEY is set)
            → conflict/dedup (Semantica GraphBuilder merge_entities)
            → ontology-enriched KG
            → persist: GraphService (FalkorDB LPG) AND Qdrant vectors

Semantica 0.6.7 API mapping (verified against the installed package):

- NER:        ``semantica.semantic_extract.NERExtractor(method=...)``. The
  built-in ``method="pattern"`` fallback targets generic PERSON/ORG/GPE/DATE
  shapes and is unsuitable for production-test vocabulary, so the fault/
  component/instrument vocabulary is recognized by the domain pattern stage
  in ``patterns.py``; Semantica ``Entity`` objects are still produced and fed
  downstream. ``method="ml"`` requires spaCy (not installed) and ``method="llm"``
  requires a provider key.
- Relations:  ``semantica.semantic_extract.RelationExtractor(method="pattern"|"llm")``
  → ``.extract(text, entities)`` returns ``Relation(subject, predicate, object)``.
- Triplets:   ``semantica.semantic_extract.TripletExtractor(method=...)`` →
  ``.extract(text, entities, relations)`` returns ``Triplet``.
- KG build / merge / conflict resolution:
  ``semantica.kg.GraphBuilder(merge_entities=True, resolve_conflicts=True)``
  → ``.build({"entities": [...], "relationships": [...]})`` returns a merged
  graph dict (duplicate entities merged, ``metadata.provenance`` recorded).
- LPG persistence: the app's FalkorDB connection is owned by
  :class:`~ate_cloud.services.falkordb_graph_service.FalkorDBGraphService`
  (GraphService protocol); the pipeline does NOT open a second FalkorDB path —
  it writes Cypher MERGE statements through the injected GraphService.
- Vectors: embeddings go to Qdrant via the injected client (COSINE), mirroring
  FailureIndexer / FaultSymptomVectorStore; vectors are never graph properties.

All Semantica imports are confined to :mod:`ate_cloud.services.kg_pipeline._semantica`.
"""

from __future__ import annotations

from ate_cloud.services.kg_pipeline.errors import (
    KGPipelineError,
    KGPipelineUnavailable,
)
from ate_cloud.services.kg_pipeline.factory import build_pipeline
from ate_cloud.services.kg_pipeline.models import (
    Document,
    PipelineConfig,
    PipelineResult,
)
from ate_cloud.services.kg_pipeline.pipeline import KGPipeline

__all__ = [
    "Document",
    "KGPipeline",
    "KGPipelineError",
    "KGPipelineUnavailable",
    "PipelineConfig",
    "PipelineResult",
    "build_pipeline",
]
