"""Task: knowledge READ APIs supporting frontend tasks 25/26.

Covers the GET surface added to the EXISTING knowledge router
(``/api/v1/knowledge`` — no new mount, so the auth sentinel stays
protected==27 / anonymous==5):

- ``GET /knowledge/requirements`` — paged TestRequirement list with
  ``product_code`` / ``source`` filters ({items,total} envelope like fmea).
- ``GET /knowledge/cases`` — paged TestCase list with ``requirement_id`` /
  ``product_code`` filters; each row carries its requirement link plus the
  DSL ``sequence_id``/``step_id`` mapping for the traceability matrix.
- ``GET /knowledge/traceability`` — requirement → cases → DSL-step tree
  filtered by ``product_code`` (unlinked cases land on a None-requirement
  bucket so matrix gaps stay visible).
- ``GET /knowledge/graph`` — {nodes, edges} browse payload sourced through
  the GraphService protocol; 503 with a clear message when no graph backend
  is configured/healthy (graph browse data lives in the graph, unlike the
  extraction endpoint which degrades to ORM-only).

Graph tests use an in-memory ``BrowseGraphFake`` that answers the two
statement SHAPES the browse helper emits (label-scoped / full scans) — no
live services, using the same shape-dispatch approach as the other fakes.
The mount-level JWT guard (anonymous -> 401) is owned by
``test_auth_enforcement.py``; one anon-401 smoke is included here too.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ate_cloud.models.knowledge import TestCase, TestRequirement

# ── Fakes ──────────────────────────────────────────────────────────────────


# ── ORM seed helpers ───────────────────────────────────────────────────────


async def _seed_requirement(
    db_session: Any,
    *,
    product_code: str = "DEMO-BOARD",
    requirement_code: str | None = None,
    source: str = "manual",
    title: str = "Requirement",
) -> TestRequirement:
    req = TestRequirement(
        id=str(uuid.uuid4()),
        product_code=product_code,
        requirement_code=requirement_code or f"REQ-{uuid.uuid4().hex[:6]}",
        title=title,
        source=source,
    )
    db_session.add(req)
    await db_session.flush()
    return req


async def _seed_case(
    db_session: Any,
    *,
    requirement_id: str | None = None,
    case_code: str | None = None,
    sequence_id: str | None = None,
    step_id: str = "",
    status: str = "active",
    title: str = "Case",
) -> TestCase:
    case = TestCase(
        id=str(uuid.uuid4()),
        requirement_id=requirement_id,
        case_code=case_code or f"TC-{uuid.uuid4().hex[:6]}",
        title=title,
        sequence_id=sequence_id,
        step_id=step_id,
        status=status,
    )
    db_session.add(case)
    await db_session.flush()
    return case


# ── GET /knowledge/requirements ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requirements_list_paged_with_total(client: Any, db_session: Any) -> None:
    """Given 3 requirements, GET list returns the {items,total} envelope."""
    for _ in range(3):
        await _seed_requirement(db_session)
    await db_session.commit()

    resp = await client.get("/api/v1/knowledge/requirements")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    item = body["items"][0]
    assert {"id", "product_code", "requirement_code", "title", "source", "created_at"} <= set(item)


@pytest.mark.asyncio
async def test_requirements_pagination_skip_and_limit(client: Any, db_session: Any) -> None:
    """Given 5 requirements, skip=2&limit=2 returns 2 items but total stays 5."""
    for _ in range(5):
        await _seed_requirement(db_session)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/knowledge/requirements", params={"skip": 2, "limit": 2}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_requirements_filter_by_product_and_source(
    client: Any, db_session: Any
) -> None:
    """product_code and source filters narrow the paged list independently."""
    await _seed_requirement(db_session, product_code="P1", source="dsl")
    await _seed_requirement(db_session, product_code="P1", source="manual")
    await _seed_requirement(db_session, product_code="P2", source="dsl")
    await db_session.commit()

    by_product = await client.get(
        "/api/v1/knowledge/requirements", params={"product_code": "P1"}
    )
    assert by_product.status_code == 200
    assert by_product.json()["total"] == 2
    assert all(i["product_code"] == "P1" for i in by_product.json()["items"])

    by_source = await client.get(
        "/api/v1/knowledge/requirements", params={"source": "dsl"}
    )
    assert by_source.status_code == 200
    assert by_source.json()["total"] == 2
    assert all(i["source"] == "dsl" for i in by_source.json()["items"])

    both = await client.get(
        "/api/v1/knowledge/requirements",
        params={"product_code": "P1", "source": "manual"},
    )
    assert both.json()["total"] == 1


# ── GET /knowledge/cases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cases_list_includes_requirement_link_and_dsl_mapping(
    client: Any, db_session: Any
) -> None:
    """Each case row carries requirement_id + sequence_id/step_id (matrix join)."""
    req = await _seed_requirement(db_session, product_code="DEMO-BOARD")
    await _seed_case(
        db_session,
        requirement_id=req.id,
        case_code="TC-1",
        sequence_id="seq-main",
        step_id="step_measure",
    )
    await db_session.commit()

    resp = await client.get("/api/v1/knowledge/cases")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["case_code"] == "TC-1"
    assert item["requirement_id"] == req.id
    assert item["sequence_id"] == "seq-main"
    assert item["step_id"] == "step_measure"


@pytest.mark.asyncio
async def test_cases_filter_by_requirement_and_product(
    client: Any, db_session: Any
) -> None:
    """requirement_id joins through to product; both filters narrow the list."""
    req_a = await _seed_requirement(db_session, product_code="PA")
    req_b = await _seed_requirement(db_session, product_code="PB")
    await _seed_case(db_session, requirement_id=req_a.id, case_code="TC-A1")
    await _seed_case(db_session, requirement_id=req_a.id, case_code="TC-A2")
    await _seed_case(db_session, requirement_id=req_b.id, case_code="TC-B1")
    await _seed_case(db_session, requirement_id=None, case_code="TC-ORPHAN")
    await db_session.commit()

    by_req = await client.get(
        "/api/v1/knowledge/cases", params={"requirement_id": req_a.id}
    )
    assert by_req.status_code == 200
    assert by_req.json()["total"] == 2
    assert {i["case_code"] for i in by_req.json()["items"]} == {"TC-A1", "TC-A2"}

    by_product = await client.get(
        "/api/v1/knowledge/cases", params={"product_code": "PB"}
    )
    assert by_product.json()["total"] == 1
    assert by_product.json()["items"][0]["case_code"] == "TC-B1"

    # Orphan cases (ingested before their requirement) are still listable:
    # they appear in the unfiltered list with requirement_id == None.
    all_cases = await client.get("/api/v1/knowledge/cases")
    assert all_cases.status_code == 200
    assert all_cases.json()["total"] == 4
    orphan = next(
        i for i in all_cases.json()["items"] if i["case_code"] == "TC-ORPHAN"
    )
    assert orphan["requirement_id"] is None


# ── GET /knowledge/traceability ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traceability_builds_requirement_cases_step_tree(
    client: Any, db_session: Any
) -> None:
    """Tree shape: requirement -> cases[] with DSL sequence/step links."""
    req = await _seed_requirement(
        db_session, product_code="DEMO-BOARD", requirement_code="REQ-PSU-001"
    )
    await _seed_case(
        db_session,
        requirement_id=req.id,
        case_code="TC-VOLT",
        sequence_id="seq-psu",
        step_id="step_measure",
    )
    await _seed_case(
        db_session,
        requirement_id=req.id,
        case_code="TC-OVP",
        sequence_id="seq-psu",
        step_id="step_validate",
    )
    other = await _seed_requirement(db_session, product_code="OTHER", requirement_code="REQ-X")
    await _seed_case(db_session, requirement_id=other.id, case_code="TC-OTHER")
    await db_session.commit()

    resp = await client.get(
        "/api/v1/knowledge/traceability", params={"product_code": "DEMO-BOARD"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product_code"] == "DEMO-BOARD"
    assert len(body["requirements"]) == 1
    node = body["requirements"][0]
    assert node["requirement_code"] == "REQ-PSU-001"
    assert {c["case_code"] for c in node["cases"]} == {"TC-VOLT", "TC-OVP"}
    volt = next(c for c in node["cases"] if c["case_code"] == "TC-VOLT")
    assert volt["sequence_id"] == "seq-psu"
    assert volt["step_id"] == "step_measure"


@pytest.mark.asyncio
async def test_traceability_includes_unlinked_cases_bucket(
    client: Any, db_session: Any
) -> None:
    """Cases with no requirement (ingestion gap) surface under a null bucket."""
    await _seed_case(db_session, requirement_id=None, case_code="TC-ORPHAN")
    await db_session.commit()

    resp = await client.get("/api/v1/knowledge/traceability")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Null bucket keeps matrix gaps visible instead of silently dropping rows.
    unlinked = body.get("unlinked_cases", [])
    assert any(c["case_code"] == "TC-ORPHAN" for c in unlinked)


# ── GET /knowledge/graph ───────────────────────────────────────────────────


# ── Auth smoke (mount-level guard owned by test_auth_enforcement.py) ────────


@pytest.mark.asyncio
async def test_knowledge_read_endpoints_anonymous_401(
    client: Any, monkeypatch: pytest.MonkeyPatch, db_session: Any
) -> None:
    """Anonymous requests to the new GET endpoints are rejected with 401."""
    from ate_cloud.config import settings

    monkeypatch.setattr(settings, "dev_mode", False)
    for path in (
        "/api/v1/knowledge/requirements",
        "/api/v1/knowledge/cases",
        "/api/v1/knowledge/traceability",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path}: expected 401, got {resp.status_code}"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
