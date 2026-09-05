"""Graph browse query for the knowledge-graph visualization (frontend task 25).

Reads nodes + relationships through the backend-agnostic
:class:`~ate_cloud.services.graph_service.GraphService` Protocol — no raw
FalkorDB driver here — and projects them into the UI-shaped
:class:`~ate_cloud.schemas.knowledge.GraphBrowse` payload
(``{nodes:[{id,label,type,properties}], edges:[{source,target,type}]}``).

Node rows are projected as SCALAR columns (``id`` / ``labels`` / ``name`` /
``properties``) rather than returning whole node maps: FalkorDB serializes a
node map into a positional/Node object that does not survive the
``_rows_to_dicts`` header-zip cleanly, whereas scalar columns map reliably on
both FalkorDB and the in-memory test fakes. Edge rows are scalar triples
(``source``/``target``/``type``) for the same reason.
"""

from __future__ import annotations

from typing import Any

from ate_cloud.schemas.knowledge import GraphBrowse, GraphEdge, GraphNode

#: Hard ceiling for a single browse payload (visualization sanity bound).
MAX_BROWSE_LIMIT = 500

_NODE_SCAN = (
    "MATCH (n) "
    "WHERE $label = '' OR $label IN labels(n) "
    "RETURN n.id AS id, labels(n) AS labels, coalesce(n.name, n.id) AS name, "
    "properties(n) AS properties "
    "LIMIT $limit"
)
_EDGE_SCAN = (
    "MATCH (a)-[r]->(b) "
    "RETURN a.id AS source, b.id AS target, type(r) AS type "
    "LIMIT $limit"
)


def _first_label(labels: Any) -> str:
    """Extract a single label from a Cypher ``labels(n)`` result.

    FalkorDB returns the label list; fakes may return a list or a bare
    string. Empty/None degrades to ``"Node"`` so the UI always has a type.
    """
    if isinstance(labels, (list, tuple)):
        return str(labels[0]) if labels else "Node"
    return str(labels) if labels else "Node"


def _project_node(row: dict[str, Any]) -> GraphNode | None:
    """Project one scalar node row into a GraphNode; skip rows without an id."""
    node_id = row.get("id")
    if node_id is None:
        return None
    label = _first_label(row.get("labels"))
    properties = row.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    name = str(row.get("name") or properties.get("name") or "")
    return GraphNode(
        id=str(node_id), label=label, type=label, name=name, properties=properties
    )


async def browse_graph(
    graph: Any, *, limit: int = 100, label: str | None = None
) -> GraphBrowse:
    """Read a bounded nodes+edges view from the graph service.

    Args:
        graph: A GraphService implementation (Protocol-typed at the caller).
        limit: Maximum node count (clamped to :data:`MAX_BROWSE_LIMIT`).
        label: Optional node-label filter; only nodes carrying that label are
            returned, and edges are pruned to those touching a returned node.

    Returns:
        A :class:`GraphBrowse` payload.

    Raises:
        Exception: Propagates graph-backend errors (callers map to 503); the
            app itself boots fine without a reachable graph.
    """
    bounded = max(1, min(int(limit), MAX_BROWSE_LIMIT))
    node_rows = await graph.query(
        _NODE_SCAN, {"label": label or "", "limit": bounded}
    )

    nodes: list[GraphNode] = []
    for row in node_rows:
        node = _project_node(row)
        if node is not None:
            nodes.append(node)

    edge_rows = await graph.query(
        _EDGE_SCAN, {"limit": max(bounded * 4, MAX_BROWSE_LIMIT)}
    )
    node_ids = {n.id for n in nodes}
    edges = [
        GraphEdge(
            source=str(r["source"]), target=str(r["target"]), type=str(r["type"])
        )
        for r in edge_rows
        if r.get("source") is not None
        and r.get("target") is not None
        and r.get("type") is not None
        # When a label filter is active, keep only edges incident on the
        # returned subgraph so the visualization has no dangling endpoints.
        and (not label or r["source"] in node_ids or r["target"] in node_ids)
    ]

    return GraphBrowse(nodes=nodes, edges=edges)


__all__ = ["MAX_BROWSE_LIMIT", "browse_graph"]
