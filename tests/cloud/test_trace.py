"""Integration tests for the trace API endpoints (T33).

Covers:
- GET /api/v1/trace/{serial_number} - JSON-LD response shape, 404 when
  no data, full chain with executions + instruments + measurements.
- GET /api/v1/trace/{serial_number}/structured - structured TestTraceResult
  response, 404 when no data, chronological ordering.
- Empty serial number edge case (404).
- Multiple executions + instruments + measurements end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement


def _make_execution(
    exec_id: str,
    dut_serial: str | None = "SN-001",
    station_id: str | None = "STATION-A",
    instrument_ids: list[str] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    status: str = "COMPLETED",
    sequence_id: str | None = "seq-001",
) -> Execution:
    """Build an Execution row with sensible defaults."""
    return Execution(
        id=exec_id,
        sequence_id=sequence_id,
        status=status,
        dut_serial=dut_serial,
        station_id=station_id,
        instrument_ids=instrument_ids,
        started_at=started_at,
        completed_at=completed_at,
    )


def _make_measurement(
    meas_id: str,
    dut_serial: str = "SN-001",
    execution_ref: str | None = "exec-001",
    station_ref: str | None = "STATION-A",
    product_ref: str = "PROD-X",
    name: str = "voltage_3v3",
    value: float | None = 3.3,
    unit: str = "V",
    limits_min: float | None = 3.2,
    limits_max: float | None = 3.4,
    outcome: str = "PASS",
    timestamp: datetime | None = None,
) -> Measurement:
    """Build a Measurement row with sensible defaults."""
    return Measurement(
        measurement_id=meas_id,
        execution_ref=execution_ref,
        station_ref=station_ref,
        product_ref=product_ref,
        dut_serial=dut_serial,
        timestamp=timestamp or datetime.now(UTC),
        name=name,
        value=value,
        limits_min=limits_min,
        limits_max=limits_max,
        unit=unit,
        outcome=outcome,
    )


async def _seed_trace(
    db_session: Any,
    serial: str = "SN-001",
) -> None:
    """Seed a DB with two executions + two measurements for a serial."""
    base = datetime.now(UTC)
    db_session.add(_make_execution(
        "exec-1",
        dut_serial=serial,
        started_at=base,
        completed_at=base + timedelta(minutes=5),
        instrument_ids=["osc-001", "dm-001"],
    ))
    db_session.add(_make_execution(
        "exec-2",
        dut_serial=serial,
        started_at=base + timedelta(hours=1),
        completed_at=base + timedelta(hours=1, minutes=5),
        instrument_ids=["osc-001"],
    ))
    db_session.add(_make_measurement(
        "m-1", dut_serial=serial, execution_ref="exec-1",
        timestamp=base + timedelta(minutes=1),
    ))
    db_session.add(_make_measurement(
        "m-2", dut_serial=serial, execution_ref="exec-2",
        name="current_5v", value=4.98, unit="A",
        timestamp=base + timedelta(hours=1, minutes=1),
    ))
    await db_session.commit()


# ---------------------------------------------------------------------------
# GET /api/v1/trace/{serial_number} - JSON-LD endpoint.
# ---------------------------------------------------------------------------


class TestGetTraceJsonLd:
    """Tests for GET /api/v1/trace/{serial_number} (JSON-LD)."""

    @pytest.mark.asyncio
    async def test_404_when_no_data(self, client: Any) -> None:
        """A serial number with no executions and no measurements returns 404."""
        resp = await client.get("/api/v1/trace/SN-NOPE")
        assert resp.status_code == 404
        assert "SN-NOPE" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_returns_jsonld_with_context_and_graph(
        self, client: Any, db_session: Any
    ) -> None:
        """The response is a JSON-LD document with @context and @graph."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001")
        assert resp.status_code == 200
        data = resp.json()
        assert "@context" in data
        assert data["@context"]["prov"] == "http://www.w3.org/ns/prov#"
        assert "@graph" in data
        assert isinstance(data["@graph"], list)

    @pytest.mark.asyncio
    async def test_graph_contains_dut_entity(self, client: Any, db_session: Any) -> None:
        """The graph always contains the DUT entity node."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001")
        graph = resp.json()["@graph"]
        dut_nodes = [n for n in graph if n.get("@id") == "ate:dut/SN-001"]
        assert len(dut_nodes) == 1
        assert dut_nodes[0]["@type"] == "prov:Entity"
        assert dut_nodes[0]["ate:dutSerial"] == "SN-001"

    @pytest.mark.asyncio
    async def test_graph_contains_activity_per_execution(
        self, client: Any, db_session: Any
    ) -> None:
        """Each execution produces one prov:Activity node."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001")
        graph = resp.json()["@graph"]
        activities = [n for n in graph if n.get("@type") == "prov:Activity"]
        assert len(activities) == 2
        act_ids = {a["@id"] for a in activities}
        assert "ate:exec/exec-1" in act_ids
        assert "ate:exec/exec-2" in act_ids

    @pytest.mark.asyncio
    async def test_graph_contains_instrument_entities(
        self, client: Any, db_session: Any
    ) -> None:
        """Instruments appear as prov:Entity nodes (deduplicated)."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001")
        graph = resp.json()["@graph"]
        instr_nodes = [
            n for n in graph
            if n.get("@type") == "prov:Entity"
            and str(n.get("@id", "")).startswith("ate:instr/")
        ]
        # osc-001 appears in both executions but emitted once; dm-001 once.
        instr_ids = {n["@id"] for n in instr_nodes}
        assert instr_ids == {"ate:instr/osc-001", "ate:instr/dm-001"}

    @pytest.mark.asyncio
    async def test_graph_contains_measurement_entities_with_was_generated_by(
        self, client: Any, db_session: Any
    ) -> None:
        """Measurements appear as prov:Entity with prov:wasGeneratedBy."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001")
        graph = resp.json()["@graph"]
        meas_nodes = [
            n for n in graph
            if n.get("@type") == "prov:Entity"
            and str(n.get("@id", "")).startswith("ate:meas/")
        ]
        assert len(meas_nodes) == 2
        meas_by_id = {n["@id"]: n for n in meas_nodes}
        assert meas_by_id["ate:meas/m-1"]["prov:wasGeneratedBy"]["@id"] == "ate:exec/exec-1"
        assert meas_by_id["ate:meas/m-2"]["prov:wasGeneratedBy"]["@id"] == "ate:exec/exec-2"

    @pytest.mark.asyncio
    async def test_activity_used_references_dut_and_instruments(
        self, client: Any, db_session: Any
    ) -> None:
        """prov:used on each activity references the DUT + its instruments."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001")
        graph = resp.json()["@graph"]
        act1 = next(n for n in graph if n.get("@id") == "ate:exec/exec-1")
        used_ids = {u["@id"] for u in act1["prov:used"]}
        assert "ate:dut/SN-001" in used_ids
        assert "ate:instr/osc-001" in used_ids
        assert "ate:instr/dm-001" in used_ids


# ---------------------------------------------------------------------------
# GET /api/v1/trace/{serial_number}/structured - structured endpoint.
# ---------------------------------------------------------------------------


class TestGetTraceStructured:
    """Tests for GET /api/v1/trace/{serial_number}/structured."""

    @pytest.mark.asyncio
    async def test_404_when_no_data(self, client: Any) -> None:
        """A serial number with no data returns 404."""
        resp = await client.get("/api/v1/trace/SN-NOPE/structured")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_structured_result(self, client: Any, db_session: Any) -> None:
        """The response is a TestTraceResult with ordered steps."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001/structured")
        assert resp.status_code == 200
        data = resp.json()
        assert data["dut_serial"] == "SN-001"
        assert len(data["steps"]) == 2
        assert data["steps"][0]["execution_id"] == "exec-1"
        assert data["steps"][1]["execution_id"] == "exec-2"

    @pytest.mark.asyncio
    async def test_structured_step_has_instruments_and_measurements(
        self, client: Any, db_session: Any
    ) -> None:
        """Each structured step carries its instruments and measurements."""
        await _seed_trace(db_session)

        resp = await client.get("/api/v1/trace/SN-001/structured")
        steps = resp.json()["steps"]
        step1 = next(s for s in steps if s["execution_id"] == "exec-1")
        assert len(step1["instruments"]) == 2
        assert {i["instrument_id"] for i in step1["instruments"]} == {"osc-001", "dm-001"}
        assert len(step1["measurements"]) == 1
        assert step1["measurements"][0]["measurement_id"] == "m-1"

    @pytest.mark.asyncio
    async def test_structured_chronological_ordering(
        self, client: Any, db_session: Any
    ) -> None:
        """Steps are ordered by started_at ascending."""
        base = datetime.now(UTC)
        db_session.add(_make_execution(
            "exec-late", started_at=base + timedelta(hours=2),
        ))
        db_session.add(_make_execution(
            "exec-early", started_at=base,
        ))
        await db_session.commit()

        resp = await client.get("/api/v1/trace/SN-001/structured")
        steps = resp.json()["steps"]
        assert steps[0]["execution_id"] == "exec-early"
        assert steps[1]["execution_id"] == "exec-late"


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


class TestTraceEdgeCases:
    """Edge-case tests for the trace endpoints."""

    @pytest.mark.asyncio
    async def test_execution_without_dut_serial_excluded(
        self, client: Any, db_session: Any
    ) -> None:
        """An execution with dut_serial=None is not in the trace."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution("exec-no-dut", dut_serial=None, started_at=ts))
        db_session.add(_make_execution("exec-with-dut", dut_serial="SN-001", started_at=ts))
        await db_session.commit()

        resp = await client.get("/api/v1/trace/SN-001/structured")
        assert resp.status_code == 200
        steps = resp.json()["steps"]
        assert len(steps) == 1
        assert steps[0]["execution_id"] == "exec-with-dut"

    @pytest.mark.asyncio
    async def test_measurement_only_no_execution_returns_404(
        self, client: Any, db_session: Any
    ) -> None:
        """A measurement with execution_ref=None yields no steps -> 404."""
        ts = datetime.now(UTC)
        db_session.add(_make_measurement("m-1", execution_ref=None, timestamp=ts))
        await db_session.commit()

        # No execution row + no execution_ref -> no synthetic step -> 404.
        resp = await client.get("/api/v1/trace/SN-001")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_orphan_measurement_surfaces_in_chain(
        self, client: Any, db_session: Any
    ) -> None:
        """An orphan measurement (execution_ref with no row) is surfaced."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution("exec-real", started_at=ts - timedelta(hours=2)))
        db_session.add(_make_measurement(
            "m-orphan", execution_ref="exec-deleted", timestamp=ts - timedelta(hours=1),
        ))
        await db_session.commit()

        resp = await client.get("/api/v1/trace/SN-001/structured")
        assert resp.status_code == 200
        steps = resp.json()["steps"]
        # Real execution + synthetic orphan step.
        assert len(steps) == 2
        synthetic = next(s for s in steps if s["execution_id"] == "exec-deleted")
        assert synthetic["status"] == "UNKNOWN"
        assert len(synthetic["measurements"]) == 1
