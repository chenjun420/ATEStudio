"""ATERag bundle contract — the import-side mirror of the producer's schema.

This module is the ATEStudio half of the ATERag↔ATEStudio data-plane contract.
It deliberately re-declares the models rather than importing ATERag's package:
the two repos release independently, and a shared code dependency would turn
every contract change into a coordinated two-repo release. The coupling is kept
honest by ``contract_hash`` instead — both sides derive a fingerprint from their
own Pydantic schema and refuse to talk when they differ.

Why the hash matters
--------------------
A field added on one side and not the other does not raise: the unknown field is
either dropped (silent data loss) or errors on every request (loud, but after
deploy). With the hash, the mismatch is caught at the first import with a message
naming both hashes, before anything is written.

Keep in sync
------------
``bundle_version`` must match ATERag's ``BUNDLE_VERSION``. Contract rules:

* additive optional field → bump minor, keep major
* required field added / field removed / semantic change → bump major

The producer's current baseline is::

    bundle_version = 1.0
    contract_hash  = 59a852c26ca277e4141c6a60ae723b20
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Must equal ATERag's CONTRACT_MAJOR / BUNDLE_VERSION.
CONTRACT_MAJOR = 1
BUNDLE_VERSION = "1.0"

#: Producer baseline recorded at contract freeze; asserted in tests.
KNOWN_CONTRACT_HASH = "59a852c26ca277e4141c6a60ae723b20"

ContractStatus = Literal["approved", "draft"]


class ClauseModel(BaseModel):
    """One condition clause."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, max_length=64)
    text: str = Field("", max_length=2000)
    role: Literal["input", "output"]
    value: dict[str, Any] | None = None
    source: str = Field("notes", max_length=32)
    confidence: str = Field("rule", max_length=32)
    status: ContractStatus = "approved"
    method_ref: str = Field("", max_length=64)
    cond_fingerprint: str = Field("", max_length=64)


class LimitModel(BaseModel):
    """One limit. Any of min/typ/max may be absent — one-sided is the norm."""

    model_config = ConfigDict(extra="forbid")

    rail: str = Field("", max_length=32)
    qualifier: str = Field("", max_length=64)
    min: float | None = None
    typ: float | None = None
    max: float | None = None
    unit: str = Field("", max_length=32)


class ScenarioModel(BaseModel):
    """One executable condition combination."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., min_length=1, max_length=200)
    seq: int = Field(..., ge=0)
    name: str = Field("", max_length=200)
    rail: str = Field("", max_length=32)
    bindings: dict[str, str] = Field(default_factory=dict)
    derived: dict[str, float] = Field(default_factory=dict)
    basis: str = Field("", max_length=200)
    source: str = Field("spec", max_length=32)


class RequirementModel(BaseModel):
    """One requirement with its conditions, scenarios and verdict."""

    model_config = ConfigDict(extra="forbid")

    requirement_code: str = Field(..., min_length=1, max_length=200)
    spec_requirement_id: str = Field("", max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    section_path: str = Field("", max_length=64)
    role: str = Field("", max_length=32)
    description: str = Field("", max_length=8000)
    notes: str = Field("", max_length=2000)
    req_fingerprint: str = Field("", max_length=64)
    flags: list[str] = Field(default_factory=list)
    input_conditions: list[ClauseModel] = Field(default_factory=list)
    output_conditions: list[ClauseModel] = Field(default_factory=list)
    limits: list[LimitModel] = Field(default_factory=list)
    scenarios: list[ScenarioModel] = Field(default_factory=list)
    one_sided: dict[str, Any] = Field(default_factory=dict)
    assessment: dict[str, str] = Field(default_factory=dict)

    @field_validator("scenarios")
    @classmethod
    def _seq_unique(cls, v: list[ScenarioModel]) -> list[ScenarioModel]:
        """Per-requirement scenario seq must be unique.

        ``case_code = f"{requirement_code}-S{seq:03d}"`` — colliding seq
        collapses two scenarios into one case and silently drops conditions.
        Caught here so the producer learns about it instead of the line.
        """
        seqs = [s.seq for s in v]
        if len(seqs) != len(set(seqs)):
            raise ValueError(f"场景序号重复: {sorted({s for s in seqs if seqs.count(s) > 1})}")
        ids = [s.scenario_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("场景标识重复")
        return v


class ExcludedModel(BaseModel):
    """Excluded row — reason is mandatory (exclusion must stay auditable)."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field("", max_length=100)
    title: str = Field("", max_length=255)
    section_path: str = Field("", max_length=64)
    reason: str = Field("", max_length=500)
    matched_word: str = Field("", max_length=64)
    notes: str = Field("", max_length=2000)


class ReviewModel(BaseModel):
    """Pending review item."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., max_length=64)
    section_path: str = Field("", max_length=64)
    heading: str = Field("", max_length=255)
    detail: str = Field("", max_length=1000)
    hint: str = Field("", max_length=1000)
    method_ref: str = Field("", max_length=64)


class ExcludedScenarioModel(BaseModel):
    """Excluded (infeasible) scenario combination."""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field("", max_length=100)
    title: str = Field("", max_length=255)
    bindings: dict[str, str] = Field(default_factory=dict)
    reason: str = Field("", max_length=200)
    rule_id: str = Field("", max_length=64)


class BundleModel(BaseModel):
    """The whole bundle — the only data-plane artefact between the two repos."""

    model_config = ConfigDict(extra="forbid")

    bundle_version: str = Field(...)
    contract_hash: str = Field(..., max_length=64)
    product_code: str = Field(..., min_length=1, max_length=100)
    doc_version: str = Field("", max_length=32)
    product_doc_fingerprint: str = Field("", max_length=64)
    generated_at: str = Field("", max_length=64)
    generator: str = Field("", max_length=64)
    bundle_fingerprint: str = Field("", max_length=64)
    requirements: list[RequirementModel] = Field(default_factory=list)
    excluded: list[ExcludedModel] = Field(default_factory=list)
    review: list[ReviewModel] = Field(default_factory=list)
    excluded_scenarios: list[ExcludedScenarioModel] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    removed_requirement_codes: list[str] = Field(default_factory=list)

    @field_validator("bundle_version")
    @classmethod
    def _version_ok(cls, v: str) -> str:
        try:
            major = int(v.split(".")[0])
        except (ValueError, IndexError) as e:
            raise ValueError(f"bundle_version 格式非法: {v!r}") from e
        if major != CONTRACT_MAJOR:
            raise ValueError(f"bundle 主版本不兼容: 导入方为 {CONTRACT_MAJOR}.x, 收到 {v}")
        return v

    @field_validator("requirements")
    @classmethod
    def _req_codes_unique(cls, v: list[RequirementModel]) -> list[RequirementModel]:
        codes = [r.requirement_code for r in v]
        if len(codes) != len(set(codes)):
            raise ValueError(f"requirement_code 重复: {sorted({c for c in codes if codes.count(c) > 1})}")
        return v


#: Schema keys excluded from the hash: descriptive prose only.
_VOLATILE_SCHEMA_KEYS = frozenset({"title", "description", "examples", "$comment"})


def _stable_repr(node: Any) -> str:
    """Deterministic serialisation with volatile prose removed.

    Both sides must produce byte-identical input, so mappings are key-sorted and
    rendered structurally rather than via ``json.dumps`` on an arbitrary dict.
    """
    if isinstance(node, Mapping):
        items = {k: v for k, v in node.items() if k not in _VOLATILE_SCHEMA_KEYS}
        return "{" + ",".join(f"{k}:{_stable_repr(items[k])}" for k in sorted(items)) + "}"
    if isinstance(node, list | tuple):
        return "[" + ",".join(_stable_repr(x) for x in node) + "]"
    return json.dumps(node, sort_keys=True, ensure_ascii=False)


def contract_hash() -> str:
    """Fingerprint of the contract definition, computed independently here.

    Equal to the producer's hash iff both schema declarations agree.
    """
    schema = BundleModel.model_json_schema()
    return hashlib.sha256(_stable_repr(schema).encode("utf-8")).hexdigest()[:32]


class ContractMismatchError(RuntimeError):
    """Producer/consumer contract drift — refuse to import rather than guess."""

    def __init__(self, received: str, expected: str) -> None:
        super().__init__(
            f"契约不匹配: 发送方 contract_hash={received}, 本方={expected}。"
            " 两侧 schema 已漂移, 请先对齐 extract/bundle.py 与 schemas/aterag_bundle.py"
            " (勿用关闭校验的方式绕过 —— 那会让缺失的字段静默丢失)"
        )
        self.received = received
        self.expected = expected


def assert_contract(bundle: BundleModel, *, strict: bool = True) -> None:
    """Gate an import on contract agreement.

    ``strict=False`` is only for a deliberate operator override, and the caller
    must log it — see :func:`contract_mismatch_report`.
    """
    mine = contract_hash()
    if bundle.contract_hash == mine:
        return
    if strict:
        raise ContractMismatchError(bundle.contract_hash, mine)


def contract_mismatch_report(received: str) -> dict[str, Any]:
    """Machine-readable mismatch detail for the 409 response body."""
    mine = contract_hash()
    return {
        "received": received,
        "expected": mine,
        "known_baseline": KNOWN_CONTRACT_HASH,
        "hint": "两侧 Pydantic schema 已漂移; 对齐后 contract_hash 会一致",
    }
