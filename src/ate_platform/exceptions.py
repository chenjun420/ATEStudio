"""Custom exceptions for ATE Platform.

This module defines platform-specific exceptions for:
- Step execution timeouts
- Condition evaluation timeouts
- Resource acquisition failures
- Script execution errors
"""


class StepTimeoutError(Exception):
    """Raised when a step execution exceeds its timeout limit."""

    pass


class ConditionTimeoutError(Exception):
    """Raised when condition evaluation exceeds its timeout limit."""

    pass


class ResourceAcquireError(Exception):
    """Raised when a resource cannot be acquired.

    This can occur when:
    - The resource does not exist
    - The resource is already in use and not available
    - Permission denied to access the resource
    """

    pass


class ScriptExecutionError(Exception):
    """Raised when a script fails during execution.

    This can occur when:
    - The script file is not found
    - The script has syntax errors
    - The script raises an exception during execution
    """

    pass


class ExecutionAborted(Exception):  # noqa: N818 — 名称由 v41-gap-analysis T4 规格钦定
    """Raised when a step's ``on_failure='abort'`` policy terminates the run.

    :meth:`ScannerScheduler.handle_step_result` raises this after retries or
    repeats are exhausted (or when no retry/repeat policy applies) and the
    step's :class:`~ate_platform.scheduler.step_registry.StepExecutionConfig`
    declares ``on_failure='abort'``. Callers must let it propagate — a global
    try/except that swallows it would defeat the abort semantics.
    """

    pass
