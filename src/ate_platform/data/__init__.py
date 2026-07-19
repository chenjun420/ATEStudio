"""Data persistence layer for ATE Platform.

This module provides caching and data storage capabilities:
- SQLiteCache: Async SQLite-based result cache with WAL mode
"""

from .cache import SQLiteCache

__all__ = ["SQLiteCache"]
