"""Executor module for ATE Platform.

This module provides context proxy and execution utilities for test scripts.
"""

from .context_proxy import ContextProxy, measure
from .process_executor import ProcessExecutor

__all__ = ["ContextProxy", "measure", "ProcessExecutor"]
