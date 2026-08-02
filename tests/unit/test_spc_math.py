"""Unit tests for spc_math pure functions.

Covers: mean, population_stddev, chunk, subgroup_ranges, control_constants,
d2, cp, cpk, ppk, western_electric_rules. Edge cases: empty sequences,
single values, zero sigma, out-of-range subgroup sizes.
"""

from __future__ import annotations

import math

import pytest

from ate_cloud.services import spc_math

# ---------------------------------------------------------------------------
# mean
# ---------------------------------------------------------------------------


class TestMean:
    """Tests for spc_math.mean."""

    def test_mean_basic(self) -> None:
        """Arithmetic mean of a small sequence."""
        assert spc_math.mean([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_mean_single_value(self) -> None:
        """Single value returns itself."""
        assert spc_math.mean([42.0]) == 42.0

    def test_mean_negative_values(self) -> None:
        """Mean handles negatives."""
        assert spc_math.mean([-2.0, 2.0]) == 0.0

    def test_mean_empty_raises(self) -> None:
        """Empty sequence raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            spc_math.mean([])

    def test_mean_integer_inputs(self) -> None:
        """Integer inputs accepted (Sequence[float])."""
        assert spc_math.mean([1, 2, 3]) == 2.0


# ---------------------------------------------------------------------------
# population_stddev
# ---------------------------------------------------------------------------


class TestPopulationStddev:
    """Tests for spc_math.population_stddev."""

    def test_population_stddev_known_value(self) -> None:
        """Population stddev of [2,4,4,4,5,5,7,9] is 2.0."""
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        assert spc_math.population_stddev(values) == 2.0

    def test_population_stddev_single_value_zero(self) -> None:
        """Single value has zero stddev."""
        assert spc_math.population_stddev([5.0]) == 0.0

    def test_population_stddev_constant_sequence_zero(self) -> None:
        """Constant sequence has zero stddev."""
        assert spc_math.population_stddev([3.0, 3.0, 3.0]) == 0.0

    def test_population_stddev_with_provided_mu(self) -> None:
        """When mu is provided, uses it instead of recomputing."""
        values = [1.0, 2.0, 3.0]
        # mu=2 -> variance = (1+0+1)/3 = 2/3 -> stddev = sqrt(2/3)
        expected = math.sqrt(2.0 / 3.0)
        assert spc_math.population_stddev(values, mu=2.0) == pytest.approx(expected)

    def test_population_stddev_mu_none_recomputes(self) -> None:
        """mu=None recomputes mean internally."""
        values = [1.0, 2.0, 3.0, 4.0]
        mu = spc_math.mean(values)
        assert spc_math.population_stddev(values) == spc_math.population_stddev(
            values, mu=mu
        )

    def test_population_stddev_empty_raises(self) -> None:
        """Empty sequence raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            spc_math.population_stddev([])

    def test_population_stddev_uses_population_not_sample(self) -> None:
        """Population stddev divides by N, not N-1 (sample stddev)."""
        # For [1,2,3]: pop var = 2/3, sample var = 1.0
        # pop stddev = sqrt(2/3) != 1.0
        assert spc_math.population_stddev([1.0, 2.0, 3.0]) != pytest.approx(1.0)


# ---------------------------------------------------------------------------
# chunk
# ---------------------------------------------------------------------------


class TestChunk:
    """Tests for spc_math.chunk."""

    def test_chunk_even_split(self) -> None:
        """Even split has no remainder chunk."""
        result = spc_math.chunk([1.0, 2.0, 3.0, 4.0], 2)
        assert result == [[1.0, 2.0], [3.0, 4.0]]

    def test_chunk_uneven_split(self) -> None:
        """Last chunk may be shorter."""
        result = spc_math.chunk([1.0, 2.0, 3.0, 4.0, 5.0], 2)
        assert result == [[1.0, 2.0], [3.0, 4.0], [5.0]]

    def test_chunk_size_one(self) -> None:
        """Size 1 yields single-element chunks."""
        result = spc_math.chunk([1.0, 2.0, 3.0], 1)
        assert result == [[1.0], [2.0], [3.0]]

    def test_chunk_size_larger_than_sequence(self) -> None:
        """Size larger than input yields single short chunk."""
        result = spc_math.chunk([1.0, 2.0], 10)
        assert result == [[1.0, 2.0]]

    def test_chunk_empty_sequence(self) -> None:
        """Empty input yields empty list."""
        assert spc_math.chunk([], 5) == []

    def test_chunk_size_zero_raises(self) -> None:
        """Size < 1 raises ValueError."""
        with pytest.raises(ValueError, match="size must be >= 1"):
            spc_math.chunk([1.0, 2.0], 0)

    def test_chunk_size_negative_raises(self) -> None:
        """Negative size raises ValueError."""
        with pytest.raises(ValueError, match="size must be >= 1"):
            spc_math.chunk([1.0, 2.0], -3)

    def test_chunk_returns_lists_not_views(self) -> None:
        """Returned chunks are independent lists."""
        original = [1.0, 2.0, 3.0, 4.0]
        result = spc_math.chunk(original, 2)
        result[0][0] = 999.0
        assert original[0] == 1.0


# ---------------------------------------------------------------------------
# subgroup_ranges
# ---------------------------------------------------------------------------


class TestSubgroupRanges:
    """Tests for spc_math.subgroup_ranges."""

    def test_ranges_basic(self) -> None:
        """Range = max - min for each subgroup."""
        result = spc_math.subgroup_ranges([[1.0, 5.0], [2.0, 2.0, 8.0]])
        assert result == [4.0, 6.0]

    def test_ranges_single_element_subgroup_zero(self) -> None:
        """Single-element subgroups have range 0."""
        result = spc_math.subgroup_ranges([[5.0], [1.0, 9.0]])
        assert result[0] == 0.0
        assert result[1] == 8.0

    def test_ranges_empty_subgroups_skipped(self) -> None:
        """Empty subgroups are filtered out (len > 0 guard)."""
        result = spc_math.subgroup_ranges([[], [1.0, 2.0], []])
        assert result == [1.0]

    def test_ranges_empty_input(self) -> None:
        """Empty list of subgroups yields empty list."""
        assert spc_math.subgroup_ranges([]) == []

    def test_ranges_negative_values(self) -> None:
        """Ranges handle negative values."""
        result = spc_math.subgroup_ranges([[-5.0, 5.0], [-3.0, -1.0]])
        assert result == [10.0, 2.0]


# ---------------------------------------------------------------------------
# control_constants
# ---------------------------------------------------------------------------


class TestControlConstants:
    """Tests for spc_math.control_constants."""

    @pytest.mark.parametrize(
        "n,expected",
        [
            (2, (1.880, 0.000, 3.267)),
            (3, (1.023, 0.000, 2.574)),
            (4, (0.729, 0.000, 2.282)),
            (5, (0.577, 0.000, 2.114)),
            (6, (0.483, 0.000, 2.004)),
            (7, (0.419, 0.076, 1.924)),
        ],
    )
    def test_control_constants_for_n_2_to_7(
        self, n: int, expected: tuple[float, float, float]
    ) -> None:
        """Returns (A2, D3, D4) per Shewhart table for n=2..7."""
        assert spc_math.control_constants(n) == expected

    def test_control_constants_below_range_raises(self) -> None:
        """n=1 raises KeyError (not in table)."""
        with pytest.raises(KeyError):
            spc_math.control_constants(1)

    def test_control_constants_above_range_raises(self) -> None:
        """n=8 raises KeyError (not in table)."""
        with pytest.raises(KeyError):
            spc_math.control_constants(8)

    def test_d3_zero_for_n_below_7(self) -> None:
        """D3=0 for n<7 (R LCL clamped to 0)."""
        for n in range(2, 7):
            _, d3, _ = spc_math.control_constants(n)
            assert d3 == 0.0

    def test_d3_nonzero_for_n_7(self) -> None:
        """D3>0 for n=7."""
        _, d3, _ = spc_math.control_constants(7)
        assert d3 > 0


# ---------------------------------------------------------------------------
# d2
# ---------------------------------------------------------------------------


class TestD2:
    """Tests for spc_math.d2."""

    @pytest.mark.parametrize(
        "n,expected",
        [
            (2, 1.128),
            (3, 1.693),
            (4, 2.059),
            (5, 2.326),
            (6, 2.534),
            (7, 2.704),
        ],
    )
    def test_d2_for_n_2_to_7(self, n: int, expected: float) -> None:
        """d2 constant increases monotonically with n."""
        assert spc_math.d2(n) == expected

    def test_d2_below_range_raises(self) -> None:
        """n=1 raises KeyError."""
        with pytest.raises(KeyError):
            spc_math.d2(1)

    def test_d2_above_range_raises(self) -> None:
        """n=8 raises KeyError."""
        with pytest.raises(KeyError):
            spc_math.d2(8)


# ---------------------------------------------------------------------------
# cp, cpk, ppk
# ---------------------------------------------------------------------------


class TestCp:
    """Tests for spc_math.cp."""

    def test_cp_basic(self) -> None:
        """Cp = (USL-LSL) / (6*sigma)."""
        assert spc_math.cp(usl=10.0, lsl=-10.0, sigma_within=2.0) == pytest.approx(
            20.0 / 12.0
        )

    def test_cp_zero_sigma_raises_division(self) -> None:
        """Cp with zero sigma raises ZeroDivisionError (not guarded)."""
        with pytest.raises(ZeroDivisionError):
            spc_math.cp(usl=10.0, lsl=-10.0, sigma_within=0.0)

    def test_cp_negative_sigma(self) -> None:
        """Cp with negative sigma yields negative (math is honest)."""
        # Implementation does not guard against negative sigma; it computes.
        result = spc_math.cp(usl=10.0, lsl=-10.0, sigma_within=-2.0)
        assert result < 0


class TestCpk:
    """Tests for spc_math.cpk."""

    def test_cpk_centered_process(self) -> None:
        """Centered process: Cpk = Cp."""
        mu = 0.0
        usl, lsl, sigma = 6.0, -6.0, 1.0
        cp = spc_math.cp(usl, lsl, sigma)
        cpk = spc_math.cpk(usl, lsl, mu, sigma)
        assert cpk == pytest.approx(cp)

    def test_cpk_off_center_lower(self) -> None:
        """Process shifted toward LSL: Cpk < Cp, equals lower-side index."""
        mu = -2.0
        usl, lsl, sigma = 6.0, -6.0, 1.0
        cpk = spc_math.cpk(usl, lsl, mu, sigma)
        upper = (usl - mu) / (3 * sigma)
        lower = (mu - lsl) / (3 * sigma)
        assert cpk == pytest.approx(min(upper, lower))
        assert cpk == pytest.approx(lower)

    def test_cpk_off_center_upper(self) -> None:
        """Process shifted toward USL: Cpk = upper-side index."""
        mu = 2.0
        usl, lsl, sigma = 6.0, -6.0, 1.0
        cpk = spc_math.cpk(usl, lsl, mu, sigma)
        upper = (usl - mu) / (3 * sigma)
        assert cpk == pytest.approx(upper)

    def test_cpk_zero_sigma_raises(self) -> None:
        """Zero sigma raises ZeroDivisionError."""
        with pytest.raises(ZeroDivisionError):
            spc_math.cpk(usl=10.0, lsl=-10.0, mu=0.0, sigma_within=0.0)


class TestPpk:
    """Tests for spc_math.ppk."""

    def test_ppk_centered_process(self) -> None:
        """Centered: Ppk = (USL-LSL)/(6*sigma)."""
        result = spc_math.ppk(usl=6.0, lsl=-6.0, mu=0.0, sigma_overall=1.0)
        assert result == pytest.approx(2.0)

    def test_ppk_off_center(self) -> None:
        """Off-center: Ppk = min of upper/lower indices."""
        result = spc_math.ppk(usl=6.0, lsl=-6.0, mu=3.0, sigma_overall=1.0)
        upper = (6.0 - 3.0) / 3.0
        lower = (3.0 - (-6.0)) / 3.0
        assert result == pytest.approx(min(upper, lower))

    def test_ppk_below_one_for_poor_process(self) -> None:
        """Wide spread relative to spec yields Ppk < 1.0."""
        result = spc_math.ppk(usl=6.0, lsl=-6.0, mu=0.0, sigma_overall=3.0)
        assert result < 1.0

    def test_ppk_zero_sigma_raises(self) -> None:
        """Zero sigma raises ZeroDivisionError."""
        with pytest.raises(ZeroDivisionError):
            spc_math.ppk(usl=10.0, lsl=-10.0, mu=0.0, sigma_overall=0.0)


# ---------------------------------------------------------------------------
# western_electric_rules
# ---------------------------------------------------------------------------


class TestWesternElectricRules:
    """Tests for spc_math.western_electric_rules."""

    def test_empty_values_returns_empty(self) -> None:
        """Empty input yields no rule triggers."""
        assert spc_math.western_electric_rules([], mu=0.0, sigma=1.0) == []

    def test_zero_sigma_returns_empty(self) -> None:
        """sigma<=0 short-circuits to no triggers."""
        assert spc_math.western_electric_rules([1.0, 2.0, 3.0], mu=0.0, sigma=0.0) == []

    def test_negative_sigma_returns_empty(self) -> None:
        """Negative sigma short-circuits to no triggers."""
        assert (
            spc_math.western_electric_rules([1.0, 2.0, 3.0], mu=0.0, sigma=-1.0) == []
        )

    def test_we1_single_point_beyond_3sigma(self) -> None:
        """WE1: latest point beyond 3 sigma triggers."""
        # mu=0, sigma=1; latest value 4.0 -> |4-0|>3*1
        result = spc_math.western_electric_rules([0.0, 0.0, 0.0, 4.0], mu=0.0, sigma=1.0)
        assert "WE1_beyond_3sigma" in result

    def test_we1_not_triggered_within_3sigma(self) -> None:
        """WE1 not triggered when latest within 3 sigma."""
        result = spc_math.western_electric_rules([0.0, 1.0, 2.0], mu=0.0, sigma=1.0)
        assert "WE1_beyond_3sigma" not in result

    def test_we1_triggers_on_negative_side(self) -> None:
        """WE1 triggers for points below -3 sigma too."""
        result = spc_math.western_electric_rules([0.0, 0.0, -4.0], mu=0.0, sigma=1.0)
        assert "WE1_beyond_3sigma" in result

    def test_we2_two_of_three_beyond_2sigma_same_side(self) -> None:
        """WE2: 2 of last 3 beyond 2 sigma, same side."""
        # mu=0, sigma=1; values [-3, 0, 3] -> last 3 has 2 beyond 2sigma but
        # they are on opposite sides; _same_side returns False. Use same side.
        result = spc_math.western_electric_rules(
            [0.0, 3.0, 0.0, 3.0], mu=0.0, sigma=1.0
        )
        assert "WE2_2of3_beyond_2sigma" in result

    def test_we2_not_triggered_opposite_sides(self) -> None:
        """WE2 not triggered when beyond-2sigma points are on opposite sides."""
        # Last 3 values: [-3, 0, 3]; both beyond 2sigma but opposite sides
        result = spc_math.western_electric_rules(
            [0.0, -3.0, 0.0, 3.0], mu=0.0, sigma=1.0
        )
        assert "WE2_2of3_beyond_2sigma" not in result

    def test_we2_two_values_only(self) -> None:
        """WE2 can trigger with only 2 values if both beyond 2sigma same side."""
        result = spc_math.western_electric_rules([3.0, 3.0], mu=0.0, sigma=1.0)
        assert "WE2_2of3_beyond_2sigma" in result

    def test_we3_four_of_five_beyond_1sigma_same_side(self) -> None:
        """WE3: 4 of 5 consecutive beyond 1 sigma, same side."""
        # mu=0, sigma=1; 5 values all > 1sigma above
        values = [2.0, 2.0, 2.0, 2.0, 0.0]
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE3_4of5_beyond_1sigma" in result

    def test_we3_not_triggered_insufficient_samples(self) -> None:
        """WE3 needs at least 4 samples."""
        result = spc_math.western_electric_rules([2.0, 2.0, 2.0], mu=0.0, sigma=1.0)
        assert "WE3_4of5_beyond_1sigma" not in result

    def test_we4_eight_consecutive_one_side(self) -> None:
        """WE4: 8 consecutive points on one side of center."""
        values = [1.0] * 8
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE4_8_consecutive_one_side" in result

    def test_we4_eight_consecutive_below(self) -> None:
        """WE4 triggers for 8 consecutive below center too."""
        values = [-1.0] * 8
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE4_8_consecutive_one_side" in result

    def test_we4_not_triggered_seven_only(self) -> None:
        """WE4 needs 8 consecutive; 7 doesn't trigger."""
        values = [1.0] * 7
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE4_8_consecutive_one_side" not in result

    def test_we4_not_triggered_mixed_sides(self) -> None:
        """WE4 not triggered when 8 values span both sides."""
        values = [1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0]
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE4_8_consecutive_one_side" not in result

    def test_multiple_rules_triggered_simultaneously(self) -> None:
        """Multiple WE rules can fire at once on the latest sample."""
        # All 8 values far beyond 3 sigma on the same side - triggers WE1, WE2,
        # WE3, and WE4 simultaneously.
        values = [10.0] * 8
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE1_beyond_3sigma" in result
        assert "WE2_2of3_beyond_2sigma" in result
        assert "WE3_4of5_beyond_1sigma" in result
        assert "WE4_8_consecutive_one_side" in result

    def test_no_rules_triggered_for_stable_process(self) -> None:
        """Stable in-control process triggers no rules."""
        values = [0.1, -0.1, 0.2, -0.2, 0.0, 0.1, -0.1, 0.0]
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert result == []

    def test_only_latest_point_evaluated(self) -> None:
        """Rules are evaluated only against the latest sample's window.

        A point 3 samples back that was beyond 3 sigma does not retroactively
        fire if the latest sample is in control.
        """
        # Index 2 is beyond 3 sigma, but the latest (index 5) is in control.
        values = [0.0, 0.0, 5.0, 0.0, 0.0, 0.0]
        result = spc_math.western_electric_rules(values, mu=0.0, sigma=1.0)
        assert "WE1_beyond_3sigma" not in result


# ---------------------------------------------------------------------------
# _same_side (internal helper)
# ---------------------------------------------------------------------------


class TestSameSide:
    """Tests for the internal _same_side helper."""

    def test_all_above(self) -> None:
        """All values above center returns True."""
        assert spc_math._same_side([1.0, 2.0, 3.0], 0.0) is True

    def test_all_below(self) -> None:
        """All values below center returns True."""
        assert spc_math._same_side([-1.0, -2.0], 0.0) is True

    def test_mixed_returns_false(self) -> None:
        """Mixed sides returns False."""
        assert spc_math._same_side([-1.0, 1.0], 0.0) is False

    def test_empty_returns_false(self) -> None:
        """Empty input returns False."""
        assert spc_math._same_side([], 0.0) is False

    def test_value_equal_to_center_excluded(self) -> None:
        """Value equal to center is neither above nor below (strict)."""
        # [1.0, 0.0] - 0.0 is not > 0 and not < 0, so neither all-above nor all-below
        assert spc_math._same_side([1.0, 0.0], 0.0) is False
