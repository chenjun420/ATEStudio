"""Tests for the deterministic ATML-aligned production-test domain ontology.

Covers (task 9 acceptance criteria):
- every required entity (class) and relationship (object property) exists;
- the FOUR duplicate instrument/fault vocabularies resolve to ONE enum/ID space;
- FMEA ratings live on the 1-10 scale (distinct from fixture 3-level Severity);
- OWL export produces valid, rdflib-parseable turtle to a defined path;
- SHACL validation PASSES on a conforming sample graph and FAILS with a
  violation report (no crash) on a graph missing a required relationship;
- Semantica stays a lazy import: importing the package never imports semantica.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ate_cloud.services.ontology import (
    ONTOLOGY_BASE_URI,
    OntologyServiceUnavailable,
    ValidationOutcome,
    build_domain_ontology,
)
from ate_cloud.services.ontology.vocab import (
    FaultCategory,
    FaultKind,
    InstrumentKind,
    canonical_fault,
    canonical_instrument,
    concepts,
    resolve_fault,
    resolve_instrument,
)

# ---------------------------------------------------------------------------
# Entity + relationship coverage
# ---------------------------------------------------------------------------

REQUIRED_CLASSES = [
    "Product",
    "Component",
    "Instrument",
    "Fixture",
    "TestItem",
    "TestStep",
    "TestRequirement",
    "TestCase",
    "UUTResult",
    "Fault",
    "Symptom",
    "Cause",
    "Solution",
    "FMEA",
    "TestStation",
]

REQUIRED_PROPERTIES = [
    "hasRequirement",      # Product -> TestRequirement
    "verifiedBy",          # TestRequirement -> TestCase
    "hasStep",             # TestCase -> TestStep (also TestItem -> TestStep)
    "hasCause",            # Fault/Symptom -> Cause
    "hasSolution",         # Cause -> Solution
    "affectsComponent",    # Fault -> Component
    "coversComponent",     # TestCase -> Component
    "fmeaCoversFault",     # FMEA -> Fault
    "fmeaCoversComponent", # FMEA -> Component
    "usesInstrument",      # TestStep -> Instrument
    "runsOnFixture",       # TestStep -> Fixture
    "runsOnStation",       # TestStep/TestCase -> TestStation
    "observedOn",          # UUTResult -> TestStep
    "producedResult",      # TestStep -> UUTResult
    "exhibits",            # Component/Product -> Fault
]


def test_required_entity_types_exist() -> None:
    """Every required ATML/FMEA entity type is present as an OWL class."""
    ontology = build_domain_ontology()
    names = {c["name"] for c in ontology["classes"]}
    missing = [n for n in REQUIRED_CLASSES if n not in names]
    assert missing == [], f"missing ontology classes: {missing}"


def test_required_relationship_types_exist() -> None:
    """Every required relationship is present as an object property with domain/range."""
    ontology = build_domain_ontology()
    props = {p["name"]: p for p in ontology["properties"]}
    missing = [n for n in REQUIRED_PROPERTIES if n not in props]
    assert missing == [], f"missing ontology properties: {missing}"
    for name in REQUIRED_PROPERTIES:
        prop = props[name]
        assert prop["type"] == "object", f"{name} must be an object property"
        assert prop["domain"], f"{name} needs a domain"
        assert prop["range"], f"{name} needs a range"


def test_ontology_models_both_atml_standards() -> None:
    """TestDescription (IEEE 1671) side and TestResults (IEEE 1636.1) side both modeled."""
    ontology = build_domain_ontology()
    comment = " ".join(str(c.get("comment", "")) for c in ontology["classes"]).lower()
    assert "1671" in comment, "IEEE 1671 TestDescription alignment must be documented"
    assert "1636.1" in comment, "IEEE 1636.1 TestResults alignment must be documented"
    # Requirements/cases/items/steps come from TestDescription; results from TestResults.
    assert ontology["uri"] == ONTOLOGY_BASE_URI


def test_fmea_ratings_use_one_to_ten_scale() -> None:
    """FMEA severity/occurrence/detection are 1-10 integer ratings (task 10 scale).

    Distinct from shared.fixture_topology.Severity (warning/error/critical),
    which is a 3-level runtime severity and is NOT reused here.
    """
    ontology = build_domain_ontology()
    props = {p["name"]: p for p in ontology["properties"]}
    for name in ("fmeaSeverity", "fmeaOccurrence", "fmeaDetection"):
        assert name in props, f"missing FMEA rating property {name}"
        prop = props[name]
        assert prop["type"] == "datatype"
        assert prop["range"] == "integer"
        assert prop.get("min") == 1 and prop.get("max") == 10


# ---------------------------------------------------------------------------
# Unified vocabulary
# ---------------------------------------------------------------------------

def test_vocab_concepts_are_controlled_and_stable() -> None:
    instrument_concepts = [c for c in concepts() if c.scheme == "instrument"]
    fault_concepts = [c for c in concepts() if c.scheme == "fault"]
    assert {c.canonical for c in instrument_concepts} == {k.value for k in InstrumentKind}
    assert {c.canonical for c in fault_concepts} == {k.value for k in FaultKind}
    # IDs are stable: canonical value == enum value == concept id.
    for concept in concepts():
        assert concept.canonical == concept.enum_member.value


def test_four_instrument_vocabularies_resolve_to_one_id_space() -> None:
    """FixtureDeviceTemplate.category, InstrumentType enum, KG Instrument node
    names, and InstrumentConfig.instrument_type free strings all resolve to
    InstrumentKind canonical IDs."""
    # 1. shared.fixture_topology.InstrumentType values (psu/dmm/...)
    from shared.fixture_topology import InstrumentType

    for member in InstrumentType:
        kind = resolve_instrument(member.value)
        assert kind is not None, f"InstrumentType.{member.name} unresolved"
        assert kind.canonical == canonical_instrument(member.value)

    # 2. KG seed free-text instrument names (kg_seeder FaultRecord.instrument)
    kg_names = [
        "Digital Multimeter",
        "Oscilloscope",
        "Logic Analyzer",
        "CAN Analyzer",
        "USB Analyzer",
        "Cable Tester",
        "Spectrum Analyzer",
        "Thermal Camera",
    ]
    for name in kg_names:
        kind = resolve_instrument(name)
        assert kind is not None, f"KG instrument name {name!r} unresolved"
        assert kind.scheme == "instrument"

    # 3. InstrumentConfig.instrument_type free strings (config_schema examples)
    config_strings = ["digital_multimeter", "DMM", "oscilloscope", "psu", "PSU", "eload"]
    resolved = {canonical_instrument(s) for s in config_strings}
    assert None not in resolved
    assert len({c for c in resolved if c}) >= 3

    # 4. FixtureDeviceTemplate.category is the entity bucket; type carries the
    # instrument kind — a template "type" string resolves like any other vocab.
    template_type = "psu"
    assert resolve_instrument(template_type) is not None

    # Case-insensitive unknown => None, not a crash.
    assert resolve_instrument("not-a-real-instrument") is None


def test_fault_vocabularies_resolve_to_one_id_space() -> None:
    """Fixture FaultType (7 electrical), KG 6 FMEA categories, and free-text
    fault words all resolve to the unified fault vocab (kind + category)."""
    from shared.fixture_topology import FaultType

    for member in FaultType:
        concept = resolve_fault(member.value)
        assert concept is not None, f"FaultType.{member.name} unresolved"
        assert concept.scheme == "fault"
        assert isinstance(FaultCategory(concept.category), FaultCategory)

    # KG seeder category constants.
    kg_categories = [c.value for c in FaultCategory]
    for cat in [
        "communication_interconnects",
        "power",
        "assembly_soldering",
        "passive_components",
        "environmental_esd",
        "mixed_signal_timing",
    ]:
        assert cat in kg_categories

    # Free-text symptom words (fault_injector / failure descriptions).
    assert canonical_fault("over voltage") == FaultKind.OVER_VOLTAGE.value
    assert canonical_fault("short circuit") == FaultKind.SHORT_CIRCUIT.value
    assert canonical_fault("unknown-gibberish") is None


def test_vocab_namespace_is_single_skos_scheme() -> None:
    """Instruments and faults are published as ONE SKOS concept scheme each,
    and the ontology enumerates the instrument kinds as sh:in values on the
    Instrument.instrumentKind property."""
    ontology = build_domain_ontology()
    props = {p["name"]: p for p in ontology["properties"]}
    kind_prop = props["instrumentKind"]
    assert kind_prop["type"] == "datatype"
    assert set(kind_prop["one_of"]) == {k.value for k in InstrumentKind}


# ---------------------------------------------------------------------------
# OWL export
# ---------------------------------------------------------------------------

def test_export_owl_writes_valid_turtle(tmp_path: Path) -> None:
    from ate_cloud.services.ontology import build_ontology_service

    out = tmp_path / "ate-ontology.ttl"
    service = build_ontology_service()
    service.export_owl(out)

    assert out.exists() and out.stat().st_size > 0
    # Parseable as valid RDF turtle.
    import rdflib

    graph = rdflib.Graph()
    graph.parse(out.as_posix(), format="turtle")

    # All required classes are declared owl:Class.
    owl_ns = rdflib.Namespace("http://www.w3.org/2002/07/owl#")
    class_names = {
        str(s).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        for s in graph.subjects(rdflib.RDF.type, owl_ns.Class)
    }
    for name in REQUIRED_CLASSES:
        assert name in class_names, f"{name} not declared owl:Class in OWL"


def test_export_owl_supports_rdfxml(tmp_path: Path) -> None:
    from ate_cloud.services.ontology import build_ontology_service

    out = tmp_path / "ate-ontology.owl"
    service = build_ontology_service()
    service.export_owl(out, fmt="rdfxml")

    import xml.etree.ElementTree as ET

    ET.fromstring(out.read_text(encoding="utf-8"))  # well-formed XML, no raise


# ---------------------------------------------------------------------------
# SHACL validation
# ---------------------------------------------------------------------------

def test_shacl_passes_on_conforming_sample_graph(tmp_path: Path) -> None:
    from ate_cloud.services.ontology import build_ontology_service

    service = build_ontology_service()
    sample = service.conforming_sample_graph()
    outcome = service.validate_sample_graph(sample)

    assert isinstance(outcome, ValidationOutcome)
    assert outcome.conforms, f"conforming graph reported violations: {outcome.violations}"
    assert outcome.violation_count == 0


def test_shacl_fails_with_report_on_missing_relationship(tmp_path: Path) -> None:
    from ate_cloud.services.ontology import build_ontology_service

    service = build_ontology_service()
    # A TestCase node with NO verifiedBy/hasStep/... required edges.
    bad_graph = f"""
@prefix ex: <{ONTOLOGY_BASE_URI}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:TestCaseBad a ex:TestCase ;
    ex:identifier "TC-BAD" ;
    ex:title "incomplete case" .
"""
    outcome = service.validate_sample_graph(bad_graph)

    assert not outcome.conforms
    assert outcome.violation_count >= 1
    # The report names the missing required relationship and the focus node.
    paths = [v["result_path"] for v in outcome.violations]
    assert any("hasStep" in p for p in paths), f"expected missing-hasStep violation, got: {paths}"
    focus = [v["focus_node"] for v in outcome.violations]
    assert any("TestCaseBad" in f for f in focus)


def test_shacl_unavailable_raises_controlled_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pyshacl/Semantica validation cannot be built, callers get a
    controlled exception (never an app-boot crash) — mirrors kg_pipeline."""
    from ate_cloud.services import ontology as ontology_pkg

    service = ontology_pkg.build_ontology_service()

    def _boom(*args: object, **kwargs: object) -> object:
        raise ImportError("pyshacl is required for SHACL validation")

    monkeypatch.setattr(service, "_run_validation", _boom, raising=False)
    with pytest.raises(OntologyServiceUnavailable):
        service.validate_sample_graph("@prefix ex: <http://x/> . ex:a a ex:B .")


# ---------------------------------------------------------------------------
# Boundary discipline
# ---------------------------------------------------------------------------

def test_semantica_is_not_imported_by_package_import() -> None:
    """Importing the ontology facade must not import semantica (lazy boundary)."""
    import os
    import subprocess

    env = dict(os.environ)
    src = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = str(src)
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import ate_cloud.services.ontology; "
         "print('semantica' in sys.modules)"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("False")
