"""Deterministic ATML-aligned production-test domain ontology.

Hand-authored (NO LLM — LLM enrichment hooks arrive in task 12). The ontology
is a plain, stable Python data structure shaped for Semantica's ontology
layer (``OntologyEngine.export_owl`` / ``to_shacl`` / ``validate_graph``),
but it imports nothing from Semantica: this module is safe to import at app
boot and carries zero optional dependencies.

Standard reconciliation (docs vs. code):
- IEEE 1671 (ATML TestDescription) supplies the *requirements* side:
  TestDescription → TestItem/TestStep, TestRequirement, TestCase.
- IEEE 1636.1 (ATML TestResults — what ``atml_exporter.py`` emits) supplies
  the *results* side: UUTResult / outcomes for executed steps.
Both are modeled explicitly so requirement→case→step traceability and the
executed results live in one coherent vocabulary.

FMEA severity/occurrence/detection are 1-10 integer ratings (task 10). The
fixture runtime ``Severity`` (warning/error/critical, 3-level) is a separate
concept and is deliberately NOT reused for FMEA.
"""

from __future__ import annotations

from typing import Any

from ate_cloud.services.ontology.vocab import FaultKind, InstrumentKind

ONTOLOGY_BASE_URI = "https://atestudio.io/ontology/ate#"
ONTOLOGY_VERSION = "1.0.0"
ONTOLOGY_NAME = "ATE Studio Production-Test Domain Ontology"

#: RDF vocabulary namespaces referenced in comments/IRIs (informational).
ATML_1671_NS = "urn:IEEE-1671:2010:TestDescription"
ATML_1636_NS = "urn:IEEE-1636.1:2012:TestResults"


def _cls(name: str, comment: str, parent: str | None = None) -> dict[str, Any]:
    """Build an OWL class definition with an explicit, stable IRI."""
    definition: dict[str, Any] = {
        "name": name,
        "uri": f"{ONTOLOGY_BASE_URI}{name}",
        "label": name,
        "comment": comment,
    }
    if parent:
        definition["parent"] = parent
    return definition


def _obj(
    name: str,
    domain: str | list[str],
    rng: str | list[str],
    *,
    comment: str,
    required: bool = False,
) -> dict[str, Any]:
    """Build an object property (relationship) definition."""
    return {
        "name": name,
        "uri": f"{ONTOLOGY_BASE_URI}{name}",
        "label": name,
        "type": "object",
        "domain": [domain] if isinstance(domain, str) else domain,
        "range": [rng] if isinstance(rng, str) else rng,
        "comment": comment,
        "required": required,
    }


def _dat(
    name: str,
    domain: str | list[str],
    rng: str,
    *,
    comment: str,
    required: bool = False,
    one_of: list[str] | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> dict[str, Any]:
    """Build a datatype property definition.

    ``range`` is a bare XSD type name (``string``/``integer``/``float``) — the
    shape Semantica's SHACL generator expects; the OWL adapter prefixes it with
    ``xsd:``. ``one_of`` becomes ``sh:in``; ``min_value``/``max_value`` carry a
    numeric range Semantica's PropertyShape cannot express in SHACL (enforced
    by the service/model layer, documented in the ontology).
    """
    prop: dict[str, Any] = {
        "name": name,
        "uri": f"{ONTOLOGY_BASE_URI}{name}",
        "label": name,
        "type": "datatype",
        "domain": [domain] if isinstance(domain, str) else domain,
        "range": rng,
        "comment": comment,
        "required": required,
    }
    if one_of is not None:
        prop["one_of"] = one_of
    if min_value is not None:
        prop["min"] = min_value
    if max_value is not None:
        prop["max"] = max_value
    return prop


_CLASSES: tuple[dict[str, Any], ...] = (
    _cls("Product", "Product / UUT type under test (ATML UUT). The manufactured item a test program targets."),
    _cls("Component", "Electronic component on a product (IC, bus, sensor, connector, passive, ...)."),
    _cls("Instrument", "Test instrument (PSU, DMM, oscilloscope, ...); instrumentKind comes from the unified vocab."),
    _cls("Fixture", "Test fixture / jig (ATML TestFixture; fixture topology Instrument/Fixture entities)."),
    _cls("TestStation", "Test station / cell on which a test runs (ATML TestStation, station_config)."),
    _cls("TestItem", "ATML IEEE 1671 TestDescription TestItem: a named test group/item in a test program."),
    _cls("TestStep", "ATML IEEE 1671 TestStep: an executable step/measurement within a test item."),
    _cls("TestRequirement", "Verifiable requirement from product spec (IEEE 1671 TestDescription side)."),
    _cls("TestCase", "Test case implementing requirements (IEEE 1671 TestDescription), mapped to DSL steps."),
    _cls("UUTResult", "UUT result/outcome for an executed step (IEEE 1636.1 TestResults: Outcome/Measurement)."),
    _cls("Fault", "Failure mode: a symptom with a root cause and solution (Symptom->Cause->Solution FMEA chain)."),
    _cls("Symptom", "Observable fault symptom (kg_seeder FaultSymptom)."),
    _cls("Cause", "Root cause of a fault symptom (kg_seeder Cause)."),
    _cls("Solution", "Repair / resolution action for a fault cause (kg_seeder Solution)."),
    _cls("FMEA", "FMEA analysis entry: failure mode + effect + 1-10 severity/occurrence/detection ratings and RPN."),
)

_OBJECT_PROPERTIES: tuple[dict[str, Any], ...] = (
    # Traceability chain: requirement -> test case -> test step.
    _obj("hasRequirement", "Product", "TestRequirement",
         comment="Product carries a test requirement (IEEE 1671 TestDescription).", required=True),
    _obj("verifiedBy", "TestRequirement", "TestCase",
         comment="A test requirement is verified by one or more test cases.", required=True),
    _obj("hasStep", ["TestCase", "TestItem"], "TestStep",
         comment="A test case / test item contains executable test steps (IEEE 1671).", required=True),
    _obj("partOfItem", "TestStep", "TestItem",
         comment="A test step belongs to a test item."),
    _obj("coversComponent", "TestCase", "Component",
         comment="A test case exercises/covers a component on the product."),
    # Fault chain: symptom -> cause -> solution.
    _obj("hasSymptom", "Fault", "Symptom",
         comment="A fault presents an observable symptom.", required=True),
    _obj("hasCause", "Symptom", "Cause",
         comment="A symptom has a root cause (FMEA Symptom->Cause).", required=True),
    _obj("hasSolution", "Cause", "Solution",
         comment="A root cause has a repair solution (FMEA Cause->Solution).", required=True),
    _obj("affectsComponent", "Fault", "Component",
         comment="A fault affects / is located on a component (kg_seeder AFFECTS_COMPONENT).", required=True),
    _obj("exhibits", ["Component", "Product"], "Fault",
         comment="A component/product exhibits a fault."),
    _obj("observedOn", ["Symptom", "Fault"], "TestStep",
         comment="A fault/symptom is observed during a test step."),
    # FMEA coverage.
    _obj("fmeaCoversFault", "FMEA", "Fault",
         comment="An FMEA entry analyzes a fault/failure mode.", required=True),
    _obj("fmeaCoversComponent", "FMEA", "Component",
         comment="An FMEA entry applies to a component/function.", required=True),
    # Test execution resources.
    _obj("usesInstrument", "TestStep", "Instrument",
         comment="A test step uses / drives a test instrument.", required=True),
    _obj("runsOnFixture", "TestStep", "Fixture",
         comment="A test step runs with / through a test fixture."),
    _obj("runsOnStation", ["TestStep", "TestCase"], "TestStation",
         comment="A test step/case executes on a test station."),
    # Results side (IEEE 1636.1).
    _obj("producedResult", "TestStep", "UUTResult",
         comment="An executed test step produced a UUT result (IEEE 1636.1 TestResults).", required=True),
    _obj("resultFor", "UUTResult", "Product",
         comment="A UUT result is for the tested product/UUT."),
    _obj("resultIndicatesFault", "UUTResult", "Fault",
         comment="A failing UUT result indicates a fault."),
    _obj("hasComponent", "Product", "Component",
         comment="A product contains a component."),
    _obj("mountedOnFixture", "Instrument", "Fixture",
         comment="An instrument is wired into / mounted on a fixture."),
)

#: FMEA ratings are integers in [1, 10]. SHACL/sh:in cannot express a numeric
#: range over integers cleanly, so the bounds are recorded as min/max (enforced
#: by the service/model layer in task 10/13) while SHACL enforces xsd:integer.
_FMEA_MIN = 1
_FMEA_MAX = 10

_DATATYPE_PROPERTIES: tuple[dict[str, Any], ...] = (
    _dat("identifier", ["TestCase", "TestRequirement", "TestItem", "TestStep"], "string",
         comment="Stable identifier (e.g. requirement/case/step id).", required=True),
    _dat("title", ["TestCase", "TestRequirement", "TestItem"], "string",
         comment="Human-readable title/name."),
    _dat("standard", ["TestCase", "TestRequirement", "TestItem", "UUTResult"], "string",
         comment="ATML standard alignment: 'IEEE 1671 TestDescription' or 'IEEE 1636.1 TestResults'."),
    _dat("outcome", "UUTResult", "string",
         comment="ATML outcome verdict: Passed / Failed / Aborted / Inconclusive (IEEE 1636.1).",
         one_of=["Passed", "Failed", "Aborted", "Inconclusive"]),
    _dat("measuredValue", "UUTResult", "float",
         comment="Measured value recorded for the result (IEEE 1636.1 Measurement)."),
    _dat("instrumentKind", "Instrument", "string",
         comment="Canonical instrument type from the unified controlled vocabulary (InstrumentKind).",
         required=True, one_of=[k.value for k in InstrumentKind]),
    _dat("faultKind", "Fault", "string",
         comment="Canonical failure mode from the unified controlled vocabulary (FaultKind).",
         one_of=[k.value for k in FaultKind]),
    _dat("faultCategory", "Fault", "string",
         comment="FMEA fault category (6 unified buckets)."),
    _dat("fmeaSeverity", "FMEA", "integer",
         comment="FMEA severity rating, 1 (negligible) to 10 (catastrophic). Distinct from fixture runtime Severity.",
         required=True, min_value=_FMEA_MIN, max_value=_FMEA_MAX),
    _dat("fmeaOccurrence", "FMEA", "integer",
         comment="FMEA occurrence rating, 1 (rare) to 10 (almost inevitable).",
         required=True, min_value=_FMEA_MIN, max_value=_FMEA_MAX),
    _dat("fmeaDetection", "FMEA", "integer",
         comment="FMEA detection rating, 1 (certain detection) to 10 (undetectable).",
         required=True, min_value=_FMEA_MIN, max_value=_FMEA_MAX),
    _dat("rpn", "FMEA", "integer",
         comment="Risk priority number = severity x occurrence x detection (1-1000)."),
    _dat("failureMode", "FMEA", "string",
         comment="FMEA failure mode description.", required=True),
    _dat("failureEffect", "FMEA", "string",
         comment="FMEA failure effect description."),
    _dat("failureCause", "FMEA", "string",
         comment="FMEA failure cause description."),
)


def build_domain_ontology() -> dict[str, Any]:
    """Return the deterministic domain ontology as a Semantica-shaped dict.

    Shape (consumed by ``OntologyEngine.export_owl`` / ``to_shacl``)::

        {"uri", "name", "version", "namespace": {"base_uri"},
         "classes": [{"name", "uri", "label", "comment", "parent"?}],
         "properties": [{"name", "uri", "type": "object"|"datatype",
                         "domain", "range", "required"?, "one_of"?}]}
    """
    return {
        "uri": ONTOLOGY_BASE_URI,
        "name": ONTOLOGY_NAME,
        "version": ONTOLOGY_VERSION,
        "namespace": {"base_uri": ONTOLOGY_BASE_URI},
        "classes": [dict(c) for c in _CLASSES],
        "properties": [dict(p) for p in (*_OBJECT_PROPERTIES, *_DATATYPE_PROPERTIES)],
    }


__all__ = [
    "ATML_1636_NS",
    "ATML_1671_NS",
    "ONTOLOGY_BASE_URI",
    "ONTOLOGY_NAME",
    "ONTOLOGY_VERSION",
    "build_domain_ontology",
]
