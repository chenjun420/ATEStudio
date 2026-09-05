"""ATML IEEE 1671 TestDescription importer (task 11).

Takes the output of :func:`ate_cloud.services.atml_td_parser.parse_test_description`
and persists it as ``TestRequirement`` / ``TestCase`` ORM rows (the relational
traceability layer from task 10 — NOT the knowledge graph, which task 12 owns).

Behavior:
- ``source="atml"`` and ``atml_ref`` are set on every persisted row.
- A test case is linked to a DSL step only when its ``DslMapping`` hint
  resolves to a REAL ``sequences`` row AND the step id exists in that
  sequence's YAML. Unresolved / absent hints leave ``sequence_id`` NULL and
  ``step_id`` "" and are reported in ``unmapped`` (a recorded traceability
  gap — never an error).
- Idempotent: requirements key on ``(product_code, requirement_code)`` and
  cases on ``case_code``; re-importing updates rows in place instead of
  creating duplicates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.knowledge import SOURCE_ATML, TestCase, TestRequirement
from ate_cloud.models.sequence import Sequence
from ate_cloud.services.atml_td_parser import (
    ParsedCase,
    ParsedTestDescription,
    parse_test_description,
)


@dataclass(frozen=True, slots=True)
class ImportCounts:
    """Created/updated counters for one entity kind."""

    created: int = 0
    updated: int = 0


@dataclass(frozen=True, slots=True)
class UnmappedCase:
    """A test case that could not be linked to a DSL step (traceability gap)."""

    case_code: str
    title: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Structured result of a TestDescription import (route serializes this)."""

    product_code: str
    requirements: ImportCounts
    cases: ImportCounts
    unmapped: list[UnmappedCase] = field(default_factory=list)


def _step_ids_from_yaml(yaml_content: str) -> set[str]:
    """Collect every step/loop ``id`` declared in a sequence's YAML plan.

    Tolerant of the v3.2 nested structure (loops/branches/subsequences carry
    their own ``steps`` / ``then`` / ``else``). Returns an empty set if the
    YAML cannot be parsed.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    ids: set[str] = set()

    def walk(items: object) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            step_id = item.get("id")
            if isinstance(step_id, str) and step_id:
                ids.add(step_id)
            walk(item.get("steps"))
            walk(item.get("then"))
            walk(item.get("else"))

    walk(data.get("steps"))
    return ids


def _map_dsl(
    case: ParsedCase, seq_index: dict[str, tuple[str, set[str]]]
) -> tuple[str | None, str, str | None]:
    """Resolve a case's DSL hint against real sequences.

    Returns ``(sequence_id, step_id, unmapped_reason)``; ``sequence_id`` is
    None and ``step_id`` "" when no valid mapping exists.
    """
    if not case.sequence_id:
        return None, "", "no DSL mapping in TestDescription"
    entry = seq_index.get(case.sequence_id)
    if entry is None:
        return None, "", f"DSL sequence '{case.sequence_id}' not found"
    canonical_id, steps = entry
    if not case.step_id or case.step_id not in steps:
        return None, "", f"step '{case.step_id}' not found in DSL sequence"
    return canonical_id, case.step_id, None


class ATMLImporter:
    """Persist parsed IEEE 1671 TestDescription content as ORM rows."""

    async def import_test_description(
        self, db: AsyncSession, xml: str | bytes
    ) -> ImportResult:
        """Parse ``xml`` and upsert its requirements/cases into ``db``.

        Raises:
            ATMLParseError: on malformed/non-TestDescription XML (route → 400).
        """
        parsed = parse_test_description(xml)

        seq_index = await self._load_sequence_index(db, parsed)
        req_ids, req_counts = await self._upsert_requirements(db, parsed)
        case_counts, unmapped = await self._upsert_cases(
            db, parsed, req_ids, seq_index
        )
        await db.flush()
        return ImportResult(
            product_code=parsed.product_code,
            requirements=req_counts,
            cases=case_counts,
            unmapped=unmapped,
        )

    async def _load_sequence_index(
        self, db: AsyncSession, parsed: ParsedTestDescription
    ) -> dict[str, tuple[str, set[str]]]:
        """Map each hinted sequence id/name -> (canonical sequence id, step ids).

        A hint may use either the sequence's UUID ``id`` or its unique ``name``;
        both keys resolve to the same canonical id + DSL step-id set.
        """
        hints = {c.sequence_id for c in parsed.cases if c.sequence_id}
        if not hints:
            return {}
        result = await db.execute(
            select(Sequence).where(
                (Sequence.id.in_(hints)) | (Sequence.name.in_(hints))
            )
        )
        index: dict[str, tuple[str, set[str]]] = {}
        for seq in result.scalars().all():
            entry = (seq.id, _step_ids_from_yaml(seq.yaml_content))
            index[seq.id] = entry
            index[seq.name] = entry
        return index

    async def _upsert_requirements(
        self, db: AsyncSession, parsed: ParsedTestDescription
    ) -> tuple[dict[str, str], ImportCounts]:
        """Upsert requirements keyed on (product_code, requirement_code)."""
        codes = [r.code for r in parsed.requirements]
        existing = {
            r.requirement_code: r
            for r in (
                await db.execute(
                    select(TestRequirement).where(
                        TestRequirement.product_code == parsed.product_code,
                        TestRequirement.requirement_code.in_(codes),
                    )
                )
            )
            .scalars()
            .all()
        }
        id_by_code: dict[str, str] = {}
        created = updated = 0
        for req in parsed.requirements:
            row = existing.get(req.code)
            if row is None:
                row = TestRequirement(
                    id=str(uuid.uuid4()),
                    product_code=parsed.product_code,
                    requirement_code=req.code,
                    title=req.title,
                )
                db.add(row)
                created += 1
            else:
                row.title = req.title
                updated += 1
            row.description = req.description
            row.source = SOURCE_ATML
            row.atml_ref = req.atml_ref
            id_by_code[req.code] = row.id
        return id_by_code, ImportCounts(created=created, updated=updated)

    async def _upsert_cases(
        self,
        db: AsyncSession,
        parsed: ParsedTestDescription,
        req_id_by_code: dict[str, str],
        seq_index: dict[str, tuple[str, set[str]]],
    ) -> tuple[ImportCounts, list[UnmappedCase]]:
        """Upsert cases keyed on case_code, linking DSL steps where valid."""
        codes = [c.case_code for c in parsed.cases]
        existing = {
            c.case_code: c
            for c in (
                await db.execute(
                    select(TestCase).where(TestCase.case_code.in_(codes))
                )
            )
            .scalars()
            .all()
        }
        created = updated = 0
        unmapped: list[UnmappedCase] = []
        for case in parsed.cases:
            row = existing.get(case.case_code)
            if row is None:
                row = TestCase(
                    id=str(uuid.uuid4()), case_code=case.case_code, title=case.title
                )
                db.add(row)
                created += 1
            else:
                row.title = case.title
                updated += 1

            row.atml_ref = case.atml_ref
            row.requirement_id = (
                req_id_by_code.get(case.requirement_code)
                if case.requirement_code
                else None
            )

            sequence_id, step_id, reason = _map_dsl(case, seq_index)
            row.sequence_id = sequence_id
            row.step_id = step_id
            if reason:
                unmapped.append(UnmappedCase(case.case_code, case.title, reason))
        return ImportCounts(created=created, updated=updated), unmapped


__all__ = ["ATMLImporter", "ImportResult", "ImportCounts", "UnmappedCase"]
