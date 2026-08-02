"""Debug module for ATE Platform.

Provides debugpy-based breakpoint debugging for test script execution:
- DebugProcessExecutor: wraps ProcessExecutor, spawns child processes in
  debugpy --connect mode, captures variable snapshots on breakpoint hit.
- BreakpointManager: manages the breakpoint list for a debug session,
  handles X6 node serialization for canvas restoration.
"""

from .breakpoint_manager import (
    BreakpointData,
    BreakpointManager,
    new_breakpoint_id,
)
from .debug_executor import DebugProcessExecutor

__all__ = [
    "BreakpointData",
    "BreakpointManager",
    "DebugProcessExecutor",
    "new_breakpoint_id",
]
