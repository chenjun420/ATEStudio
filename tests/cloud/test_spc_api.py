"""API integration tests for SPC endpoints.

Covers:
- GET /api/v1/spc/{product_type}/{measurement_name} - statistics
- GET /api/v1/spc/{product_type}/{measurement_name}/chart - X-bar/R chart
- GET /api/v1/spc/alerts - recent alerts from streaming processor

Uses the real SQLite in-memory DB from tests/cloud/conftest.py and inserts
Measurement ORM rows directly. The statistics/chart endpoints build a
per-request SPCProcessor seeded from the DB; the alerts endpoint reads
from app.state.spc_processor when one is attached.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ate_cloud.models.measurement import Measurement
from ate_cloud.services.spc import SPCProcessor


def _make_measurement(
    *,
    product_ref: str = "comm_module",
    name: str = "tx_power",
    value: float | None = -5.0,
    limits_min: float | None = -10.0,
    limits_max: float | None = 10.0,
    timestamp: datetime | None = None,
    dut_serial: str | None = None,
) -> Measurement:
    """Build a Measurement ORM row (not yet persisted)."""
    if timestamp is None:
        timestamp = datetime.now(UTC)
    if dut_serial is None:
        dut_serial = f"DUT-{uuid.uuid4().hex[:8]}"
    return Measurement(
        measurement_id=str(uuid.uuid4()),
        execution_ref=None,
        station_ref="station-test",
        product_ref=product_ref,
        dut_serial=dut_serial,
        timestamp=timestamp,
        name=name,
        value=value,
        limits_min=limits_min,
        limits_max=limits_max,
        unit="dBm",
        outcome="PASS",
    )


async def _insert_measurements(
    db_session: AsyncSession,
    count: int,
    *,
    product_ref: str = "comm_module",
    name: str = "tx_power",
    value_fn=lambda i: -5.0 + (i % 5) * 0.1,
    limits_min: float | None = -10.0,
    limits_max: float | None = 10.0,
    start_ts: datetime | None = None,
) -> list[Measurement]:
    """Persist ``count`` Measurement rows and return them."""
    if start_ts is None:
        start_ts = datetime.now(UTC) - timedelta(seconds=count)
    rows = [
        _make_measurement(
            product_ref=product_ref,
            name=name,
            value=value_fn(i),
            limits_min=limits_min,
            limits_max=limits_max,
            timestamp=start_ts + timedelta(seconds=i),
        )
        for i in range(count)
    ]
    db_session.add_all(rows)
    await db_session.flush()
    return rows


# ---------------------------------------------------------------------------
# GET /api/v1/spc/{product_type}/{measurement_name}
# ---------------------------------------------------------------------------


class TestGetSpcStatistics:
    """Tests for GET /api/v1/spc/{product_type}/{measurement_name}."""

    @pytest.mark.asyncio
    async def test_no_data_returns_sample_count_zero(
        self, client: AsyncClient
    ) -> None:
        """Stream with no measurements returns sample_count=0."""
        response = await client.get("/api/v1/spc/comm_module/tx_power")

        assert response.status_code == 200
        data = response.json()
        assert data["product_type"] == "comm_module"
        assert data["measurement_name"] == "tx_power"
        assert data["sample_count"] == 0
        assert data["mean"] is None
        assert data["cp"] is None
        assert data["cpk"] is None
        assert data["ppk"] is None

    @pytest.mark.asyncio
    async def test_with_measurements_returns_statistics(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Stream with measurements returns populated statistics."""
        await _insert_measurements(
            db_session,
            10,
            product_ref="comm_module",
            name="tx_power",
            value_fn=lambda i: -5.0 + (i % 5) * 0.1,
            limits_min=-10.0,
            limits_max=10.0,
        )

        response = await client.get("/api/v1/spc/comm_module/tx_power")

        assert response.status_code == 200
        data = response.json()
        assert data["sample_count"] == 10
        assert data["mean"] is not None
        assert data["std_dev_overall"] is not None
        assert data["cp"] is not None
        assert data["cpk"] is not None
        assert data["ppk"] is not None
        assert data["usl"] == 10.0
        assert data["lsl"] == -10.0

    @pytest.mark.asyncio
    async def test_filters_by_product_and_name(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Only measurements matching product+name are loaded."""
        await _insert_measurements(
            db_session, 5, product_ref="prod_a", name="voltage"
        )
        await _insert_measurements(
            db_session, 3, product_ref="prod_b", name="voltage"
        )
        await _insert_measurements(
            db_session, 2, product_ref="prod_a", name="current"
        )

        response = await client.get("/api/v1/spc/prod_a/voltage")
        assert response.status_code == 200
        assert response.json()["sample_count"] == 5

        response = await client.get("/api/v1/spc/prod_b/voltage")
        assert response.status_code == 200
        assert response.json()["sample_count"] == 3

    @pytest.mark.asyncio
    async def test_null_values_excluded(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Measurements with value=NULL are excluded from SPC computation."""
        # 3 valid + 2 null
        await _insert_measurements(
            db_session, 3, product_ref="p1", name="v"
        )
        null_rows = [
            _make_measurement(
                product_ref="p1",
                name="v",
                value=None,
                timestamp=datetime.now(UTC),
            )
            for _ in range(2)
        ]
        db_session.add_all(null_rows)
        await db_session.flush()

        response = await client.get("/api/v1/spc/p1/v")
        assert response.status_code == 200
        assert response.json()["sample_count"] == 3

    @pytest.mark.asyncio
    async def test_limit_query_parameter(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """limit query parameter caps the number of loaded measurements."""
        await _insert_measurements(db_session, 20, product_ref="p1", name="v")

        response = await client.get("/api/v1/spc/p1/v?limit=5")
        assert response.status_code == 200
        assert response.json()["sample_count"] == 5

    @pytest.mark.asyncio
    async def test_limit_below_minimum_rejected(
        self, client: AsyncClient
    ) -> None:
        """limit < 2 is rejected with 422."""
        response = await client.get("/api/v1/spc/p1/v?limit=1")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_limit_above_maximum_rejected(
        self, client: AsyncClient
    ) -> None:
        """limit > 500 is rejected with 422."""
        response = await client.get("/api/v1/spc/p1/v?limit=501")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/spc/{product_type}/{measurement_name}/chart
# ---------------------------------------------------------------------------


class TestGetSpcChart:
    """Tests for GET /api/v1/spc/{product_type}/{measurement_name}/chart."""

    @pytest.mark.asyncio
    async def test_no_data_returns_empty_chart(
        self, client: AsyncClient
    ) -> None:
        """Stream with no data returns empty chart (all limits None)."""
        response = await client.get("/api/v1/spc/comm_module/tx_power/chart")

        assert response.status_code == 200
        data = response.json()
        assert data["product_type"] == "comm_module"
        assert data["measurement_name"] == "tx_power"
        assert data["center_line"] is None
        assert data["ucl"] is None
        assert data["lcl"] is None
        assert data["r_center"] is None
        assert data["r_ucl"] is None
        assert data["r_lcl"] is None
        assert data["subgroups"] == []
        assert data["subgroup_size"] == 5

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_empty_chart(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Fewer than subgroup_size (5) samples returns empty chart."""
        await _insert_measurements(db_session, 3, product_ref="p1", name="v")

        response = await client.get("/api/v1/spc/p1/v/chart")
        assert response.status_code == 200
        data = response.json()
        assert data["center_line"] is None
        assert data["subgroups"] == []

    @pytest.mark.asyncio
    async def test_with_complete_subgroups_returns_chart(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Enough data for one complete subgroup returns populated chart."""
        await _insert_measurements(
            db_session,
            5,
            product_ref="p1",
            name="v",
            value_fn=lambda i: 10.0 + i * 0.5,
        )

        response = await client.get("/api/v1/spc/p1/v/chart")
        assert response.status_code == 200
        data = response.json()
        assert data["center_line"] is not None
        assert data["ucl"] is not None
        assert data["lcl"] is not None
        assert data["r_center"] is not None
        assert len(data["subgroups"]) == 1
        assert data["subgroups"][0]["sample_count"] == 5

    @pytest.mark.asyncio
    async def test_multiple_subgroups(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """15 measurements yield 3 complete subgroups."""
        await _insert_measurements(
            db_session,
            15,
            product_ref="p1",
            name="v",
            value_fn=lambda i: 10.0 + (i % 5) * 0.5,
        )

        response = await client.get("/api/v1/spc/p1/v/chart")
        assert response.status_code == 200
        data = response.json()
        assert len(data["subgroups"]) == 3
        for sg in data["subgroups"]:
            assert sg["sample_count"] == 5
            assert "mean" in sg
            assert "range" in sg

    @pytest.mark.asyncio
    async def test_chart_limit_query_parameter(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """limit parameter on chart endpoint caps loaded measurements."""
        await _insert_measurements(db_session, 20, product_ref="p1", name="v")

        response = await client.get("/api/v1/spc/p1/v/chart?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data["subgroups"]) == 1  # 5 samples = 1 subgroup

    @pytest.mark.asyncio
    async def test_chart_limit_below_minimum_rejected(
        self, client: AsyncClient
    ) -> None:
        """Chart endpoint limit < 5 is rejected with 422."""
        response = await client.get("/api/v1/spc/p1/v/chart?limit=4")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/spc/alerts
# ---------------------------------------------------------------------------


class TestGetSpcAlerts:
    """Tests for GET /api/v1/spc/alerts."""

    @pytest.mark.asyncio
    async def test_no_processor_returns_empty_list(
        self, client: AsyncClient
    ) -> None:
        """When app.state.spc_processor is not set, returns [] (200)."""
        response = await client.get("/api/v1/spc/alerts")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_processor_attached_returns_alerts(
        self, app, client: AsyncClient
    ) -> None:
        """When app.state.spc_processor is attached, its alerts are returned."""
        proc = SPCProcessor()
        # Trigger a Ppk alert by feeding a poor-process measurement.
        # Use a stub object compatible with process_measurement's duck typing.
        from datetime import UTC, datetime
        from types import SimpleNamespace

        m1 = SimpleNamespace(
            value=5.0,
            limits_min=9.9,
            limits_max=10.1,
            product_ref="prod_alert_test",
            name="v",
            timestamp=datetime.now(UTC),
        )
        m2 = SimpleNamespace(
            value=15.0,
            limits_min=9.9,
            limits_max=10.1,
            product_ref="prod_alert_test",
            name="v",
            timestamp=datetime.now(UTC),
        )
        proc.process_measurement(m1)
        proc.process_measurement(m2)

        app.state.spc_processor = proc
        try:
            response = await client.get("/api/v1/spc/alerts")
            assert response.status_code == 200
            data = response.json()
            assert len(data) >= 1
            alert = data[0]
            assert alert["severity"] in {"critical", "warning"}
            assert "rule" in alert
            assert "message" in alert
            assert "timestamp" in alert
            assert "product_type" in alert
            assert "measurement_name" in alert
            assert "sample_count" in alert
        finally:
            # Clean up so other tests don't see the attached processor.
            if hasattr(app.state, "spc_processor"):
                del app.state.spc_processor

    @pytest.mark.asyncio
    async def test_alerts_limit_query_parameter(
        self, app, client: AsyncClient
    ) -> None:
        """limit query parameter caps the number of returned alerts."""
        proc = SPCProcessor()
        from datetime import UTC, datetime
        from types import SimpleNamespace

        # Generate many alerts
        for i in range(20):
            m = SimpleNamespace(
                value=float(i * 100),
                limits_min=45.0,
                limits_max=55.0,
                product_ref="p",
                name="v",
                timestamp=datetime.now(UTC),
            )
            proc.process_measurement(m)

        app.state.spc_processor = proc
        try:
            response = await client.get("/api/v1/spc/alerts?limit=3")
            assert response.status_code == 200
            data = response.json()
            assert len(data) <= 3
        finally:
            if hasattr(app.state, "spc_processor"):
                del app.state.spc_processor

    @pytest.mark.asyncio
    async def test_alerts_limit_below_minimum_rejected(
        self, client: AsyncClient
    ) -> None:
        """limit < 1 is rejected with 422."""
        response = await client.get("/api/v1/spc/alerts?limit=0")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_alerts_limit_above_maximum_rejected(
        self, client: AsyncClient
    ) -> None:
        """limit > 500 is rejected with 422."""
        response = await client.get("/api/v1/spc/alerts?limit=501")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_non_spc_processor_attached_returns_500(
        self, app, client: AsyncClient
    ) -> None:
        """When app.state.spc_processor is set but not an SPCProcessor, 500."""
        app.state.spc_processor = "not a processor"
        try:
            response = await client.get("/api/v1/spc/alerts")
            assert response.status_code == 500
        finally:
            if hasattr(app.state, "spc_processor"):
                del app.state.spc_processor
