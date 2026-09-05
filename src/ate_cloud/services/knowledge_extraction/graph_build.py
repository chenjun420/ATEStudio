"""Build ontology KG nodes/edges for extracted requirements/cases/results.

Pure functions (no I/O): map extracted DSL plans, ATML-imported ORM rows and
recorded step outcomes onto the task-9 ontology vocabulary — labels
Product / TestRequirement / TestCase / TestStep / UUTResult and relationships
HAS_REQUIREMENT / VERIFIED_BY / HAS_STEP / PRODUCED_RESULT / RESULT_FOR.
Persistence (Cypher UNWIND/MERGE) lives in :mod:`kg_writer`.
"""

from __future__ import annotations

from collections.abc import Iterable

from ate_cloud.services.knowledge_extraction.dsl_extract import ExtractedPlan
from ate_cloud.services.knowledge_extraction.ids import (
    case_node_id,
    product_node_id,
    requirement_node_id,
    result_node_id,
    slug,
    step_node_id,
)
from ate_cloud.services.knowledge_extraction.kg_writer import KGEdge, KGNode
from ate_cloud.services.knowledge_extraction.recordings import RecordedStepResult


def product_node(product_code: str) -> KGNode:
    return KGNode(
        label="Product",
        node_id=product_node_id(product_code),
        name=product_code,
        props={"product_code": product_code},
    )


def dsl_graph(
    product_code: str, plan: ExtractedPlan
) -> tuple[list[KGNode], list[KGEdge]]:
    """Nodes/edges for one DSL plan: requirement + per-step case/TestStep."""
    product_id = product_node_id(product_code)
    req_id = requirement_node_id(product_code, plan.requirement_code)
    nodes: list[KGNode] = [
        product_node(product_code),
        KGNode(
            label="TestRequirement",
            node_id=req_id,
            name=plan.title,
            props={
                "requirement_code": plan.requirement_code,
                "source": "dsl",
                "plan": plan.plan_name,
            },
        ),
    ]
    edges: list[KGEdge] = [KGEdge(product_id, "HAS_REQUIREMENT", req_id)]
    for step in plan.steps:
        case_code = f"TC-DSL-{slug(plan.plan_name)}-{slug(step.step_id)}"
        case_id = case_node_id(case_code)
        step_id = step_node_id(case_code)
        nodes.append(KGNode(
            label="TestCase",
            node_id=case_id,
            name=f"{plan.plan_name}: {step.title}",
            props={"case_code": case_code, "step_id": step.step_id,
                   "source": "dsl", "status": "active"},
        ))
        nodes.append(KGNode(
            label="TestStep",
            node_id=step_id,
            name=step.step_id,
            props={"step_id": step.step_id, "step_type": step.step_type,
                   "sequence_index": step.sequence_index},
        ))
        edges.append(KGEdge(req_id, "VERIFIED_BY", case_id))
        edges.append(KGEdge(case_id, "HAS_STEP", step_id))
    return nodes, edges


def atml_graph(
    product_code: str, requirements: Iterable[tuple[str, str, str, str | None]],
    cases: Iterable[tuple[str, str, str, str, str | None]],
) -> tuple[list[KGNode], list[KGEdge]]:
    """Nodes/edges for ATML-imported rows.

    ``requirements`` yields ``(requirement_code, title, source, atml_ref)``;
    ``cases`` yields ``(case_code, title, requirement_code, status, atml_ref)``.
    """
    product_id = product_node_id(product_code)
    nodes: list[KGNode] = [product_node(product_code)]
    edges: list[KGEdge] = []
    req_node_by_code: dict[str, str] = {}
    for req_code, title, source, atml_ref in requirements:
        requirement_id = requirement_node_id(product_code, req_code)
        req_node_by_code[req_code] = requirement_id
        nodes.append(KGNode(
            label="TestRequirement",
            node_id=requirement_id,
            name=title,
            props={"requirement_code": req_code, "source": source,
                   "atml_ref": atml_ref},
        ))
        edges.append(KGEdge(product_id, "HAS_REQUIREMENT", requirement_id))
    for case_code, title, req_code, status, atml_ref in cases:
        case_id = case_node_id(case_code)
        nodes.append(KGNode(
            label="TestCase",
            node_id=case_id,
            name=title,
            props={"case_code": case_code, "source": "atml",
                   "status": status, "atml_ref": atml_ref},
        ))
        linked_requirement: str | None = req_node_by_code.get(req_code)
        if linked_requirement is not None:
            edges.append(KGEdge(linked_requirement, "VERIFIED_BY", case_id))
    return nodes, edges


def result_graph(
    case_code: str, product_code: str | None, recorded: RecordedStepResult
) -> tuple[list[KGNode], list[KGEdge]]:
    """Nodes/edges for one recorded step outcome (a UUTResult instance)."""
    result_id = result_node_id(recorded.execution_id, recorded.step_id)
    node = KGNode(
        label="UUTResult",
        node_id=result_id,
        name=f"{recorded.step_id} {recorded.outcome}",
        props={"outcome": recorded.outcome,
               "execution_id": recorded.execution_id,
               "step_id": recorded.step_id,
               "error": recorded.error,
               "standard": "IEEE 1636.1 TestResults"},
    )
    edges = [KGEdge(step_node_id(case_code), "PRODUCED_RESULT", result_id)]
    if product_code is not None:
        edges.append(KGEdge(result_id, "RESULT_FOR", product_node_id(product_code)))
    return [node], edges


__all__ = ["atml_graph", "dsl_graph", "product_node", "result_graph"]
