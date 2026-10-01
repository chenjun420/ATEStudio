"""``GET /api/v1/models`` must count what the interface will claim it counts.

Why this is tested against the real schema rather than a mock
------------------------------------------------------------
The endpoint is four chained conditional aggregates. Every way it can be wrong
produces a number that looks fine: a missing model, a case counted twice, a
condition attached to a *case* folded into the requirement totals, a model with
no cases silently dropped by an inner join. A mock returns whatever it was told
to return, so it cannot distinguish "correct" from "plausible".

The fixture is shaped around those specific mistakes:

* ``MDL-A`` — two requirements, one case, conditions in both review states,
  plus one condition attached to the case. The case-attached row is the trap:
  ``test_conditions`` is owned by ``(owner_type, owner_id)`` and a condition can
  hang off a requirement *or* a case, so an aggregate that forgets the filter
  double-counts.
* ``MDL-B`` — one requirement and nothing else. This is the outer-join branch.
  Written as an inner join, the model vanishes, and a selector built on this
  endpoint then offers no way to reach a product that has requirements but no
  cases yet — which is the normal state of a freshly ingested specification.

What ``draft_conditions`` is
----------------------------
The count of conditions still awaiting a signature. It is the opposite of the
red line "an unapproved condition must not become a judgement criterion": this
counts what is *not* yet signed so the interface can say so. Reporting it as
"ready" would be the violation.

Where ``product_type`` comes from
---------------------------------
ATERag, over read-only MCP (``list_models``) — see
``services/aterag_catalog.py``. Not this database: ``test_requirements`` carries
``product_code`` and ``product_configs`` carries ``product_type`` with no key
between them, and reading ATERag's files or database directly would build an
unauthenticated private line between the two repositories (D2/D6).

An earlier version of this file asserted the opposite — that ``product_type``
must *not* appear, on the grounds that the mapping "only arrives with the P4
bundle contract". That was wrong: the mapping is ``ProductEntry{domain}`` keyed by
``model_id`` in ATERag's ``registry.py``, already exposed by the ``list_models``
MCP tool, and that tool was already in ``ALLOWED_TOOLS``. The level was missing
from this side, not from the data. The tests below replace that claim.

The endpoint is read-only. Signing goes through the approve endpoint, which
requires ``by``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.api.v1.models_catalog import list_models
from ate_cloud.models import Base
from ate_cloud.models.knowledge import TestCase, TestRequirement
from ate_cloud.models.test_conditions import (
    OWNER_CASE,
    OWNER_REQUIREMENT,
    STATUS_APPROVED,
    STATUS_DRAFT,
    TestCondition,
)
from ate_cloud.services.aterag_agent import AgentUnavailableError

NOW = datetime.now(UTC)


def _requirement(idx: int, product_code: str) -> TestRequirement:
    return TestRequirement(
        id=f"r{idx}",
        product_code=product_code,
        requirement_code=f"REQ-{idx}",
        title=f"requirement {idx}",
        source="manual",
        status="active",
    )


def _condition(
    idx: int,
    owner_id: str,
    status: str,
    owner_type: str = OWNER_REQUIREMENT,
) -> TestCondition:
    return TestCondition(
        id=f"c{idx}",
        owner_type=owner_type,
        owner_id=owner_id,
        # `side` carries a CHECK constraint (input|output); 'positive' fails at
        # insert, which is the schema doing its job.
        side="input",
        kind="electrical",
        text="t",
        status=status,
        cond_fingerprint=f"fp{idx}",
    )


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded(db: AsyncSession) -> AsyncSession:
    # MDL-A: two requirements, one case, conditions in both review states.
    db.add_all([_requirement(1, "MDL-A"), _requirement(2, "MDL-A")])
    db.add(
        TestCase(
            id="t1",
            requirement_id="r1",
            case_code="TC-1",
            title="case 1",
            step_id="s1",
            status="active",
        )
    )
    db.add_all(
        [
            _condition(1, "r1", STATUS_DRAFT),
            _condition(2, "r1", STATUS_APPROVED),
            _condition(3, "r1", STATUS_APPROVED),
            _condition(4, "r2", STATUS_DRAFT),
            # Attached to the case, not the requirement. Must not be counted.
            _condition(5, "t1", STATUS_DRAFT, OWNER_CASE),
        ]
    )
    # MDL-B: one requirement, no cases, no conditions. The outer-join branch.
    db.add(_requirement(3, "MDL-B"))
    await db.commit()
    return db


@pytest.fixture(autouse=True)
def _no_aterag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the catalogue to "ATERag knows nothing" for the count tests.

    Without this the count assertions would depend on whether a board happens to
    be reachable, which is not a property of this unit. The tests that care about
    the catalogue override it.
    """

    async def empty(url: str = "") -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        "ate_cloud.api.v1.models_catalog.fetch_model_catalog", empty
    )


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(
        "ate_cloud.api.v1.models_catalog.fetch_model_catalog", fn
    )


async def _by_code(db: AsyncSession) -> dict[str, dict]:
    result = await list_models(db, _user=object())  # type: ignore[arg-type]
    assert result["total"] == len(result["items"])
    return {item["product_code"]: item for item in result["items"]}


@pytest.mark.asyncio
async def test_counts_requirements_cases_and_both_review_states(
    seeded: AsyncSession,
) -> None:
    items = await _by_code(seeded)
    assert "MDL-A" in items, f"型号缺失，返回 {sorted(items)}"
    a = items["MDL-A"]
    assert a["requirement_count"] == 2
    assert a["case_count"] == 1
    # r1 has one draft, r2 has one draft => 2. The row owned by the case
    # (owner_type='case') is deliberately excluded by the aggregate's filter.
    assert a["draft_conditions"] == 2, "owner_type='case' 的条件被算进了需求计数"
    assert a["approved_conditions"] == 2


@pytest.mark.asyncio
async def test_model_with_no_cases_or_conditions_is_still_listed(
    seeded: AsyncSession,
) -> None:
    """The outer-join branch.

    A model whose requirements have no cases yet is the normal state right after
    a specification is ingested. Dropping it would make that product
    unreachable from the 产测开发 context selector at exactly the moment someone
    is trying to work on it.
    """
    items = await _by_code(seeded)
    assert "MDL-B" in items, "无用例无条件的型号消失了 —— outer join 写成了 inner join"
    b = items["MDL-B"]
    assert b["requirement_count"] == 1
    assert b["case_count"] == 0
    assert b["draft_conditions"] == 0
    assert b["approved_conditions"] == 0


@pytest.mark.asyncio
async def test_empty_database_returns_an_empty_list_not_an_error(
    db: AsyncSession,
) -> None:
    """A deployment with nothing ingested yet must still render the selector.

    The selector shows levels; a selector that errors on an empty installation
    turns "no data yet" into a broken screen, which is the same shape as the
    capability this project keeps refusing to fake.
    """
    result = await list_models(db, _user=object())  # type: ignore[arg-type]
    assert result["total"] == 0
    assert result["items"] == []
    assert result["product_types"] == []


@pytest.mark.asyncio
async def test_models_are_ordered_by_product_code(seeded: AsyncSession) -> None:
    result = await list_models(seeded, _user=object())  # type: ignore[arg-type]
    codes = [item["product_code"] for item in result["items"]]
    assert codes == sorted(codes)


# ── product_type, from ATERag over read-only MCP ─────────────────────────


@pytest.mark.asyncio
async def test_product_type_comes_from_aterag_not_guessed(
    seeded: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """产品类型 is sourced, never derived from the model code.

    The fixture's codes (``MDL-A``) carry no prefix that names a product type, so
    a derivation from the string could not produce the right answer by accident.
    Also pins that the catalogue is fetched **once** for the whole response —
    once per model would put a round trip on every selector open.
    """
    calls: list[str] = []

    async def fake(url: str = "") -> dict[str, str]:
        calls.append(url)
        return {"MDL-A": "空调", "MDL-B": "空调"}

    _patch_catalog(monkeypatch, fake)

    result = await list_models(seeded, _user=object())  # type: ignore[arg-type]
    by_code = {i["product_code"]: i for i in result["items"]}

    assert by_code["MDL-A"]["product_type"] == "空调"
    assert by_code["MDL-B"]["product_type"] == "空调"
    assert result["product_types"] == ["空调"]
    assert result["catalog_source"] == "aterag-mcp"
    assert result["catalog_warning"] is None
    assert len(calls) == 1, "目录应当只取一次，而不是每个型号一次"


@pytest.mark.asyncio
async def test_unreachable_aterag_degrades_with_a_stated_reason(
    seeded: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ATERag down => product_type null *and* a warning, never silence.

    This is why the field carries a null instead of a value: a product type
    inferred from the model code would look right and be wrong, and a silently
    missing level reads as a designed single-level selector. The response has to
    say which one it is — the frontend disables the level and explains.
    """

    async def boom(url: str = "") -> dict[str, str]:
        raise AgentUnavailableError("无法连接 ATERag MCP (http://x/mcp): refused")

    _patch_catalog(monkeypatch, boom)

    result = await list_models(seeded, _user=object())  # type: ignore[arg-type]

    assert result["catalog_source"] == "unavailable"
    assert result["catalog_warning"]
    assert "ATERag" in result["catalog_warning"]
    assert result["product_types"] == []
    for item in result["items"]:
        assert item["product_type"] is None
    # The counts still work: they are local, and losing them too would hide that
    # the degradation is narrow.
    assert result["total"] == 2


@pytest.mark.asyncio
async def test_a_model_aterag_does_not_know_is_null_not_a_guess(
    seeded: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requirement can arrive before its model is registered in ATERag.

    That is a real state (import ordering) and it differs from ATERag being
    unreachable. ``catalog_source`` stays ``aterag-mcp`` while that one
    ``product_type`` is null, so the interface can tell them apart instead of
    blanking the level.
    """

    async def partial(url: str = "") -> dict[str, str]:
        return {"MDL-A": "空调"}  # MDL-B deliberately absent

    _patch_catalog(monkeypatch, partial)

    result = await list_models(seeded, _user=object())  # type: ignore[arg-type]
    by_code = {i["product_code"]: i for i in result["items"]}

    assert by_code["MDL-A"]["product_type"] == "空调"
    assert by_code["MDL-B"]["product_type"] is None
    assert result["catalog_source"] == "aterag-mcp"
    assert result["catalog_warning"] is None
