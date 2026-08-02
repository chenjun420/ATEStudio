"""Unit tests for TestTraceService.

Covers:
- build_trace: empty result, single execution with measurements, multiple
  executions ordered chronologically, orphan measurements (execution_ref
  with no matching execution row), executions without dut_serial are
  excluded, measurements grouped under the correct execution.
- to_jsonld: W3C PROV projection - DUT entity always present, each
  execution is a prov:Activity, each measurement is a prov:Entity with
  prov:wasGeneratedBy, each instrument is a prov:Entity referenced via
  prov:used, instrument deduplication across steps, empty trace still
  produces a valid document with the DUT node.
- Integration: build_trace then to_jsonld round-trip preserves all
  instruments and measurements.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ate_cloud.models import Base
from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement
from ate_cloud.schemas.trace import TestTraceResult
from ate_cloud.services.test_trace_service import TestTraceService


# Local in-memory SQLite engine fixture for DB-backed unit tests.
# The cloud conftest.py test_engine fixture is not available under tests/unit/.
@pytest.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create an in-memory SQLite engine with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session that rolls back after the test."""
    factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session
        await session.rollback()


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


# ---------------------------------------------------------------------------
# build_trace - core query and ordering.
# ---------------------------------------------------------------------------


class TestBuildTraceEmpty:
    """Tests for build_trace when no data exists."""

    @pytest.mark.asyncio
    async def test_empty_when_no_executions_and_no_measurements(
        self, db_session: AsyncSession
    ) -> None:
        """build_trace returns an empty steps list when nothing exists."""
        service = TestTraceService(db_session)
        result = await service.build_trace("SN-NOPE")
        assert result.dut_serial == "SN-NOPE"
        assert result.steps == []

    @pytest.mark.asyncio
    async def test_empty_result_has_correct_serial(self, db_session: AsyncSession) -> None:
        """The returned result echoes the queried serial number."""
        service = TestTraceService(db_session)
        result = await service.build_trace("SN-XYZ")
        assert result.dut_serial == "SN-XYZ"


class TestBuildTraceSingleExecution:
    """Tests for build_trace with a single execution + measurements."""

    @pytest.mark.asyncio
    async def test_single_execution_with_measurements(self, db_session: AsyncSession) -> None:
        """One execution with two measurements produces one step with both."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution(
            "exec-1",
            started_at=ts - timedelta(minutes=10),
            completed_at=ts,
            instrument_ids=["osc-001", "dm-001"],
        ))
        db_session.add(_make_measurement(
            "m-1", execution_ref="exec-1", timestamp=ts - timedelta(minutes=5),
        ))
        db_session.add(_make_measurement(
            "m-2", name="current_5v", value=4.98, unit="A",
            execution_ref="exec-1", timestamp=ts - timedelta(minutes=3),
        ))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        assert len(result.steps) == 1
        step = result.steps[0]
        assert step.execution_id == "exec-1"
        assert step.station_id == "STATION-A"
        assert step.status == "COMPLETED"
        assert step.sequence_id == "seq-001"
        assert len(step.instruments) == 2
        assert step.instruments[0].instrument_id == "osc-001"
        assert step.instruments[1].instrument_id == "dm-001"
        assert len(step.measurements) == 2
        assert step.measurements[0].name == "voltage_3v3"
        assert step.measurements[1].name == "current_5v"

    @pytest.mark.asyncio
    async def test_execution_without_dut_serial_excluded(self, db_session: AsyncSession) -> None:
        """An execution with dut_serial=None is not in the chain."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution("exec-no-dut", dut_serial=None, started_at=ts))
        db_session.add(_make_execution("exec-with-dut", dut_serial="SN-001", started_at=ts))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        assert len(result.steps) == 1
        assert result.steps[0].execution_id == "exec-with-dut"

    @pytest.mark.asyncio
    async def test_execution_with_null_instrument_ids(self, db_session: AsyncSession) -> None:
        """instrument_ids=None yields an empty instruments list (no crash)."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution("exec-1", instrument_ids=None, started_at=ts))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        assert len(result.steps) == 1
        assert result.steps[0].instruments == []


class TestBuildTraceMultipleExecutions:
    """Tests for build_trace with multiple executions - ordering."""

    @pytest.mark.asyncio
    async def test_chronological_ordering_by_started_at(
        self, db_session: AsyncSession
    ) -> None:
        """Steps are ordered by started_at ascending."""
        base = datetime.now(UTC)
        db_session.add(_make_execution("exec-late", started_at=base + timedelta(hours=2)))
        db_session.add(_make_execution("exec-early", started_at=base + timedelta(hours=1)))
        db_session.add(_make_execution("exec-first", started_at=base))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        assert len(result.steps) == 3
        assert result.steps[0].execution_id == "exec-first"
        assert result.steps[1].execution_id == "exec-early"
        assert result.steps[2].execution_id == "exec-late"

    @pytest.mark.asyncio
    async def test_null_started_at_sorts_last(self, db_session: AsyncSession) -> None:
        """Executions with no started_at sort after dated ones (NULLS LAST)."""
        base = datetime.now(UTC)
        db_session.add(_make_execution("exec-null-ts", started_at=None))
        db_session.add(_make_execution("exec-dated", started_at=base))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        assert len(result.steps) == 2
        assert result.steps[0].execution_id == "exec-dated"
        assert result.steps[1].execution_id == "exec-null-ts"

    @pytest.mark.asyncio
    async def test_measurements_grouped_under_correct_execution(
        self, db_session: AsyncSession
    ) -> None:
        """Each measurement lands under its execution_ref, not a sibling."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution("exec-a", started_at=ts - timedelta(minutes=20)))
        db_session.add(_make_execution("exec-b", started_at=ts - timedelta(minutes=10)))
        db_session.add(_make_measurement("m-a1", execution_ref="exec-a", timestamp=ts - timedelta(minutes=19)))
        db_session.add(_make_measurement("m-b1", execution_ref="exec-b", timestamp=ts - timedelta(minutes=9)))
        db_session.add(_make_measurement("m-a2", execution_ref="exec-a", timestamp=ts - timedelta(minutes=18)))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        assert len(result.steps) == 2
        step_a = next(s for s in result.steps if s.execution_id == "exec-a")
        step_b = next(s for s in result.steps if s.execution_id == "exec-b")
        assert {m.measurement_id for m in step_a.measurements} == {"m-a1", "m-a2"}
        assert {m.measurement_id for m in step_b.measurements} == {"m-b1"}


class TestBuildTraceOrphanMeasurements:
    """Tests for build_trace with orphan measurements (no matching execution)."""

    @pytest.mark.asyncio
    async def test_orphan_measurement_surfaces_as_synthetic_step(
        self, db_session: AsyncSession
    ) -> None:
        """A measurement whose execution_ref has no row becomes a synthetic step."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution("exec-real", started_at=ts - timedelta(hours=2)))
        db_session.add(_make_measurement(
            "m-orphan", execution_ref="exec-deleted", timestamp=ts - timedelta(hours=1),
        ))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        # Two steps: the real execution + the synthetic orphan step.
        assert len(result.steps) == 2
        synthetic = next(s for s in result.steps if s.execution_id == "exec-deleted")
        assert synthetic.status == "UNKNOWN"
        assert synthetic.station_id is None
        assert synthetic.sequence_id is None
        assert synthetic.instruments == []
        assert len(synthetic.measurements) == 1
        assert synthetic.measurements[0].measurement_id == "m-orphan"

    @pytest.mark.asyncio
    async def test_orphan_step_uses_earliest_measurement_timestamp(
        self, db_session: AsyncSession
    ) -> None:
        """The synthetic step's started_at is the earliest measurement ts."""
        base = datetime.now(UTC)
        db_session.add(_make_measurement(
            "m-1", execution_ref="orphan-ref",
            timestamp=base + timedelta(minutes=10),
        ))
        db_session.add(_make_measurement(
            "m-2", execution_ref="orphan-ref",
            timestamp=base + timedelta(minutes=5),
        ))
        db_session.add(_make_measurement(
            "m-3", execution_ref="orphan-ref",
            timestamp=base + timedelta(minutes=15),
        ))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        synthetic = next(s for s in result.steps if s.execution_id == "orphan-ref")
        # SQLite strips tzinfo on read, so compare naive UTC values.
        expected = (base + timedelta(minutes=5)).replace(tzinfo=None)
        assert synthetic.started_at == expected

    @pytest.mark.asyncio
    async def test_measurement_with_null_execution_ref_not_orphan(
        self, db_session: AsyncSession
    ) -> None:
        """A measurement with execution_ref=None is not surfaced as a step."""
        ts = datetime.now(UTC)
        db_session.add(_make_measurement("m-1", execution_ref=None, timestamp=ts))
        await db_session.commit()

        service = TestTraceService(db_session)
        result = await service.build_trace("SN-001")

        # No execution row and no execution_ref -> no synthetic step, no
        # real step. The measurement is dropped (it cannot be attributed
        # to any execution).
        assert result.steps == []


# ---------------------------------------------------------------------------
# to_jsonld - W3C PROV projection.
# ---------------------------------------------------------------------------


class TestToJsonLdDutEntity:
    """Tests for the DUT entity in the JSON-LD projection."""

    def test_empty_trace_has_dut_entity_only(self) -> None:
        """An empty trace still produces a document with the DUT node."""
        trace = TestTraceResult(dut_serial="SN-001", steps=[])
        doc = TestTraceService.to_jsonld(trace)

        assert "@context" in doc
        assert "@graph" in doc
        graph = doc["@graph"]
        assert isinstance(graph, list)
        assert len(graph) == 1
        dut_node = graph[0]
        assert dut_node["@id"] == "ate:dut/SN-001"
        assert dut_node["@type"] == "prov:Entity"
        assert dut_node["ate:dutSerial"] == "SN-001"

    def test_context_has_prov_namespace(self) -> None:
        """The @context declares the prov namespace."""
        trace = TestTraceResult(dut_serial="SN-001", steps=[])
        doc = TestTraceService.to_jsonld(trace)
        ctx = doc["@context"]
        assert ctx["prov"] == "http://www.w3.org/ns/prov#"


class TestToJsonLdActivityAndRelations:
    """Tests for activity nodes and their PROV relations."""

    def test_execution_becomes_prov_activity(self) -> None:
        """Each execution step is projected as a prov:Activity."""
        from ate_cloud.schemas.trace import TraceInstrument, TraceStep

        ts = datetime.now(UTC)
        trace = TestTraceResult(
            dut_serial="SN-001",
            steps=[
                TraceStep(
                    execution_id="exec-1",
                    sequence_id="seq-9",
                    station_id="STN-A",
                    status="COMPLETED",
                    started_at=ts,
                    completed_at=ts + timedelta(minutes=5),
                    instruments=[TraceInstrument(instrument_id="osc-1")],
                    measurements=[],
                ),
            ],
        )

        doc = TestTraceService.to_jsonld(trace)
        activities = [
            n for n in doc["@graph"] if n.get("@type") == "prov:Activity"
        ]
        assert len(activities) == 1
        act = activities[0]
        assert act["@id"] == "ate:exec/exec-1"
        assert act["ate:status"] == "COMPLETED"
        assert act["ate:sequenceId"] == "seq-9"
        assert act["ate:stationId"] == "STN-A"
        assert act["prov:startedAtTime"] == ts.isoformat()
        assert act["prov:endedAtTime"] == (ts + timedelta(minutes=5)).isoformat()

    def test_activity_used_includes_dut_and_instruments(self) -> None:
        """prov:used lists the DUT entity and every instrument entity."""
        from ate_cloud.schemas.trace import TraceInstrument, TraceStep

        trace = TestTraceResult(
            dut_serial="SN-001",
            steps=[
                TraceStep(
                    execution_id="exec-1",
                    status="COMPLETED",
                    instruments=[
                        TraceInstrument(instrument_id="osc-1"),
                        TraceInstrument(instrument_id="dm-1"),
                    ],
                    measurements=[],
                ),
            ],
        )

        doc = TestTraceService.to_jsonld(trace)
        act = next(n for n in doc["@graph"] if n.get("@type") == "prov:Activity")
        used_ids = {u["@id"] for u in act["prov:used"]}
        assert "ate:dut/SN-001" in used_ids
        assert "ate:instr/osc-1" in used_ids
        assert "ate:instr/dm-1" in used_ids

    def test_instrument_entity_emitted_once_across_steps(self) -> None:
        """An instrument appearing in two steps is emitted as an Entity once."""
        from ate_cloud.schemas.trace import TraceInstrument, TraceStep

        trace = TestTraceResult(
            dut_serial="SN-001",
            steps=[
                TraceStep(
                    execution_id="exec-1",
                    status="COMPLETED",
                    instruments=[TraceInstrument(instrument_id="osc-1")],
                    measurements=[],
                ),
                TraceStep(
                    execution_id="exec-2",
                    status="COMPLETED",
                    instruments=[TraceInstrument(instrument_id="osc-1")],
                    measurements=[],
                ),
            ],
        )

        doc = TestTraceService.to_jsonld(trace)
        instr_nodes = [
            n for n in doc["@graph"]
            if n.get("@type") == "prov:Entity" and str(n.get("@id", "")).startswith("ate:instr/")
        ]
        assert len(instr_nodes) == 1
        assert instr_nodes[0]["@id"] == "ate:instr/osc-1"


class TestToJsonLdMeasurementEntity:
    """Tests for measurement entities and prov:wasGeneratedBy."""

    def test_measurement_becomes_prov_entity_with_was_generated_by(self) -> None:
        """Each measurement is a prov:Entity linked to its activity."""
        from ate_cloud.schemas.trace import TraceMeasurement, TraceStep

        ts = datetime.now(UTC)
        trace = TestTraceResult(
            dut_serial="SN-001",
            steps=[
                TraceStep(
                    execution_id="exec-1",
                    status="COMPLETED",
                    instruments=[],
                    measurements=[
                        TraceMeasurement(
                            measurement_id="m-1",
                            name="voltage_3v3",
                            value=3.3,
                            unit="V",
                            limits_min=3.2,
                            limits_max=3.4,
                            outcome="PASS",
                            timestamp=ts,
                        ),
                    ],
                ),
            ],
        )

        doc = TestTraceService.to_jsonld(trace)
        meas_nodes = [
            n for n in doc["@graph"]
            if n.get("@type") == "prov:Entity" and str(n.get("@id", "")).startswith("ate:meas/")
        ]
        assert len(meas_nodes) == 1
        m = meas_nodes[0]
        assert m["@id"] == "ate:meas/m-1"
        assert m["ate:measurementName"] == "voltage_3v3"
        assert m["ate:value"] == 3.3
        assert m["ate:unit"] == "V"
        assert m["ate:limitsMin"] == 3.2
        assert m["ate:limitsMax"] == 3.4
        assert m["ate:outcome"] == "PASS"
        assert m["prov:generatedAtTime"] == ts.isoformat()
        assert m["prov:wasGeneratedBy"]["@id"] == "ate:exec/exec-1"

    def test_measurement_with_null_value_omits_value_field(self) -> None:
        """A measurement with value=None omits the ate:value field."""
        from ate_cloud.schemas.trace import TraceMeasurement, TraceStep

        trace = TestTraceResult(
            dut_serial="SN-001",
            steps=[
                TraceStep(
                    execution_id="exec-1",
                    status="COMPLETED",
                    instruments=[],
                    measurements=[
                        TraceMeasurement(
                            measurement_id="m-1",
                            name="binary_check",
                            value=None,
                            unit=None,
                            limits_min=None,
                            limits_max=None,
                            outcome="PASS",
                            timestamp=datetime.now(UTC),
                        ),
                    ],
                ),
            ],
        )

        doc = TestTraceService.to_jsonld(trace)
        m = next(
            n for n in doc["@graph"]
            if n.get("@type") == "prov:Entity" and str(n.get("@id", "")).startswith("ate:meas/")
        )
        assert "ate:value" not in m
        assert "ate:unit" not in m
        assert "ate:limitsMin" not in m
        assert "ate:limitsMax" not in m


# ---------------------------------------------------------------------------
# Integration: build_trace then to_jsonld round-trip.
# ---------------------------------------------------------------------------


class TestBuildTraceToJsonLdRoundTrip:
    """Round-trip: DB -> build_trace -> to_jsonld preserves all data."""

    @pytest.mark.asyncio
    async def test_round_trip_preserves_instruments_and_measurements(
        self, db_session: AsyncSession
    ) -> None:
        """A populated DB projects to a JSON-LD doc with all nodes."""
        ts = datetime.now(UTC)
        db_session.add(_make_execution(
            "exec-1",
            started_at=ts - timedelta(minutes=10),
            completed_at=ts,
            instrument_ids=["osc-001", "dm-001"],
        ))
        db_session.add(_make_measurement(
            "m-1", execution_ref="exec-1", timestamp=ts - timedelta(minutes=5),
        ))
        db_session.add(_make_measurement(
            "m-2", name="current_5v", value=4.98, unit="A",
            execution_ref="exec-1", timestamp=ts - timedelta(minutes=3),
        ))
        await db_session.commit()

        service = TestTraceService(db_session)
        trace = await service.build_trace("SN-001")
        doc = service.to_jsonld(trace)

        graph = doc["@graph"]
        # 1 DUT + 1 activity + 2 instruments + 2 measurements = 6 nodes.
        assert len(graph) == 6
        ids = {n["@id"] for n in graph}
        assert "ate:dut/SN-001" in ids
        assert "ate:exec/exec-1" in ids
        assert "ate:instr/osc-001" in ids
        assert "ate:instr/dm-001" in ids
        assert "ate:meas/m-1" in ids
        assert "ate:meas/m-2" in ids

    @pytest.mark.asyncio
    async def test_round_trip_multiple_executions_chronological(
        self, db_session: AsyncSession
    ) -> None:
        """Multiple executions produce multiple activities in order."""
        base = datetime.now(UTC)
        db_session.add(_make_execution(
            "exec-1", started_at=base, instrument_ids=["osc-001"],
        ))
        db_session.add(_make_execution(
            "exec-2", started_at=base + timedelta(hours=1), instrument_ids=["osc-001"],
        ))
        db_session.add(_make_measurement("m-1", execution_ref="exec-1", timestamp=base))
        db_session.add(_make_measurement("m-2", execution_ref="exec-2", timestamp=base + timedelta(hours=1)))
        await db_session.commit()

        service = TestTraceService(db_session)
        trace = await service.build_trace("SN-001")

        assert len(trace.steps) == 2
        assert trace.steps[0].execution_id == "exec-1"
        assert trace.steps[1].execution_id == "exec-2"

        doc = service.to_jsonld(trace)
        activities = [
            n for n in doc["@graph"] if n.get("@type") == "prov:Activity"
        ]
        assert len(activities) == 2
        # Instrument osc-001 appears in both steps but emitted once.
        instr_nodes = [
            n for n in doc["@graph"]
            if n.get("@type") == "prov:Entity" and str(n.get("@id", "")).startswith("ate:instr/")
        ]
        assert len(instr_nodes) == 1
