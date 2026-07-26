"""Workflow intermediate representation (IR) — single source of truth for graph semantics.

Background
----------
Before this module, two code paths each implemented their own traversal of
the same `(nodes, edges)` JSON:

  * `core.workflow_builder._build_steps_in_order` — runtime compilation
    into an agno `Workflow`.
  * `core.generator._assemble_workflow` — code generation for the exported
    `.py` file.

Both paths had to compute the same facts (entry node, topo order, branch
targets for parallel/condition/loop, "which nodes are nested inside a
compound so must not also appear at top level"). Keeping them in sync was
manual, and BUG_FIX history shows at least three recent regressions
(parallel-research-style doubling of branch children, conditional-greeting
ignoring the second edge, `loop` body executing twice) — all caused by
the two paths diverging.

This module fixes that by being the **only** place where graph semantics
are computed. The runtime and the generator both consume the resulting
`WorkflowIR` and never re-derive these facts themselves.

Public API
----------
  * `build_ir(nodes, edges) -> WorkflowIR` — top-level entry.
  * `WorkflowIR.top_level_step_ids()` — executable nodes at the top level,
    in topo order, with nested children already excluded.
  * `WorkflowIR.is_nested_child(nid)` — true for ids nested inside a
    parallel / condition / loop.
  * `WorkflowIR.get_branch_targets(nid)` — for flow: all branch targets
    in order (mode='parallel' or 'sequential'); for condition: (then, else);
    for loop: (body,); for any other type: outgoing edges.

Design choices
--------------
  * Pure data: `WorkflowIR` is a `@dataclass(frozen=True)` carrying
    pre-computed facts. No I/O, no side effects, no global state. The
    builder below is the only place that touches the raw `(nodes, edges)`
    shape.
  * One-pass construction: `build_ir` walks nodes + edges exactly once
    per category. Cheap enough for the workflow sizes this platform sees
    (≤ a few dozen nodes).
  * Validation: `build_ir` raises `GraphError` for malformed inputs
    (unknown node type, dangling edge, cycle). This is a strict subset of
    `validate_connections`; callers that want full rule validation should
    call `validate_connections` separately. We don't call it here to
    keep the IR builder dependency-light (no `connection_rules` import).
  * `nested_children` is **the** authoritative list of ids that must not
    appear at top level. Both consumers (runtime + generator) read this
    set; they no longer compute child ids themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.core.graph import (
    GraphError,
    Node,
    build_adjacency,
    edge_kind,
    find_start,
    split_edges_by_kind,
    topo_sort,
)

# Compound-node emitters bind their object to `<nid>_<suffix>` in the
# generated source. Executable nodes emit `<nid>_step`. Two consumers
# read this table (see `WorkflowIR.object_suffix` docstring):
#   - `compile._helpers.assembly.assemble_workflow` — top-level steps list
#   - `strategies.flow.FlowStrategy._branch_arg` — branch refs in
#     Parallel(...) / Steps(...)
#
# `parallel` and `steps` collapsed to `flow`.
# `flow`'s suffix is mode-aware — `object_suffix()` returns
# `_parallel` when `config.mode='parallel'` and `_steps` otherwise.
# That preserves the prior emitted shape (`nid_parallel = Parallel(...)`
# vs `nid_steps = Steps(...)`) so exported `.py` files and the runtime
# assembly stay unchanged for each mode.
#
# `router` and `condition` collapsed to `branch`.
# `branch`'s suffix is mode-aware — `_router` for `mode='switch'`
# (matches the prior `Router` primitive's emitted name) and
# `_condition` for `mode='if-else'` (matches the prior `Condition`
# primitive's emitted name). Byte-stable for both prior shapes.
_COMPOUND_OBJECT_SUFFIX: dict[str, str] = {
    "loop": "_loop",
    # `flow` and `branch` are handled specially by `object_suffix()`
    # based on `config.mode`.
    "flow": "_flow",
    "branch": "_branch",
}
_COMPOUND_OBJECT_DEFAULT_SUFFIX = "_step"
# Mode-specific suffixes for `flow` (see `object_suffix()`).
_FLOW_SUFFIX_BY_MODE: dict[str, str] = {
    "parallel": "_parallel",
    "sequential": "_steps",
}
# Mode-specific suffixes for `branch` (see `object_suffix()`).
_BRANCH_SUFFIX_BY_MODE: dict[str, str] = {
    "switch": "_router",
    "if-else": "_condition",
}

# ─────────────────────────────────────────────────────────────────
# Types the IR cares about (subset of `schemas.workflow.NNode_TYPES`)
# ─────────────────────────────────────────────────────────────────
# Executable = node that becomes a Step / Router / Flow / Condition / Loop /
# ask step in the compiled workflow AND a top-level emission in the
# generated code. Tool-source nodes (`tool` in any of `mcp` / `http` /
# `function` mode) are NOT executable — they're referenced by an Agent
# via `cfg.toolsRef` / `tool_attachments`. The `ask` node (kind
# `control_flow`) is still a top-level Step emission, so it stays in
# this set.
EXECUTABLE_TYPES: frozenset[str] = frozenset({
    "agent", "ask", "branch", "flow", "loop",
})

# Compound types — they own their children and emit a single object
# (Parallel / Steps / Router / Condition / Loop) that inlines them.
COMPOUND_TYPES: frozenset[str] = frozenset({"flow", "branch", "loop"})

# ─────────────────────────────────────────────────────────────────
# IR data structures
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IRNode:
    """A single node, normalised from the raw dict."""
    id: str
    type: str
    data: dict

@dataclass(frozen=True)
class WorkflowIR:
    """Authoritative graph semantics for a workflow.

    All consumers (runtime `workflow_builder`, generator `generator`) read
    from this object and never re-derive traversal facts. See module
    docstring for why this exists.
    """
    node_map: dict[str, IRNode]
    outgoing: dict[str, list[str]]
    incoming: dict[str, list[str]]
    topo_order: list[str]
    entry_id: str | None

    # Compound-node branch resolutions (the canonical targets).
    # `parallel_branches` and `steps_branches`
    # are gone — the merged `flow` node carries both modes via
    # `config.mode` and emits both runtime primitives (`Parallel`,
    # `Steps`) from one bucket.
    # `router_branches` and
    # `condition_branches` collapse to `branch_branches`. The shape is
    # `dict[str, list[str]]` — uniform across both modes. The
    # if-else semantics (then = branches[0], else = branches[1]) come
    # from `_resolve_condition_branches` and surface through
    # `get_branch_branches(nid)`.
    flow_branches: dict[str, list[str]]            # fid → [target_id, ...] (mode='parallel' or 'sequential')
    branch_branches: dict[str, list[str]]          # bid → [target_id, ...] (mode='switch' or 'if-else')
    loop_bodies: dict[str, str | None]             # lid → body_id (or None)

    # Authoritative set of ids nested inside a compound node. Consumers
    # must exclude these from top-level step lists.
    nested_children: frozenset[str] = field(default_factory=frozenset)

    # Per-agent → tool-node references (preserves order, deduped).
    tool_refs: dict[str, list[str]] = field(default_factory=dict)

    # Authoritative per-agent → tool-node references for the
    # runtime / generator. Derived from `kind="tool_attachment"` edges
    # ONLY (legacy `cfg.toolsRef` is mirrored to `tool_refs` above as
    # a back-compat shim). Edge order is preserved; duplicates are
    # dropped. When both are present, consumers (runtime / generator)
    # are expected to read `tool_attachments` first and fall back to
    # `tool_refs` only when the new table is empty for an agent —
    # which covers pre-migration workflows where `cfg.toolsRef` was
    # the only signal.
    tool_attachments: dict[str, list[str]] = field(default_factory=dict)

    # RAG / knowledge — per-agent → knowledge-node references derived
    # from `kind="knowledge_attachment"` edges. Mirrors the
    # `tool_attachments` shape but for the `knowledge=...` parameter
    # (NOT `tools=[...]`). Connection rule `max_incoming: 1` per
    # agent (see `shared/connection_rules.json`'s
    # `edge_kinds.knowledge_attachment.rules.agent.max_incoming`) keeps
    # the bucket at length ≤ 1 per agent_id; the runtime + generator
    # still treat it as a list for symmetry with `tool_attachments`.
    # See plan [[gleaming-munching-grove]] for the architectural
    # mirror rationale. No legacy `cfg.knowledgeRef` shim — RAG is a
    # fresh feature, no pre-migration workflows to back-fill.
    knowledge_attachments: dict[str, list[str]] = field(default_factory=dict)

    # ─────────────────────────────────────────────────────────────
    # Convenience queries
    # ─────────────────────────────────────────────────────────────
    def is_nested_child(self, nid: str) -> bool:
        return nid in self.nested_children

    def is_executable(self, nid: str) -> bool:
        n = self.node_map.get(nid)
        return n is not None and n.type in EXECUTABLE_TYPES

    def top_level_step_ids(self) -> list[str]:
        """Executable nodes that should appear at top level.

        Order: topological. `nested_children` are excluded.
        """
        out: list[str] = []
        for nid in self.topo_order:
            if not self.is_executable(nid):
                continue
            if nid in self.nested_children:
                continue
            out.append(nid)
        return out

    def object_suffix(self, nid: str) -> str:
        """Suffix for the emitted object variable of `nid` in generated
        source. Compound nodes use their `_parallel`/`_router`/etc.
        variant; executable nodes fall through to the bare `_step`
        suffix.

        Kernel origin: this helper replaces the
        `if type == 'parallel': ... elif type == 'router': ...` chains
        in `compile._helpers.assembly.assemble_workflow` and
        `strategies.flow.FlowStrategy._branch_arg`. Two consumers
        made it a kernel.

        `flow` is mode-aware — `_parallel` for
        `mode='parallel'`, `_steps` for `mode='sequential'`. Preserves
        the pre-merge emitted shape for each mode so existing exported
        `.py` files and runtime assembly stay unchanged.

        `branch` is mode-aware — `_router` for
        `mode='switch'` (preserves the prior `Router` shape) and
        `_condition` for `mode='if-else'` (preserves the prior
        `Condition` shape). Byte-stable for both prior shapes.
        """
        node = self.node_map.get(nid)
        if node is None:
            return _COMPOUND_OBJECT_DEFAULT_SUFFIX
        if node.type == "flow":
            cfg = _cfg(node)
            mode = cfg.get("mode") or "parallel"
            return _FLOW_SUFFIX_BY_MODE.get(mode, _COMPOUND_OBJECT_SUFFIX["flow"])
        if node.type == "branch":
            cfg = _cfg(node)
            mode = cfg.get("mode") or "switch"
            return _BRANCH_SUFFIX_BY_MODE.get(mode, _COMPOUND_OBJECT_SUFFIX["branch"])
        return _COMPOUND_OBJECT_SUFFIX.get(node.type, _COMPOUND_OBJECT_DEFAULT_SUFFIX)

    def get_branch_targets(self, nid: str) -> list[str]:
        """Branch targets for a compound node, in canonical order.

        * `flow` → all branches (preserving edge order); runtime
            primitive (`Parallel` or `Steps`) is chosen by `config.mode`
        * `branch` (mode='switch') → all branches (selector picks one)
        * `branch` (mode='if-else') → `[then, else]` (filtering Nones)
        * `loop` → `[body]` (filtering Nones)
        * any other node → its outgoing edges (fallback)

        Returns an empty list if the node has no resolvable branches.
        """
        node = self.node_map.get(nid)
        if node is None:
            return []
        if node.type == "flow":
            return list(self.flow_branches.get(nid, []))
        if node.type == "branch":
            cfg = _cfg(node)
            mode = cfg.get("mode") or "switch"
            if mode == "if-else":
                then_id, else_id = self.get_branch_branches(nid)
                return [t for t in (then_id, else_id) if t is not None]
            return list(self.branch_branches.get(nid, []))
        if node.type == "loop":
            body = self.loop_bodies.get(nid)
            return [body] if body else []
        # Fallback for non-compound nodes — the runtime / generator
        # use this for "post-loop continuation" etc.
        return list(self.outgoing.get(nid, []))

    def get_branch_branches(self, nid: str) -> tuple[str | None, str | None]:
        """`(then, else)` for a `branch` node — None if absent.

        Provided so callers that need the dual semantics (then vs else)
        — primarily `BranchStrategy._build_if_else` — can avoid
        re-deriving from `outgoing`. Falls back to `cfg.elseTarget`
        when the 2nd outgoing edge is missing (matches the prior
        `_resolve_condition_branches` behaviour).

        Replaces `get_condition_branches` after the
        `condition` → `branch` collapse. Switch-mode branches call
        `branch_branches.get(nid, [])` directly instead.
        """
        node = self.node_map.get(nid)
        if node is None:
            return None, None
        # Try the if-else-shaped slice first (most common path)
        branch_list = list(self.branch_branches.get(nid, []))
        then_id = branch_list[0] if len(branch_list) >= 1 else None
        else_id = branch_list[1] if len(branch_list) >= 2 else None
        # Back-compat: if there's no second outgoing edge but the user
        # configured `cfg.elseTarget`, honour that (matches prior
        # `_resolve_condition_branches` fallback).
        if not else_id:
            cfg = _cfg(node)
            else_id = cfg.get("elseTarget") or None
        return then_id, else_id

    def get_condition_branches(self, nid: str) -> tuple[str | None, str | None]:
        """Deprecated alias for `get_branch_branches`.

        Kept as a shim for callers that haven't migrated off the
        prior `condition` API. New code should use
        `get_branch_branches` directly. Will be removed once the
        upstream callers (chat builder / templates) finish migrating.
        """
        return self.get_branch_branches(nid)

# ─────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────
def _to_ir_node(n: Node | dict) -> IRNode:
    if isinstance(n, Node):
        return IRNode(id=n.id, type=n.type, data=dict(n.data or {}))
    return IRNode(
        id=n["id"],
        type=n["type"],
        data=dict(n.get("data") or {}),
    )

def _cfg(node: IRNode) -> dict:
    return (node.data.get("config") or {}) if isinstance(node.data, dict) else {}

# The `_resolve_condition_branches` helper was inlined into
# `WorkflowIR.get_branch_branches` (which reads `branch_branches[nid]`
# directly + falls back to `cfg.elseTarget`). Removed to keep the IR
# builder single-pass.

def _resolve_loop_body(
    nid: str,
    outgoing: list[str],
    cfg: dict,
) -> str | None:
    """Resolve a loop node's body target.

    Resolution order — matches the runtime's `_build_steps_in_order`:
      1. `cfg.bodyTarget` (preferred — explicit, unambiguous).
      2. First outgoing edge (fallback — when the user didn't configure
         a bodyTarget but did wire the loop to a node).

    Returning `None` is legal: a loop with no body is a no-op at runtime
    (the validator at save time flags it as `missingOutgoing`/`noThen`
    depending on type, but the IR builder stays tolerant).
    """
    body = cfg.get("bodyTarget") or None
    if not body and outgoing:
        body = outgoing[0]
    return body

def build_ir(nodes: Iterable[dict | Node], edges: Iterable[dict]) -> WorkflowIR:
    """Construct a `WorkflowIR` from raw workflow JSON.

    Raises `GraphError` (from `app.core.graph`) for:
      * edges referencing unknown nodes (source / target not in node_map)
      * cycles (topo sort fails)

    Does NOT raise for unknown node types — those are caught by
    `validate_connections` / `validate_workflow`. The IR builder is
    tolerant of node types it doesn't know about; it just won't put them
    in any compound-specific bucket.

    Edge kind dispatch:
      * Only `dataflow` edges (kind=None or "dataflow") contribute to
        `outgoing` / `incoming` / topo order / branch resolutions /
        nested_children. The graph topology stays the same as before
        the kind split.
      * `tool_attachment` edges go into a separate
        `tool_attachments[agent_id]` table. They are NOT counted
        toward an agent's `incoming` dataflow degree and so don't
        affect "noThen" / "missingIncoming" / etc.
    """
    # Normalise inputs and split edges by kind. `dataflow` is what
    # drives the workflow topology; `tool_attachment` is a separate
    # wiring layer that the IR surfaces to the runtime / generator
    # but never mixes into topo or branch facts.
    edges_list = list(edges)
    by_kind = split_edges_by_kind(edges_list)
    # Default `None` already mapped to "dataflow" by `split_edges_by_kind`.
    dataflow_edges = by_kind.get("dataflow") or []
    tool_edges = by_kind.get("tool_attachment") or []
    # RAG / knowledge — mirrors `tool_edges` but for the
    # `knowledge_attachment` edge kind. Same shape, same dedup
    # semantics, but consumed in `knowledge_attachments` (parallel to
    # `tool_attachments`). See plan [[gleaming-munching-grove]].
    knowledge_edges = by_kind.get("knowledge_attachment") or []

    node_map_raw, outgoing_raw, incoming_raw = build_adjacency(nodes, dataflow_edges)

    # Convert to IR nodes (immutable, normalised).
    ir_nodes: dict[str, IRNode] = {
        nid: _to_ir_node(n) for nid, n in node_map_raw.items()
    }

    # Topo sort. If there's no entry (every node has an incoming
    # edge — i.e. only cycles), topo_sort raises GraphError on its
    # own — no special-casing needed here. Callers that want a
    # "tolerant" build (e.g. tests constructing trivial graphs with
    # no incoming edges for every node) should arrange the graph
    # correctly; otherwise the cycle check catches the problem.
    entry_id = find_start(ir_nodes, incoming_raw)
    topo_order = topo_sort(ir_nodes, outgoing_raw)

    # Compute compound-node branch resolutions.
    flow_branches: dict[str, list[str]] = {}
    branch_branches: dict[str, list[str]] = {}
    loop_bodies: dict[str, str | None] = {}

    for nid, node in ir_nodes.items():
        out = outgoing_raw.get(nid, [])
        cfg = _cfg(node)
        if node.type == "flow":
            # `flow` covers both the prior `parallel` and
            # `steps` types — the runtime primitive is chosen by
            # `config.mode` at strategy-dispatch time, but the branch
            # list is the same (all outgoing edges in order).
            flow_branches[nid] = list(out)
        elif node.type == "branch":
            # `branch` covers both the prior `router` and
            # `condition` types. The branch list is the same — all
            # outgoing edges in order. Mode-specific semantics
            # (`switch` = choices, `if-else` = then/else) live in
            # `get_branch_branches` / `get_branch_targets`.
            branch_branches[nid] = list(out)
        elif node.type == "loop":
            loop_bodies[nid] = _resolve_loop_body(nid, out, cfg)
        elif node.type == "router":
            # Back-compat: legacy IR dicts may still carry
            # `router_branches`. Treat them as `branch_branches` so
            # legacy IR consumers keep reading. The `_compat` layer
            # rewrites the type string at envelope-load time so the
            # IR builder shouldn't normally see this case.
            branch_branches[nid] = list(out)
        elif node.type == "condition":
            # Same back-compat path for legacy `condition` rows.
            branch_branches[nid] = list(out)

    # nested_children = the union of all "owned" targets. This is THE
    # set that both consumers (runtime + generator) exclude from their
    # top-level step lists.
    nested: set[str] = set()
    for nid, branches in flow_branches.items():
        nested.update(branches)
    for nid, branches in branch_branches.items():
        nested.update(branches)
    for nid, body in loop_bodies.items():
        if body:
            nested.add(body)

    # tool_refs: per-agent → ordered tool-node ids from the legacy
    # `cfg.toolsRef` config field. Kept for back-compat with templates
    # / pre-migration workflows where the wiring wasn't yet captured
    # as a typed edge.
    tool_refs: dict[str, list[str]] = {}
    for nid, node in ir_nodes.items():
        if node.type != "agent":
            continue
        cfg = _cfg(node)
        refs = cfg.get("toolsRef") or []
        # Preserve user order, drop unknown / dup ids.
        seen: set[str] = set()
        ordered: list[str] = []
        for ref in refs:
            if ref in ir_nodes and ref not in seen:
                seen.add(ref)
                ordered.append(ref)
        if ordered:
            tool_refs[nid] = ordered

    # tool_attachments (): per-agent → tool-node
    # ids derived from `kind=tool_attachment` edges. When an agent has
    # BOTH a `cfg.toolsRef` entry and a typed edge for the same tool,
    # the edge wins (canonical source of truth); the cfg list is
    # surfaced separately as `tool_refs` for back-compat. Empty list
    # for an agent means "no attached tools" — distinct from
    # "agent has no entry at all" — but consumers typically handle
    # both with `.get(agent_id, [])`.
    tool_attachments: dict[str, list[str]] = {}
    agent_ids = {nid for nid, n in ir_nodes.items() if n.type == "agent"}
    if agent_ids:
        # First pass: build per-agent lists from typed edges. Preserve
        # the order in which edges were added to the workflow (which
        # is the order in which the user drew them — React Flow
        # appends to `edges` on each new connection).
        for e in tool_edges:
            tgt = e.get("target") if isinstance(e, dict) else e.target
            src = e.get("source") if isinstance(e, dict) else e.source
            if tgt not in agent_ids:
                continue
            if src not in ir_nodes:
                # Ignore dangling tool-edges — the connection
                # validator already rejects these on save; we silently
                # drop here so the IR builder is robust.
                continue
            bucket = tool_attachments.setdefault(tgt, [])
            if src not in bucket:
                bucket.append(src)

        # Second pass: pick up agents whose `cfg.toolsRef` lists
        # tools that no edge covers yet. This preserves behaviour for
        # pre-migration workflows and saves us from a hard migration
        # cutover — the cfg fallback is silently promoted to an
        # attachment record.
        for agent_id in agent_ids:
            cfg_refs = tool_refs.get(agent_id) or []
            existing = set(tool_attachments.get(agent_id) or [])
            for ref in cfg_refs:
                if ref in ir_nodes and ref not in existing:
                    tool_attachments.setdefault(agent_id, []).append(ref)
                    existing.add(ref)

    # knowledge_attachments (RAG ): per-agent → knowledge-node
    # ids derived from `kind=knowledge_attachment` edges. Connection
    # rule `max_incoming: 1` per agent keeps each bucket at length
    # ≤ 1, but the IR still stores a list for shape symmetry with
    # `tool_attachments` (consumers don't have to special-case single-
    # vs list-of-one). No back-compat cfg fallback — RAG is fresh,
    # no legacy workflows to upgrade.
    knowledge_attachments: dict[str, list[str]] = {}
    if agent_ids:
        for e in knowledge_edges:
            tgt = e.get("target") if isinstance(e, dict) else e.target
            src = e.get("source") if isinstance(e, dict) else e.source
            if tgt not in agent_ids:
                continue
            if src not in ir_nodes:
                # Dangling knowledge-edge — the connection validator
                # already rejects these on save; we silently drop here
                # so the IR builder is robust.
                continue
            bucket = knowledge_attachments.setdefault(tgt, [])
            if src not in bucket:
                bucket.append(src)

    return WorkflowIR(
        node_map=ir_nodes,
        outgoing=outgoing_raw,
        incoming=incoming_raw,
        topo_order=topo_order,
        entry_id=entry_id,
        flow_branches=flow_branches,
        branch_branches=branch_branches,
        loop_bodies=loop_bodies,
        nested_children=frozenset(nested),
        tool_refs=tool_refs,
        tool_attachments=tool_attachments,
        knowledge_attachments=knowledge_attachments,
    )