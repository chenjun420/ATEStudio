"""SPCProcessor - streaming Statistical Process Control for measurement events.

Consumes Measurement ORM rows, maintains a sliding window of recent samples
per (product_type, measurement_name), and computes:
  - Cp / Cpk (within-subgroup sigma via R-bar / d2)
  - Ppk (overall sigma)
  - X-bar / R control charts with Shewhart control limits
  - Western Electric rule violations (online anomaly detection)

When Ppk drops below 1.00 the processor emits an alert and writes it to the
Qdrant failure index via FailureIndexer (when one is attached). Alerts are
also kept in an in-memory ring buffer for the alerts API endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ate_cloud.schemas.spc import SPCAlert, SPCChart, SPCStatistics, SPCSubgroupStat
from ate_cloud.services import spc_math

if TYPE_CHECKING:
    from ate_cloud.models.measurement import Measurement
    from ate_cloud.services.failure_indexer import FailureIndexer

logger = logging.getLogger(__name__)

#: Sliding window size per stream.
WINDOW_SIZE = 100

#: Subgroup size for X-bar / R charts.
SUBGROUP_SIZE = 5

#: Ppk threshold below which a critical alert is raised.
PPK_ALERT_THRESHOLD = 1.00

#: Max alerts retained in the in-memory ring buffer.
ALERT_BUFFER_SIZE = 200

# Stream key type.
_StreamKey = tuple[str, str]


class SPCProcessor:
    """Streaming SPC engine maintaining per-stream sliding windows.

    Attributes:
        _windows: Per-stream deque of recent measurement values.
        _limits: Per-stream cached (lsl, usl) from the first measurement seen.
        _timestamps: Per-stream deque of timestamps aligned with _windows.
        _alerts: Ring buffer of recent alerts across all streams.
        _failure_indexer: Optional FailureIndexer for Qdrant alert indexing.
    """

    def __init__(
        self,
        failure_indexer: FailureIndexer | None = None,
        window_size: int = WINDOW_SIZE,
        subgroup_size: int = SUBGROUP_SIZE,
    ) -> None:
        self._windows: dict[_StreamKey, deque[float]] = {}
        self._limits: dict[_StreamKey, tuple[float | None, float | None]] = {}
        self._timestamps: dict[_StreamKey, deque[datetime]] = {}
        self._alerts: deque[SPCAlert] = deque(maxlen=ALERT_BUFFER_SIZE)
        self._failure_indexer = failure_indexer
        self._window_size = window_size
        self._subgroup_size = subgroup_size

    def process_measurement(self, measurement: Measurement) -> SPCAlert | None:
        """Ingest one measurement; return an alert if a rule fires, else None.

        Skips measurements with no numeric value (SPC requires a scalar).
        Updates the sliding window, recomputes statistics, evaluates Western
        Electric rules and the Ppk threshold, and (on alert) writes to the
        failure index asynchronously.
        """
        if measurement.value is None:
            return None

        key: _StreamKey = (measurement.product_ref, measurement.name)
        win = self._windows.setdefault(key, deque(maxlen=self._window_size))
        ts = self._timestamps.setdefault(key, deque(maxlen=self._window_size))
        win.append(float(measurement.value))
        ts.append(measurement.timestamp)

        # Cache spec limits from the first measurement that carries them.
        cached = self._limits.get(key)
        if cached is None or (cached[0] is None and measurement.limits_min is not None):
            self._limits[key] = (measurement.limits_min, measurement.limits_max)
        elif cached[1] is None and measurement.limits_max is not None:
            self._limits[key] = (cached[0], measurement.limits_max)

        if len(win) < 2:
            return None

        values = list(win)
        mu = spc_math.mean(values)
        sigma_overall = spc_math.population_stddev(values, mu)

        lsl, usl = self._limits.get(key, (None, None))
        sigma_within = self._within_sigma(values)

        # Ppk alert (critical) - needs both limits and overall sigma.
        alert = self._check_ppk(key, mu, sigma_overall, lsl, usl, values, measurement)

        # Western Electric rules (warning) - need within-subgroup sigma.
        if sigma_within > 0:
            we_rules = spc_math.western_electric_rules(values, mu, sigma_within)
            for rule in we_rules:
                we_alert = SPCAlert(
                    product_type=key[0],
                    measurement_name=key[1],
                    rule=rule,
                    severity="warning",
                    message=f"Western Electric rule violated: {rule}",
                    value=float(measurement.value),
                    timestamp=datetime.now(UTC),
                    sample_count=len(values),
                )
                self._alerts.append(we_alert)
                self._index_alert(we_alert)
                if alert is None:
                    alert = we_alert

        return alert

    def get_statistics(
        self, product_type: str, measurement_name: str
    ) -> SPCStatistics:
        """Return current SPC statistics for a stream (empty stats if unknown)."""
        key: _StreamKey = (product_type, measurement_name)
        win = self._windows.get(key)
        if not win:
            return SPCStatistics(
                product_type=product_type,
                measurement_name=measurement_name,
                sample_count=0,
            )

        values = list(win)
        mu = spc_math.mean(values)
        sigma_overall = spc_math.population_stddev(values, mu)
        sigma_within = self._within_sigma(values)
        lsl, usl = self._limits.get(key, (None, None))

        cp_val = cpk_val = ppk_val = None
        if usl is not None and lsl is not None:
            if sigma_within and sigma_within > 0:
                cp_val = spc_math.cp(usl, lsl, sigma_within)
                cpk_val = spc_math.cpk(usl, lsl, mu, sigma_within)
            if sigma_overall > 0:
                ppk_val = spc_math.ppk(usl, lsl, mu, sigma_overall)

        last_ts = self._timestamps.get(key, deque())
        last_updated = last_ts[-1] if last_ts else None

        return SPCStatistics(
            product_type=product_type,
            measurement_name=measurement_name,
            sample_count=len(values),
            mean=mu,
            std_dev_within=sigma_within if sigma_within and sigma_within > 0 else None,
            std_dev_overall=sigma_overall if sigma_overall > 0 else None,
            cp=cp_val,
            cpk=cpk_val,
            ppk=ppk_val,
            usl=usl,
            lsl=lsl,
            last_updated=last_updated,
        )

    def get_chart(self, product_type: str, measurement_name: str) -> SPCChart:
        """Return X-bar / R chart data with control limits for a stream."""
        key: _StreamKey = (product_type, measurement_name)
        win = self._windows.get(key)
        chart = SPCChart(
            product_type=product_type,
            measurement_name=measurement_name,
            subgroup_size=self._subgroup_size,
        )
        if not win or len(win) < self._subgroup_size:
            return chart

        values = list(win)
        subgroups_raw = spc_math.chunk(values, self._subgroup_size)
        # Drop a trailing partial subgroup so stats reflect complete subgroups.
        if len(subgroups_raw[-1]) < self._subgroup_size:
            subgroups_raw = subgroups_raw[:-1]
        if not subgroups_raw:
            return chart

        subgroup_means = [spc_math.mean(sg) for sg in subgroups_raw]
        ranges = spc_math.subgroup_ranges(subgroups_raw)
        grand_mean = spc_math.mean(subgroup_means)
        r_bar = spc_math.mean(ranges)

        try:
            a2, d3, d4 = spc_math.control_constants(self._subgroup_size)
        except KeyError:
            return chart

        chart.center_line = grand_mean
        chart.ucl = grand_mean + a2 * r_bar
        chart.lcl = grand_mean - a2 * r_bar
        chart.r_center = r_bar
        chart.r_ucl = d4 * r_bar
        chart.r_lcl = d3 * r_bar
        chart.subgroups = [
            SPCSubgroupStat(index=i, mean=m, range=r, sample_count=len(sg))
            for i, (m, r, sg) in enumerate(
                zip(subgroup_means, ranges, subgroups_raw, strict=True)
            )
        ]
        return chart

    def get_alerts(self, limit: int = 50) -> list[SPCAlert]:
        """Return the most recent alerts (newest first)."""
        items = list(self._alerts)
        items.reverse()
        return items[:limit]

    def reset(self, product_type: str | None = None, measurement_name: str | None = None) -> None:
        """Clear windows/alerts. With no args, clears everything."""
        if product_type is None:
            self._windows.clear()
            self._limits.clear()
            self._timestamps.clear()
            self._alerts.clear()
            return
        if measurement_name is None:
            keys = [k for k in self._windows if k[0] == product_type]
        else:
            keys = [(product_type, measurement_name)]
        for k in keys:
            self._windows.pop(k, None)
            self._limits.pop(k, None)
            self._timestamps.pop(k, None)

    # -- internals ---------------------------------------------------------

    def _within_sigma(self, values: list[float]) -> float:
        """Estimate within-subgroup sigma via R-bar / d2 over subgroups."""
        if len(values) < self._subgroup_size:
            return 0.0
        subs = spc_math.chunk(values, self._subgroup_size)
        if len(subs[-1]) < self._subgroup_size:
            subs = subs[:-1]
        if not subs:
            return 0.0
        try:
            d2_val = spc_math.d2(self._subgroup_size)
        except KeyError:
            return 0.0
        r_bar = spc_math.mean(spc_math.subgroup_ranges(subs))
        return r_bar / d2_val

    def _check_ppk(
        self,
        key: _StreamKey,
        mu: float,
        sigma_overall: float,
        lsl: float | None,
        usl: float | None,
        values: list[float],
        measurement: Measurement,
    ) -> SPCAlert | None:
        """Raise a critical alert when Ppk < 1.00; index to Qdrant."""
        if usl is None or lsl is None or sigma_overall <= 0:
            return None
        if measurement.value is None:
            return None
        ppk_val = spc_math.ppk(usl, lsl, mu, sigma_overall)
        if ppk_val >= PPK_ALERT_THRESHOLD:
            return None
        alert = SPCAlert(
            product_type=key[0],
            measurement_name=key[1],
            rule=f"Ppk_below_{PPK_ALERT_THRESHOLD:.2f}",
            severity="critical",
            message=f"Ppk={ppk_val:.3f} below threshold {PPK_ALERT_THRESHOLD:.2f}",
            value=float(measurement.value),
            timestamp=datetime.now(UTC),
            sample_count=len(values),
        )
        self._alerts.append(alert)
        self._index_alert(alert)
        return alert

    def _index_alert(self, alert: SPCAlert) -> None:
        """Write an alert to the Qdrant failure index (non-blocking)."""
        if self._failure_indexer is None:
            return
        asyncio.create_task(self._index_alert_async(alert))

    async def _index_alert_async(self, alert: SPCAlert) -> None:
        """Async worker: build an Event and feed it to FailureIndexer.index_failure."""
        if self._failure_indexer is None:
            return
        try:
            from shared.events import Event, EventType

            event = Event(
                type=EventType.ALARM_RAISED,
                data={
                    "alarm_id": str(uuid.uuid4()),
                    "product_type": alert.product_type,
                    "measurement_name": alert.measurement_name,
                    "rule": alert.rule,
                    "severity": alert.severity,
                    "message": alert.message,
                    "value": alert.value,
                    "sample_count": alert.sample_count,
                },
            )
            self._failure_indexer.index_failure(event)
        except Exception:
            logger.exception("Failed to index SPC alert in Qdrant")


__all__ = ["SPCProcessor"]
