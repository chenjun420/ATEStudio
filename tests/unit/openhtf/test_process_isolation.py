"""Unit tests for OpenHTFStepExecutor process isolation (Todo 22).

Tests cover the spawn-context integration that runs OpenHTF tests in a
child process when ``use_isolation=True``:

- ``test_uses_spawn_context``: verifies ``multiprocessing.get_context`` is
  called with ``"spawn"`` when ``use_isolation=True``.
- ``test_rejects_fork_context``: verifies a ``RuntimeError`` is raised when
  ``start_method="fork"`` is passed with ``use_isolation=True``.
- ``test_script_runs_in_separate_process``: a real spawn -- the child
  fails to import a nonexistent module and returns an ERROR StepResult;
  the PID stored on the executor proves the test ran in a separate process.

The default ``use_isolation=False`` path is covered by the existing tests
in ``test_step_executor.py``, ``test_testrecord_capture.py``,
``test_outcome_mapping.py``, and ``test_serialization.py`` -- this file
only exercises the isolated path.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from ate_platform.openhtf.step_executor import OpenHTFStepExecutor
from ate_platform.types import StepStatus


class TestProcessIsolation:
    """Verify spawn-context process isolation for OpenHTFStepExecutor."""

    def test_uses_spawn_context(self) -> None:
        """Executor with use_isolation=True obtains spawn context.

        Given: ``multiprocessing.get_context`` is patched.
        When: an executor is created with ``use_isolation=True``.
        Then: ``get_context`` is called exactly once with ``"spawn"``.
        """
        with patch(
            "ate_platform.openhtf.step_executor.multiprocessing.get_context"
        ) as mock_get_context:
            mock_get_context.return_value = MagicMock()
            OpenHTFStepExecutor(use_isolation=True)

        mock_get_context.assert_called_once_with("spawn")

    def test_rejects_fork_context(self) -> None:
        """Passing start_method='fork' with use_isolation=True raises RuntimeError.

        Given: ``use_isolation=True`` and ``start_method="fork"``.
        When: the executor is constructed.
        Then: ``RuntimeError`` is raised with a message mentioning spawn.
        """
        with pytest.raises(RuntimeError, match="Only spawn context is supported"):
            OpenHTFStepExecutor(use_isolation=True, start_method="fork")

    def test_script_runs_in_separate_process(self) -> None:
        """Isolated execution runs in a separate process.

        Given: an executor with ``use_isolation=True`` and a nonexistent
            module path.
        When: ``execute()`` is called.
        Then: the child process fails to import the module and returns an
            ERROR StepResult, and the PID stored on the executor is
            different from the parent's PID -- proving the test ran in a
            separate process.
        """
        executor = OpenHTFStepExecutor(use_isolation=True)
        result = executor.execute("nonexistent.module.12345", {})

        assert result.status is StepStatus.ERROR
        assert executor._last_child_pid is not None
        assert executor._last_child_pid != os.getpid()
