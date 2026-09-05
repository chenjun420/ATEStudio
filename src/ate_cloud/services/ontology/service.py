"""Domain ontology service facade.

The deterministic core (:mod:`ate_cloud.services.ontology.core`) needs no
Semantica; this facade adds the optional, lazily-imported Semantica/pyshacl
capabilities — OWL export and SHACL validation — behind the same boundary
discipline as :mod:`ate_cloud.services.kg_pipeline`: Semantica is imported
only inside :mod:`ate_cloud.services.ontology._semantica`, and any backend
failure is raised as :class:`OntologyServiceUnavailable` (callers degrade),
never an app-boot crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ate_cloud.services.ontology import _semantica
from ate_cloud.services.ontology.core import ONTOLOGY_BASE_URI, build_domain_ontology
from ate_cloud.services.ontology.errors import OntologyServiceUnavailable
from ate_cloud.services.ontology.sample import conforming_sample_graph

#: Where the SHACL shapes themselves live (distinct from the terms' namespace).
SHAPES_BASE_URI = "https://atestudio.io/ontology/shapes/"

#: Default on-disk location for the exported OWL ontology (relative to repo).
DEFAULT_OWL_PATH = Path("data/ontology/ate-production-test-ontology.ttl")


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Boundary-safe SHACL validation result (no Semantica type leaks)."""

    conforms: bool
    violations: list[dict[str, str]] = field(default_factory=list)
    text: str = ""

    @property
    def violation_count(self) -> int:
        return len(self.violations)


class DomainOntologyService:
    """Owns the deterministic ontology and provides OWL/SHACL operations."""

    def __init__(self) -> None:
        self._ontology: dict[str, Any] | None = None
        self._shacl: str | None = None

    # ── Deterministic core (no Semantica) ───────────────────────────────────
    def ontology(self) -> dict[str, Any]:
        """Return the deterministic domain ontology dict (cached)."""
        if self._ontology is None:
            self._ontology = build_domain_ontology()
        return self._ontology

    def conforming_sample_graph(self) -> str:
        """Return a hand-authored turtle data graph that satisfies all shapes."""
        return conforming_sample_graph()

    # ── OWL export (Semantica/rdflib, lazy) ─────────────────────────────────
    def export_owl(self, path: Path | str = DEFAULT_OWL_PATH, fmt: str = "turtle") -> Path:
        """Export the ontology as OWL/RDF to ``path`` (turtle or rdfxml).

        Returns the resolved path. Raises :class:`OntologyServiceUnavailable`
        when the backend is missing/broken (caller degrades; app stays up).
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        semantica_fmt = "rdfxml" if fmt in ("rdfxml", "xml", "owl") else "turtle"
        _semantica.export_owl(self.ontology(), out, semantica_fmt)
        return out

    # ── SHACL validation (Semantica/pyshacl, lazy) ──────────────────────────
    def shacl_shapes(self) -> str:
        """Generate (and cache) the SHACL shapes turtle for the ontology."""
        if self._shacl is None:
            self._shacl = _semantica.generate_shacl(
                self.ontology(), SHAPES_BASE_URI, ONTOLOGY_BASE_URI
            )
        return self._shacl

    def validate_sample_graph(self, data_graph_turtle: str) -> ValidationOutcome:
        """Validate a turtle data graph against the domain SHACL shapes.

        Returns a :class:`ValidationOutcome`. A conforming graph reports
        ``conforms=True``; a non-conforming graph returns a report listing each
        violation (no raise). Backend/import problems raise
        :class:`OntologyServiceUnavailable`.
        """
        try:
            report = self._run_validation(data_graph_turtle, self.shacl_shapes())
        except OntologyServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — boundary: never leak backend errors
            raise OntologyServiceUnavailable(f"SHACL validation unavailable: {exc}") from exc
        violations = [
            {
                "focus_node": str(getattr(v, "focus_node", "")),
                "result_path": str(getattr(v, "result_path", "") or ""),
                "constraint": str(getattr(v, "constraint", "")),
                "message": str(getattr(v, "message", "") or ""),
            }
            for v in getattr(report, "violations", [])
        ]
        return ValidationOutcome(
            conforms=bool(getattr(report, "conforms", False)),
            violations=violations,
            text=str(getattr(report, "raw_report", "") or ""),
        )

    def _run_validation(self, data_graph_turtle: str, shacl_turtle: str) -> Any:
        """Indirection point so tests can force a backend failure."""
        return _semantica.run_shacl(data_graph_turtle, shacl_turtle)


def build_ontology_service() -> DomainOntologyService:
    """Factory mirroring :func:`kg_pipeline.build_pipeline`."""
    return DomainOntologyService()


__all__ = [
    "DEFAULT_OWL_PATH",
    "SHAPES_BASE_URI",
    "DomainOntologyService",
    "ValidationOutcome",
    "build_ontology_service",
]
