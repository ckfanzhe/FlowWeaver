"""Per-node connection rules — multi-kind validator.

The platform ships 6 base node types plus a handful of preset
tool types that ride on `tool` via a `preset` discriminator.
Three of the base types — `tool` (with source=http|mcp|function) —
are tool *sources*, and they participate in the workflow graph
through TWO distinct edge kinds:

  * `dataflow`        — control-flow edges between executable nodes
                        (existing behaviour, unchanged). `tools`/`mcp`/`http`
                        are NOT part of `dataflow` — they're definitions,
                        not steps. The `ask` node (kind=`control_flow`)
                        participates as a regular dataflow gate; its
                        edge shape is identical to agent↔agent.
  * `tool_attachment` — a typed-source edge from a tool-source node
                        INTO an agent. Doesn't enter the workflow topology,
                        only attaches the tool to the agent's `tools=[...]`
                        at runtime / export. Replaces the legacy
                        `cfg.toolsRef` string list.

The connection rules live in **JSON** (`connection_rules.json` next to
this file) so product / QA can read them without understanding the code.
Loading expands `@group` references (e.g. `"@executable"`) into
concrete type sets once at startup.

This module exposes three module-level rule tables (all populated by the
same `_load_all_rules()` lru_cache):

  * `CONNECTION_RULES`         — same shape as before:
                                   `{node_type: ConnectionRule}` for the
                                   `dataflow` kind. Kept as the
                                   backward-compat top-level so existing
                                   tests / imports keep working.
  * `TOOL_ATTACHMENT_RULES`    — `{node_type: ConnectionRule}` for the
                                   `tool_attachment` kind.
  * `EDGE_RULES`               — `{kind: {node_type: ConnectionRule}}`
                                   keyed by every kind declared in JSON.

Validation surfaces three classes of problem:

  1. Edge-level: `incompatibleSource` / `incompatibleTarget` / `selfLoop`
     / `duplicateEdge` — checked per-edge, per-kind.
  2. Node-level counts: `tooManyOutgoing` / `tooManyIncoming` /
     `noThen` — checked after counting degrees per kind.
  3. Workflow-level: `loopBodyViaEdge` — the loop's `cfg.bodyTarget`
     also has an incoming edge from the loop, which would make the
     runtime emit the body both at the top level AND inside the loop.
     Only meaningful for the `dataflow` kind.

`validate_connections(nodes, edges)` walks the edges, splits them by
`kind` (default `None` is treated as `dataflow`), and runs the
node-centric `check_node_view` once per kind with the matching rules
table.

Frontend (`frontend/src/lib/connectionValidation.ts`) mirrors this file
verbatim so the canvas can reject a drag *before* it commits.
"""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────
# Rule declaration
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConnectionRule:
    allowed_source_types: frozenset[str]   # who can have an outgoing edge INTO this node
    allowed_target_types: frozenset[str]   # who this node can have an outgoing edge TO
    max_outgoing: int | None               # None = unlimited
    min_outgoing: int
    min_incoming: int
    max_incoming: int | None               # None = unlimited

# ─────────────────────────────────────────────────────────────────
# Loader — reads connection_rules.json once, expands @group refs.
# ─────────────────────────────────────────────────────────────────
# The JSON lives at the repo's `shared/` root so the frontend can
# import the same file via Vite. Two consumers, one file — drift is
# no longer possible (see `scripts/check_connection_rules_consistency.py`
# which pins the agreement).
#
# `_RULES_PATH` resolves from this file's location: `backend/src/app/core/`
# → `backend/src/app/` → `backend/src/` → `backend/` → repo root → `shared/`.
_SHARED_RULES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "shared"
    / "connection_rules.json"
)
_RULES_PATH = _SHARED_RULES_PATH

@functools.lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    """Load and parse the JSON config. Cached forever — the file is
    frozen at startup. Raises if missing or malformed (fail-loud)."""
    with _RULES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)

@functools.lru_cache(maxsize=1)
def _load_groups() -> dict[str, frozenset[str]]:
    raw = _load_raw()
    return {
        name: frozenset(members)
        for name, members in raw.get("groups", {}).items()
    }

def _resolve_value(value: Any) -> frozenset[str]:
    """Expand a rule value: a `@group` string → that group's set, a
    list → frozenset of its strings (after expanding any `@group`).
    Anything else (None, missing) is left to the caller."""
    if isinstance(value, str):
        groups = _load_groups()
        if value.startswith("@"):
            name = value[1:]
            if name not in groups:
                raise ValueError(f"unknown group reference {value!r}")
            return groups[name]
        # Bare string = a single allowed type
        return frozenset({value})
    if isinstance(value, list):
        groups = _load_groups()
        out: set[str] = set()
        for item in value:
            if isinstance(item, str) and item.startswith("@"):
                name = item[1:]
                if name not in groups:
                    raise ValueError(f"unknown group reference {item!r}")
                out.update(groups[name])
            else:
                out.add(str(item))
        return frozenset(out)
    raise ValueError(f"unsupported set value: {value!r}")

def _build_rule(spec: dict[str, Any]) -> ConnectionRule:
    """One rule from JSON → `ConnectionRule`. Shared by every kind."""
    return ConnectionRule(
        allowed_source_types=_resolve_value(spec.get("allowed_source_types", [])),
        allowed_target_types=_resolve_value(spec.get("allowed_target_types", [])),
        max_outgoing=spec.get("max_outgoing"),
        min_outgoing=int(spec.get("min_outgoing", 0)),
        min_incoming=int(spec.get("min_incoming", 0)),
        max_incoming=spec.get("max_incoming"),
    )

def _is_doc_key(name: str) -> bool:
    """JSON keys prefixed with `_` are inline docs (e.g. `_doc_`), not
    rule entries. Skipped silently by the loader."""
    return name.startswith("_")

@functools.lru_cache(maxsize=1)
def _load_all_rules() -> dict[str, dict[str, ConnectionRule]]:
    """Load rule tables for every edge kind declared in JSON.

    Returns `{kind: {node_type: ConnectionRule}}`. The top-level `rules`
    block becomes the `dataflow` kind's table. `edge_kinds.<kind>.rules`
    blocks become their respective kind's table. Unknown / missing kinds
    are not pre-loaded; callers fall back to per-call provided rules.

    `name.startswith('_')` keys are skipped so JSON files can carry inline
    documentation alongside the rules.
    """
    raw = _load_raw()
    out: dict[str, dict[str, ConnectionRule]] = {}

    # top-level `rules` block = dataflow
    dataflow_rules: dict[str, ConnectionRule] = {}
    for type_name, spec in (raw.get("rules") or {}).items():
        if _is_doc_key(type_name):
            continue
        if not isinstance(spec, dict):
            continue
        dataflow_rules[type_name] = _build_rule(spec)
    out["dataflow"] = dataflow_rules

    # `edge_kinds.<kind>.rules` blocks = per-kind tables
    for kind_name, kind_body in (raw.get("edge_kinds") or {}).items():
        if _is_doc_key(kind_name) or not isinstance(kind_body, dict):
            continue
        kind_rules: dict[str, ConnectionRule] = {}
        for type_name, spec in (kind_body.get("rules") or {}).items():
            if _is_doc_key(type_name):
                continue
            if not isinstance(spec, dict):
                continue
            kind_rules[type_name] = _build_rule(spec)
        if kind_rules:
            out[kind_name] = kind_rules

    return out

# Module-level singletons. The lru_cache is process-lifetime so these
# are effectively const (tests can override via `rule_overrides=`).
#
# Backward compat: older callers do `CONNECTION_RULES[t]` — we keep
# that shape by re-exporting the dataflow table here.
CONNECTION_RULES: dict[str, ConnectionRule] = _load_all_rules().get("dataflow", {})
TOOL_ATTACHMENT_RULES: dict[str, ConnectionRule] = _load_all_rules().get("tool_attachment", {})
EDGE_RULES: dict[str, dict[str, ConnectionRule]] = _load_all_rules()

EXECUTABLE_TYPES: frozenset[str] = _load_groups().get("executable", frozenset())
TOOL_SOURCE_TYPES: frozenset[str] = _load_groups().get("tool_source", frozenset())

# ─────────────────────────────────────────────────────────────────
# Edge kind resolution — `None` (legacy) and missing field → dataflow.
# ─────────────────────────────────────────────────────────────────
def _kind_of(edge: Any) -> str:
    """Return the kind of an edge (dict, Pydantic model, dataclass).

    Defaults to `dataflow` so legacy edges without a `kind` field keep
    the existing semantics. Anything else is passed through verbatim.
    """
    if isinstance(edge, dict):
        k = edge.get("kind")
    else:
        k = getattr(edge, "kind", None)
    if k is None or k == "":
        return "dataflow"
    return str(k)

def _normalize_rule_overrides(rule_overrides: Any) -> dict[str, dict[str, ConnectionRule]] | None:
    """Tests override rules via the `rule_overrides=` kwarg. Two shapes
    are accepted:

      * legacy / dataflow-only: `{node_type: ConnectionRule}` — the
        loader fills the corresponding kind's table; if the kind is
        missing, we default to `dataflow`.
      * per-kind: `{kind_name: {node_type: ConnectionRule}}`.

    Returns the per-kind dict, or None if no override was passed.
    """
    if not rule_overrides:
        return None
    # Heuristic: if every value is a ConnectionRule, treat it as the
    # dataflow table. Otherwise it's already the per-kind shape.
    first_value = next(iter(rule_overrides.values()), None)
    if isinstance(first_value, ConnectionRule):
        return {"dataflow": rule_overrides}
    return rule_overrides

# ─────────────────────────────────────────────────────────────────
# Error type — mirrors the JSON shape returned by the API at 422.
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConnectionError:
    code: str                              # i18n key suffix, e.g. "incompatibleSource"
    node_id: str | None = None
    edge_id: str | None = None
    source_id: str | None = None
    target_id: str | None = None
    message: str = ""                      # human-readable fallback

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "message": self.message,
        }

# ─────────────────────────────────────────────────────────────────
# Node-centric view (the per-kind validator passes these around).
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NodeView:
    """Per-node aggregated view the caller has already computed.

    `inputs`  = ids of nodes with edges INTO this node.
    `outputs` = ids of nodes this node has edges TO.
    `body_target` = optional loop body target (only meaningful when
                    type == "loop"). Pass the loop's `cfg.bodyTarget` here
                    so the checker can flag `loopBodyViaEdge`.
    """
    type: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    body_target: str | None = None

def _closed_rule() -> ConnectionRule:
    """A fully-closed rule: no edges in or out, none required.

    Used as the default when a kind declares no entry for a given
    node type. Without it `check_node_view` would emit a spurious
    "unknown type" error for every node whose kind hasn't been
    declared (e.g. `loop` under the `tool_attachment` kind).
    """
    return ConnectionRule(
        allowed_source_types=frozenset(),
        allowed_target_types=frozenset(),
        max_outgoing=0,
        min_outgoing=0,
        min_incoming=0,
        max_incoming=0,
    )

def check_node_view(
    nodes: dict[str, NodeView],
    *,
    rules: dict[str, ConnectionRule] | None = None,
    error_on_unknown: bool = True,
) -> list[ConnectionError]:
    """Pure-function check on a node-centric view.

    Example input (the shape the user described):
        {
          "node1": NodeView(type="agent",       inputs=["node3","node4"], outputs=["node2"]),
          "node2": NodeView(type="ask", inputs=["node5"],         outputs=["node6"]),
        }

    Returns the same `ConnectionError` list as `validate_connections`.
    Does NOT re-derive the view — the caller passes it pre-computed, so
    the runtime / generator / AGUI adapter doesn't have to rebuild an
    edge list.

    `error_on_unknown` (default True): when a node's type isn't
    declared in `rules`, emit an "unknown type" error. Pass
    `error_on_unknown=False` for kinds like `tool_attachment` that
    deliberately omit most node types — there "missing rule entry"
    means "this kind doesn't apply", not "this type is invalid".
    """
    rules = rules or CONNECTION_RULES
    errors: list[ConnectionError] = []

    # ── 1. Per-node checks.
    for nid, view in nodes.items():
        rule = rules.get(view.type)
        if rule is None:
            if error_on_unknown:
                errors.append(ConnectionError(
                    code="incompatibleSource",
                    node_id=nid,
                    message=f"node {nid!r}: unknown type {view.type!r}",
                ))
                continue
            # Type not declared under this kind — fully closed
            # (no edges allowed). The runner already validated
            # globally-known node types before dispatching.
            rule = _closed_rule()

        # ── 2. Inputs: every incoming source must be a type this node accepts.
        for src in view.inputs:
            src_view = nodes.get(src)
            if src_view is None:
                errors.append(ConnectionError(
                    code="incompatibleSource",
                    node_id=nid, source_id=src,
                    message=f"node {nid!r}: input {src!r} not found",
                ))
                continue
            if src_view.type not in rule.allowed_source_types:
                errors.append(ConnectionError(
                    code="incompatibleSource",
                    node_id=nid, source_id=src,
                    message=(
                        f"node {nid!r} ({view.type}) cannot accept input from "
                        f"{src!r} ({src_view.type})"
                    ),
                ))

        # ── 3. Outputs: every target must be a type this node can reach,
        #       AND the target's own type must accept this source.
        for tgt in view.outputs:
            tgt_view = nodes.get(tgt)
            if tgt_view is None:
                errors.append(ConnectionError(
                    code="incompatibleTarget",
                    node_id=nid, target_id=tgt,
                    message=f"node {nid!r}: output target {tgt!r} not found",
                ))
                continue

            # 3a. Source can't have outgoing edges at all?
            if not rule.allowed_target_types:
                errors.append(ConnectionError(
                    code="incompatibleSource",
                    node_id=nid, source_id=nid, target_id=tgt,
                    message=(
                        f"node {nid!r} ({view.type}) cannot be the source of an edge; "
                        f"tool-source nodes must be wired via cfg.toolsRef"
                    ),
                ))
                continue

            # 3b. Target's own type forbids being wired at all? Check
            #     FIRST so the user gets a clearer "can't wire TO"
            #     message when the target is a tool-source.
            tgt_rule = rules.get(tgt_view.type)
            if tgt_rule is None:
                tgt_rule = _closed_rule()
            if not tgt_rule.allowed_source_types:
                errors.append(ConnectionError(
                    code="incompatibleTarget",
                    node_id=nid, target_id=tgt,
                    message=(
                        f"node {tgt!r} ({tgt_view.type}) cannot be the target of an edge"
                    ),
                ))
                continue

            # 3c. Target's type not in source's allowed targets?
            if tgt_view.type not in rule.allowed_target_types:
                errors.append(ConnectionError(
                    code="incompatibleSource",
                    node_id=nid, source_id=nid, target_id=tgt,
                    message=(
                        f"node {nid!r} ({view.type}) cannot connect to "
                        f"{tgt!r} ({tgt_view.type})"
                    ),
                ))
                continue

        # ── 4. Self-loop.
        for tgt in view.outputs:
            if tgt == nid:
                errors.append(ConnectionError(
                    code="selfLoop",
                    node_id=nid, source_id=nid, target_id=tgt,
                    message=f"node {nid!r} cannot connect to itself",
                ))

        # ── 5. Degree bounds.
        out_deg = len(view.outputs)
        in_deg = len(view.inputs)
        if rule.max_outgoing is not None and out_deg > rule.max_outgoing:
            errors.append(ConnectionError(
                code="tooManyOutgoing",
                node_id=nid,
                message=(
                    f"node {nid!r} ({view.type}) has {out_deg} outgoing edges; "
                    f"max is {rule.max_outgoing}"
                ),
            ))
        if rule.min_outgoing > out_deg:
            errors.append(ConnectionError(
                code="noThen" if view.type == "condition" else "missingOutgoing",
                node_id=nid,
                message=(
                    f"node {nid!r} ({view.type}) has {out_deg} outgoing edges; "
                    f"min is {rule.min_outgoing}"
                ),
            ))
        if rule.max_incoming is not None and in_deg > rule.max_incoming:
            errors.append(ConnectionError(
                code="tooManyIncoming",
                node_id=nid,
                message=(
                    f"node {nid!r} ({view.type}) has {in_deg} incoming edges; "
                    f"max is {rule.max_incoming}"
                ),
            ))
        if rule.min_incoming > in_deg:
            errors.append(ConnectionError(
                code="missingIncoming",
                node_id=nid,
                message=(
                    f"node {nid!r} ({view.type}) has {in_deg} incoming edges; "
                    f"min is {rule.min_incoming}"
                ),
            ))

        # ── 6. Loop-body via edge.
        if view.type == "loop" and view.body_target and view.body_target in view.outputs:
            errors.append(ConnectionError(
                code="loopBodyViaEdge",
                node_id=nid,
                source_id=nid,
                target_id=view.body_target,
                message=(
                    f"loop {nid!r} has bodyTarget={view.body_target!r} but also an outgoing edge to it; "
                    f"the body would execute twice. Either remove the edge or clear bodyTarget."
                ),
            ))

    # ── 7. Duplicate edges (same pair appears twice).
    seen: set[tuple[str, str]] = set()
    for nid, view in nodes.items():
        for tgt in view.outputs:
            pair = (nid, tgt)
            if pair in seen:
                errors.append(ConnectionError(
                    code="duplicateEdge",
                    node_id=nid,
                    source_id=nid, target_id=tgt,
                    message=f"duplicate edge {nid!r} → {tgt!r}",
                ))
            seen.add(pair)

    return errors

# ─────────────────────────────────────────────────────────────────
# Single-edge validator (drag-time / drop-time UX)
# ─────────────────────────────────────────────────────────────────
def would_be_valid_connection(
    source_id: str,
    target_id: str,
    nodes: list[dict],
    edges: list[dict],
    *,
    rules: dict[str, ConnectionRule] | None = None,
    kind: str = "dataflow",
) -> list[ConnectionError]:
    """Check whether adding the candidate edge `source_id → target_id`
    to the graph would violate any rule **that is caused by the
    candidate itself**.

    The full `validate_connections` also surfaces workflow-level
    problems like `missingOutgoing` / `loopBodyViaEdge` that EVERY
    candidate edge "fails" because the graph is incomplete during a
    drag. That's not useful for the canvas's "which targets are
    reachable?" view — you'd see every node dimmed even when the
    candidate edge itself is perfectly fine.

    This function only checks rules that the candidate could cause:

      * `selfLoop`           — source == target
      * `duplicateEdge`      — that exact (src, tgt) pair already exists
      * `incompatibleSource` — source's type can't have outgoing edges
                                 OR the source's allowed_target_types
                                 doesn't include the target's type
      * `incompatibleTarget` — the target's type can't be wired to
                                 (e.g. tool-source under dataflow rules)
      * `tooManyOutgoing`    — the candidate would push source over
                                 its max outgoing
      * `tooManyIncoming`    — the candidate would push target over
                                 its max incoming

    Workflow-level checks like `loopBodyViaEdge` are deliberately
    excluded — they describe the graph as a whole, not the candidate.

    `kind` selects which rule table to apply:
    `"dataflow"` (default, legacy behaviour) or `"tool_attachment"`.
    Returns an empty list if the candidate is legal.
    """
    # `kind` is intentionally the public kwarg here; older callers
    # don't pass it and stay on the dataflow rules.
    rules = rules or EDGE_RULES.get(kind) or (
        CONNECTION_RULES if kind == "dataflow" else TOOL_ATTACHMENT_RULES
    )
    errors: list[ConnectionError] = []

    if not source_id or not target_id:
        return errors
    if source_id == target_id:
        errors.append(ConnectionError(
            code="selfLoop",
            source_id=source_id,
            target_id=target_id,
            message=f"node {source_id!r} cannot connect to itself",
        ))
        return errors

    # Build a minimal lookup so we can get the types of source/target.
    by_id: dict[str, dict] = {n.get("id"): n for n in nodes if n.get("id")}
    src_node = by_id.get(source_id)
    tgt_node = by_id.get(target_id)
    if src_node is None or tgt_node is None:
        # Either the source or the target node doesn't exist in the
        # graph yet — it's not a valid edge. We use the same code
        # `validate_connections` would emit.
        errors.append(ConnectionError(
            code="incompatibleSource" if src_node is None else "incompatibleTarget",
            source_id=source_id,
            target_id=target_id,
            message=(
                f"node {source_id!r} not found" if src_node is None
                else f"node {target_id!r} not found"
            ),
        ))
        return errors

    src_type = _type_of(src_node)
    tgt_type = _type_of(tgt_node)
    src_rule = rules.get(src_type)
    tgt_rule = rules.get(tgt_type)
    if src_rule is None:
        src_rule = _closed_rule()
    if tgt_rule is None:
        tgt_rule = _closed_rule()

    # – 3a. Source has no outgoing edges at all (tool-source under
    #       dataflow, or agent under tool_attachment) ?
    if not src_rule.allowed_target_types:
        errors.append(ConnectionError(
            code="incompatibleSource",
            source_id=source_id,
            target_id=target_id,
            node_id=source_id,
            message=(
                f"node {source_id!r} ({src_type}) cannot be the source of an edge; "
                f"tool-source nodes must be wired via cfg.toolsRef"
                if kind == "dataflow"
                else f"node {source_id!r} ({src_type}) cannot be the source of a {kind} edge"
            ),
        ))
        return errors

    # – 3b. Target's type refuses to be wired (tool-source under
    #       dataflow, agent under tool_attachment) ?
    if not tgt_rule.allowed_source_types:
        errors.append(ConnectionError(
            code="incompatibleTarget",
            source_id=source_id,
            target_id=target_id,
            node_id=target_id,
            message=(
                f"node {target_id!r} ({tgt_type}) cannot be the target of an edge"
            ),
        ))
        return errors

    # – 3c. Target's type not in source's allowed targets ?
    if tgt_type not in src_rule.allowed_target_types:
        errors.append(ConnectionError(
            code="incompatibleSource",
            source_id=source_id,
            target_id=target_id,
            node_id=source_id,
            message=(
                f"node {source_id!r} ({src_type}) cannot connect to "
                f"{target_id!r} ({tgt_type})"
            ),
        ))
        return errors

    # – 4. Duplicate edge: same (src, tgt) pair already exists in the
    #       edges list AND with the same kind as the candidate?
    for e in edges:
        if (
            e.get("source") == source_id
            and e.get("target") == target_id
            and _kind_of(e) == kind
        ):
            errors.append(ConnectionError(
                code="duplicateEdge",
                edge_id=e.get("id"),
                source_id=source_id,
                target_id=target_id,
                message=f"edge {e.get('id')!r}: duplicate of an existing edge",
            ))
            return errors

    # – 5. Degree counts: would adding this edge push source/target
    #       over their max? We count EXISTING edges of the same kind
    #       only (the candidate itself isn't in the graph yet, and
    #       dataflow / tool_attachment are independent counters).
    src_out_count = sum(
        1 for e in edges
        if e.get("source") == source_id and _kind_of(e) == kind
    )
    tgt_in_count = sum(
        1 for e in edges
        if e.get("target") == target_id and _kind_of(e) == kind
    )
    if src_rule.max_outgoing is not None and src_out_count + 1 > src_rule.max_outgoing:
        errors.append(ConnectionError(
            code="tooManyOutgoing",
            node_id=source_id,
            message=(
                f"node {source_id!r} ({src_type}) has {src_out_count} outgoing edges; "
                f"max is {src_rule.max_outgoing}"
            ),
        ))
    if tgt_rule.max_incoming is not None and tgt_in_count + 1 > tgt_rule.max_incoming:
        errors.append(ConnectionError(
            code="tooManyIncoming",
            node_id=target_id,
            message=(
                f"node {target_id!r} ({tgt_type}) has {tgt_in_count} incoming edges; "
                f"max is {tgt_rule.max_incoming}"
            ),
        ))

    return errors

# ─────────────────────────────────────────────────────────────────
# Edge-centric API (legacy / curried form)
# ─────────────────────────────────────────────────────────────────
def _type_of(node: dict) -> str:
    return (node.get("type") or "").strip()

def _config(node: dict) -> dict:
    return (node.get("data") or {}).get("config") or {}

def validate_connections(
    nodes: list[dict],
    edges: list[dict],
    *,
    rule_overrides: dict[str, Any] | None = None,
) -> list[ConnectionError]:
    """Return a list of `ConnectionError`s. Empty list = graph is valid.

    `rule_overrides` is a test-only knob — production callers pass None.

    Per-kind dispatch:
      1. Split edges by `edge.kind` (defaulting None → `"dataflow"`).
      2. For each kind with at least one edge, build a kind-specific
         `NodeView` and run `check_node_view` with the matching rules.
      3. Cross-kind duplicate edges (same (src, tgt) regardless of kind)
         are flagged once at the end.

    Tests may pass `rule_overrides` as either:
      * `{node_type: ConnectionRule}` — legacy shape, treated as the
        `dataflow` table.
      * `{kind_name: {node_type: ConnectionRule}}` — per-kind.
    """
    rules_by_kind = _normalize_rule_overrides(rule_overrides) or EDGE_RULES

    # Lazy import to avoid a circular import at module load time
    # (schemas.workflow pulls Pydantic which is fine, but historically
    # the validator module had to stay dependency-light for CLI scripts).
    # Read the manifest registry instead of the legacy
    # `app.schemas.workflow.NODE_TYPES` tuple. The tuple predates
    # `extends:` and only lists the 9 base types; preset names
    # (`tavily_search` / `calculator` / …) come from the live manifest.
    from app.core.node_types import NODE_TYPES as _REGISTRY_NODE_TYPES

    # Globally unknown node types are surfaced once at the entry point.
    # Per-kind rules don't carry the "is this type really a known node
    # type at all?" signal — a type simply missing from a kind's table
    # means "this kind doesn't apply to that type", not "the type is
    # invalid".
    errors: list[ConnectionError] = []
    for n in nodes:
        ntype = _type_of(n)
        if ntype and ntype not in _REGISTRY_NODE_TYPES:
            errors.append(ConnectionError(
                code="incompatibleSource",
                node_id=n.get("id"),
                message=f"node {n.get('id')!r}: unknown type {ntype!r}",
            ))

    # Duplicate node id detection — must come first. The view builder
    # uses a dict (which silently overwrites) so calling `check_node_view`
    # on a list with duplicate ids would mask the problem with a
    # misleading workflow-level error.
    seen_ids: set[str] = set()
    for n in nodes:
        nid = n.get("id") or ""
        if not nid:
            continue
        if nid in seen_ids:
            return [ConnectionError(
                code="duplicateNodeId",
                node_id=nid,
                message=f"duplicate node id {nid!r}",
            )]
        seen_ids.add(nid)

    # Build a single lookup of node bodies.
    by_id: dict[str, dict] = {n.get("id"): n for n in nodes if n.get("id")}

    # Per-kind edge bucket.
    by_kind: dict[str, list[dict]] = {}
    for e in edges:
        k = _kind_of(e)
        by_kind.setdefault(k, []).append(e)

    # Validate each kind with its matching rules table. We iterate the
    # kind set including any test-injected rule overrides so a kind only
    # declared via `rule_overrides` still gets visited.
    #
    # `dataflow` is always visited (so degree bounds / noThen / etc.
    # fire even when there are zero edges — the prior behaviour pre-1.A).
    # Other kinds are visited only when at least one edge carries them,
    # since their degree bounds target tool-source ↔ agent relationships
    # that can't trigger in edge-free workflows.
    kinds_to_check = ["dataflow"] + sorted(
        k for k in (set(by_kind.keys()) | set(rules_by_kind.keys())) if k != "dataflow"
    )
    for kind in kinds_to_check:
        kind_edges = by_kind.get(kind) or []
        # `dataflow` always validates (degree bounds, noThen etc.),
        # even with zero edges. Other kinds opt out when no edges AND
        # the kind's rules have no min_* constraints worth reporting.
        if kind != "dataflow" and not kind_edges:
            continue
        kind_rules = rules_by_kind.get(kind)
        if not kind_rules:
            # No rules defined for this kind — accept silently (the
            # kind itself is unknown; not the source/target types).
            continue

        # Index per-kind edge ids so we can tag errors with `edge_id`.
        edge_ids: dict[tuple[str, str], str] = {}
        for e in kind_edges:
            pair = (e.get("source") or "", e.get("target") or "")
            if pair not in edge_ids:
                edge_ids[pair] = e.get("id") or ""

        # Build a NodeView for this kind's edges only.
        views: dict[str, NodeView] = {}
        for n in nodes:
            nid = n.get("id") or ""
            if not nid:
                continue
            cfg = _config(n)
            views[nid] = NodeView(
                type=_type_of(n),
                inputs=[],
                outputs=[],
                body_target=cfg.get("bodyTarget") if _type_of(n) == "loop" else None,
            )
        for e in kind_edges:
            s = e.get("source") or ""
            t = e.get("target") or ""
            if s in views:
                views[s].outputs.append(t)
            if t in views:
                views[t].inputs.append(s)

        kind_errs = check_node_view(views, rules=kind_rules, error_on_unknown=False)

        # Re-tag with edge_id where possible.
        tagged: list[ConnectionError] = []
        for err in kind_errs:
            pair = (err.source_id, err.target_id)
            eid = edge_ids.get(pair)
            if eid and err.edge_id is None:
                tagged.append(ConnectionError(
                    code=err.code,
                    node_id=err.node_id,
                    edge_id=eid,
                    source_id=err.source_id,
                    target_id=err.target_id,
                    message=err.message,
                ))
            else:
                tagged.append(err)
        errors.extend(tagged)

    # Cross-kind duplicate edge check: same (src, tgt) regardless of kind.
    seen_pairs: set[tuple[str, str]] = set()
    for e in edges:
        s = e.get("source") or ""
        t = e.get("target") or ""
        pair = (s, t)
        if pair in seen_pairs:
            errors.append(ConnectionError(
                code="duplicateEdge",
                edge_id=e.get("id"),
                source_id=s,
                target_id=t,
                message=f"edge {e.get('id')!r}: duplicate of an existing edge",
            ))
        seen_pairs.add(pair)

    return errors
