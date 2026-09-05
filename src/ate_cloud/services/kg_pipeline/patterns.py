"""Domain pattern extraction stage (no LLM key required).

Semantica's built-in ``NERExtractor(method="pattern")`` targets generic
PERSON/ORG/GPE/DATE shapes and cannot recognize the production-test
vocabulary (faults, components, instruments, …). This stage supplies the
deterministic, key-free recognizer for that vocabulary and produces Semantica
``Entity`` objects (Semantica imports stay inside this package) which are then
fed to Semantica's RelationExtractor/TripletExtractor and GraphBuilder.

The recognized vocabulary intentionally overlaps the ATML/FMEA domain the
ontology (task 9) will formalize: Component, Symptom, Cause, Instrument,
TestStep and Measurement entities, plus symptom→cause fault relations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Semantica types imported lazily at runtime (confined to package).
    from semantica.semantic_extract.types import Entity, Relation

# Ordered (label, regex). Word-bounded, case-insensitive. More specific
# patterns come first so e.g. "capacitor C12" is claimed before a generic
# capitalized-token rule could grab "C12".
_INSTRUMENT = r"\b(?:DMM|OSC|OSCILLOSCOPE|AWG|SCOPE|METER)[- ]?[A-Z0-9]{1,4}\b"
_COMPONENT_KIND = (
    r"capacitor|resistor|inductor|diode|fuse|relay|connector|"
    r"IC|chip|module|sensor|PSU|power supply"
)
_COMPONENT = rf"\b(?:{_COMPONENT_KIND})\s*[- ]?[A-Z]?\d{{0,3}}[A-Z0-9-]*\b"
_DESIGNATOR = r"\b[CRUJKQDL]\d{1,3}\b"
_PSU_UNIT = r"\bPSU[- ]?\d+\b"
_SYMPTOM = (
    r"\b(?:excessive\s+\w+|ripple|noise|drift|offset|overvoltage|over-?voltage|"
    r"undervoltage|overcurrent|over-?current|short\s*circuit|open\s*circuit|"
    r"intermittent|failure|fault|anomaly|deviation|out[- ]of[- ]spec|leakage)\b"
)
_CAUSE = (
    r"\b(?:degraded|worn|aged|defective|loose|cold[- ]solder|shorted|open|"
    r"failed|overheated|contaminated)\s+"
    r"(?:capacitor|resistor|inductor|diode|fuse|relay|connector|joint|"
    r"solder\s*joint|component|part|IC|chip)?\b"
)
_MEASUREMENT = r"\b\d+(?:\.\d+)?\s?V(?:\s?rail| voltage)?\b"
_TESTSTEP = (
    r"\b(?:functional\s*test|in-?circuit\s*test|boundary\s*scan|burn[- ]in|"
    r"power[- ]up\s*test|self[- ]test|calibration|fixture\s*test)\b"
)

_ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Instrument", re.compile(_INSTRUMENT, re.IGNORECASE)),
    ("Component", re.compile(_COMPONENT, re.IGNORECASE)),
    ("Component", re.compile(_DESIGNATOR)),
    ("Component", re.compile(_PSU_UNIT, re.IGNORECASE)),
    ("Symptom", re.compile(_SYMPTOM, re.IGNORECASE)),
    ("Cause", re.compile(_CAUSE, re.IGNORECASE)),
    ("Measurement", re.compile(_MEASUREMENT, re.IGNORECASE)),
    ("TestStep", re.compile(_TESTSTEP, re.IGNORECASE)),
)

# Relation cue → predicate. When a cue phrase sits between two recognized
# entities in a sentence, a domain relation is emitted. This augments
# Semantica's generic "related_to" with FMEA-meaningful edges.
_RELATION_CUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\broot\s+cause\s+is\b", re.IGNORECASE), "HAS_CAUSE"),
    (re.compile(r"\bcaused?\s+by\b", re.IGNORECASE), "HAS_CAUSE"),
    (re.compile(r"\b(?:resolv|fix|repair|replac)\w*\b", re.IGNORECASE), "RESOLVED_BY"),
    (re.compile(r"\bexhibits?\b", re.IGNORECASE), "EXHIBITS"),
    (re.compile(r"\bmeasures?\b", re.IGNORECASE), "MEASURES"),
    (re.compile(r"\bduring\b", re.IGNORECASE), "OBSERVED_DURING"),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class DomainExtraction:
    """Pattern-stage output: Semantica entities + domain relations."""

    entities: list[Entity]
    relations: list[Relation]


def _dedup_entities(entities: list[Entity]) -> list[Entity]:
    """Drop overlapping/duplicate spans, keeping the longest, highest-priority."""
    ordered = sorted(entities, key=lambda e: (e.start_char, -(e.end_char - e.start_char)))
    kept: list[Entity] = []
    for ent in ordered:
        overlap = any(
            ent.start_char < prev.end_char and ent.end_char > prev.start_char for prev in kept
        )
        if not overlap:
            kept.append(ent)
    return kept


def extract_domain_entities(text: str) -> list[Entity]:
    """Recognize production-test domain entities in ``text`` via regex.

    Returns Semantica ``Entity`` objects with real character spans.
    """
    from semantica.semantic_extract.types import Entity  # lazy: confined to package

    found: list[Entity] = []
    for label, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0).strip()
            if not value:
                continue
            start = match.start()
            end = start + len(match.group(0))
            # Recompute end on the stripped value to trim trailing space.
            end = text.index(value, start) + len(value)
            start = text.index(value, start)
            found.append(
                Entity(
                    text=value,
                    label=label,
                    start_char=start,
                    end_char=end,
                    confidence=0.9,
                    metadata={"extraction_method": "domain_pattern"},
                )
            )
    return _dedup_entities(found)


def _entity_at(entities: list[Entity], pos: int) -> Entity | None:
    for ent in entities:
        if ent.start_char <= pos < ent.end_char:
            return ent
    return None


def extract_domain_relations(text: str, entities: list[Entity]) -> list[Relation]:
    """Emit FMEA-meaningful relations from cue phrases between entities.

    Scans each sentence for a cue (e.g. "root cause is") and links the nearest
    entity on each side of the cue.
    """
    from semantica.semantic_extract.types import Relation  # lazy: confined to package

    relations: list[Relation] = []
    cursor = 0
    for sentence in _SENTENCE_SPLIT.split(text):
        s_start = text.index(sentence, cursor)
        s_end = s_start + len(sentence)
        cursor = s_end
        sent_ents = [
            e for e in entities if e.start_char >= s_start and e.end_char <= s_end
        ]
        if len(sent_ents) < 2:
            continue
        for cue, predicate in _RELATION_CUES:
            for cue_match in cue.finditer(sentence):
                cue_pos = s_start + cue_match.start()
                before = [e for e in sent_ents if e.end_char <= cue_pos]
                after = [e for e in sent_ents if e.start_char >= cue_pos]
                if not before or not after:
                    continue
                subject = max(before, key=lambda e: e.end_char)
                obj = min(after, key=lambda e: e.start_char)
                if subject is obj:
                    continue
                relations.append(
                    Relation(
                        subject=subject,
                        predicate=predicate,
                        object=obj,
                        confidence=0.8,
                        context=sentence,
                        metadata={"extraction_method": "domain_pattern", "cue": cue.pattern},
                    )
                )
    return relations


def domain_extract(text: str) -> DomainExtraction:
    """Run the full key-free domain pattern stage.

    Combines domain entity recognition, domain cue relations, and returns
    Semantica typed objects for downstream stages.
    """
    entities = extract_domain_entities(text)
    relations = extract_domain_relations(text, entities)
    return DomainExtraction(entities=entities, relations=relations)


__all__ = [
    "DomainExtraction",
    "domain_extract",
    "extract_domain_entities",
    "extract_domain_relations",
]
