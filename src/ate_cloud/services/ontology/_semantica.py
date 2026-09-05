"""Semantica adapter — the ONLY module in the ontology package that imports
Semantica / pyshacl.

Verified against semantica 0.6.7 (installed):
- ``semantica.ontology.OWLGenerator().export_owl(ontology, path, format=...)``
  writes OWL/RDF (rdflib-backed; rdflib is a semantica core dependency).
- ``semantica.ontology.ontology_generator.SHACLGenerator(base_uri=...,
  target_namespace=...).generate(ontology)`` returns a ``SHACLGraph``;
  ``.serialize(graph, "turtle")`` emits SHACL shapes. Standalone use avoids
  the heavyweight ``OntologyEngine`` ctor (which builds a change-management
  VersionManager).
- ``semantica.ontology.ontology_validator.run_shacl_validation(data, shacl,
  data_graph_format=..., shacl_format=...)`` returns a
  ``SHACLValidationReport`` (``conforms`` / ``violations`` / ``raw_report``);
  it imports ``pyshacl`` lazily and raises ``ImportError`` when absent
  (install via the ``semantica[shacl]`` extra, already in pyproject).

All failures crossing this module are converted to
:class:`~ate_cloud.services.ontology.errors.OntologyServiceUnavailable` so the
app boots even if Semantica/pyshacl break.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ate_cloud.services.ontology.errors import OntologyServiceUnavailable

logger = logging.getLogger(__name__)


def export_owl(ontology: dict[str, Any], path: Path, fmt: str) -> None:
    """Serialize ``ontology`` to an OWL file (``turtle`` or ``rdfxml``)."""
    try:
        from semantica.ontology import OWLGenerator

        OWLGenerator().export_owl(ontology, str(path), format=fmt)
    except Exception as exc:  # noqa: BLE001 — boundary: normalize to controlled error
        logger.warning("OWL export failed: %s", exc)
        raise OntologyServiceUnavailable(f"OWL export unavailable: {exc}") from exc


def generate_shacl(ontology: dict[str, Any], shapes_base_uri: str, target_namespace: str) -> str:
    """Generate SHACL shapes (turtle) for ``ontology`` via Semantica."""
    try:
        from semantica.ontology.ontology_generator import SHACLGenerator

        generator = SHACLGenerator(
            base_uri=shapes_base_uri,
            target_namespace=target_namespace,
            severity="Violation",
            quality_tier="standard",
        )
        graph = generator.generate(ontology)
        serialized: str = generator.serialize(graph, format="turtle")
        return serialized
    except Exception as exc:  # noqa: BLE001 — boundary
        logger.warning("SHACL generation failed: %s", exc)
        raise OntologyServiceUnavailable(f"SHACL generation unavailable: {exc}") from exc


def run_shacl(data_graph_turtle: str, shacl_turtle: str) -> Any:
    """Validate a data graph against SHACL shapes. Returns Semantica's report.

    Raises :class:`OntologyServiceUnavailable` when pyshacl/rdflib are missing
    or validation cannot run.
    """
    try:
        from semantica.ontology.ontology_validator import run_shacl_validation
    except Exception as exc:  # noqa: BLE001 — import boundary
        logger.warning("Semantica SHACL validator unavailable: %s", exc)
        raise OntologyServiceUnavailable(f"SHACL validator unavailable: {exc}") from exc

    try:
        return run_shacl_validation(
            data_graph_turtle,
            shacl_turtle,
            data_graph_format="turtle",
            shacl_format="turtle",
        )
    except ImportError as exc:  # pyshacl not installed
        logger.warning("pyshacl not installed: %s", exc)
        raise OntologyServiceUnavailable(f"SHACL validation unavailable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — malformed graph/shapes
        logger.warning("SHACL validation failed: %s", exc)
        raise OntologyServiceUnavailable(f"SHACL validation failed: {exc}") from exc


__all__ = ["export_owl", "generate_shacl", "run_shacl"]
