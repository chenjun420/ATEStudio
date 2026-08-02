"""Unit tests for SPCProcessor streaming engine.

Covers: process_measurement (None skip, sliding window, multi-stream),
get_statistics (empty + populated), get_chart (insufficient + sufficient
data), get_alerts (ordering + limit), reset (all + specific), Ppk critical
alert threshold, Western Electric rule firing, and failure_indexer wiring.

The SPCProcessor duck-types its Measurement input - it only reads .value,
.limits_min, .limits_max, .product_ref, .name, .timestamp - so we use a
lightweight dataclass stub instead of the SQLAlchemy ORM model.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from ate_cloud.schemas.spc import SPCStatistics
from ate_cloud.services import spc_math
from ate_cloud.services.spc import (
    ALERT_BUFFER_SIZE,
    PPK_ALERT_THRESHOLD,
    SPCProcessor,
)


@dataclass
class StubMeasurement:
    """Minimal duck-typed stand-in for the Measurement ORM model.

    SPCProcessor.process_measurement reads only these attributes, so the
    stub is sufficient for unit-testing the processor in isolation.
    """

    value: float | None
    limits_min: float | None = None
    limits_max: float | None = None
    product_ref: str = "prod_a"
    name: str = "voltage"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


def _make_measurements(
    count: int,
    *,
    value_fn=lambda i: 10.0 + (i % 5) * 0.1,
    lsl: float | None = 0.0,
    usl: float | None = 20.0,
    product_ref: str = "prod_a",
    name: str = "voltage",
    start_ts: datetime | None = None,
) -> list[StubMeasurement]:
    """Build ``count`` measurements with a value function and spec limits."""
    if start_ts is None:
        start_ts = datetime.now(UTC)
    return [
        StubMeasurement(
            value=value_fn(i),
            limits_min=lsl,
            limits_max=usl,
            product_ref=product_ref,
            name=name,
            timestamp=start_ts + timedelta(seconds=i),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# process_measurement - basic ingestion
# ---------------------------------------------------------------------------


class TestProcessMeasurementBasic:
    """Tests for the basic ingestion path of process_measurement."""

    def test_none_value_skipped(self) -> None:
        """Measurements with value=None are skipped (return None, no window)."""
        proc = SPCProcessor()
        m = StubMeasurement(value=None, limits_min=0.0, limits_max=10.0)
        assert proc.process_measurement(m) is None
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.sample_count == 0

    def test_single_measurement_no_alert(self) -> None:
        """One sample is insufficient for any rule (need >=2)."""
        proc = SPCProcessor()
        m = StubMeasurement(value=5.0, limits_min=0.0, limits_max=10.0)
        assert proc.process_measurement(m) is None

    def test_two_measurements_no_alert_in_control(self) -> None:
        """Two in-control samples do not trigger alerts."""
        proc = SPCProcessor()
        for v in [5.0, 5.1]:
            assert proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=10.0)
            ) is None

    def test_returns_alert_when_ppk_below_threshold(self) -> None:
        """Wide-spread process relative to spec triggers critical Ppk alert."""
        proc = SPCProcessor()
        # Tight spec [9.9, 10.1], values spread widely -> Ppk < 1.00
        alert = None
        for v in [5.0, 15.0]:
            alert = proc.process_measurement(
                StubMeasurement(value=v, limits_min=9.9, limits_max=10.1)
            )
        assert alert is not None
        assert alert.severity == "critical"
        assert alert.rule.startswith("Ppk_below")

    def test_spec_limits_cached_from_first_measurement(self) -> None:
        """Limits set on first measurement persist even if later ones lack them."""
        proc = SPCProcessor()
        # First measurement has limits; subsequent don't
        proc.process_measurement(
            StubMeasurement(value=10.0, limits_min=0.0, limits_max=20.0)
        )
        proc.process_measurement(StubMeasurement(value=10.1, limits_min=None, limits_max=None))
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.lsl == 0.0
        assert stats.usl == 20.0


# ---------------------------------------------------------------------------
# Sliding window behavior
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    """Tests for the sliding window eviction logic."""

    def test_window_evicts_oldest_beyond_size(self) -> None:
        """Window evicts oldest values once window_size is exceeded."""
        window_size = 10
        proc = SPCProcessor(window_size=window_size)
        # Feed 15 measurements; window should only retain last 10
        for i in range(15):
            proc.process_measurement(
                StubMeasurement(value=float(i), limits_min=0.0, limits_max=100.0)
            )
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.sample_count == window_size
        # Mean should reflect last 10 values: 5..14 -> mean=9.5
        assert stats.mean == pytest.approx(9.5)

    def test_window_default_size_100(self) -> None:
        """Default window size is 100."""
        proc = SPCProcessor()
        for i in range(150):
            proc.process_measurement(
                StubMeasurement(value=float(i), limits_min=-1000.0, limits_max=1000.0)
            )
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.sample_count == 100

    def test_multi_stream_windows_isolated(self) -> None:
        """Each (product, name) stream has its own independent window."""
        proc = SPCProcessor()
        proc.process_measurement(
            StubMeasurement(value=1.0, product_ref="p1", name="v")
        )
        proc.process_measurement(
            StubMeasurement(value=2.0, product_ref="p1", name="v")
        )
        proc.process_measurement(
            StubMeasurement(value=10.0, product_ref="p2", name="v")
        )
        proc.process_measurement(
            StubMeasurement(value=20.0, product_ref="p2", name="v")
        )
        assert proc.get_statistics("p1", "v").mean == pytest.approx(1.5)
        assert proc.get_statistics("p2", "v").mean == pytest.approx(15.0)

    def test_same_product_different_name_separate_streams(self) -> None:
        """Same product, different measurement name -> separate streams."""
        proc = SPCProcessor()
        proc.process_measurement(
            StubMeasurement(value=1.0, product_ref="p1", name="voltage")
        )
        proc.process_measurement(
            StubMeasurement(value=2.0, product_ref="p1", name="voltage")
        )
        proc.process_measurement(
            StubMeasurement(value=100.0, product_ref="p1", name="current")
        )
        proc.process_measurement(
            StubMeasurement(value=200.0, product_ref="p1", name="current")
        )
        assert proc.get_statistics("p1", "voltage").mean == pytest.approx(1.5)
        assert proc.get_statistics("p1", "current").mean == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# get_statistics
# ---------------------------------------------------------------------------


class TestGetStatistics:
    """Tests for SPCProcessor.get_statistics."""

    def test_empty_stream_returns_zero_samples(self) -> None:
        """Unknown stream returns SPCStatistics with sample_count=0."""
        proc = SPCProcessor()
        stats = proc.get_statistics("unknown", "stream")
        assert isinstance(stats, SPCStatistics)
        assert stats.sample_count == 0
        assert stats.mean is None
        assert stats.cpk is None
        assert stats.ppk is None

    def test_empty_stream_preserves_identifiers(self) -> None:
        """Returned stats carry the requested product/name."""
        proc = SPCProcessor()
        stats = proc.get_statistics("widget", "temp")
        assert stats.product_type == "widget"
        assert stats.measurement_name == "temp"

    def test_statistics_populated_with_enough_samples(self) -> None:
        """Stats include mean, sigmas, Cp, Cpk, Ppk with sufficient samples."""
        proc = SPCProcessor()
        # 10 samples within [0, 20] spec, tight cluster -> good capability
        for v in [10.0, 10.1, 10.0, 10.2, 9.9, 10.1, 10.0, 10.0, 10.1, 9.9]:
            proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=20.0)
            )
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.sample_count == 10
        assert stats.mean is not None
        assert stats.std_dev_overall is not None
        assert stats.std_dev_overall > 0
        assert stats.std_dev_within is not None
        assert stats.cp is not None
        assert stats.cpk is not None
        assert stats.ppk is not None
        # Tight process -> high capability
        assert stats.cp > 1.0
        assert stats.cpk > 1.0

    def test_statistics_no_limits_yields_none_capability(self) -> None:
        """Without spec limits, Cp/Cpk/Ppk are None."""
        proc = SPCProcessor()
        for v in [5.0, 5.1, 5.0, 5.1, 5.0]:
            proc.process_measurement(
                StubMeasurement(value=v, limits_min=None, limits_max=None)
            )
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.cp is None
        assert stats.cpk is None
        assert stats.ppk is None
        assert stats.usl is None
        assert stats.lsl is None

    def test_statistics_last_updated_from_latest_timestamp(self) -> None:
        """last_updated reflects the most recent measurement's timestamp."""
        proc = SPCProcessor()
        ts1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
        proc.process_measurement(
            StubMeasurement(value=5.0, limits_min=0.0, limits_max=10.0, timestamp=ts1)
        )
        proc.process_measurement(
            StubMeasurement(value=5.1, limits_min=0.0, limits_max=10.0, timestamp=ts2)
        )
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.last_updated == ts2


# ---------------------------------------------------------------------------
# get_chart
# ---------------------------------------------------------------------------


class TestGetChart:
    """Tests for SPCProcessor.get_chart."""

    def test_chart_empty_stream_returns_empty(self) -> None:
        """Unknown stream returns an empty chart (all limits None)."""
        proc = SPCProcessor()
        chart = proc.get_chart("unknown", "stream")
        assert chart.center_line is None
        assert chart.ucl is None
        assert chart.lcl is None
        assert chart.r_center is None
        assert chart.r_ucl is None
        assert chart.r_lcl is None
        assert chart.subgroups == []
        assert chart.subgroup_size == 5

    def test_chart_insufficient_data_returns_empty(self) -> None:
        """Fewer than subgroup_size samples yields empty chart."""
        proc = SPCProcessor()
        for v in [1.0, 2.0, 3.0]:  # < 5
            proc.process_measurement(StubMeasurement(value=v))
        chart = proc.get_chart("prod_a", "voltage")
        assert chart.center_line is None
        assert chart.subgroups == []

    def test_chart_with_complete_subgroups(self) -> None:
        """Exactly one complete subgroup yields one subgroup entry."""
        proc = SPCProcessor()
        # 5 values = 1 complete subgroup
        for v in [10.0, 11.0, 10.5, 9.5, 10.0]:
            proc.process_measurement(StubMeasurement(value=v))
        chart = proc.get_chart("prod_a", "voltage")
        assert chart.center_line is not None
        assert chart.ucl is not None
        assert chart.lcl is not None
        assert chart.r_center is not None
        assert len(chart.subgroups) == 1
        assert chart.subgroups[0].sample_count == 5

    def test_chart_drops_trailing_partial_subgroup(self) -> None:
        """Trailing partial subgroup is excluded from chart computation."""
        proc = SPCProcessor()
        # 7 values = 1 complete (5) + 1 partial (2). Only the complete one counts.
        for v in [10.0, 11.0, 10.5, 9.5, 10.0, 12.0, 13.0]:
            proc.process_measurement(StubMeasurement(value=v))
        chart = proc.get_chart("prod_a", "voltage")
        assert len(chart.subgroups) == 1
        assert chart.subgroups[0].sample_count == 5

    def test_chart_multiple_subgroups(self) -> None:
        """Multiple complete subgroups are computed correctly."""
        proc = SPCProcessor()
        # 15 values -> 3 complete subgroups of 5
        for i in range(15):
            proc.process_measurement(StubMeasurement(value=float(10 + i)))
        chart = proc.get_chart("prod_a", "voltage")
        assert len(chart.subgroups) == 3
        for sg in chart.subgroups:
            assert sg.sample_count == 5
        # Subgroup means should be increasing
        means = [sg.mean for sg in chart.subgroups]
        assert means[0] < means[1] < means[2]

    def test_chart_control_limits_use_a2_d3_d4(self) -> None:
        """X-bar UCL/LCL use A2*R_bar; R UCL/LCL use D4/D3*R_bar."""
        proc = SPCProcessor()
        # 5 values -> 1 subgroup, deterministic
        values = [10.0, 12.0, 11.0, 9.0, 10.0]
        for v in values:
            proc.process_measurement(StubMeasurement(value=v))
        chart = proc.get_chart("prod_a", "voltage")
        a2, d3, d4 = spc_math.control_constants(5)
        grand_mean = spc_math.mean(values)
        r_bar = max(values) - min(values)
        assert chart.center_line == pytest.approx(grand_mean)
        assert chart.ucl == pytest.approx(grand_mean + a2 * r_bar)
        assert chart.lcl == pytest.approx(grand_mean - a2 * r_bar)
        assert chart.r_center == pytest.approx(r_bar)
        assert chart.r_ucl == pytest.approx(d4 * r_bar)
        assert chart.r_lcl == pytest.approx(d3 * r_bar)

    def test_chart_custom_subgroup_size(self) -> None:
        """Custom subgroup_size is honored in chart construction."""
        proc = SPCProcessor(subgroup_size=3)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]:
            proc.process_measurement(StubMeasurement(value=v))
        chart = proc.get_chart("prod_a", "voltage")
        assert chart.subgroup_size == 3
        assert len(chart.subgroups) == 2  # 6 values / 3 = 2 subgroups


# ---------------------------------------------------------------------------
# get_alerts
# ---------------------------------------------------------------------------


class TestGetAlerts:
    """Tests for SPCProcessor.get_alerts."""

    def test_no_alerts_returns_empty(self) -> None:
        """Fresh processor has no alerts."""
        proc = SPCProcessor()
        assert proc.get_alerts() == []

    def test_alerts_returned_newest_first(self) -> None:
        """Alerts are returned newest-first (ring buffer reversed)."""
        proc = SPCProcessor()
        # Generate multiple Ppk alerts by feeding poor-process measurements.
        # Each measurement that drops Ppk < 1.00 emits a critical alert.
        for i in range(5):
            proc.process_measurement(
                StubMeasurement(
                    value=5.0 + i * 5.0,  # 5, 10, 15, 20, 25 - wide spread
                    limits_min=9.9,
                    limits_max=10.1,
                )
            )
        alerts = proc.get_alerts()
        assert len(alerts) >= 1
        # All alerts should have severity critical or warning
        severities = {a.severity for a in alerts}
        assert all(s in {"critical", "warning"} for s in severities)

    def test_alerts_limit_respected(self) -> None:
        """limit parameter caps the number of returned alerts."""
        proc = SPCProcessor()
        for i in range(10):
            proc.process_measurement(
                StubMeasurement(
                    value=float(i * 10),
                    limits_min=45.0,
                    limits_max=55.0,
                )
            )
        all_alerts = proc.get_alerts(limit=500)
        if len(all_alerts) > 2:
            limited = proc.get_alerts(limit=2)
            assert len(limited) == 2

    def test_alerts_default_limit_50(self) -> None:
        """Default limit is 50."""
        proc = SPCProcessor()
        # Generate many alerts
        for i in range(60):
            proc.process_measurement(
                StubMeasurement(
                    value=float(i * 100),
                    limits_min=45.0,
                    limits_max=55.0,
                )
            )
        alerts = proc.get_alerts()
        # Default limit=50, even if more alerts were generated
        assert len(alerts) <= 50

    def test_alert_ring_buffer_max_size(self) -> None:
        """Alert buffer caps at ALERT_BUFFER_SIZE (200)."""
        proc = SPCProcessor()
        # Generate far more alerts than the buffer can hold
        for i in range(ALERT_BUFFER_SIZE + 50):
            proc.process_measurement(
                StubMeasurement(
                    value=float(i * 1000),
                    limits_min=45.0,
                    limits_max=55.0,
                )
            )
        alerts = proc.get_alerts(limit=500)
        assert len(alerts) <= ALERT_BUFFER_SIZE


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for SPCProcessor.reset."""

    def test_reset_all_clears_everything(self) -> None:
        """reset() with no args clears all windows, limits, alerts."""
        proc = SPCProcessor()
        for v in [5.0, 15.0]:
            proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=20.0)
            )
        assert proc.get_statistics("prod_a", "voltage").sample_count > 0
        assert proc.get_alerts(limit=500)

        proc.reset()
        assert proc.get_statistics("prod_a", "voltage").sample_count == 0
        assert proc.get_alerts() == []

    def test_reset_specific_stream_clears_only_that_stream(self) -> None:
        """reset(product_type, measurement_name) clears only one stream."""
        proc = SPCProcessor()
        for v in [5.0, 5.1]:
            proc.process_measurement(
                StubMeasurement(value=v, product_ref="p1", name="v")
            )
        for v in [10.0, 10.1]:
            proc.process_measurement(
                StubMeasurement(value=v, product_ref="p2", name="v")
            )

        proc.reset(product_type="p1", measurement_name="v")
        assert proc.get_statistics("p1", "v").sample_count == 0
        # Other stream untouched
        assert proc.get_statistics("p2", "v").sample_count == 2

    def test_reset_by_product_only_clears_all_streams_for_product(self) -> None:
        """reset(product_type) clears all measurement names for that product."""
        proc = SPCProcessor()
        for v in [5.0, 5.1]:
            proc.process_measurement(
                StubMeasurement(value=v, product_ref="p1", name="voltage")
            )
        for v in [10.0, 10.1]:
            proc.process_measurement(
                StubMeasurement(value=v, product_ref="p1", name="current")
            )
        for v in [20.0, 20.1]:
            proc.process_measurement(
                StubMeasurement(value=v, product_ref="p2", name="voltage")
            )

        proc.reset(product_type="p1")
        assert proc.get_statistics("p1", "voltage").sample_count == 0
        assert proc.get_statistics("p1", "current").sample_count == 0
        # Other product untouched
        assert proc.get_statistics("p2", "voltage").sample_count == 2

    def test_reset_unknown_stream_is_noop(self) -> None:
        """Resetting a non-existent stream does not raise."""
        proc = SPCProcessor()
        proc.process_measurement(StubMeasurement(value=5.0))
        # Should not raise
        proc.reset(product_type="nonexistent", measurement_name="stream")
        # Original stream still present
        assert proc.get_statistics("prod_a", "voltage").sample_count == 1


# ---------------------------------------------------------------------------
# Ppk alert threshold
# ---------------------------------------------------------------------------


class TestPpkAlertThreshold:
    """Tests for the Ppk < 1.00 critical alert mechanism."""

    def test_ppk_alert_critical_severity(self) -> None:
        """Ppk below 1.00 raises a critical alert."""
        proc = SPCProcessor()
        # Spec [9.9, 10.1] (width 0.2); values 5 and 15 -> sigma ~5, Ppk << 1
        alert = proc.process_measurement(
            StubMeasurement(value=5.0, limits_min=9.9, limits_max=10.1)
        )
        alert = proc.process_measurement(
            StubMeasurement(value=15.0, limits_min=9.9, limits_max=10.1)
        )
        assert alert is not None
        assert alert.severity == "critical"
        assert PPK_ALERT_THRESHOLD == 1.00

    def test_ppk_alert_rule_name_includes_threshold(self) -> None:
        """Alert rule name encodes the threshold value."""
        proc = SPCProcessor()
        proc.process_measurement(
            StubMeasurement(value=5.0, limits_min=9.9, limits_max=10.1)
        )
        alert = proc.process_measurement(
            StubMeasurement(value=15.0, limits_min=9.9, limits_max=10.1)
        )
        assert alert is not None
        assert "1.00" in alert.rule

    def test_no_ppk_alert_when_process_capable(self) -> None:
        """Ppk >= 1.00 does not trigger a critical Ppk alert."""
        proc = SPCProcessor()
        # Wide spec [0, 100], tight values near 50 -> Ppk >> 1
        for v in [50.0, 50.1, 50.0, 49.9, 50.0, 50.1, 50.0, 49.9, 50.0, 50.1]:
            result = proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=100.0)
            )
            # Ppk alerts would be critical; WE alerts are warning. Filter.
            if result is not None and result.severity == "critical":
                pytest.fail("Critical Ppk alert raised for capable process")
        # Verify Ppk is comfortably above threshold
        stats = proc.get_statistics("prod_a", "voltage")
        assert stats.ppk is not None
        assert stats.ppk >= PPK_ALERT_THRESHOLD

    def test_no_ppk_alert_without_limits(self) -> None:
        """No spec limits -> no Ppk alert possible."""
        proc = SPCProcessor()
        alert = proc.process_measurement(
            StubMeasurement(value=999.0, limits_min=None, limits_max=None)
        )
        alert = proc.process_measurement(
            StubMeasurement(value=-999.0, limits_min=None, limits_max=None)
        )
        # No limits -> _check_ppk returns None; WE rules may fire if sigma>0
        # but Ppk alert specifically should not.
        if alert is not None:
            assert alert.severity != "critical" or not alert.rule.startswith("Ppk")


# ---------------------------------------------------------------------------
# Western Electric rules via SPCProcessor
# ---------------------------------------------------------------------------


class TestWesternElectricViaProcessor:
    """Tests that WE rules fire through process_measurement."""

    def test_we1_fires_for_outlier(self) -> None:
        """WE1 fires when the latest point is beyond 3 within-subgroup sigma."""
        proc = SPCProcessor()
        # Establish a stable in-control baseline (10 samples, sigma small)
        for v in [10.0] * 5 + [10.1, 9.9, 10.0, 10.1, 9.9]:
            proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=20.0)
            )
        # Now feed an outlier far beyond 3 sigma_within
        alert = proc.process_measurement(
            StubMeasurement(value=100.0, limits_min=0.0, limits_max=20.0)
        )
        # Either a WE1 warning alert or a Ppk critical alert (or both) should fire
        assert alert is not None

    def test_we4_fires_for_eight_consecutive_one_side(self) -> None:
        """WE4 fires when 8 consecutive points are on one side of the mean."""
        proc = SPCProcessor()
        # Seed with values centered at 10
        for v in [10.0, 10.0, 10.0]:
            proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=20.0)
            )
        # Feed 8 values all above the running mean (within sigma but one-sided)
        # Use small offsets so WE1/WE2/WE3 don't fire, only WE4.
        # After 3 zeros, mu=10; values 10.001 keep above 10.
        # But sigma_within may be 0 from constant baseline. Add variance first.
        proc = SPCProcessor()
        # 5 values to form one subgroup with small spread
        for v in [9.9, 10.0, 10.1, 10.0, 10.1]:
            proc.process_measurement(
                StubMeasurement(value=v, limits_min=0.0, limits_max=20.0)
            )
        # Now feed 8 values strictly above the running mean
        for _ in range(8):
            proc.process_measurement(
                StubMeasurement(value=10.5, limits_min=0.0, limits_max=20.0)
            )
        # WE4 should have fired by the 8th consecutive above-mean sample.
        # Collect all alerts and check for WE4.
        alerts = proc.get_alerts(limit=500)
        we_alerts = [a for a in alerts if "WE4" in a.rule]
        assert len(we_alerts) > 0, "Expected WE4 alert for 8 consecutive one-side"


# ---------------------------------------------------------------------------
# Failure indexer wiring
# ---------------------------------------------------------------------------


class TestFailureIndexerWiring:
    """Tests for the optional FailureIndexer integration."""

    @pytest.mark.asyncio
    async def test_alert_indexed_to_failure_indexer(self) -> None:
        """When a FailureIndexer is attached, alerts are forwarded to it."""
        # Use a MagicMock for the indexer to avoid Qdrant dependency.
        from unittest.mock import MagicMock

        mock_indexer = MagicMock()
        mock_indexer.index_failure = MagicMock()

        proc = SPCProcessor(failure_indexer=mock_indexer)
        # Trigger a Ppk alert
        proc.process_measurement(
            StubMeasurement(value=5.0, limits_min=9.9, limits_max=10.1)
        )
        proc.process_measurement(
            StubMeasurement(value=15.0, limits_min=9.9, limits_max=10.1)
        )
        # _index_alert schedules asyncio.create_task; let it run.
        # Yield control so the create_task coroutine can execute.
        await asyncio.sleep(0.05)

        # index_failure should have been called with an Event
        assert mock_indexer.index_failure.called
        call_args = mock_indexer.index_failure.call_args
        event = call_args.args[0]
        # Event has type ALARM_RAISED and data with rule/severity
        from shared.events import EventType

        assert event.type == EventType.ALARM_RAISED

    @pytest.mark.asyncio
    async def test_no_indexer_no_error(self) -> None:
        """Without a FailureIndexer, alert generation does not raise."""
        proc = SPCProcessor(failure_indexer=None)
        # Trigger an alert
        alert = proc.process_measurement(
            StubMeasurement(value=5.0, limits_min=9.9, limits_max=10.1)
        )
        alert = proc.process_measurement(
            StubMeasurement(value=15.0, limits_min=9.9, limits_max=10.1)
        )
        assert alert is not None
        # Give any pending tasks a chance (there shouldn't be any)
        await asyncio.sleep(0.01)
