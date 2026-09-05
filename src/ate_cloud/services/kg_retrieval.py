"""Ontology knowledge-graph retrieval for hybrid diagnosis.

Reads the ontology-aligned KG written by the task-8 seed
(:mod:`ate_cloud.services.kg_seed_facts`) and the task-12 extraction
(:mod:`ate_cloud.services.knowledge_extraction`) through the backend-agnostic
:class:`~ate_cloud.services.graph_service.GraphService`. There is NO
text-prefix join heuristic and NO legacy-label query here: candidate matches
are found by stable **entity ids** (and, as a fallback, ontology node
properties), then the ontology relationship vocabulary is traversed to
enrich each candidate Fault with its symptom -> cause -> solution chain,
affected component, product and diagnostic instrument.

Ontology graph shape this module reads (the single source of truth is
:mod:`ate_cloud.services.ontology.core` plus the seed/extraction writers):

* node LABELS: ``Fault`` / ``Symptom`` / ``Cause`` / ``Solution`` /
  ``Component`` / ``Product`` / ``Instrument`` (seed) and
  ``TestRequirement`` / ``TestCase`` / ``TestStep`` / ``UUTResult``
  (extraction). Every node carries a stable ``id`` (the writers' MERGE key).
* relationship types traversed for enrichment: the seed fault chain
  ``HAS_SYMPTOM`` (Fault->Symptom), ``HAS_CAUSE`` (Symptom->Cause),
  ``HAS_SOLUTION`` (Cause->Solution), plus ``AFFECTS_COMPONENT``
  (Fault->Component), ``OCCURS_IN_PRODUCT`` (Fault->Product) and
  ``DIAGNOSED_WITH`` (Fault->Instrument). These are exactly the edges the
  task-8 seed writer emits (:func:`kg_seed_facts.build_seed_graph`); the
  task-12 extraction traceability edges (``HAS_REQUIREMENT`` /
  ``VERIFIED_BY`` / ``HAS_STEP`` / ``PRODUCED_RESULT`` / ``RESULT_FOR``)
  do NOT link back to ``Fault`` nodes, so they are not traversed here.

Retrieval is two-stage:

1. **Candidate Faults by entity id** — seed Fault ids are
   ``fault:<slug(error_code)>``; the caller (hybrid retriever) resolves
   query tokens (e.g. an ``ERR_I2C_TIMEOUT`` error code, or the entity id
   carried by a vector hit) to that id and passes candidate ids directly.
   This is the shared-ID join: a vector hit and a graph hit carrying the
   same stable id describe the same fault and fuse.
2. **Keyword fallback over ontology properties** — when candidate ids are
   not enough (free-text query), a Cypher scan over ontology
   Fault/Symptom/Cause **node properties** (name, description_en/zh,
   error_code) supplies more candidate Fault ids. Only ontology labels are
   scanned (never the retired ``FaultSymptom`` label).

Graph failures propagate (FalkorDBGraphService is breaker-protected and
raises ``CircuitBreakerOpenError``); the hybrid retriever catches them per
branch so a graph outage degrades to vector-only retrieval.
"""

from __future__ import annotations

import re
from typing import Any

from ate_cloud.services.graph_service import GraphService

#: Cypher: keyword fallback — ontology Fault/Symptom/Cause nodes whose text
#: properties contain the keyword, projected back to their owning Fault.
_CANDIDATES_BY_KEYWORD_CYPHER = (
    "MATCH (f:Fault) "
    "OPTIONAL MATCH (f)-[:HAS_SYMPTOM]->(s:Symptom) "
    "OPTIONAL MATCH (s)-[:HAS_CAUSE]->(c:Cause) "
    "WITH f, s, c WHERE "
    "toLower(coalesce(f.error_code, '')) CONTAINS toLower($keyword) "
    "OR toLower(coalesce(f.name, '')) CONTAINS toLower($keyword) "
    "OR toLower(coalesce(f.description_en, '')) CONTAINS toLower($keyword) "
    "OR toLower(coalesce(f.description_zh, '')) CONTAINS toLower($keyword) "
    "OR toLower(coalesce(s.name, '')) CONTAINS toLower($keyword) "
    "OR toLower(coalesce(c.name, '')) CONTAINS toLower($keyword) "
    "RETURN DISTINCT f.id AS fault_id"
)

#: Cypher: enrich Fault ids across the ontology fault-chain relationships
#: (Fault->Symptom->Cause->Solution, component, product, instrument). These
#: are exactly the edges ``build_seed_graph`` emits; unknown ids match
#: nothing. One row per Fault (head symbols aggregated in Python on the
#: stable fault_id, so multi-chain faults stay a single result).
_ENRICH_CYPHER = (
    "UNWIND $ids AS fid "
    "MATCH (f:Fault {id: fid}) "
    "OPTIONAL MATCH (f)-[:HAS_SYMPTOM]->(s:Symptom) "
    "OPTIONAL MATCH (s)-[:HAS_CAUSE]->(c:Cause) "
    "OPTIONAL MATCH (c)-[:HAS_SOLUTION]->(sol:Solution) "
    "OPTIONAL MATCH (f)-[:AFFECTS_COMPONENT]->(comp:Component) "
    "OPTIONAL MATCH (f)-[:OCCURS_IN_PRODUCT]->(prod:Product) "
    "OPTIONAL MATCH (f)-[:DIAGNOSED_WITH]->(inst:Instrument) "
    "RETURN "
    "f.id AS fault_id, "
    "f.error_code AS error_code, "
    "f.fault_kind AS fault_kind, "
    "coalesce(f.name, f.description_en, '') AS symptom, "
    "coalesce(c.name, c.description_en, '') AS cause, "
    "coalesce(sol.name, sol.description_en, '') AS solution, "
    "coalesce(comp.name, '') AS component, "
    "coalesce(prod.name, prod.product_type, '') AS product, "
    "coalesce(inst.name, inst.instrument_kind, '') AS instrument"
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Common English stop words + generic fault nouns skipped by keyword extraction.
_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "to", "of",
    "in", "on", "at", "by", "for", "with", "from", "into", "and",
    "or", "not", "no", "but", "if", "then", "else", "when", "where",
    "what", "which", "who", "how", "why", "that", "this", "these",
    "those", "it", "its", "as", "so", "than", "too", "very",
    "error", "failure", "fault", "fail", "failed", "test", "issue",
})


def fault_entity_id(error_code: str) -> str:
    """Build the stable Fault entity id from an error code.

    Mirrors the task-8 seed slug scheme (``fault:<slug(code)>`` — slug
    lower-cases and replaces non-alphanumeric runs with ``_``) so an error
    code in a diagnosis request resolves to the SAME id the seed MERGEd.
    """
    slug = _SLUG_RE.sub("_", str(error_code).lower()).strip("_")
    return f"fault:{slug}"


def extract_keyword(query: str) -> str:
    """Pick the most specific keyword from a free-text query.

    Used only for the ontology property fallback scan. Picks the longest
    non-stopword token (longer tokens are more specific, fewer false
    positives in a Cypher ``CONTAINS`` scan). Returns "" for an empty query
    (caller then skips the keyword fallback).
    """
    tokens: list[str] = re.findall(r"[A-Za-z0-9]+", query)
    candidates = [t for t in tokens if t.lower() not in _STOP_WORDS and len(t) >= 2]
    if not candidates:
        return ""
    return str(max(candidates, key=len)).lower()


async def retrieve_faults(
    graph: GraphService,
    *,
    candidate_ids: list[str] | None = None,
    keyword: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve enriched ontology Fault records from the knowledge graph.

    Args:
        graph: The GraphService backend (FalkorDB in prod; fake in tests).
        candidate_ids: Stable Fault entity ids to fetch (the shared-ID join
            from vector hits / a parsed error code). Unknown ids are
            unmatched by Cypher and simply absent from the results.
        keyword: Free-text fallback matched against ontology Fault/Symptom/
            Cause node properties.
        limit: Maximum number of enriched Fault records to return.

    Returns:
        Result dicts, one per Fault, keyed by the stable entity id
        (``id`` = Fault node id), ``source="graph"``, ``score=0.0`` (the
        graph has no similarity score; rank comes from traversal order),
        plus enriched symptom/cause/solution/component/product/instrument/
        failed_step fields.

    Raises:
        CircuitBreakerOpenError: Propagated from the GraphService when the
            graph backend is down (the caller degrades the graph branch).
    """
    fault_ids = _dedupe(candidate_ids or [])
    if keyword.strip():
        rows = await graph.query(_CANDIDATES_BY_KEYWORD_CYPHER, {"keyword": keyword.strip()})
        for row in rows:
            fid = str(row.get("fault_id") or "").strip()
            if fid:
                fault_ids.append(fid)
        fault_ids = _dedupe(fault_ids)
    if not fault_ids:
        return []

    rows = await graph.query(
        _ENRICH_CYPHER + " LIMIT $limit",
        {"ids": fault_ids, "limit": limit},
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fault_id = str(row.get("fault_id") or "")
        if not fault_id or fault_id in seen:
            continue
        seen.add(fault_id)
        results.append(_to_result(fault_id, row))
    return results


def _dedupe(values: list[str]) -> list[str]:
    """Drop blank/duplicate ids preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        fid = str(value or "").strip()
        if fid and fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def _to_result(fault_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Map an enriched Cypher row to a hybrid-retriever result dict."""
    def _text(key: str) -> str:
        value = row.get(key)
        return str(value) if value else ""

    result: dict[str, Any] = {
        "id": fault_id,
        "score": 0.0,
        "source": "graph",
        "entity_id": fault_id,
        "symptom": _text("symptom"),
        "cause": _text("cause"),
        "solution": _text("solution"),
        "component": _text("component"),
        "product": _text("product"),
        "instrument": _text("instrument"),
    }
    error_code = _text("error_code")
    if error_code:
        result["error_code"] = error_code
    fault_kind = _text("fault_kind")
    if fault_kind:
        result["fault_kind"] = fault_kind
    return result


__all__ = [
    "extract_keyword",
    "fault_entity_id",
    "retrieve_faults",
]
