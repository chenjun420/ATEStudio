"""Map raw FMEA seed facts onto the deterministic ontology ID space.

Pure (no I/O, no Semantica): turns the 104 hand-authored
:class:`~ate_cloud.services.kg_seed_data.FaultRecord` facts into ontology
:class:`~ate_cloud.services.ontology.core` entity nodes and relationships —

* node LABELS are the ontology class names (Fault, Symptom, Cause, Solution,
  Component, Product, Instrument) — no legacy ``FaultSymptom``/``ErrorCode``
  labels (the error code becomes a ``Fault.error_code`` property);
* every node carries a stable, deterministic ``id`` (MERGE key) so re-seeding
  never duplicates;
* ``Fault.faultCategory`` is a :class:`~ate_cloud.services.ontology.vocab.FaultCategory`
  value, ``Fault.faultKind`` and ``Instrument.instrumentKind`` are
  :class:`~ate_cloud.services.ontology.vocab.FaultKind` /
  :class:`~ate_cloud.services.ontology.vocab.InstrumentKind` values — the
  unified vocab ID space (the legacy free-text instrument names and the
  category strings all resolve through the vocab aliases).

The graph persistence (Cypher UNWIND/MERGE via GraphService) lives in
:mod:`ate_cloud.services.kg_seeder`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ate_cloud.services.kg_seed_data import FAULT_RECORDS, FaultRecord
from ate_cloud.services.ontology.vocab import (
    FaultCategory,
    FaultKind,
    InstrumentKind,
    resolve_instrument,
)

# ── Stable-ID slugs ───────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """Deterministic lowercase slug for stable entity ids."""
    return _SLUG_RE.sub("_", text.lower()).strip("_")


# ── Mapped entities ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SeedNode:
    """One ontology entity node to MERGE (id is the idempotency key)."""

    label: str
    node_id: str
    name: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SeedEdge:
    """One ontology relationship (by node id)."""

    src: str
    rel: str
    dst: str


# ── Instrument resolution ────────────────────────────────────────────────

# Seed-specific free-text instrument names that are NOT test instruments in
# the 14-kind InstrumentKind enum (inspection / measurement / environmental
# equipment). After the unified vocab alias resolver fails, these fall back
# to the closest canonical kind; everything truly outside the enum maps to
# InstrumentKind.GENERIC (the enum's sanctioned "custom/other" bucket). The
# specific instrument name is preserved in the fact/solution text.
_SEED_INSTRUMENT_FALLBACK: dict[str, InstrumentKind] = {
    "multimeter continuity": InstrumentKind.DIGITAL_MULTIMETER,
    "precision multimeter": InstrumentKind.DIGITAL_MULTIMETER,
    "phase noise analyzer": InstrumentKind.SPECTRUM_ANALYZER,
    "vector network analyzer": InstrumentKind.SPECTRUM_ANALYZER,
    "precision voltage source": InstrumentKind.SIGNAL_GENERATOR,
}


def resolve_seed_instrument(name: str) -> InstrumentKind:
    """Resolve a legacy free-text instrument name to a canonical InstrumentKind.

    Uses the unified vocab alias resolver first; seed-specific inspection/
    measurement equipment falls back to the closest kind, else ``GENERIC``.
    Always returns a valid :class:`InstrumentKind` (so every Instrument node
    satisfies the ontology ``instrumentKind`` controlled vocabulary).
    """
    concept = resolve_instrument(name)
    if concept is not None:
        return InstrumentKind(concept.canonical)
    return _SEED_INSTRUMENT_FALLBACK.get(name.strip().lower(), InstrumentKind.GENERIC)


# ── FaultKind classification ─────────────────────────────────────────────

# Ordered (substring token → FaultKind) rules applied over error code +
# symptom + cause text. First match wins; order matters (more specific first).
# Every rule maps to a canonical FaultKind so Fault.faultKind is always valid.
_FAULT_KIND_RULES: tuple[tuple[tuple[str, ...], FaultKind], ...] = (
    (("timeout", "timed out", "no response", "time out"), FaultKind.TIMEOUT),
    (("bus-off", "bus off", "busoff", "link down", "linkdown", "communication",
      "framing", "parity", "address collision", "enumeration", "sync loss",
      "pairing", "no connect", "interruption", "bit error", "level abnormality"),
     FaultKind.COMMUNICATION),
    (("signal loss", "loss of signal", "eye diagram", "eye closed", "sync"),
     FaultKind.SIGNAL_LOSS),
    (("intermittent", "flaky", "chatter"), FaultKind.INTERMITTENT),
    (("relay", "stuck contact", "contact oxidation"), FaultKind.RELAY_FAULT),
    (("over-current", "overcurrent", "over current", "ocp", "inrush",
      "fuse blown", "over-current protection", "saturation current"),
     FaultKind.OVER_CURRENT),
    (("over-voltage", "overvoltage", "over voltage", "ovp", "over-voltage protection"),
     FaultKind.OVER_VOLTAGE),
    (("short circuit", "shorted", "bridge short", "short", "solder bridge"),
     FaultKind.SHORT_CIRCUIT),
    (("open circuit", "open", "no output", "not starting", "fracture",
      "cold solder", "dry joint", "insufficient solder", "loosening", "loose"),
     FaultKind.OPEN_CIRCUIT),
    (("thermal runaway", "overheat", "over-temperature", "thermal shutdown",
      "tsd", "junction over", "thermal"),
     FaultKind.OVERHEAT),
    (("noise", "ripple", "glitch", "jitter", "crosstalk", "emi", "phase noise",
      "piezoelectric", "self-oscillation", "chatter"),
     FaultKind.NOISE),
    (("drift", "offset", "deviation", "gain error", "aging", "frequency drift",
      "decay", "esr increase", "accuracy", "out of tolerance", "drop",
      "oxidation", "degradation", "abnormality"),
     FaultKind.DRIFT),
    (("out of range", "out-of-range", "tolerance", "nonlinearity", "nonlinear",
      "settling", "aliasing", "timing violation", "sequencing", "order"),
     FaultKind.OUT_OF_RANGE),
)

# Per-category fallback when no token rule fires.
_CATEGORY_FAULT_KIND: dict[FaultCategory, FaultKind] = {
    FaultCategory.COMMUNICATION_INTERCONNECTS: FaultKind.COMMUNICATION,
    FaultCategory.POWER: FaultKind.OUT_OF_RANGE,
    FaultCategory.ASSEMBLY_SOLDERING: FaultKind.OPEN_CIRCUIT,
    FaultCategory.PASSIVE_COMPONENTS: FaultKind.DRIFT,
    FaultCategory.ENVIRONMENTAL_ESD: FaultKind.OVERHEAT,
    FaultCategory.MIXED_SIGNAL_TIMING: FaultKind.OUT_OF_RANGE,
}


def classify_fault_kind(record: FaultRecord) -> FaultKind:
    """Deterministically map a seed fact to a canonical :class:`FaultKind`.

    Scans the error code + English symptom/cause text for fault-mode tokens;
    falls back to the category's default kind. Always returns a valid kind.
    """
    haystack = " ".join(
        [record.error_code, record.symptom_en, record.cause_en]
    ).lower()
    for tokens, kind in _FAULT_KIND_RULES:
        if any(token in haystack for token in tokens):
            return kind
    return _CATEGORY_FAULT_KIND[FaultCategory(record.category)]


# ── Graph assembly ───────────────────────────────────────────────────────


def _fault_node_id(code: str) -> str:
    return f"fault:{_slug(code)}"


def build_seed_graph() -> tuple[dict[str, list[SeedNode]], list[SeedEdge]]:
    """Build the deduplicated ontology node/edge sets from the seed facts.

    Returns:
        ``(nodes_by_label, edges)`` where ``nodes_by_label`` maps ontology
        class label → list of :class:`SeedNode` (deduplicated by id) and
        ``edges`` is a list of :class:`SeedEdge` (deduplicated by
        src/rel/dst). Nodes/edges are deterministic and idempotent.
    """
    nodes: dict[str, dict[str, SeedNode]] = {}
    edge_set: set[tuple[str, str, str]] = set()
    edges: list[SeedEdge] = []

    def add_node(node: SeedNode) -> None:
        bucket = nodes.setdefault(node.label, {})
        if node.node_id not in bucket:
            bucket[node.node_id] = node

    def add_edge(src: str, rel: str, dst: str) -> None:
        key = (src, rel, dst)
        if key not in edge_set:
            edge_set.add(key)
            edges.append(SeedEdge(src, rel, dst))

    # 1) Canonical unified-vocab Instrument nodes (one per InstrumentKind).
    for instrument_kind in InstrumentKind:
        add_node(SeedNode(
            label="Instrument",
            node_id=f"instrument:{instrument_kind.value}",
            name=instrument_kind.value.replace("_", " ").title(),
            props={"instrument_kind": instrument_kind.value, "vocab": "canonical"},
        ))

    # 2) One Fault aggregate + Symptom/Cause/Solution chain per fact.
    for record in FAULT_RECORDS:
        code = record.error_code
        category = FaultCategory(record.category)
        fault_kind = classify_fault_kind(record)
        instrument_kind = resolve_seed_instrument(record.instrument)

        fault_id = _fault_node_id(code)
        symptom_id = f"symptom:{_slug(code)}"
        cause_id = f"cause:{_slug(code)}"
        solution_id = f"solution:{_slug(code)}"
        component_id = f"component:{_slug(record.component)}"
        product_id = f"product:{_slug(record.product_type)}"
        instrument_id = f"instrument:{instrument_kind.value}"

        add_node(SeedNode(
            label="Fault", node_id=fault_id, name=record.symptom_en,
            props={
                "error_code": code,
                "fault_kind": fault_kind.value,
                "fault_category": category.value,
                "diagnostic_instrument": record.instrument,
                "description_zh": record.symptom_zh,
                "description_en": record.symptom_en,
            },
        ))
        add_node(SeedNode(
            label="Symptom", node_id=symptom_id, name=record.symptom_en,
            props={"description_zh": record.symptom_zh, "description_en": record.symptom_en},
        ))
        add_node(SeedNode(
            label="Cause", node_id=cause_id, name=record.cause_en,
            props={"description_zh": record.cause_zh, "description_en": record.cause_en},
        ))
        add_node(SeedNode(
            label="Solution", node_id=solution_id, name=record.solution_en,
            props={"description_zh": record.solution_zh, "description_en": record.solution_en},
        ))
        add_node(SeedNode(
            label="Component", node_id=component_id, name=record.component,
            props={"component_class": record.component_type},
        ))
        add_node(SeedNode(
            label="Product", node_id=product_id, name=record.product_type,
            props={"product_type": record.product_type},
        ))

        # Ontology fault chain: Fault -hasSymptom-> Symptom -hasCause-> Cause
        # -hasSolution-> Solution; Fault -affectsComponent-> Component;
        # Component/Product -exhibits-> Fault; diagnostic instrument + product.
        add_edge(fault_id, "HAS_SYMPTOM", symptom_id)
        add_edge(symptom_id, "HAS_CAUSE", cause_id)
        add_edge(cause_id, "HAS_SOLUTION", solution_id)
        add_edge(fault_id, "AFFECTS_COMPONENT", component_id)
        add_edge(component_id, "EXHIBITS", fault_id)
        add_edge(product_id, "EXHIBITS", fault_id)
        add_edge(fault_id, "OCCURS_IN_PRODUCT", product_id)
        add_edge(fault_id, "DIAGNOSED_WITH", instrument_id)

    nodes_by_label = {label: list(bucket.values()) for label, bucket in nodes.items()}
    return nodes_by_label, edges


def seed_summary() -> dict[str, int]:
    """Return deterministic node/edge counts for the seed (diagnostics/tests)."""
    nodes_by_label, edges = build_seed_graph()
    counts = {label.lower(): len(rows) for label, rows in nodes_by_label.items()}
    counts["nodes_total"] = sum(len(rows) for rows in nodes_by_label.values())
    counts["edges_total"] = len(edges)
    counts["facts"] = len(FAULT_RECORDS)
    return counts


__all__ = [
    "SeedEdge",
    "SeedNode",
    "build_seed_graph",
    "classify_fault_kind",
    "resolve_seed_instrument",
    "seed_summary",
]
