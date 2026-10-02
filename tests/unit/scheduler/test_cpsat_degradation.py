"""CP-SAT scheduler — behaviour when OR-Tools is absent.

`cpsat.py` is the largest module in the repository with no coverage at all, and
the reason is structural rather than an oversight: OR-Tools is an optional
dependency that is not declared, so on a stock install every path below the
`_ORTOOLS_AVAILABLE` guard cannot execute. What *can* execute — and is what every
caller actually depends on — is the degradation contract, and nothing tested it.

So the tests below pin the guards rather than the solver. They force
`_ORTOOLS_AVAILABLE` to False instead of skipping when OR-Tools is missing,
because a test that skips on the machine most likely to be misconfigured is a
test that never runs where it matters.

The contract, as documented on each method:

* ``__init__``          warns, naming the import error
* ``schedule([])``      -> ``{}`` (the empty check runs before the guard)
* ``schedule([step])``  -> ``None``
* ``schedule_pareto``   -> ``[]`` for both empty and non-empty input
"""

from __future__ import annotations

import logging

import pytest

from ate_platform.scheduler import cpsat as cpsat_module
from ate_platform.scheduler.cpsat import CPSATScheduler
from shared.dsl import YamlStep


@pytest.fixture
def no_ortools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the unavailable-OR-Tools path regardless of what is installed."""
    monkeypatch.setattr(cpsat_module, "_ORTOOLS_AVAILABLE", False)


@pytest.fixture
def step() -> YamlStep:
    return YamlStep(id="s1", script="noop.py")


class TestEmptyInputShortCircuitsTheGuard:
    """The empty-input check precedes the OR-Tools check, and must keep doing so.

    Callers rely on ``{}`` meaning "nothing to schedule" rather than "no solver",
    which is why this is asserted separately instead of folded into the
    degradation cases below: if the two checks were ever swapped, every empty
    sequence would start reporting a missing solver instead of a completed
    schedule.
    """

    def test_schedule_empty_returns_empty_mapping(self, no_ortools: None) -> None:
        assert CPSATScheduler().schedule([]) == {}

    def test_schedule_pareto_empty_returns_empty_list(self, no_ortools: None) -> None:
        assert CPSATScheduler().schedule_pareto([]) == []


class TestDegradesWithoutOrTools:
    """Documented behaviour when the optional solver is unavailable."""

    def test_schedule_returns_none(self, no_ortools: None, step: YamlStep) -> None:
        assert CPSATScheduler().schedule([step]) is None

    def test_schedule_pareto_returns_empty_frontier(
        self, no_ortools: None, step: YamlStep
    ) -> None:
        assert CPSATScheduler().schedule_pareto([step]) == []

    def test_optimal_statuses_are_empty(self, no_ortools: None) -> None:
        """The solver-status set is derived from cp_model at import time.

        With no cp_model there is nothing to enumerate, so the set must be empty
        rather than populated with invented constants — anything else would let a
        caller compare against a status the solver can never produce.

        Reads the real module-level value rather than the monkeypatched flag:
        ``_OPTIMAL_OR_FEASIBLE`` is built once at import, so forcing the flag to
        False afterwards does nothing to it. On a machine that *does* have
        OR-Tools the set is legitimately non-empty, and asserting emptiness there
        would be asserting something false — so the value is reported and the
        check only runs when OR-Tools is genuinely absent.
        """
        if cpsat_module._ORTOOLS_AVAILABLE:
            pytest.skip("OR-Tools is installed, so the status set is legitimately populated")
        assert cpsat_module._OPTIMAL_OR_FEASIBLE == set()


class TestImportFailureIsExplained:
    """A missing optional solver has to say why, in the log the operator reads.

    The warning exists because the alternative is a scheduling feature that
    silently never produces a schedule — the failure mode the rest of the
    dependency work in this repository keeps running into.
    """

    def test_constructor_warns_naming_the_import_error(
        self,
        no_ortools: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(cpsat_module, "_ORTOOLS_IMPORT_ERROR", "No module named 'ortools'")
        with caplog.at_level(logging.WARNING, logger=cpsat_module.logger.name):
            CPSATScheduler()
        assert "OR-Tools not available" in caplog.text
        assert "No module named 'ortools'" in caplog.text

    def test_constructor_does_not_raise(
        self, no_ortools: None
    ) -> None:
        """Constructing must succeed.

        If the constructor raised, the documented degradation contract could never
        be reached — callers would have to guard the construction too, and the
        None/[] returns on the other two methods would be unreachable code.
        """
        CPSATScheduler(time_limit=1.0)
