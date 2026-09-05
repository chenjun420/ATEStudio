"""Tests for task 11: ATML IEEE 1671 TestDescription import/parse.

Covers the INGEST side of ATML (the exporter only handled IEEE 1636.1
TestResults):

- a well-formed IEEE 1671 TestDescription is parsed into TestRequirement /
  TestCase ORM rows (source="atml", atml_ref populated);
- test cases that reference a DSL step are linked (sequence_id/step_id);
- test cases with no DSL mapping are still persisted with null refs and
  reported as a traceability gap (never crashed);
- malformed XML / wrong root element -> a controlled 400 (never a raw 500);
- re-importing the same document is idempotent (stable codes update in place,
  no duplicate rows).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.knowledge import TestCase, TestRequirement
from ate_cloud.models.sequence import Sequence

# A representative IEEE 1671 TestDescription. The parser is namespace-tolerant
# (matches elements by local name), but this fixture uses the real 1671
# TestDescription + Common namespaces the way a conforming document would.
SAMPLE_1671_XML = """<?xml version="1.0" encoding="UTF-8"?>
<td:TestDescription xmlns:td="urn:IEEE-1671:2010:TestDescription"
                    xmlns:c="urn:IEEE-1671:2010:Common"
                    id="TD-PSU-001" version="1.0" name="PSU Board Test Description">
  <td:UUT>
    <c:Identifier>PSU-BOARD-V2</c:Identifier>
    <c:Name>Power Supply Board V2</c:Name>
  </td:UUT>
  <td:Requirements>
    <td:TestRequirement id="REQ-PSU-001" name="Output voltage in tolerance">
      <td:Description>The 5V rail shall be 5.0V +/- 1% under load.</td:Description>
    </td:TestRequirement>
    <td:TestRequirement id="REQ-PSU-002" name="Ripple within limit">
      <td:Description>Output ripple shall be below 50mV peak-to-peak.</td:Description>
    </td:TestRequirement>
  </td:Requirements>
  <td:TestGroups>
    <td:TestGroup id="TG-POWER" name="Power Tests">
      <td:Test id="TC-VOLT-001" name="Measure 5V rail voltage" requirementId="REQ-PSU-001">
        <td:DslMapping sequenceId="seq-psu" stepId="measure_5v" />
      </td:Test>
      <td:Test id="TC-RIPPLE-001" name="Measure output ripple" requirementId="REQ-PSU-002" />
    </td:TestGroup>
  </td:TestGroups>
</td:TestDescription>
"""

# Same content but a requirement title changed — used to prove update-in-place.
SAMPLE_1671_XML_REIMPORT = SAMPLE_1671_XML.replace(
    "Output voltage in tolerance", "Output voltage within 5V +/- 1%"
)

DSL_SEQUENCE_YAML = """name: psu_power_test
version: "3.2"
scope: production
steps:
  - id: measure_5v
    script: measure_voltage.py
  - id: measure_ripple
    script: measure_ripple.py
"""


async def _seed_dsl_sequence(db_session: AsyncSession) -> None:
    """Insert a DSL sequence whose step ``measure_5v`` a test case maps to."""
    db_session.add(
        Sequence(
            id="seq-psu",
            name="psu_power_test",
            yaml_content=DSL_SEQUENCE_YAML,
        )
    )
    await db_session.flush()


# ── Parser-level (pure, no DB) ──────────────────────────────────────────────


def test_parser_extracts_product_requirements_and_cases() -> None:
    """Given a 1671 document, the parser yields product code, requirements and
    cases with their DSL mapping hints (namespace-tolerant)."""
    from ate_cloud.services.atml_td_parser import parse_test_description

    parsed = parse_test_description(SAMPLE_1671_XML)

    assert parsed.product_code == "PSU-BOARD-V2"
    assert [r.code for r in parsed.requirements] == ["REQ-PSU-001", "REQ-PSU-002"]
    assert parsed.requirements[0].title == "Output voltage in tolerance"
    assert "5.0V" in (parsed.requirements[0].description or "")

    by_code = {c.case_code: c for c in parsed.cases}
    assert set(by_code) == {"TC-VOLT-001", "TC-RIPPLE-001"}
    volt = by_code["TC-VOLT-001"]
    assert volt.requirement_code == "REQ-PSU-001"
    assert volt.sequence_id == "seq-psu"
    assert volt.step_id == "measure_5v"
    assert volt.atml_ref  # populated traceability ref
    ripple = by_code["TC-RIPPLE-001"]
    assert ripple.sequence_id is None
    assert ripple.step_id == ""


def test_parser_tolerates_missing_namespaces() -> None:
    """A document with no namespace declarations still parses by local name."""
    from ate_cloud.services.atml_td_parser import parse_test_description

    xml = (
        "<TestDescription id='TD-1'>"
        "<UUT><Identifier>PROD-X</Identifier></UUT>"
        "<TestGroups><TestGroup id='G1'><Test id='TC-1' name='T1'>"
        "</Test></TestGroup></TestGroups>"
        "</TestDescription>"
    )
    parsed = parse_test_description(xml)
    assert parsed.product_code == "PROD-X"
    assert [c.case_code for c in parsed.cases] == ["TC-1"]


@pytest.mark.parametrize(
    "bad_xml",
    [
        "<TestDescription><UUT>",  # not well-formed
        "this is not xml at all",
        "<OtherRoot><UUT/></OtherRoot>",  # well-formed but wrong document type
    ],
)
def test_parser_raises_typed_error_on_bad_input(bad_xml: str) -> None:
    """Malformed or unknown-root XML raises ATMLParseError, never a raw
    ElementTree traceback leaking to the caller."""
    from ate_cloud.services.atml_td_parser import ATMLParseError, parse_test_description

    with pytest.raises(ATMLParseError):
        parse_test_description(bad_xml)


# ── Service + API (DB-backed) ───────────────────────────────────────────────


async def test_import_well_formed_persists_rows_and_maps_dsl(
    client, db_session: AsyncSession
) -> None:
    """Given a 1671 document and a matching DSL sequence, import persists
    requirements + cases and links the mapped case to its DSL step."""
    await _seed_dsl_sequence(db_session)

    resp = await client.post(
        "/api/v1/atml/import-test-description",
        content=SAMPLE_1671_XML,
        headers={"Content-Type": "application/xml"},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["product_code"] == "PSU-BOARD-V2"
    assert summary["requirements"]["created"] == 2
    assert summary["cases"]["created"] == 2

    reqs = (
        (await db_session.execute(select(TestRequirement))).scalars().all()
    )
    assert len(reqs) == 2
    assert all(r.source == "atml" for r in reqs)
    assert all(r.atml_ref for r in reqs)
    assert all(r.product_code == "PSU-BOARD-V2" for r in reqs)

    cases = (await db_session.execute(select(TestCase))).scalars().all()
    by_code = {c.case_code: c for c in cases}
    volt = by_code["TC-VOLT-001"]
    assert volt.sequence_id == "seq-psu"
    assert volt.step_id == "measure_5v"
    assert volt.atml_ref
    # requirement linkage resolved from requirementId
    volt_req = next(r for r in reqs if r.requirement_code == "REQ-PSU-001")
    assert volt.requirement_id == volt_req.id


async def test_unmapped_case_persisted_with_null_refs_and_reported(
    client, db_session: AsyncSession
) -> None:
    """A case with no DSL mapping is still persisted (traceability gap is
    recorded, not crashed): null sequence_id, empty step_id, and listed in the
    response unmapped section."""
    resp = await client.post(
        "/api/v1/atml/import-test-description",
        content=SAMPLE_1671_XML,
        headers={"Content-Type": "application/xml"},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    unmapped_codes = {u["case_code"] for u in summary["unmapped"]}
    # No DSL sequence exists: the mapped hint cannot resolve, so BOTH cases are
    # reported unmapped (and still persisted).
    assert "TC-RIPPLE-001" in unmapped_codes

    cases = (await db_session.execute(select(TestCase))).scalars().all()
    ripple = next(c for c in cases if c.case_code == "TC-RIPPLE-001")
    assert ripple.sequence_id is None
    assert ripple.step_id == ""
    assert ripple.atml_ref  # ATML traceability ref always populated


async def test_malformed_xml_returns_400_not_500(client) -> None:
    """Malformed XML yields a controlled 400 with a clear message."""
    resp = await client.post(
        "/api/v1/atml/import-test-description",
        content="<TestDescription><UUT>",
        headers={"Content-Type": "application/xml"},
    )
    assert resp.status_code == 400, resp.text
    assert "TestDescription" in resp.json()["detail"] or "xml" in resp.json()[
        "detail"
    ].lower()


async def test_unknown_document_returns_400(client) -> None:
    """A well-formed document that is not a TestDescription is rejected 400."""
    resp = await client.post(
        "/api/v1/atml/import-test-description",
        content="<?xml version='1.0'?><TestResults><UUT/></TestResults>",
        headers={"Content-Type": "application/xml"},
    )
    assert resp.status_code == 400, resp.text


async def test_empty_body_returns_400(client) -> None:
    resp = await client.post(
        "/api/v1/atml/import-test-description",
        content="   ",
        headers={"Content-Type": "application/xml"},
    )
    assert resp.status_code == 400, resp.text


async def test_reimport_is_idempotent_updates_in_place(
    client, db_session: AsyncSession
) -> None:
    """Re-importing the same document updates rows rather than duplicating."""
    first = await client.post(
        "/api/v1/atml/import-test-description",
        content=SAMPLE_1671_XML,
        headers={"Content-Type": "application/xml"},
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        "/api/v1/atml/import-test-description",
        content=SAMPLE_1671_XML_REIMPORT,
        headers={"Content-Type": "application/xml"},
    )
    assert second.status_code == 200, second.text
    summary = second.json()
    assert summary["requirements"]["created"] == 0
    assert summary["requirements"]["updated"] == 2
    assert summary["cases"]["created"] == 0
    assert summary["cases"]["updated"] == 2

    reqs = (
        (await db_session.execute(select(TestRequirement))).scalars().all()
    )
    cases = (await db_session.execute(select(TestCase))).scalars().all()
    assert len(reqs) == 2  # no duplicates
    assert len(cases) == 2
    changed = next(r for r in reqs if r.requirement_code == "REQ-PSU-001")
    assert changed.title == "Output voltage within 5V +/- 1%"


async def test_import_accepts_xml_body_bytes(client, db_session: AsyncSession) -> None:
    """The endpoint accepts the raw XML document as the request body (the way an
    uploaded .xml file is sent), including text/xml content type."""
    resp = await client.post(
        "/api/v1/atml/import-test-description",
        content=SAMPLE_1671_XML.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["product_code"] == "PSU-BOARD-V2"
