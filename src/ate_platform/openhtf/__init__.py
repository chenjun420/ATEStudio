"""OpenHTF integration for ATE Platform.

This package provides the OpenHTFStepExecutor, which implements the
StepExecutor Protocol by wrapping OpenHTF's htf.Test execution model.
Test modules are imported dynamically via importlib, and htf.Test instances
are discovered via convention (module-level ``test`` variable or
``create_test()`` factory function).

The ``as_base_types`` function converts OpenHTF TestRecord objects to plain
dicts of base Python types for cross-process communication (Todo 22).
"""

from .serialization import as_base_types
from .step_executor import OpenHTFStepExecutor

__all__ = ["OpenHTFStepExecutor", "as_base_types"]
