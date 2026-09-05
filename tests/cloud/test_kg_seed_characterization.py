"""Characterization test: pins the preserved seed facts carried into the
ontology-aligned rewrite (task 8).

The 104 hand-authored FMEA fault records (6 categories, 30 free-text
instruments, 13 legacy component buckets, 104 unique error codes) were
migrated verbatim from the legacy ad-hoc Neo4j seeder into the pure-data
module ``kg_seed_data``. Before the rewrite, a characterization test ran
against the legacy module and pinned this exact content (104 records,
category counts 18/18/17/17/17/17, 30 instrument names, unique codes, 13
component types); the new data module was then diffed field-by-field against
the legacy dataclass tuples (identical). This test locks that preserved
content so the ontology mapping and any future change cannot silently drop
or alter a fact.
"""

from __future__ import annotations

from collections import Counter

from ate_cloud.services.kg_seed_data import (
    CAT_ASSEMBLY,
    CAT_COMM,
    CAT_ENVIRONMENT,
    CAT_MIXED,
    CAT_PASSIVE,
    CAT_POWER,
    FAULT_RECORDS,
)


def test_preserved_fact_count() -> None:
    assert len(FAULT_RECORDS) == 104


def test_preserved_six_categories() -> None:
    counts = Counter(r.category for r in FAULT_RECORDS)
    assert counts == {
        CAT_COMM: 18,
        CAT_POWER: 18,
        CAT_ASSEMBLY: 17,
        CAT_PASSIVE: 17,
        CAT_ENVIRONMENT: 17,
        CAT_MIXED: 17,
    }


def test_preserved_instrument_names() -> None:
    instruments = {r.instrument for r in FAULT_RECORDS}
    assert instruments == {
        "Oscilloscope",
        "Logic Analyzer",
        "CAN Analyzer",
        "USB Analyzer",
        "Digital Multimeter",
        "Cable Tester",
        "Spectrum Analyzer",
        "Current Probe",
        "Power Analyzer",
        "Vector Network Analyzer",
        "Thermal Camera",
        "Precision Voltage Source",
        "Precision Multimeter",
        "Phase Noise Analyzer",
        "Memory Tester",
        "LCR Meter",
        "ESR Meter",
        "Frequency Counter",
        "Thermal Chamber",
        "X-Ray Inspector",
        "Visual Inspector",
        "Multimeter Continuity",
        "Force Gauge",
        "SEM Inspector",
        "Hipot Tester",
        "Insulation Tester",
        "Vibration Tester",
        "Salt Spray Chamber",
        "Pressure Gauge",
        "ESD Simulator",
    }


def test_preserved_error_codes_unique() -> None:
    codes = [r.error_code for r in FAULT_RECORDS]
    assert len(codes) == 104
    assert len(set(codes)) == 104


def test_preserved_component_types() -> None:
    types = {r.component_type for r in FAULT_RECORDS}
    assert types == {
        "Bus", "Sensor", "IC", "Connector", "Protection",
        "Module", "Passive", "PCB", "Solder", "Finish",
        "Material", "Electromechanical", "Mechanical",
    }


def test_preserved_facts_have_bilingual_text() -> None:
    for r in FAULT_RECORDS:
        assert r.symptom_zh and r.symptom_en
        assert r.cause_zh and r.cause_en
        assert r.solution_zh and r.solution_en
        assert r.component and r.product_type
