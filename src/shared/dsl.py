"""YAML DSL type definitions for ATE Platform.

This module defines data structures for YAML DSL:
- YamlStep: Represents a single step in the execution plan
- YamlPlan: Represents the complete execution plan from YAML
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class YamlStep:
    """Represents a single step in the execution plan."""

    id: str
    script: str
    params: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    timeout: int = 60
    retry: int = 0
    on_fail: str | None = None


@dataclass
class YamlPlan:
    """Represents the complete execution plan from YAML."""

    name: str
    version: str
    scope: str
    max_concurrency: int = 1
    steps: list[YamlStep] = field(default_factory=list)