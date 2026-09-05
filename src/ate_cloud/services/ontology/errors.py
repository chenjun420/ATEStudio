"""Controlled exceptions for the domain-ontology service boundary.

Mirrors :mod:`ate_cloud.services.kg_pipeline.errors`: Semantica / pyshacl are
imported lazily inside the ontology package, and any construction or import
failure is converted to :class:`OntologyServiceUnavailable` so callers can
degrade (503 / skip validation) while the application keeps booting.
"""

from __future__ import annotations


class OntologyError(Exception):
    """Base class for ontology failures crossing the service boundary."""


class OntologyServiceUnavailable(OntologyError):  # noqa: N818 — boundary vocabulary (503)
    """The Semantica/pyshacl ontology backend cannot be imported or built.

    Raised when Semantica or pyshacl fails to import, or when OWL/SHACL
    generation cannot run. Callers translate this to a controlled degrade;
    it never crashes app startup.
    """


__all__ = ["OntologyError", "OntologyServiceUnavailable"]
