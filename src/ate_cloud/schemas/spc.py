"""Pydantic schemas for SPC (Statistical Process Control) resources.

Defines response models for:
- SPCStatistics: capability indices (Cp, Cpk, Ppk) and process metrics
- SPCChart: X-bar / R control chart data with control limits
- SPCAlert: anomaly alert (Western Electric rule violation or Ppk threshold)
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SPCStatistics(BaseModel):
    """SPC capability indices and process statistics for one stream.

    Attributes:
        product_type: Product type identifier.
        measurement_name: Measurement name.
        sample_count: Number of samples in the sliding window.
        mean: Arithmetic mean of values.
        std_dev_within: Within-subgroup sigma (R-bar / d2), used for Cp/Cpk.
        std_dev_overall: Overall sigma (population), used for Ppk.
        cp: Process capability index (potential).
        cpk: Process capability index (actual, within-subgroup).
        ppk: Preliminary process performance index (overall sigma).
        usl: Upper specification limit.
        lsl: Lower specification limit.
        last_updated: Timestamp of the last sample processed.
    """

    product_type: str
    measurement_name: str
    sample_count: int = Field(ge=0)
    mean: float | None = None
    std_dev_within: float | None = None
    std_dev_overall: float | None = None
    cp: float | None = None
    cpk: float | None = None
    ppk: float | None = None
    usl: float | None = None
    lsl: float | None = None
    last_updated: datetime | None = None


class SPCSubgroupStat(BaseModel):
    """One subgroup's X-bar and R statistics.

    Attributes:
        index: Subgroup ordinal (0-based).
        mean: Subgroup mean (X-bar).
        range: Subgroup range (max - min).
        sample_count: Number of samples in the subgroup.
    """

    index: int
    mean: float
    range: float
    sample_count: int


class SPCChart(BaseModel):
    """X-bar / R control chart data with control limits.

    Attributes:
        product_type: Product type identifier.
        measurement_name: Measurement name.
        center_line: X-bar grand average (center line of X-bar chart).
        ucl: Upper control limit for X-bar chart.
        lcl: Lower control limit for X-bar chart.
        r_center: Center line for R chart (R-bar).
        r_ucl: Upper control limit for R chart.
        r_lcl: Lower control limit for R chart (0 for n<=6).
        subgroup_size: Number of samples per subgroup.
        subgroups: List of subgroup statistics.
    """

    product_type: str
    measurement_name: str
    center_line: float | None = None
    ucl: float | None = None
    lcl: float | None = None
    r_center: float | None = None
    r_ucl: float | None = None
    r_lcl: float | None = None
    subgroup_size: int = 5
    subgroups: list[SPCSubgroupStat] = Field(default_factory=list)


class SPCAlert(BaseModel):
    """An SPC alert raised by anomaly detection.

    Attributes:
        product_type: Product type identifier.
        measurement_name: Measurement name.
        rule: Rule that triggered (e.g. 'WE1_beyond_3sigma', 'Ppk_below_1.00').
        severity: 'warning' or 'critical'.
        message: Human-readable description.
        value: The value that triggered the alert (if applicable).
        timestamp: When the alert was generated.
        sample_count: Number of samples seen when the alert fired.
    """

    product_type: str
    measurement_name: str
    rule: str
    severity: str
    message: str
    value: float | None = None
    timestamp: datetime
    sample_count: int
