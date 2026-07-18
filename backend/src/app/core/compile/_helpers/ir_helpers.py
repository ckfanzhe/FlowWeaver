"""Helpers that bridge the IR dataclass API to the dict-of-dicts shape
the rest of the generator uses.

The runtime executor and the code generator both consume `WorkflowIR`.
For historical reasons the per-emitter helpers read `node["type"]` and
`node["data"]` (a dict) — not the frozen `IRNode` dataclass. This module
is the one place that builds the dict-of-dicts view; everything else
can pretend the IR is still a raw `(nodes, edges)` payload.
"""
from __future__ import annotations

from typing import Any

from app.core.ir import WorkflowIR

# ─────────────────────────────────────────────────────────────────
# Dict view for legacy emitter code
# ─────────────────────────────────────────────────────────────────
def nodes_by_id_from_ir(ir: WorkflowIR) -> dict[str, dict[str, Any]]:
    """Return a `{nid: {id, type, data}}` dict for every node in the IR.

    Frozen dataclasses aren't iterable, so the conversion is explicit.
    `position` is intentionally left empty — emitters don't read it.
    """
    return {
        nid: {"id": nid, "type": n.type, "data": n.data, "position": {}}
        for nid, n in ir.node_map.items()
    }

# ─────────────────────────────────────────────────────────────────
# Target reference resolution
# ─────────────────────────────────────────────────────────────────
def target_ref(tid: str, nodes_by_id: dict[str, dict]) -> str:
    """Return the exported Python identifier for a node id.

    Mirrors the runtime's `_node_to_step` choice between Step / Router /
    Flow / Condition / Loop. Returns the literal string `"None"` if
    the target isn't in the graph (caller decides how to handle).

    : `flow` is mode-aware — emits `_<tid>_parallel`
    when `config.mode='parallel'`, `_<tid>_steps` otherwise. Matches
    `WorkflowIR.object_suffix()` so the runtime assembly and the
    generated source reference the same variable.
    """
    n = nodes_by_id.get(tid)
    if n is None:
        return "None"
    tt = n.get("type")
    if tt in ("agent", "ask"):
        return f"{tid}_step"
    if tt == "router":
        return f"{tid}_router"
    if tt == "flow":
        cfg = ((n.get("data") or {}).get("config") or {})
        mode = cfg.get("mode") or "parallel"
        suffix = "_parallel" if mode == "parallel" else "_steps"
        return f"{tid}{suffix}"
    if tt == "condition":
        return f"{tid}_condition"
    if tt == "loop":
        return f"{tid}_loop"
    # Default: most types emit a `_step` wrapper.
    return f"{tid}_step"