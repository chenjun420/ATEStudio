"""Multi-format report exporter for test execution data.

Supports three export formats:
- **ATML** — IEEE 1636.1 TestResults XML (via :class:`ATMLExporter`).
- **CSV** — flat table of all measurements across steps.
- **Parquet** — columnar binary format (requires ``pyarrow``; gracefully
  falls back to CSV if pyarrow is not installed).
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Literal

from ate_cloud.models.execution import Execution
from ate_cloud.models.measurement import Measurement
from ate_cloud.services.atml_exporter import ATMLExporter

logger = logging.getLogger(__name__)

ExportFormat = Literal["atml", "csv", "parquet"]

_CSV_COLUMNS = [
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


class ReportExporter:
    """Export execution + measurement data in multiple formats.

    Args:
        atml_exporter: Optional ``ATMLExporter`` instance (created if omitted).
    """

    def __init__(self, atml_exporter: ATMLExporter | None = None) -> None:
        self._atml = atml_exporter or ATMLExporter()

    def export(
        self,
        execution: Execution,
        measurements: list[Measurement],
        fmt: ExportFormat,
    ) -> tuple[bytes, str]:
        """Export execution data in the requested format.

        Args:
            execution: The execution record.
            measurements: List of measurement records for this execution.
            fmt: Export format — ``"atml"``, ``"csv"``, or ``"parquet"``.

        Returns:
            Tuple of ``(content_bytes, media_type)``.

        Raises:
            ValueError: If ``fmt`` is not one of the supported formats.
        """
        match fmt:
            case "atml":
                xml = self._atml.generate_atml(execution, measurements)
                return xml.encode("utf-8"), "text/xml"
            case "csv":
                return self._export_csv(measurements), "text/csv"
            case "parquet":
                return self._export_parquet(measurements)
            case _:
                raise ValueError(f"Unsupported export format: {fmt}")

    def _export_csv(self, measurements: list[Measurement]) -> bytes:
        """Export measurements as a flat CSV table.

        Args:
            measurements: List of measurement records.

        Returns:
            CSV content as UTF-8 encoded bytes.
        """
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        for m in measurements:
            writer.writerow([
                m.measurement_id,
                m.execution_ref or "",
                m.station_ref or "",
                m.product_ref,
                m.dut_serial,
                m.timestamp.isoformat() if m.timestamp else "",
                m.name,
                m.value if m.value is not None else "",
                m.limits_min if m.limits_min is not None else "",
                m.limits_max if m.limits_max is not None else "",
                m.unit or "",
                m.outcome,
            ])
        return buf.getvalue().encode("utf-8")

    def _export_parquet(
        self, measurements: list[Measurement]
    ) -> tuple[bytes, str]:
        """Export measurements as Parquet, falling back to CSV.

        If ``pyarrow`` is not installed, logs a warning and returns CSV
        content with ``text/csv`` media type.

        Args:
            measurements: List of measurement records.

        Returns:
            Tuple of ``(content_bytes, media_type)``.
        """
        try:
            import pyarrow as pa  # type: ignore[import-not-found]
            import pyarrow.parquet as pq  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "pyarrow not installed — falling back to CSV for Parquet export"
            )
            return self._export_csv(measurements), "text/csv"

        data: dict[str, list[object]] = {col: [] for col in _CSV_COLUMNS}
        for m in measurements:
            data["measurement_id"].append(m.measurement_id)
            data["execution_ref"].append(m.execution_ref or "")
            data["station_ref"].append(m.station_ref or "")
            data["product_ref"].append(m.product_ref)
            data["dut_serial"].append(m.dut_serial)
            data["timestamp"].append(
                m.timestamp.isoformat() if m.timestamp else ""
            )
            data["name"].append(m.name)
            data["value"].append(m.value)
            data["limits_min"].append(m.limits_min)
            data["limits_max"].append(m.limits_max)
            data["unit"].append(m.unit or "")
            data["outcome"].append(m.outcome)

        table = pa.table(data)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink)
        return sink.getvalue().to_pybytes(), "application/vnd.apache.parquet"


__all__ = ["ReportExporter", "ExportFormat"]
