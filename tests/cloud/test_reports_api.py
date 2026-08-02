"""API integration tests for report export endpoints.

Tests cover:
- GET /api/v1/reports/atml/{execution_id} — ATML XML export (200, 404).
- GET /api/v1/reports/{format}/{execution_id} — parameterized format export
  for atml, csv, parquet (200, 404).
- XML well-formedness (parseable by ElementTree).
- CSV structure (header row, data rows).
- Parquet fallback to CSV when pyarrow is unavailable.
- 404 for nonexistent execution.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement


def _insert_execution(
    db: AsyncSession,
    exec_id: str = "exec-test-001",
    status: str = "COMPLETED",
) -> Execution:
    """Insert an Execution record and return it."""
    execution = Execution(
        id=exec_id,
        sequence_id="seq-test-001",
        status=status,
        config={"operator": "test_operator"},
        started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
    )
    db.add(execution)
    return execution


def _insert_measurement(
    db: AsyncSession,
    exec_id: str = "exec-test-001",
    name: str = "voltage_3v3",
    value: float = 3.3,
    outcome: str = "PASS",
    measurement_id: str = "meas-001",
) -> Measurement:
    """Insert a Measurement record and return it."""
    measurement = Measurement(
        measurement_id=measurement_id,
        execution_ref=exec_id,
        station_ref="station-A",
        product_ref="comm_module_v2",
        dut_serial="SN-12345",
        timestamp=datetime(2026, 1, 15, 10, 2, 0, tzinfo=UTC),
        name=name,
        value=value,
        limits_min=3.2,
        limits_max=3.4,
        unit="V",
        outcome=outcome,
    )
    db.add(measurement)
    return measurement


class TestATMLEndpoint:
    """Tests for GET /api/v1/reports/atml/{execution_id}."""

    @pytest.mark.asyncio
    async def test_atml_export_returns_xml(self, db_session, client) -> None:
        """ATML export returns 200 with text/xml content type."""
        _insert_execution(db_session)
        _insert_measurement(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/atml/exec-test-001")

        assert response.status_code == 200
        assert "xml" in response.headers["content-type"].lower()
        assert response.content  # non-empty body

    @pytest.mark.asyncio
    async def test_atml_export_xml_well_formed(self, db_session, client) -> None:
        """ATML export produces well-formed XML."""
        _insert_execution(db_session)
        _insert_measurement(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/atml/exec-test-001")
        xml_text = response.text
        ET.fromstring(xml_text)  # raises ParseError if malformed

    @pytest.mark.asyncio
    async def test_atml_export_has_test_results_root(self, db_session, client) -> None:
        """ATML XML root element is TestResults."""
        _insert_execution(db_session)
        _insert_measurement(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/atml/exec-test-001")
        root = ET.fromstring(response.text)
        assert root.tag.endswith("TestResults")

    @pytest.mark.asyncio
    async def test_atml_export_404_for_missing_execution(self, db_session, client) -> None:
        """ATML export returns 404 for nonexistent execution."""
        response = await client.get("/api/v1/reports/atml/nonexistent-id")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_atml_export_empty_measurements(self, db_session, client) -> None:
        """ATML export with no measurements still returns valid XML."""
        _insert_execution(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/atml/exec-test-001")
        assert response.status_code == 200
        root = ET.fromstring(response.text)
        # TestSteps should exist but be empty
        steps_found = False
        for child in root:
            if "TestSteps" in child.tag:
                steps_found = True
                break
        assert steps_found


class TestParameterizedFormat:
    """Tests for GET /api/v1/reports/{format}/{execution_id}."""

    @pytest.mark.asyncio
    async def test_csv_export(self, db_session, client) -> None:
        """CSV export returns 200 with text/csv content type."""
        _insert_execution(db_session)
        _insert_measurement(db_session, name="voltage_3v3", value=3.3)
        _insert_measurement(
            db_session,
            name="current_5v",
            value=0.5,
            measurement_id="meas-002",
        )
        await db_session.flush()

        response = await client.get("/api/v1/reports/csv/exec-test-001")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        # CSV should have header + 2 data rows
        lines = response.text.strip().split("\n")
        assert len(lines) >= 3
        assert "measurement_id" in lines[0]

    @pytest.mark.asyncio
    async def test_csv_export_empty_measurements(self, db_session, client) -> None:
        """CSV export with no measurements returns header-only CSV."""
        _insert_execution(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/csv/exec-test-001")
        assert response.status_code == 200
        lines = response.text.strip().split("\n")
        assert len(lines) == 1  # header only
        assert "measurement_id" in lines[0]

    @pytest.mark.asyncio
    async def test_atml_via_parameterized_endpoint(self, db_session, client) -> None:
        """ATML format works via parameterized endpoint."""
        _insert_execution(db_session)
        _insert_measurement(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/atml/exec-test-001")
        assert response.status_code == 200
        assert "xml" in response.headers["content-type"].lower()
        ET.fromstring(response.text)

    @pytest.mark.asyncio
    async def test_parquet_export_or_csv_fallback(self, db_session, client) -> None:
        """Parquet export returns 200 (parquet or CSV fallback)."""
        _insert_execution(db_session)
        _insert_measurement(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/parquet/exec-test-001")
        assert response.status_code == 200
        ct = response.headers["content-type"]
        # Either parquet or CSV fallback — both are valid. Content-type may
        # include charset suffix (e.g. "text/csv; charset=utf-8").
        assert ct.startswith("application/vnd.apache.parquet") or ct.startswith("text/csv")

    @pytest.mark.asyncio
    async def test_csv_404_for_missing_execution(self, db_session, client) -> None:
        """CSV export returns 404 for nonexistent execution."""
        response = await client.get("/api/v1/reports/csv/nonexistent-id")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_parquet_404_for_missing_execution(self, db_session, client) -> None:
        """Parquet export returns 404 for nonexistent execution."""
        response = await client.get("/api/v1/reports/parquet/nonexistent-id")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_atml_via_param_404_for_missing_execution(
        self, db_session, client
    ) -> None:
        """ATML via parameterized endpoint returns 404 for nonexistent execution."""
        response = await client.get("/api/v1/reports/atml/nonexistent-id")
        assert response.status_code == 404


class TestCSVContent:
    """Tests for CSV export content correctness."""

    @pytest.mark.asyncio
    async def test_csv_contains_all_columns(self, db_session, client) -> None:
        """CSV header row contains all expected columns."""
        _insert_execution(db_session)
        _insert_measurement(db_session)
        await db_session.flush()

        response = await client.get("/api/v1/reports/csv/exec-test-001")
        header = response.text.strip().split("\n")[0]
        expected_cols = [
            "measurement_id",
            "execution_ref",
            "station_ref",
            "product_ref",
            "dut_serial",
            "timestamp",
            "name",
            "value",
            "limits_min",
            "limits_max",
            "unit",
            "outcome",
        ]
        for col in expected_cols:
            assert col in header

    @pytest.mark.asyncio
    async def test_csv_data_row_values(self, db_session, client) -> None:
        """CSV data rows contain the measurement values."""
        _insert_execution(db_session)
        _insert_measurement(
            db_session,
            name="voltage_5v",
            value=5.0,
            outcome="PASS",
            measurement_id="meas-v5",
        )
        await db_session.flush()

        response = await client.get("/api/v1/reports/csv/exec-test-001")
        lines = response.text.strip().split("\n")
        assert len(lines) == 2  # header + 1 data row
        data_row = lines[1]
        assert "meas-v5" in data_row
        assert "voltage_5v" in data_row
        assert "5.0" in data_row
        assert "PASS" in data_row
