"""F3  — read-only inspection tools for the LLM.

Two tools that complement the F2 schema lookups:

  1. `get_graph_state` — "what's in the current graph?". Returns
     the staged workflow as a structured summary: node/edge counts,
     per-type breakdown, entry points, dead-ends, orphans. Built
     from `session.staged_nodes` / `session.staged_edges`. Read-only.

  2. `get_connection_rules` — "what can I connect to what?".
     Returns the connection rule table from `shared/connection_rules.json`,
     expanded so `@executable` / `@tool_source` aliases are resolved
     to concrete node-type lists. The LLM uses this before issuing
     a `plan_workflow` to avoid proposing edges the connection-
     rule table will reject.

Why this is a separate module from F2. F2 (chat_builder_schema.py)
describes ONE NODE TYPE in isolation — its config schema and
defaults. F3 describes TWO THINGS that the LLM needs to reason
about graph-level:
  * the CURRENT GRAPH state (what's there now), and
  * the GRAPH-LEVEL RULES (what's allowed between two nodes).

Together with F2, the LLM has the full picture before it issues a
plan: it knows the per-type config (F2), the current staged
state (F3.get_graph_state), and the connection rules (F3.
get_connection_rules).
"""
from __future__ import annotations

import functools
import json
from collections import Counter
from typing import Any, Optional

# Resolve the rule file via the same path the connection_rules.py
# loader uses (so we never read a stale copy). The loader is
# itself an `lru_cache`d function so importing it is cheap.
from app.core import connection_rules as _conn

# ─────────────────────────────────────────────────────────────────
# get_graph_state — what does the current graph look like?
# ─────────────────────────────────────────────────────────────────
def summarise_graph_state(
    nodes: list[dict],
    edges: list[dict],
) -> dict[str, Any]:
    """Build a structured summary of `nodes` + `edges`.

    The LLM gets:
      * `counts` — total nodes/edges and a per-type breakdown.
      * `entry_points` — nodes with no incoming dataflow edges.
      * `terminal_nodes` — nodes with no outgoing dataflow edges.
      * `orphans` — nodes with no dataflow edges in either direction.
        These are usually a sign the LLM forgot to wire something.
      * `per_node` — for every node: type, outgoing/incoming edge
        counts, a flag indicating whether it's an entry / terminal /
        dead-end (no edges at all).
      * `per_edge` — for every edge: source / target / kind.

    The output is JSON-serialisable. The LLM reads it to decide
    whether the graph needs more wiring before the user applies
    the diff.
    """
    # Group edges by source / target so the per-node lookup is O(1).
    out_edges: dict[str, list[str]] = {}
    in_edges: dict[str, list[str]] = {}
    for e in edges:
        if e.get("kind", "dataflow") != "dataflow":
            continue
        src = e.get("source")
        tgt = e.get("target")
        if src:
            out_edges.setdefault(src, []).append(tgt)
        if tgt:
            in_edges.setdefault(tgt, []).append(src)

    type_counts = Counter(n.get("type", "?") for n in nodes)

    per_node: list[dict[str, Any]] = []
    entry_points: list[str] = []
    terminal_nodes: list[str] = []
    orphans: list[str] = []

    for n in nodes:
        nid = n.get("id") or ""
        outgoing = out_edges.get(nid, [])
        incoming = in_edges.get(nid, [])
        ntype = n.get("type") or "?"
        is_entry = len(incoming) == 0
        is_terminal = len(outgoing) == 0
        is_orphan = is_entry and is_terminal
        if is_entry:
            entry_points.append(nid)
        if is_terminal:
            terminal_nodes.append(nid)
        if is_orphan:
            orphans.append(nid)
        per_node.append({
            "id": nid,
            "type": ntype,
            "label": (n.get("data") or {}).get("label", ""),
            "outgoing_count": len(outgoing),
            "incoming_count": len(incoming),
            "is_entry": is_entry,
            "is_terminal": is_terminal,
            "is_orphan": is_orphan,
        })

    per_edge: list[dict[str, Any]] = []
    for e in edges:
        per_edge.append({
            "id": e.get("id") or "",
            "source": e.get("source") or "",
            "target": e.get("target") or "",
            "kind": e.get("kind") or "dataflow",
        })

    return {
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "dataflow_edges": sum(
                1 for e in edges
                if e.get("kind", "dataflow") == "dataflow"
            ),
            "tool_attachment_edges": sum(
                1 for e in edges
                if e.get("kind") == "tool_attachment"
            ),
        },
        "type_counts": dict(type_counts),
        "entry_points": entry_points,
        "terminal_nodes": terminal_nodes,
        "orphans": orphans,
        "per_node": per_node,
        "per_edge": per_edge,
    }

def get_graph_state_tool(nodes: list[dict], edges: list[dict]) -> str:
    """Read-only: return the staged graph summary as JSON.

    Args:
        nodes: The session's `staged_nodes` (post-F0.2 snapshot).
        edges: The session's `staged_edges`.

    Returns:
        A JSON object with the keys documented in
        `summarise_graph_state`. The LLM uses this to verify a
        graph is fully wired before applying.
    """
    return json.dumps(
        summarise_graph_state(nodes, edges),
        ensure_ascii=False,
    )

# ─────────────────────────────────────────────────────────────────
# get_connection_rules — what's connectable to what?
# ─────────────────────────────────────────────────────────────────
# Note: aliases like `@executable` / `@tool_source` are already
# expanded by `connection_rules.py`'s loader into concrete
# frozensets of node-type names. We just walk the resolved table.

@functools.lru_cache(maxsize=1)
def summarise_connection_rules() -> dict[str, Any]:
    """Read the connection rule table once and produce a normalised
    summary the LLM can use directly.

    `connection_rules.py`'s loader already expands `@executable`
    / `@tool_source` aliases into concrete node-type frozensets,
    so we just walk the resolved table. The result is what
    `validate_connections` reads at validation time, so the
    output here is guaranteed to match what the validator
    actually allows.

    Returns:
        `{"by_type": {node_type: {...}, ...}, "groups": {...}}`
        where `by_type[node_type]` carries:
          * `allowed_source_types` — list of node-type names that
            may appear as the source of an outgoing edge from this
            type.
          * `allowed_target_types` — list of node-type names that
            may appear as the target of an incoming edge.
          * `max_outgoing` / `min_outgoing` / `max_incoming` /
            `min_incoming` — degree bounds (None = unbounded).
    """
    raw = _conn.EDGE_RULES
    # EDGE_RULES is a per-kind dict; collapse to the dataflow
    # rules (which is what the LLM cares about for plan_workflow).
    dataflow_rules = (
        raw.get("dataflow")
        if isinstance(raw, dict) and "dataflow" in raw
        else raw
    )
    by_type: dict[str, dict[str, Any]] = {}
    for node_type, rule in (dataflow_rules or {}).items():
        if node_type.startswith("_"):
            continue
        if rule is None:
            continue
        # ConnectionRule is a dataclass; read fields via getattr.
        def _get(key: str) -> Any:
            return getattr(rule, key, None)
        allowed_source = sorted(
            list(_get("allowed_source_types") or []),
        )
        allowed_target = sorted(
            list(_get("allowed_target_types") or []),
        )
        by_type[node_type] = {
            "allowed_source_types": allowed_source,
            "allowed_target_types": allowed_target,
            "max_outgoing": _get("max_outgoing"),
            "min_outgoing": _get("min_outgoing"),
            "max_incoming": _get("max_incoming"),
            "min_incoming": _get("min_incoming"),
        }
    groups = _load_groups_from_json()
    return {
        "by_type": by_type,
        "groups": groups,
    }

def _load_groups_from_json() -> dict[str, list[str]]:
    """Read `shared/connection_rules.json` for the `@group` aliases.

    The rule table's `allowed_source_types` / `allowed_target_types`
    are already expanded to concrete node-type lists by the loader,
    so this is only needed for the LLM to understand error messages
    that quote `@executable` / `@tool_source` directly.
    """
    from pathlib import Path
    shared = Path(__file__).resolve().parent.parent.parent.parent.parent / "shared"
    path = shared / "connection_rules.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return dict(raw.get("groups") or {})

def get_connection_rules_tool() -> str:
    """Read-only: return the connection rule table as JSON.

    The LLM calls this BEFORE issuing a `plan_workflow` to verify
    every edge's source/target types are allowed. Each entry
    reports `allowed_source_types` / `allowed_target_types` (with
    `@executable` / `@tool_source` aliases expanded to concrete
    node-type lists) plus degree bounds.

    Returns:
        A JSON object: `{"by_type": {...}, "groups": {...}}`.
    """
    return json.dumps(
        summarise_connection_rules(),
        ensure_ascii=False,
    )

__all__ = [
    "summarise_graph_state",
    "get_graph_state_tool",
    "summarise_connection_rules",
    "get_connection_rules_tool",
]