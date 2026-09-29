"""ATERag bundle importer (P2) — the data-plane ingest path.

Design constraints
------------------
* **Dialect-agnostic.** ATEStudio runs SQLite in dev/CI and PostgreSQL in
  production, so this module uses only SQLAlchemy ORM constructs. No
  ``ON CONFLICT`` / ``JSONB`` / ``RETURNING`` tricks — those would pass CI on
  SQLite and fail in production, or vice versa.
* **Idempotent.** Requirements key on ``(product_code, requirement_code)``,
  conditions on ``(owner, cond_fingerprint)``. Re-importing the same bundle
  must not duplicate anything. A duplicated condition looks fine on a dashboard
  but doubles the number of instrument settings a case is given.
* **Two-phase.** ``dry_run`` reports what *would* change, including conflicts
  against manual edits; ``commit`` applies. Overwriting a human's edit without
  asking is the failure mode that destroys trust in an import.
* **Single transaction.** All or nothing — a half-applied bundle leaves
  requirements without their conditions, which reads as "the spec has no test
  conditions".

Deletion policy
----------------
``removed_requirement_codes`` marks rows ``stale``; it never deletes. A
requirement disappearing from a spec revision is normal, and deleting the row
would destroy the traceability chain back to the earlier test results.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.knowledge import SOURCE_ATERAG, TestCase, TestRequirement
from ate_cloud.models.test_conditions import (
    OWNER_REQUIREMENT,
    STATUS_DRAFT,
    TestCondition,
)
from ate_cloud.models.test_limits import TestLimit
from ate_cloud.schemas.aterag_bundle import BundleModel, RequirementModel

#: Namespace for ATERag-owned limit_ids, so an ingest can never collide with a
#: hand-authored limit. Same reason: the importer may add versions, not edit.
LIMIT_PREFIX = "aterag"

#: Provenance marker written to ``test_cases.created_by``.
CREATED_BY = "aterag:bundle"

#: Lifecycle status of a requirement that vanished from the newest spec revision.
STATUS_STALE = "stale"


@dataclass(slots=True)
class ImportCounts:
    """Created/updated counters per entity kind.

    Mutable (not frozen) because the import tallies them as it walks the bundle;
    freezing would force a replace-copy per row.
    """

    created: int = 0
    updated: int = 0

    def bump_created(self) -> None:
        self.created += 1

    def bump_updated(self) -> None:
        self.updated += 1

    def to_dict(self) -> dict[str, int]:
        return {"created": self.created, "updated": self.updated}


@dataclass(frozen=True, slots=True)
class Conflict:
    """A change that would overwrite a manual edit — needs explicit consent."""

    entity: str
    identifier: str
    field: str
    current_value: Any
    incoming_value: Any
    last_modified_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "identifier": self.identifier,
            "field": self.field,
            "current_value": self.current_value,
            "incoming_value": self.incoming_value,
            "last_modified_by": self.last_modified_by,
        }


@dataclass(slots=True)
class ImportResult:
    """Structured result of one import (the route serialises this)."""

    product_code: str
    contract_hash: str
    requirements: ImportCounts = field(default_factory=ImportCounts)
    conditions: ImportCounts = field(default_factory=ImportCounts)
    cases: ImportCounts = field(default_factory=ImportCounts)
    limits: ImportCounts = field(default_factory=ImportCounts)
    cases_reset_to_draft: list[str] = field(default_factory=list)
    requirements_staled: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    unmapped: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.requirements.created
            or self.requirements.updated
            or self.conditions.created
            or self.conditions.updated
            or self.cases.created
            or self.cases.updated
            or self.limits.created
            or self.requirements_staled
            or self.cases_reset_to_draft
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_code": self.product_code,
            "contract_hash": self.contract_hash,
            "dry_run": self.dry_run,
            "changed": self.changed,
            "requirements": self.requirements.to_dict(),
            "conditions": self.conditions.to_dict(),
            "cases": self.cases.to_dict(),
            "limits": self.limits.to_dict(),
            "cases_reset_to_draft": self.cases_reset_to_draft,
            "requirements_staled": self.requirements_staled,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "unmapped": self.unmapped,
        }


#: Requirement fields ATERag owns. A manual edit to any of these is a conflict
#: requiring confirmation; everything else is never touched by an import.
AUTHORITATIVE_FIELDS = ("title", "description", "notes", "section_path")


def _assert_authoritative_fields_mapped() -> None:
    """Fail loudly at import time if an authoritative field has no column.

    A name here that the ORM does not map is not a no-op: ``getattr`` returns
    None, so the comparison always reports "changed", the counter reports an
    update on every re-import, and the actual value is dropped on the floor.
    That combination is the worst kind of bug — it looks like the import
    worked, and the missing data is only noticed months later when someone
    asks which spec clause a case came from.

    Raised once per import rather than at import time of the module, so tests
    that rebuild the schema still get the check.
    """
    mapped = set(TestRequirement.__table__.columns.keys())
    missing = [f for f in AUTHORITATIVE_FIELDS if f not in mapped]
    if missing:
        raise RuntimeError(
            f"AUTHORITATIVE_FIELDS 里的字段在 TestRequirement 上没有对应列: {missing}。"
            " 导入会把这些字段静默丢弃, 同时每次重导都谎报变更。"
        )


def _limit_id(req_code: str, rail: str, qualifier: str) -> str:
    """Deterministic limit id.

    Deterministic so re-import updates the same row instead of piling up
    versions. Versioning-by-date is deliberately NOT used here: an unchanged
    limit must not create a new effective_from on every import, or the limit
    table fills with no-op versions and the resolver has to pick among them.
    """
    parts = [LIMIT_PREFIX, req_code, rail or "main", qualifier or "na"]
    return "-".join(p.replace(" ", "_") for p in parts)


class ATERagImporter:
    """Persist a validated ATERag bundle as requirements / conditions / limits."""

    async def import_bundle(
        self,
        db: AsyncSession,
        bundle: BundleModel,
        *,
        dry_run: bool = True,
        confirm_overwrite: bool = False,
        today: date | None = None,
    ) -> ImportResult:
        """Import (or, with ``dry_run``, merely preview) a bundle.

        Args:
            db: Open session. A single transaction covers the whole import.
            bundle: Contract-validated bundle.
            dry_run: Compute the plan without writing.
            confirm_overwrite: Apply changes that overwrite manual edits. Without
                it such changes are reported as conflicts and left alone.
            today: Effective date for materialised limits (injectable for tests).

        Returns:
            ImportResult: counters, conflicts, unmapped kinds, staled rows.
        """
        _assert_authoritative_fields_mapped()
        res = ImportResult(
            product_code=bundle.product_code,
            contract_hash=bundle.contract_hash,
            dry_run=dry_run,
        )

        for model in bundle.requirements:
            existing = await self._get_requirement(db, bundle.product_code, model.requirement_code)
            await self._apply_requirement(
                db, res, bundle, model, existing, confirm_overwrite, today or date.today()
            )

        for code in bundle.removed_requirement_codes:
            # _mark_stale 会改 status, 所以 dry_run 也要拦: 预览把需求标成
            # stale 而不落库, 用户确认后重跑一次, 结果与预览对不上。
            if await self._mark_stale(db, bundle.product_code, code, write=not dry_run):
                res.requirements_staled.append(code)

        if not dry_run:
            await db.commit()
        return res

    # ---------- requirement ----------

    async def _get_requirement(self, db: AsyncSession, product_code: str, code: str) -> TestRequirement | None:
        stmt = select(TestRequirement).where(
            TestRequirement.product_code == product_code,
            TestRequirement.requirement_code == code,
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def _apply_requirement(
        self,
        db: AsyncSession,
        res: ImportResult,
        bundle: BundleModel,
        model: RequirementModel,
        existing: TestRequirement | None,
        confirm_overwrite: bool,
        today: date,
    ) -> None:
        if existing is None:
            row = TestRequirement(
                id=str(uuid.uuid4()),
                product_code=bundle.product_code,
                requirement_code=model.requirement_code,
                title=model.title,
                description=model.description,
                source=SOURCE_ATERAG,
                req_fingerprint=model.req_fingerprint or None,
                # section_path 与 notes 在新建时就要落盘: 若只在更新分支里写,
                # 首次导入就会把这两个字段悄悄丢掉, 而它们恰恰是"这条测试来自
                # 规格书哪一段"的唯一答案。
                section_path=model.section_path or None,
                notes=model.notes or None,
            )
            res.requirements.bump_created()
            if not res.dry_run:
                # dry_run must not insert: the caller renders a plan, and a
                # preview that quietly created rows would be worse than no
                # preview at all.
                db.add(row)
        else:
            row = existing
            # Decide manual-vs-import BEFORE rewriting ``source`` — checking
            # afterwards always sees SOURCE_ATERAG and would silently discard
            # every human edit instead of reporting it as a conflict.
            was_manual = not _was_aterag_authored(row)
            if not res.dry_run:
                row.source = SOURCE_ATERAG
            if row.status == STATUS_STALE and not res.dry_run:
                row.status = "active"
            for name in AUTHORITATIVE_FIELDS:
                incoming = getattr(model, name, None)
                if not incoming or getattr(row, name, None) == incoming:
                    continue
                if not confirm_overwrite and was_manual:
                    res.conflicts.append(
                        Conflict(
                            entity="test_requirements",
                            identifier=model.requirement_code,
                            field=name,
                            current_value=getattr(row, name, None),
                            incoming_value=incoming,
                            last_modified_by=str(row.source),
                        )
                    )
                    continue
                res.requirements.bump_updated()
                if not res.dry_run:
                    setattr(row, name, incoming)
            if not res.dry_run and (model.req_fingerprint or None) != row.req_fingerprint:
                row.req_fingerprint = model.req_fingerprint or None

        await self._sync_conditions(db, res, model, row)
        await self._materialise_limits(db, res, bundle, model, today)
        await self._sync_cases(db, res, model, row)

    async def _mark_stale(
        self, db: AsyncSession, product_code: str, code: str, *, write: bool
    ) -> bool:
        """Mark a vanished requirement stale (never delete it).

        ``write=False`` reports what *would* happen. Must be honoured: marking
        stale hides the requirement from active lists, so a preview that did it
        without asking would silently change what the user sees.
        """
        row = await self._get_requirement(db, product_code, code)
        if row is None or row.status == STATUS_STALE:
            return False
        if write:
            row.status = STATUS_STALE
        return True

    # ---------- conditions ----------

    async def _sync_conditions(
        self, db: AsyncSession, res: ImportResult, model: RequirementModel, owner: TestRequirement
    ) -> None:
        """Replace this requirement's conditions with the bundle's set.

        Delete-then-insert rather than diffing: a condition removed upstream
        must disappear, and a diff that only adds leaves ghosts behind. The
        unique (owner, fingerprint) constraint makes re-running safe.
        """
        stmt = select(TestCondition).where(
            TestCondition.owner_type == OWNER_REQUIREMENT,
            TestCondition.owner_id == owner.id,
        )
        current = {c.cond_fingerprint: c for c in (await db.execute(stmt)).scalars().all()}
        incoming_fp: set[str] = set()

        for side, clauses in (
            ("input", model.input_conditions),
            ("output", model.output_conditions),
        ):
            for cl in clauses:
                fp = cl.cond_fingerprint
                if not fp:
                    res.unmapped.append(
                        {
                            "entity": "condition",
                            "identifier": model.requirement_code,
                            "reason": "缺 cond_fingerprint, 无法保证幂等",
                        }
                    )
                    continue
                incoming_fp.add(fp)
                prev = current.get(fp)
                if prev is None:
                    res.conditions.bump_created()
                    if not res.dry_run:
                        db.add(
                            TestCondition(
                                id=str(uuid.uuid4()),
                                owner_type=OWNER_REQUIREMENT,
                                owner_id=owner.id,
                                side=side,
                                kind=cl.kind,
                                text=cl.text or "",
                                value=cl.value,
                                source=cl.source,
                                confidence=cl.confidence,
                                status=cl.status,
                                method_ref=cl.method_ref or None,
                                cond_fingerprint=fp,
                            )
                        )
                elif _clause_changed(prev, cl):
                    res.conditions.bump_updated()
                    if not res.dry_run:
                        prev.kind = cl.kind
                        prev.text = cl.text or ""
                        prev.value = cl.value
                        prev.source = cl.source
                        prev.confidence = cl.confidence
                        prev.status = cl.status
                        prev.method_ref = cl.method_ref or None

        for fp, row in current.items():
            if fp not in incoming_fp:
                if not res.dry_run:
                    await db.delete(row)
                res.conditions.bump_updated()

    # ---------- limits ----------

    async def _materialise_limits(
        self,
        db: AsyncSession,
        res: ImportResult,
        bundle: BundleModel,
        model: RequirementModel,
        today: date,
    ) -> None:
        """Turn numeric output conditions into versioned TestLimit rows.

        Only clauses carrying bounds become limits — a ``presence`` clause
        ("支持 ORING") has no number and must not become a limit row, or the
        limit table fills with rows that judge nothing.
        """
        if res.dry_run:
            return
        for lim in model.limits:
            if lim.min is None and lim.typ is None and lim.max is None:
                continue
            lid = _limit_id(model.requirement_code, lim.rail, lim.qualifier)
            stmt = select(TestLimit).where(TestLimit.limit_id == lid)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                db.add(
                    TestLimit(
                        id=str(uuid.uuid4()),
                        limit_id=lid,
                        product_type=bundle.product_code,
                        test_name=model.title[:255],
                        spec_low=lim.min,
                        spec_typ=lim.typ,
                        spec_high=lim.max,
                        unit=(lim.unit or "-")[:64],
                        effective_from=today,
                    )
                )
                res.limits.bump_created()
            elif (row.spec_low, row.spec_typ, row.spec_high, row.unit) != (
                lim.min,
                lim.typ,
                lim.max,
                (lim.unit or "-")[:64],
            ):
                # Change in place: the limit_id is derived from the
                # requirement, so a changed bound is a changed criterion, not a
                # new version. Old effective-dated versions are left for the
                # engineer to retire explicitly.
                row.spec_low = lim.min
                row.spec_typ = lim.typ
                row.spec_high = lim.max
                row.unit = (lim.unit or "-")[:64]
                res.limits.bump_updated()

    # ---------- cases ----------

    async def _sync_cases(
        self, db: AsyncSession, res: ImportResult, model: RequirementModel, owner: TestRequirement
    ) -> None:
        """One case per scenario, keyed by ``{requirement_code}-S{seq:03d}``.

        Counts only *real* changes. A no-op re-import must report zero, because
        the operator reads these counters to decide whether anything needs
        re-review — a preview that always reports hundreds of "updates" is a
        preview nobody reads, and the one real change gets lost in the noise.

        Unlike limits, this path still walks the rows on ``dry_run``: the
        question a preview exists to answer is "what will this do to my cases",
        and answering "nothing" by skipping the loop would be a false answer.
        """
        for scen in model.scenarios:
            code = f"{model.requirement_code}-S{scen.seq:03d}"
            # 场景判据指纹: 绑定了哪些条件 + 哪些限值。用来判断"判据是否真的
            # 变了", 比需求级指纹精确 —— 需求里任何一处改动都会让需求指纹变,
            # 那会把所有场景用例一并打回 draft, 包括判据没变的那些。
            scen_fp = _scenario_fingerprint(model, scen.seq)
            stmt = select(TestCase).where(TestCase.case_code == code)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                res.cases.bump_created()
                if res.dry_run:
                    continue
                db.add(
                    TestCase(
                        id=str(uuid.uuid4()),
                        requirement_id=owner.id,
                        case_code=code,
                        title=(scen.name or f"{model.title} S{scen.seq:03d}")[:255],
                        status=STATUS_DRAFT,
                        created_by=CREATED_BY,
                        # 必须在此处落盘指纹: 漏掉的话下一次重导时该字段为
                        # None, 与算出的指纹不等, 于是每个新建用例都会被
                        # 无谓地重置为 draft —— 工程师签过的字反复失效,
                        # 这个状态就再也不可信了。
                        cond_fingerprint=scen_fp,
                    )
                )
                continue

            if row.created_by != CREATED_BY:
                # Someone edited this case; report it rather than silently
                # keeping a status they approved against older data.
                res.conflicts.append(
                    Conflict(
                        entity="test_cases",
                        identifier=code,
                        field="status",
                        current_value=row.status,
                        incoming_value=STATUS_DRAFT,
                        last_modified_by=row.created_by or "",
                    )
                )
            # 判据指纹变了才回退 draft。用场景指纹而不是需求指纹: 需求里
            # 任何一个条件改动都会让需求指纹变, 而那会把所有场景用例一并
            # 打回 draft —— 包括判据其实没变的那几个。粒度过粗的重置会让
            # 工程师反复重审, 久而久之就不信这个状态了。
            # 逐字段判断是否真的变了, 变了才计数。指纹一致且标题/归属未变
            # 时必须报告 0 —— 否则每次重导都是几百条 "updated", 真正需要
            # 复审的那一条会淹没在里面, 于是这个计数就没人看了。
            new_title = scen.name[:255] if scen.name else None
            changed = (
                row.cond_fingerprint != scen_fp
                or row.requirement_id != owner.id
                or (new_title is not None and row.title != new_title)
            )
            if not changed:
                continue

            res.cases.bump_updated()
            if row.cond_fingerprint != scen_fp:
                res.cases_reset_to_draft.append(code)
                if not res.dry_run:
                    row.status = STATUS_DRAFT
            if res.dry_run:
                continue
            row.requirement_id = owner.id
            row.cond_fingerprint = scen_fp
            if new_title is not None:
                row.title = new_title

    # ---------- fast path ----------

    async def purge_orphan_conditions(self, db: AsyncSession, owner_ids: Sequence[str]) -> int:
        """Delete conditions whose owner requirement no longer exists.

        Safety valve for an interrupted legacy import; not part of the normal
        path (delete-then-insert already keeps owners consistent).
        """
        if not owner_ids:
            return 0
        stmt = delete(TestCondition).where(
            TestCondition.owner_type == OWNER_REQUIREMENT,
            TestCondition.owner_id.notin_(list(owner_ids)),
        )
        result = await db.execute(stmt)
        # DML rowcount is typed as Any on the async Result; coerce explicitly so
        # the caller gets a real int instead of leaking Any.
        return int(getattr(result, "rowcount", 0) or 0)


def _scenario_fingerprint(model: RequirementModel, seq: int) -> str:
    """Stable fingerprint of one scenario's binding + criteria.

    Covers the scenario identity (which conditions/limits it binds) rather than
    the whole requirement, so an unrelated edit elsewhere in the requirement does
    not force this case back to draft.
    """
    scen = next((s for s in model.scenarios if s.seq == seq), None)
    payload = {
        "rail": scen.rail if scen else "",
        "bindings": scen.bindings if scen else {},
        "derived": scen.derived if scen else {},
        "inputs": sorted(c.cond_fingerprint for c in model.input_conditions),
        "outputs": sorted(c.cond_fingerprint for c in model.output_conditions),
        "limits": [lim.model_dump() for lim in model.limits],
    }
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _was_aterag_authored(row: Any) -> bool:
    """Whether the row originated from an ATERag import.

    Rows created by an import may be updated freely (that is the point of an
    idempotent re-import). Rows created by hand are protected: overwriting
    them needs explicit consent.

    Failing safe matters here. Getting this wrong in the "import" direction
    discards a human's edit; getting it wrong in the "manual" direction merely
    shows a conflict prompt, which is cheap.
    """
    return str(getattr(row, "source", "")) == SOURCE_ATERAG


def _clause_changed(prev: TestCondition, incoming: Any) -> bool:
    return (
        prev.kind != incoming.kind
        or prev.text != (incoming.text or "")
        or prev.value != incoming.value
        or prev.status != incoming.status
        or (prev.method_ref or "") != (incoming.method_ref or "")
    )
