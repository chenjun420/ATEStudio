"""Stable, deterministic identifiers for extracted knowledge-graph entities.

Every id is a pure function of stable business keys (product code,
requirement/case codes, DSL step id, execution id) so re-running extraction
MERGEs the same nodes and never duplicates — the idempotency contract.
"""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: object) -> str:
    """Deterministic lowercase slug for stable graph ids."""
    return _SLUG_RE.sub("_", str(text).lower()).strip("_")


def product_node_id(product_code: str) -> str:
    return f"product:{slug(product_code)}"


def requirement_node_id(product_code: str, requirement_code: str) -> str:
    return f"requirement:{slug(product_code)}:{slug(requirement_code)}"


def case_node_id(case_code: str) -> str:
    return f"case:{slug(case_code)}"


def step_node_id(case_code: str) -> str:
    """DSL step node for a case.

    Keyed by the case code (which embeds plan + step) so DSL and ATML cases
    never collide on identical step ids across plans.
    """
    return f"step:{slug(case_code)}"


def result_node_id(execution_id: str, step_id: str) -> str:
    return f"result:{slug(execution_id)}:{slug(step_id)}"


__all__ = [
    "case_node_id",
    "product_node_id",
    "requirement_node_id",
    "result_node_id",
    "slug",
    "step_node_id",
]
