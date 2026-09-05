"""In-memory ontology GraphService fake for cloud retrieval tests.

Seeded from the pure :func:`ate_cloud.services.kg_seed_facts.build_seed_graph`
(no FalkorDB/Qdrant needed) and answers the two Cypher statement shapes that
:mod:`ate_cloud.services.kg_retrieval` emits, by recognizing the statement
shape (the same MERGE/shape-interpreting approach as
``InMemoryGraphService`` in ``test_kg_seeder.py`` — there is deliberately no
full Cypher parser):

* the keyword fallback scan (``... CONTAINS toLower($keyword) ...``) →
  matching Fault ids;
* the Fault-enrichment traversal (``coalesce(f.name ...)``) → one row per
  Fault with its symptom/cause/solution/component/product/instrument.

A ``fail_with`` exception makes every query raise (used for the
graph-outage degrade tests).
"""

from __future__ import annotations

from typing import Any

from ate_cloud.services.kg_seed_facts import build_seed_graph


class OntologyGraphFake:
    """GraphService fake holding the seeded ontology graph in memory."""

    def __init__(self) -> None:
        # id -> {"id", "label", "name", **props}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []  # (src, rel, dst)
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.fail_with: Exception | None = None

    # ── seeding ────────────────────────────────────────────────────────
    def seed_ontology(self) -> OntologyGraphFake:
        """Populate nodes/edges from the deterministic ontology seed."""
        nodes_by_label, edges = build_seed_graph()
        for label, seed_nodes in nodes_by_label.items():
            for node in seed_nodes:
                self.nodes[node.node_id] = {
                    "id": node.node_id,
                    "label": label,
                    "name": node.name,
                    **dict(node.props),
                }
        self.edges.extend((e.src, e.rel, e.dst) for e in edges)
        return self

    # ── GraphService surface ───────────────────────────────────────────
    async def query(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self.fail_with is not None:
            raise self.fail_with
        self.queries.append((statement, params))
        params = params or {}
        if "count(n)" in statement:
            return [{"total": len(self.nodes)}]
        if "count(r)" in statement:
            return [{"total": len(self.edges)}]
        if "CONTAINS toLower" in statement:
            return self._keyword_rows(str(params.get("keyword", "")))
        if "coalesce(f.name" in statement:
            return self._enrich_rows(
                list(params.get("ids", [])), int(params.get("limit", 10))
            )
        return []

    async def write(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def create_constraints(self) -> None:
        return None

    async def count_nodes(self) -> int:
        return len(self.nodes)

    async def count_relationships(self) -> int:
        return len(self.edges)

    async def health(self) -> dict[str, Any]:
        return {"status": "healthy", "backend": "in-memory-ontology"}

    # ── traversal helpers ──────────────────────────────────────────────
    def _neighbors(self, src: str, rel: str) -> list[str]:
        return [dst for (s, r, dst) in self.edges if s == src and r == rel]

    def _node(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(node_id)

    def _keyword_rows(self, keyword: str) -> list[dict[str, Any]]:
        """Ontology Fault/Symptom/Cause property CONTAINS scan → Fault ids."""
        kw = keyword.lower().strip()
        if not kw:
            return []
        rows: list[dict[str, Any]] = []
        for node in self.nodes.values():
            if node.get("label") != "Fault":
                continue
            haystack = [
                str(node.get("error_code", "")),
                str(node.get("name", "")),
                str(node.get("description_en", "")),
            ]
            for symptom_id in self._neighbors(node["id"], "HAS_SYMPTOM"):
                sym = self._node(symptom_id)
                if sym:
                    haystack.append(str(sym.get("name", "")))
                    for cause_id in self._neighbors(symptom_id, "HAS_CAUSE"):
                        cause = self._node(cause_id)
                        if cause:
                            haystack.append(str(cause.get("name", "")))
            if any(kw in text.lower() for text in haystack):
                rows.append({"fault_id": node["id"]})
        return rows

    def _enrich_rows(self, fault_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """Fault enrichment traversal (mirrors kg_retrieval._ENRICH_CYPHER)."""
        rows: list[dict[str, Any]] = []
        for fid in fault_ids[:limit]:
            fault = self._node(fid)
            if fault is None or fault.get("label") != "Fault":
                continue

            symptom_id = next(iter(self._neighbors(fid, "HAS_SYMPTOM")), None)
            symptom = self._node(symptom_id) if symptom_id else None
            cause_id = (
                next(iter(self._neighbors(symptom_id, "HAS_CAUSE")), None)
                if symptom_id else None
            )
            cause = self._node(cause_id) if cause_id else None
            solution_id = (
                next(iter(self._neighbors(cause_id, "HAS_SOLUTION")), None)
                if cause_id else None
            )
            solution = self._node(solution_id) if solution_id else None
            component = self._node(next(iter(self._neighbors(fid, "AFFECTS_COMPONENT")), None))
            product = self._node(next(iter(self._neighbors(fid, "OCCURS_IN_PRODUCT")), None))
            instrument = self._node(next(iter(self._neighbors(fid, "DIAGNOSED_WITH")), None))

            rows.append({
                "fault_id": fid,
                "error_code": fault.get("error_code", ""),
                "fault_kind": fault.get("fault_kind", ""),
                "symptom": (symptom or {}).get("name", "") or fault.get("name", ""),
                "cause": (cause or {}).get("name", ""),
                "solution": (solution or {}).get("name", ""),
                "component": (component or {}).get("name", ""),
                "product": (product or {}).get("name", "")
                or (product or {}).get("product_type", ""),
                "instrument": (instrument or {}).get("name", "")
                or (instrument or {}).get("instrument_kind", ""),
            })
        return rows


__all__ = ["OntologyGraphFake"]
