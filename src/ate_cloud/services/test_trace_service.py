"""TestTraceService -- rebuild a complete DUT traceability chain (T33).

Given a DUT serial number, the service reconstructs the full chain:

    DUT serial number
      -> executions (ordered by started_at)
        -> station that ran each execution
        -> instruments that participated in each execution
        -> measurements captured for this DUT in each execution
          -> measured value + limits + outcome verdict

Two projections are exposed:

- ``build_trace(serial_number, db) -> TestTraceResult`` - the structured,
  human-facing chain (one ``TraceStep`` per execution).
- ``to_jsonld(trace) -> dict`` - a W3C PROV JSON-LD projection of the same
  chain. Each execution is a ``prov:Activity``, each measurement is a
  ``prov:Entity`` linked to its activity via ``prov:wasGeneratedBy``, and
  each instrument is a ``prov:Entity`` linked to its activity via
  ``prov:used``.

The service is stateless across requests: each call performs fresh
database queries against the provided session.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement
from ate_cloud.schemas.trace import (
    TestTraceResult,
    TraceInstrument,
    TraceMeasurement,
    TraceStep,
)

# W3C PROV JSON-LD context. The ``prov`` namespace is the standard PROV
# vocabulary; ``ate`` is a project-local namespace for domain terms that
# have no direct PROV equivalent (serial number, station, outcome).
_PROV_CONTEXT: dict[str, str | dict[str, str]] = {
    "prov": "http://www.w3.org/ns/prov#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "ate": "https://ate-studio.local/ns#",
}


class TestTraceService:
    """Rebuild the traceability chain for a DUT serial number.

    DUT 测试追溯链构建器 -- 根据序列号重建从 DUT -> 工位 -> 仪器 ->
    测量值 -> 判定结果的完整链路，并提供 W3C PROV JSON-LD 投影。

    The service is stateless across requests: each call performs fresh
    database queries against the provided session. Construct one service
    per request or reuse a single instance - there is no internal state.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the service with a database session.

        Args:
            db: Async SQLAlchemy session used for all queries.
        """
        self._db = db

    async def build_trace(self, serial_number: str) -> TestTraceResult:
        """Rebuild the full trace chain for a DUT serial number.

        Executes two queries:

        1. ``Execution`` rows whose ``dut_serial`` matches, ordered by
           ``started_at`` ascending (NULLS LAST) so the chain reads
           chronologically. Executions without ``dut_serial`` are never
           returned - they carry no traceability data.
        2. ``Measurement`` rows whose ``dut_serial`` matches, ordered by
           ``timestamp`` ascending. Measurements are grouped under the
           execution that produced them via ``execution_ref``.

        Measurements whose ``execution_ref`` does not match any execution
        in the chain (e.g. orphaned rows) are still included - they are
        attached to a synthetic step keyed by their ``execution_ref`` so
        no measurement is silently dropped.

        Args:
            serial_number: The DUT serial number to look up.

        Returns:
            A ``TestTraceResult`` with chronologically ordered steps. When
            no executions and no measurements exist for the serial number,
            the result has an empty ``steps`` list.
        """
        exec_result = await self._db.execute(
            select(Execution)
            .where(Execution.dut_serial == serial_number)
            .order_by(Execution.started_at.asc().nullslast(), Execution.created_at.asc())
        )
        executions = list(exec_result.scalars().all())

        meas_result = await self._db.execute(
            select(Measurement)
            .where(Measurement.dut_serial == serial_number)
            .order_by(Measurement.timestamp.asc())
        )
        measurements = list(meas_result.scalars().all())

        # Index executions by id for O(1) lookup; also collect any
        # execution_ref values from measurements that have no matching
        # execution so they can be surfaced as synthetic steps.
        exec_by_id: dict[str, Execution] = {e.id: e for e in executions}
        known_refs = set(exec_by_id)
        orphan_refs = {
            m.execution_ref
            for m in measurements
            if m.execution_ref is not None and m.execution_ref not in known_refs
        }

        # Build the ordered step list. Executions first (chronological),
        # then synthetic steps for orphan measurement groups (also
        # chronological by their earliest measurement timestamp).
        steps: list[TraceStep] = []
        for execution in executions:
            steps.append(self._step_from_execution(execution, measurements))

        if orphan_refs:
            orphan_steps = [
                self._synthetic_step_for_ref(ref, measurements)
                for ref in orphan_refs
            ]
            orphan_steps.sort(key=lambda s: s.started_at or datetime.max)
            steps.extend(orphan_steps)
            # Re-sort the combined list so synthetic steps land in the
            # correct chronological position.
            steps.sort(key=lambda s: s.started_at or datetime.max)

        return TestTraceResult(dut_serial=serial_number, steps=steps)

    @staticmethod
    def to_jsonld(trace: TestTraceResult) -> dict[str, object]:
        """Project a trace chain into a W3C PROV JSON-LD document.

        The document has the shape::

            {
              "@context": {...},
              "@graph": [
                {"@id": "ate:dut/<serial>", "@type": "prov:Entity", ...},
                {"@id": "ate:exec/<id>", "@type": "prov:Activity",
                 "prov:used": [{"@id": "ate:dut/<serial>"}, ...]},
                {"@id": "ate:instr/<id>", "@type": "prov:Entity", ...},
                {"@id": "ate:meas/<id>", "@type": "prov:Entity",
                 "prov:wasGeneratedBy": {"@id": "ate:exec/<id>"}, ...},
                ...
              ]
            }

        Relations expressed:

        - The DUT is a ``prov:Entity``.
        - Each execution is a ``prov:Activity``. It ``prov:used`` each
          instrument (and the DUT, so the chain root is reachable from
          any activity).
        - Each instrument is a ``prov:Entity`` (emitted once even when
          shared across multiple executions).
        - Each measurement is a ``prov:Entity`` ``prov:wasGeneratedBy``
          its execution.

        Args:
            trace: The structured trace result to project.

        Returns:
            A JSON-LD dict with ``@context`` and ``@graph`` keys. The
            graph always contains the DUT entity; it contains activity /
            instrument / measurement nodes only when the trace has them.
        """
        graph: list[dict[str, object]] = []
        # Track instrument ids already emitted to avoid duplicate Entity
        # nodes when the same instrument appears across multiple steps.
        emitted_instruments: set[str] = set()

        # Root DUT entity.
        dut_id = f"ate:dut/{trace.dut_serial}"
        graph.append({
            "@id": dut_id,
            "@type": "prov:Entity",
            "ate:dutSerial": trace.dut_serial,
        })

        for step in trace.steps:
            activity_id = f"ate:exec/{step.execution_id}"

            # Build the prov:used list: the DUT + every instrument in
            # this step. Instruments are emitted as Entity nodes the
            # first time they are seen.
            used_ids: list[dict[str, str]] = [{"@id": dut_id}]
            for instrument in step.instruments:
                instr_uri = f"ate:instr/{instrument.instrument_id}"
                used_ids.append({"@id": instr_uri})
                if instrument.instrument_id not in emitted_instruments:
                    graph.append({
                        "@id": instr_uri,
                        "@type": "prov:Entity",
                        "ate:instrumentId": instrument.instrument_id,
                    })
                    emitted_instruments.add(instrument.instrument_id)

            # Activity node.
            activity_node: dict[str, object] = {
                "@id": activity_id,
                "@type": "prov:Activity",
                "prov:used": used_ids,
                "ate:status": step.status,
            }
            if step.sequence_id is not None:
                activity_node["ate:sequenceId"] = step.sequence_id
            if step.station_id is not None:
                activity_node["ate:stationId"] = step.station_id
            if step.started_at is not None:
                activity_node["prov:startedAtTime"] = step.started_at.isoformat()
            if step.completed_at is not None:
                activity_node["prov:endedAtTime"] = step.completed_at.isoformat()
            graph.append(activity_node)

            # Measurement entities + prov:wasGeneratedBy relations.
            for measurement in step.measurements:
                meas_id = f"ate:meas/{measurement.measurement_id}"
                meas_node: dict[str, object] = {
                    "@id": meas_id,
                    "@type": "prov:Entity",
                    "ate:measurementName": measurement.name,
                    "ate:outcome": measurement.outcome,
                    "prov:wasGeneratedBy": {"@id": activity_id},
                }
                if measurement.value is not None:
                    meas_node["ate:value"] = measurement.value
                if measurement.unit is not None:
                    meas_node["ate:unit"] = measurement.unit
                if measurement.limits_min is not None:
                    meas_node["ate:limitsMin"] = measurement.limits_min
                if measurement.limits_max is not None:
                    meas_node["ate:limitsMax"] = measurement.limits_max
                if measurement.timestamp is not None:
                    meas_node["prov:generatedAtTime"] = measurement.timestamp.isoformat()
                graph.append(meas_node)

        return {
            "@context": _PROV_CONTEXT,
            "@graph": graph,
        }

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _step_from_execution(
        execution: Execution,
        measurements: list[Measurement],
    ) -> TraceStep:
        """Build a TraceStep from an Execution row + its measurements."""
        instruments = [
            TraceInstrument(instrument_id=inst_id)
            for inst_id in (execution.instrument_ids or [])
        ]
        step_measurements = [
            TraceMeasurement(
                measurement_id=m.measurement_id,
                name=m.name,
                value=m.value,
                unit=m.unit,
                limits_min=m.limits_min,
                limits_max=m.limits_max,
                outcome=m.outcome,
                timestamp=m.timestamp,
            )
            for m in measurements
            if m.execution_ref == execution.id
        ]
        return TraceStep(
            execution_id=execution.id,
            sequence_id=execution.sequence_id,
            station_id=execution.station_id,
            status=execution.status,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            instruments=instruments,
            measurements=step_measurements,
        )

    @staticmethod
    def _synthetic_step_for_ref(
        execution_ref: str,
        measurements: list[Measurement],
    ) -> TraceStep:
        """Build a TraceStep for measurements whose execution_ref has no row.

        Orphan measurements (their execution was deleted, or the
        ``dut_serial`` was backfilled on the measurement but not the
        execution) are surfaced as a synthetic step so no measurement is
        silently dropped from the chain. The step has no station or
        instruments - only the measurements.
        """
        step_measurements = [
            TraceMeasurement(
                measurement_id=m.measurement_id,
                name=m.name,
                value=m.value,
                unit=m.unit,
                limits_min=m.limits_min,
                limits_max=m.limits_max,
                outcome=m.outcome,
                timestamp=m.timestamp,
            )
            for m in measurements
            if m.execution_ref == execution_ref
        ]
        earliest = min(
            (m.timestamp for m in step_measurements),
            default=None,
        )
        return TraceStep(
            execution_id=execution_ref,
            sequence_id=None,
            station_id=None,
            status="UNKNOWN",
            started_at=earliest,
            completed_at=None,
            instruments=[],
            measurements=step_measurements,
        )
