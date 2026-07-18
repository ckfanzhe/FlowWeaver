"""Graph utilities for workflow validation and traversal.

Workflow graphs are directed: edges go from `source` to `target`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

@dataclass
class Node:
    id: str
    type: str
    data: dict

@dataclass
class Edge:
    id: str
    source: str
    target: str
    sourceHandle: str | None = None
    targetHandle: str | None = None
    # Edge kind — drives which rule table the validator uses. The
    # default `None` is treated as `"dataflow"` everywhere downstream
    # so existing call sites (and JSON without the field) behave exactly
    # as before. introduced
    # `"tool_attachment"` for the `tool-source → agent` wiring that
    # replaces cfg.toolsRef. See `shared/connection_rules.json` for the
    # per-kind rule tables.
    kind: str | None = None

class GraphError(ValueError):
    """Raised when the workflow graph is malformed."""

def build_adjacency(nodes: Iterable[Node | dict], edges: Iterable[Edge | dict]):
    """Return (node_map, outgoing, incoming) for the graph."""
    node_map: dict[str, Node] = {}
    for n in nodes:
        node = n if isinstance(n, Node) else Node(id=n["id"], type=n["type"], data=n.get("data", {}))
        node_map[node.id] = node

    outgoing: dict[str, list[str]] = {nid: [] for nid in node_map}
    incoming: dict[str, list[str]] = {nid: [] for nid in node_map}
    for e in edges:
        edge = e if isinstance(e, Edge) else Edge(
            id=e["id"], source=e["source"], target=e["target"],
            sourceHandle=e.get("sourceHandle"), targetHandle=e.get("targetHandle"),
            kind=e.get("kind"),
        )
        if edge.source not in node_map:
            raise GraphError(f"edge {edge.id}: source {edge.source!r} is not a node")
        if edge.target not in node_map:
            raise GraphError(f"edge {edge.id}: target {edge.target!r} is not a node")
        outgoing[edge.source].append(edge.target)
        incoming[edge.target].append(edge.source)

    return node_map, outgoing, incoming

def edge_kind(edge) -> str:
    """Normalise an edge's `kind` field.

    The workflow graph now carries two edge
    kinds (`dataflow` and `tool_attachment`); absent / empty / None is
    treated as `"dataflow"` so legacy call sites / JSON without the
    field keep their prior semantics. Used by `ir.build_ir` to split
    edges before computing topo / branch / nested-children facts.
    """
    if isinstance(edge, dict):
        k = edge.get("kind")
    else:
        k = getattr(edge, "kind", None)
    if not k:
        return "dataflow"
    return str(k)

def split_edges_by_kind(edges: Iterable) -> dict[str, list]:
    """Bucket edges into per-kind lists. Edge kind defaults to
    `"dataflow"` — see `edge_kind()`."""
    out: dict[str, list] = {}
    for e in edges:
        k = edge_kind(e)
        out.setdefault(k, []).append(e)
    return out

def topo_sort(node_map: dict[str, Node], outgoing: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm. Raises GraphError on cycles."""
    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    for src, targets in outgoing.items():
        for _ in targets:
            in_degree[src] = in_degree.get(src, 0)  # ensure key
    # Compute in-degree from incoming edges
    incoming: dict[str, int] = {nid: 0 for nid in node_map}
    for src, targets in outgoing.items():
        for tgt in targets:
            incoming[tgt] = incoming.get(tgt, 0) + 1

    queue = [nid for nid, d in incoming.items() if d == 0]
    result: list[str] = []
    while queue:
        nid = queue.pop(0)
        result.append(nid)
        for tgt in outgoing.get(nid, []):
            incoming[tgt] -= 1
            if incoming[tgt] == 0:
                queue.append(tgt)
    if len(result) != len(node_map):
        raise GraphError("graph has a cycle")
    return result

def find_start(node_map: dict[str, Node], incoming: dict[str, list[str]]) -> str | None:
    """Pick the entry node — the node with no incoming edges.

    The workflow's input is supplied at run-time via `Workflow.run(input=...)`,
    so there is no dedicated entry node on the canvas. The first executable
    step is whichever node has no incoming edges.
    """
    for nid, ins in incoming.items():
        if not ins:
            return nid
    return None

def validate_workflow(nodes: list[dict], edges: list[dict]) -> None:
    """Light validation: known node types, edge references valid nodes, no cycle.

    Also delegates to `validate_connections` so callers get all the
    per-node-type connection rules (max outgoing edges, allowed sources,
    tool-source isolation, etc.) without an extra step.

    Reads the live manifest registry (`app.core.node_types.NODE_TYPES`)
    rather than the legacy `app.schemas.workflow.NODE_TYPES` tuple so
    preset names (`tavily_search` / `calculator` / …) are accepted
    without a tuple edit. The tuple is kept as a backwards-compatible
    alias for callers that still iterate the base 9 types.
    """
    from app.core.node_types import NODE_TYPES
    from app.core.connection_rules import validate_connections

    for n in nodes:
        if n["type"] not in NODE_TYPES:
            raise GraphError(f"unknown node type: {n['type']!r}")
    build_adjacency(nodes, edges)
    node_map, out, _ = build_adjacency(nodes, edges)
    topo_sort(node_map, out)

    errors = validate_connections(nodes, edges)
    if errors:
        # Raise the first error to preserve the existing `except
        # GraphError` callers; subsequent errors get joined into the
        # message so the API can show them all.
        msg = "; ".join(e.message for e in errors)
        raise GraphError(msg)