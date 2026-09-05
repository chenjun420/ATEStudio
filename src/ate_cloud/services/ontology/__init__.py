"""ontology — deterministic ATML-aligned production-test domain ontology.

Hand-authored (deterministic, stable, testable; NO LLM for the core schema —
LLM enrichment hooks arrive in task 12). The package models the production-test
domain for the knowledge graph and requirement/case/FMEA features:

- Entities: Product, Component, Instrument, Fixture, TestStation, TestItem,
  TestStep, TestRequirement, TestCase, UUTResult, Fault (Symptom→Cause→Solution),
  FMEA.
- Relationships: requirement→test case→test step, fault symptom→cause→solution,
  fault affects component, FMEA→fault/component, test uses instrument,
  test runs on fixture/station, step→UUT result (IEEE 1636.1).
- Unified controlled vocabulary: the FOUR legacy instrument/fault vocabularies
  (FixtureDeviceTemplate category/type, shared.fixture_topology InstrumentType/
  FaultType, KG seed Instrument nodes/categories, InstrumentConfig free strings)
  resolve to ONE ID space — :mod:`ate_cloud.services.ontology.vocab`.

Standard reconciliation: IEEE 1671 TestDescription models requirements/cases/
items/steps; IEEE 1636.1 TestResults (what atml_exporter emits) models UUT
results. FMEA severity/occurrence/detection are a 1-10 scale (distinct from the
fixture 3-level runtime Severity).

Boundary discipline (mirrors kg_pipeline): Semantica/pyshacl are imported ONLY
inside :mod:`ate_cloud.services.ontology._semantica`, lazily. Importing this
package never imports Semantica; backend failures raise
:class:`OntologyServiceUnavailable` and never crash app boot.
"""

from __future__ import annotations

from ate_cloud.services.ontology.core import (
    ATML_1636_NS,
    ATML_1671_NS,
    ONTOLOGY_BASE_URI,
    ONTOLOGY_NAME,
    ONTOLOGY_VERSION,
    build_domain_ontology,
)
from ate_cloud.services.ontology.errors import (
    OntologyError,
    OntologyServiceUnavailable,
)
from ate_cloud.services.ontology.service import (
    DEFAULT_OWL_PATH,
    DomainOntologyService,
    ValidationOutcome,
    build_ontology_service,
)

__all__ = [
    "ATML_1636_NS",
    "ATML_1671_NS",
    "DEFAULT_OWL_PATH",
    "DomainOntologyService",
    "ONTOLOGY_BASE_URI",
    "ONTOLOGY_NAME",
    "ONTOLOGY_VERSION",
    "OntologyError",
    "OntologyServiceUnavailable",
    "ValidationOutcome",
    "build_domain_ontology",
    "build_ontology_service",
]
