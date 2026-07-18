"""F1  — declarative Plan DSL for the chat builder.

Replaces (and subsumes) the imperative `add_node` / `connect_nodes` /
`update_node` / `remove_node` / `disconnect` tool surface. The LLM
describes the TARGET STATE in one call instead of issuing 5–10
incremental mutations; the backend validates the entire plan against
the manifest + connection-rule table + Pydantic config schemas BEFORE
touching staged state.

Three things to keep in mind when reading this file:

1. **Atomicity.** A plan either commits entirely or not at all. The
   chat builder runs validation against a SNAPSHOT — if it fails,
   `session.staged_*` is untouched. This is the same copy-on-write
   contract `_atomic_stage` enforces for the imperative tools (F0.2),
   but at a higher granularity (whole-batch instead of single-call).

2. **Structured errors.** Every validation failure surfaces as an
   `Issue {path, code, message, hint}`. The LLM sees the same shape
   whether the failure came from a schema validator (Pydantic), a
   connection-rule check, or a graph-rule check (cycle, orphan). The
   `code` is an enum value the LLM can match against in its
   self-correction loop; the `path` is JSONPath-style so it can
   pinpoint which entry in `plan.nodes` / `plan.edges` was bad.

3. **Single source of truth for node types.** `IssueCode` strings
   are kept in sync with `connection_rules.ConnectionError.code`
   values (`incompatibleSource`, `duplicateEdge`, etc.) so the LLM
   can pattern-match across both tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.connection_rules import ConnectionError as _ConnError
from app.core.graph import GraphError
from app.core.node_types import NODE_TYPES as MANIFEST_NODE_TYPES
from app.schemas.node_configs import (
    _did_you_mean,
    get_strict_schema,
    validate_node_config,
)
from app.schemas.workflow import WorkflowEdge, WorkflowNode

# ─────────────────────────────────────────────────────────────────
# Issue codes — what the LLM sees on failure
# ─────────────────────────────────────────────────────────────────
class IssueCode(str, Enum):
    """Enumeration of every structured error the plan validator can
    emit. Values are short, snake_case, stable — the LLM may match
    against them in its self-correction loop.

    The set is a SUPERSET of `connection_rules.ConnectionError.code`
    so the LLM only has to learn one vocabulary. Where a
    `ConnectionError` code already exists, we reuse its exact string
    value so the two systems stay aligned.
    """

    # ── Schema: Pydantic per-node config errors ─────────────────
    UNKNOWN_NODE_TYPE = "unknownNodeType"
    INVALID_CONFIG = "invalidConfig"
    MISSING_REQUIRED_FIELD = "missingRequiredField"

    # ── Plan structure: bad plan shape ─────────────────────────
    DUPLICATE_PLAN_NODE_ID = "duplicatePlanNodeId"
    UNKNOWN_PLAN_NODE_REF = "unknownPlanNodeRef"  # edge source/target not in plan nor staged

    # ── Graph rules (mirrors validate_workflow) ───────────────
    CYCLE = "cycle"
    DANGLING_INPUT = "danglingInput"        # non-entry node has no incoming AND needs input
    DANGLING_OUTPUT = "danglingOutput"      # non-exit node has no outgoing AND produces output

    # ── Connection rules (mirrors ConnectionError.code) ────────
    INCOMPATIBLE_SOURCE = "incompatibleSource"
    INCOMPATIBLE_TARGET = "incompatibleTarget"
    TOO_MANY_OUTGOING = "tooManyOutgoing"
    TOO_MANY_INCOMING = "tooManyIncoming"
    MIN_OUTGOING_NOT_MET = "minOutgoingNotMet"
    NO_THEN_EDGE = "noThen"                 # condition missing the `then` edge
    MISSING_INCOMING = "missingIncoming"     # ask needs at least one incoming
    LOOP_BODY_VIA_EDGE = "loopBodyViaEdge"  # loop must declare bodyTarget, not a bare edge
    DUPLICATE_EDGE = "duplicateEdge"
    SELF_LOOP = "selfLoop"

    # ── Lifecycle ──────────────────────────────────────────────
    PLAN_ATOMIC_REJECTED = "planAtomicRejected"

    @classmethod
    def from_conn_code(cls, code: str) -> "IssueCode":
        """Map a `ConnectionError.code` string to the matching `IssueCode`.

        The single source of truth for connection-rule codes lives in
        `core.connection_rules.ConnectionError` (emitted by
        `validate_connections`). This mapping is the only translation
        layer between the validator's vocabulary and the LLM-facing
        vocabulary. Values fall back to `INCOMPATIBLE_SOURCE` for any
        code the validator adds in a future version that we haven't
        mapped yet — the message still carries the underlying code so
        the LLM can pattern-match.

        Exposed as a classmethod (not a module-level dict) so it lives
        with the enum and survives enum-refactor tooling without a
        separate dict to keep in sync.
        """
        return _CONN_CODE_TO_ISSUE.get(code, cls.INCOMPATIBLE_SOURCE)

# Mapping table — internal to the enum (single source). Kept as a
# module-level dict because enum classmethods can't reference other
# classmethods at class-body parse time without a forward reference,
# and the mapping is pure data with no logic that benefits from
# being a method.
_CONN_CODE_TO_ISSUE: dict[str, "IssueCode"] = {
    "incompatibleSource": IssueCode.INCOMPATIBLE_SOURCE,
    "incompatibleTarget": IssueCode.INCOMPATIBLE_TARGET,
    "selfLoop": IssueCode.SELF_LOOP,
    "tooManyOutgoing": IssueCode.TOO_MANY_OUTGOING,
    "tooManyIncoming": IssueCode.TOO_MANY_INCOMING,
    "missingOutgoing": IssueCode.MIN_OUTGOING_NOT_MET,
    "noThen": IssueCode.NO_THEN_EDGE,
    "missingIncoming": IssueCode.MISSING_INCOMING,
    "loopBodyViaEdge": IssueCode.LOOP_BODY_VIA_EDGE,
    "duplicateEdge": IssueCode.DUPLICATE_EDGE,
    # Emitted by `validate_connections` when the post-apply graph
    # has two nodes with the same id (e.g. an upsert overwriting
    # a staged node AND the plan re-declares the same id with
    # different type — or the LLM made a mistake and listed the
    # same id twice).
    "duplicateNodeId": IssueCode.DUPLICATE_PLAN_NODE_ID,
}

# ─────────────────────────────────────────────────────────────────
# Hint templates — engineering guidance, not translated prose
# ─────────────────────────────────────────────────────────────────
# Each entry maps an `IssueCode` to a callable that formats a short
# hint given the path + context. The hint is meant to be a concrete
# next step ("Did you mean…?") rather than a restatement of the
# message. Keep hints terse; the LLM has limited context budget.
_HINT_TEMPLATES: dict[IssueCode, str] = {
    IssueCode.UNKNOWN_NODE_TYPE: (
        "Type {got} is not in the manifest. Valid types: {valid_types}. "
        "If unsure which one to use, call `get_node_types` to list them."
    ),
    IssueCode.INVALID_CONFIG: (
        "Config did not match the type's schema. Call `get_node_types` for "
        "the exact shape — every node type has a documented config schema. "
        "If the failing field is a CEL/function expression, call "
        "`get_connection_rules` to see allowed syntax."
    ),
    IssueCode.MISSING_REQUIRED_FIELD: (
        "Required field missing for this node type. Call `get_node_types` "
        "for the required fields list. Most node types need at least "
        "`instructions` (agents) or `branches` (router)."
    ),
    IssueCode.DUPLICATE_PLAN_NODE_ID: (
        "Two nodes in `plan.nodes` share this id. Ids must be unique within "
        "a plan. Drop the duplicate or rename it."
    ),
    IssueCode.UNKNOWN_PLAN_NODE_REF: (
        "Edge references a node id that doesn't exist in `plan.nodes` or "
        "in the staged graph. Add the node first (in `plan.nodes`) or "
        "correct the reference. Call `preview_workflow` to inspect existing ids."
    ),
    IssueCode.CYCLE: (
        "Dataflow edges form a cycle. Break the cycle by removing one edge "
        "in the loop, or restructure (e.g. split into two stages). "
        "Use `disconnect` then re-add corrected edges."
    ),
    IssueCode.DANGLING_INPUT: (
        "This node type requires at least one incoming dataflow edge. "
        "Add an edge from a producer node, or change the node type "
        "(e.g. an `agent` has no input requirement, a `router` does)."
    ),
    IssueCode.DANGLING_OUTPUT: (
        "This node type requires at least one outgoing dataflow edge. "
        "Wire it to a downstream node (dataflow kind='dataflow'), or "
        "to a tool source if it's an agent (kind='tool_attachment')."
    ),
    IssueCode.INCOMPATIBLE_SOURCE: (
        "This edge's source type is not allowed to send this kind of edge. "
        "Call `get_connection_rules` for the per-type outbound-edge table. "
        "Common cause: a tool source wired as `dataflow` instead of "
        "`tool_attachment`."
    ),
    IssueCode.INCOMPATIBLE_TARGET: (
        "This edge's target type is not allowed to receive this kind of edge. "
        "Call `get_connection_rules` for the per-type inbound-edge table. "
        "Common cause: a `dataflow` edge sent to a non-routable target."
    ),
    IssueCode.TOO_MANY_OUTGOING: (
        "The source node already has its maximum number of outgoing edges. "
        "Remove an existing edge, or route through a `router` / `parallel` "
        "to fan-out further."
    ),
    IssueCode.TOO_MANY_INCOMING: (
        "The target node already has its maximum number of incoming edges. "
        "Remove one, or merge producers via a `parallel`/`router`."
    ),
    IssueCode.MIN_OUTGOING_NOT_MET: (
        "This node type requires at least one outgoing edge. Add one "
        "(dataflow kind='dataflow' for most types)."
    ),
    IssueCode.NO_THEN_EDGE: (
        "Condition nodes need a `then` edge (the first outgoing). "
        "Add a second edge for `else` if you want a fallback; otherwise "
        "the condition's `then` branch is the only path executed."
    ),
    IssueCode.MISSING_INCOMING: (
        "Ask nodes must have at least one incoming edge. "
        "Connect a producer upstream."
    ),
    IssueCode.LOOP_BODY_VIA_EDGE: (
        "Loop nodes must declare `config.bodyTarget`, not a bare edge. "
        "Set `bodyTarget` to the id of the body node. `create_retry_loop` "
        "does this for you; if you're composing manually, prefer the pattern."
    ),
    IssueCode.DUPLICATE_EDGE: (
        "An edge between these two nodes already exists in the staged graph. "
        "Either drop this one, or change `kind`/`sourceHandle`/`targetHandle` "
        "to disambiguate."
    ),
    IssueCode.SELF_LOOP: (
        "An edge from a node to itself is not allowed. Use a `loop` node "
        "with `bodyTarget` if you need self-iteration."
    ),
    IssueCode.PLAN_ATOMIC_REJECTED: (
        "Plan would exceed the session cap of {cap} pending changes. "
        "Apply or cancel the staged diff first, then re-submit a smaller plan. "
        "Use `preview_workflow` to see what's currently staged."
    ),
}

def hint_for(code: IssueCode, **ctx) -> str:
    template = _HINT_TEMPLATES.get(code, "")
    if not template:
        return ""
    # Format with whatever context we have; missing keys render as
    # "{key}" which is acceptable debug output for an unimplemented case.
    try:
        return template.format(**ctx)
    except KeyError:
        return template

# ─────────────────────────────────────────────────────────────────
# Issue — the structured error shape the LLM sees
# ─────────────────────────────────────────────────────────────────
@dataclass
class Issue:
    """One validation failure inside a plan.

    `path` is JSONPath-style: `nodes[3].data.config.selector.mode`,
    `edges[2].source`, etc. Stable across runs so the LLM can
    reliably highlight a specific entry in its last plan call.

    `code` is one of `IssueCode`. The LLM should branch on this
    rather than on the human-readable `message` (which can be
    translated / reworded without notice).

    `hint` is a short engineering tip ("Did you mean…?", "Add the
    node first…") keyed to the code. It's not a translation of the
    message; it's a concrete next step the LLM can take.
    """
    path: str
    code: IssueCode
    message: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "code": self.code.value,
            "message": self.message,
            "hint": self.hint,
        }

# ─────────────────────────────────────────────────────────────────
# WorkflowPlan — the LLM's input shape
# ─────────────────────────────────────────────────────────────────
class PlanNode(BaseModel):
    """A node in the plan. Mirrors `WorkflowNode` but makes id optional
    (the backend generates one if absent).

    The official node shape is `{id, type, position, data: {label,
    config}}`. The LLM in practice often sends `{id, type, position,
    label, config}` (top-level `label`/`config` instead of nested
    under `data`) — Pydantic v2's lax default silently drops the
    extras, which means the staged node has empty `data` and
    downstream validation fails for any node type that requires
    config (e.g. `agent` needs `instructions`).

    The `before` validator below re-roots top-level `label` /
    `config` (and a few other common top-level fields the LLM
    emits) into `data` so the LLM's intent survives. Anything
    that's already correctly nested under `data` is left alone —
    this is a tolerance layer, not a rewriter.

    Trapped as P1 on : user reported "
    " after a `replace_workflow` call — the LLM's chat log
    said "✓ " but the canvas was empty. Root cause was
    the wrong-shape node silently dropping its label + config.
    """
    id: Optional[str] = None
    type: str
    position: dict[str, float] = Field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _tolerate_top_level_node_fields(cls, values: Any) -> Any:
        """Re-root top-level `label` / `config` (and a few other
        common LLM mistakes) into `data` when `data` is missing or
        empty. This catches the most common wrong-shape case
        without rewriting anything that's already correct."""
        if not isinstance(values, dict):
            return values
        # If the LLM used the correct shape (data already populated),
        # leave it alone.
        data = values.get("data")
        if isinstance(data, dict) and data:
            return values
        # Otherwise, harvest top-level fields and stuff them under
        # `data`. The field set below is the set the LLM has been
        # observed emitting at the top level in production traffic.
        rerouted: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
        for k in ("label", "config", "color", "icon"):
            if k in values and k not in rerouted:
                rerouted[k] = values[k]
        if rerouted:
            values["data"] = rerouted
        return values

    @field_validator("type")
    @classmethod
    def _type_must_be_known(cls, v: str) -> str:
        if v not in MANIFEST_NODE_TYPES:
            raise ValueError(
                f"unknown node type {v!r}; valid types: "
                f"{sorted(MANIFEST_NODE_TYPES)}"
            )
        return v

class PlanEdge(BaseModel):
    """An edge in the plan. `id` optional (generated if absent).
    `kind` is `dataflow` (default) or `tool_attachment`."""
    id: Optional[str] = None
    source: str
    target: str
    sourceHandle: Optional[str] = None
    targetHandle: Optional[str] = None
    kind: Optional[str] = None  # normalised to "dataflow" / "tool_attachment"

class WorkflowPlan(BaseModel):
    """The full plan the LLM submits.

    `nodes` — nodes to upsert. If an id matches an existing staged
              node, the staged node is REPLACED with the plan version.
              Use this for both "add" and "update" semantics.

    `edges` — edges to upsert, same id-based semantics.

    `delete_nodes` / `delete_edges` — ids to remove from the staged
              graph. Cascading: removing a node removes its incident
              edges (handled by the apply function).

    For "throw away everything and start fresh" semantics, use
    `replace_workflow` (a thin wrapper that empties delete_* lists).
    """
    nodes: list[PlanNode] = Field(default_factory=list)
    edges: list[PlanEdge] = Field(default_factory=list)
    delete_nodes: list[str] = Field(default_factory=list)
    delete_edges: list[str] = Field(default_factory=list)

# ─────────────────────────────────────────────────────────────────
# Strict write-time validator (parallels the lax read path)
# ─────────────────────────────────────────────────────────────────
def validate_node_config_for_llm(node_type: str, config: Any) -> list[Issue]:
    """Strict validator used by LLM tool entry points
    (`add_node` / `update_node` / `plan_workflow`).

    Tolerance — mirrors `validate_node_config` so behaviour is
    consistent with the read path:
      * non-dict `config` → passes through (legacy shape or empty).
      * unknown `node_type` → passes through (the upstream
        `WorkflowNode.model_validate` will surface that with its
        own code).

    Returns a list of `Issue`s on failure, [] on success. Every
    `extra_forbidden` error gets a `hint` populated via
    `_did_you_mean` — the LLM sees `Unknown field 'selector_expression'.
    Did you mean 'selector'?` and self-corrects on the next call.
    """
    if not isinstance(config, dict):
        return []
    schema_cls = get_strict_schema(node_type)
    if schema_cls is None:
        return []
    from pydantic import ValidationError
    try:
        schema_cls.model_validate(config)
        return []
    except ValidationError as exc:
        issues: list[Issue] = []
        # Pre‑compute the candidate name list once per call. We add:
        # 1. Every top-level field name + its alias.
        # 2. Every "<top_field>.<sub_field>" / "<top_alias>.<sub_field>"
        #    combination so an LLM that wrote `selector_expression`
        #    gets `selector.expression` (the nesting it was reaching
        #    for) rather than just `expression` (a leaf it doesn't
        #    own).
        valid_names: list[str] = []
        nested_map: dict[str, type[BaseModel]] = {}
        for fname, info in schema_cls.model_fields.items():
            valid_names.append(fname)
            if info.alias:
                valid_names.append(info.alias)
            # Track nested BaseModel fields so we can enumerate their
            # sub-fields for compound suggestions.
            ann = getattr(info, "annotation", None)
            # annotation can be a string under `from __future__ import
            # annotations`; resolve via the model's annotations dict
            # when present.
            try:
                resolved = schema_cls.model_fields[fname].annotation
                if isinstance(resolved, str):
                    resolved = schema_cls.__annotations__.get(fname)
            except Exception:
                resolved = ann
            if isinstance(resolved, type) and issubclass(resolved, BaseModel):
                nested_map[fname] = resolved
                if info.alias:
                    nested_map[info.alias] = resolved
        # Compound names "<top>.<sub>" / "<top_alias>.<sub>" for nested models.
        for top, nested_cls in nested_map.items():
            for sub_name, sub_info in nested_cls.model_fields.items():
                valid_names.append(f"{top}.{sub_name}")
                if sub_info.alias:
                    valid_names.append(f"{top}.{sub_info.alias}")
                if sub_name != top:
                    valid_names.append(f"{sub_name}")
        for err in exc.errors():
            err_loc = err.get("loc") or ()
            err_type = err.get("type", "")
            err_msg = err.get("msg", "")
            # Build a JSONPath-ish locator for the offending field.
            loc_str = ".".join(str(p) for p in err_loc)
            full_path = f"data.config.{loc_str}" if loc_str else "data.config"
            hint = ""
            if err_type == "extra_forbidden" and err_loc:
                bad = err_loc[-1]
                sugg = _did_you_mean(str(bad), valid_names)
                if sugg:
                    hint = (
                        f"Unknown field {bad!r}. Did you mean {sugg!r}? "
                        "Run `get_node_types()` for the full schema."
                    )
                else:
                    hint = (
                        f"Unknown field {bad!r}. "
                        "Run `get_node_types()` for the full schema."
                    )
            issues.append(Issue(
                path=full_path,
                code=IssueCode.INVALID_CONFIG,
                message=f"{loc_str}: {err_msg}" if loc_str else err_msg,
                hint=hint,
            ))
        return issues

# ─────────────────────────────────────────────────────────────────
# apply_plan_to_snapshot — pure function
# ─────────────────────────────────────────────────────────────────
def apply_plan_to_snapshot(
    base_nodes: list[dict],
    base_edges: list[dict],
    plan: WorkflowPlan,
) -> tuple[list[dict], list[dict]]:
    """Apply `plan` to a copy of (base_nodes, base_edges). Returns
    the new (nodes, edges) WITHOUT validating. Pure — easy to unit-test.

    Semantics:
      1. Remove every node whose id is in `plan.delete_nodes`.
      2. Remove every edge whose id is in `plan.delete_edges`,
         AND every edge that touches a node removed in step 1
         (cascade).
      3. Upsert every `plan.nodes[i]` (id match → replace).
      4. Upsert every `plan.edges[i]` (id match → replace).

    Cascade order matters: nodes first, then edges. Within each,
    delete first, then upsert (so a delete + re-add of the same id
    works in one plan).
    """
    # Build id indexes once.
    del_node_ids = set(plan.delete_nodes)
    del_edge_ids = set(plan.delete_edges)

    # Step 1+2: drop deleted nodes + their incident edges + explicitly
    # deleted edges. Output is a fresh list (no mutation of base_*).
    new_nodes: list[dict] = []
    for n in base_nodes:
        if n["id"] in del_node_ids:
            continue
        new_nodes.append(n)
    new_edges: list[dict] = []
    for e in base_edges:
        if e["id"] in del_edge_ids:
            continue
        if e.get("source") in del_node_ids or e.get("target") in del_node_ids:
            continue
        new_edges.append(e)

    # Step 3: upsert nodes. The plan entry may not have an id (then
    # we generate one); we need to materialise id + data shape here
    # so subsequent edge-upsert can match by id.
    import uuid
    nodes_by_id: dict[str, dict] = {}
    for n in new_nodes:
        nodes_by_id[n["id"]] = n
    for pnode in plan.nodes:
        node_id = pnode.id or f"node-{uuid.uuid4().hex[:8]}"
        # Build the dict in the shape WorkflowNode expects so the
        # validator downstream doesn't have to re-derive it.
        node_dict = {
            "id": node_id,
            "type": pnode.type,
            "position": dict(pnode.position),
            "data": dict(pnode.data),
        }
        nodes_by_id[node_id] = node_dict
    new_nodes = list(nodes_by_id.values())

    # Step 4: upsert edges. Same shape as nodes.
    edges_by_id: dict[str, dict] = {}
    for e in new_edges:
        edges_by_id[e["id"]] = e
    for pedge in plan.edges:
        edge_id = pedge.id or f"edge-{uuid.uuid4().hex[:8]}"
        edge_dict = {
            "id": edge_id,
            "source": pedge.source,
            "target": pedge.target,
            "sourceHandle": pedge.sourceHandle,
            "targetHandle": pedge.targetHandle,
            "kind": pedge.kind or "dataflow",
        }
        edges_by_id[edge_id] = edge_dict
    new_edges = list(edges_by_id.values())

    return new_nodes, new_edges

# ─────────────────────────────────────────────────────────────────
# validate_plan — full validation, returns ALL issues
# ─────────────────────────────────────────────────────────────────
def validate_plan(
    new_nodes: list[dict],
    new_edges: list[dict],
) -> list[Issue]:
    """Run every validator on the post-apply graph and return the
    full list of `Issue`s. Used by `plan_workflow` BEFORE committing
    staged state, and by the read-only `validate_graph` tool.

    Each validator is independent — we accumulate ALL issues so the
    LLM can fix multiple problems in one retry (the previous code
    raised on the first error and forced the LLM to re-submit
    multiple times). The order is:
      1. Pydantic per-node config schema (most specific feedback)
      2. Plan-level duplicates (id collisions within plan.nodes)
      3. Edge references to unknown ids
      4. Connection rules
      5. Graph rules (cycle, top-sort, orphan)
    """
    issues: list[Issue] = []

    # 0. Strict write-time validator. Runs FIRST so the
    # LLM gets a typed `INVALID_CONFIG` Issue with a "did you mean"
    # hint for any typo or wrong-nesting attempt. The lax
    # `WorkflowNode.model_validate` below still runs as defence
    # in depth — it catches shape mistakes (missing required field,
    # wrong type) that strict-mode can also surface but with
    # less precise paths.
    for idx, n in enumerate(new_nodes):
        cfg = (n.get("data") or {}).get("config")
        if not isinstance(cfg, dict):
            continue
        strict_issues = validate_node_config_for_llm(n.get("type", ""), cfg)
        for si in strict_issues:
            issues.append(Issue(
                path=f"nodes[{idx}].{si.path}",
                code=si.code,
                message=f"node {n.get('id', '?')!r}: {si.message}",
                hint=si.hint,
            ))

    # 1. Pydantic per-node. Catches config shape mismatches with
    # precise path. Each node has its own index in `new_nodes` so
    # the LLM can find it.
    for idx, n in enumerate(new_nodes):
        try:
            WorkflowNode.model_validate(n)
        except Exception as exc:
            # Pull structured `loc` / `msg` from the Pydantic v2
            # error when we have one — the str(exc) format varies
            # by version but `exc.errors()` is stable. The `loc`
            # tuple usually points inside `data.config.*` (e.g.
            # `('data', 'config', 'instructions')`); we flatten it
            # back to JSONPath-style with `.` separators.
            from pydantic import ValidationError
            path = f"nodes[{idx}]"
            message = str(exc)
            if isinstance(exc, ValidationError):
                errs = exc.errors()
                if errs:
                    first = errs[0]
                    loc = first.get("loc") or ()
                    # Pydantic's `loc` includes field names WITHOUT
                    # the `data.config.` prefix when AgentNodeConfig
                    # is validated standalone (the per-type validator
                    # in `WorkflowNode._validate_config` raises
                    # inside the validator function, so the loc
                    # resets to the model being validated). Re-add
                    # the wrapper ourselves so the path reads
                    # `nodes[N].data.config.<field>`.
                    flat = ".".join(str(p) for p in loc)
                    if flat:
                        # If the field path doesn't already start
                        # with `data.config`, prepend it — the
                        # LLM needs to know WHICH subtree failed.
                        if not flat.startswith("data"):
                            path = f"nodes[{idx}].data.config.{flat}"
                        else:
                            path = f"nodes[{idx}].{flat}"
                    msg = first.get("msg") or message
                    message = f"{'.'.join(str(p) for p in loc)}: {msg}"
            issues.append(Issue(
                path=path,
                code=IssueCode.INVALID_CONFIG,
                message=f"node {n.get('id', '?')!r} failed config validation: {message[:300]}",
                hint=hint_for(IssueCode.INVALID_CONFIG),
            ))

    # 2. Duplicate plan-node ids.
    seen_ids: dict[str, int] = {}
    for idx, n in enumerate(new_nodes):
        nid = n.get("id")
        if nid is None:
            continue
        if nid in seen_ids:
            issues.append(Issue(
                path=f"nodes[{idx}]",
                code=IssueCode.DUPLICATE_PLAN_NODE_ID,
                message=f"node id {nid!r} appears more than once in the plan",
                hint=hint_for(IssueCode.DUPLICATE_PLAN_NODE_ID),
            ))
        else:
            seen_ids[nid] = idx

    # 3. Edge references to unknown node ids (post-apply).
    node_ids = {n["id"] for n in new_nodes}
    for idx, e in enumerate(new_edges):
        for endpoint in ("source", "target"):
            ref = e.get(endpoint)
            if ref and ref not in node_ids:
                issues.append(Issue(
                    path=f"edges[{idx}].{endpoint}",
                    code=IssueCode.UNKNOWN_PLAN_NODE_REF,
                    message=f"edge {e.get('id', '?')!r}.{endpoint}={ref!r} "
                            f"does not reference any node in the post-apply graph",
                    hint=hint_for(IssueCode.UNKNOWN_PLAN_NODE_REF),
                ))

    # Skip the rest of validation if we have no nodes — the
    # top-sort will otherwise crash on an empty graph.
    if not new_nodes:
        return issues

    # 4. Connection rules. `validate_connections` returns a list of
    # `ConnectionError`s — we translate each one into an `Issue` with
    # the matching `IssueCode` (via `IssueCode.from_conn_code`) and a
    # JSONPath-style pointer so the LLM can fix the offending edge.
    from app.core.connection_rules import validate_connections
    conn_errors = validate_connections(new_nodes, new_edges)
    for cerr in conn_errors:
        issue_code = IssueCode.from_conn_code(cerr.code)
        path = _path_for_conn_error(cerr, new_nodes, new_edges)
        hint = hint_for(issue_code)
        issues.append(Issue(
            path=path,
            code=issue_code,
            message=cerr.message,
            hint=hint,
        ))

    # 5. Graph rules. `validate_workflow` raises GraphError on cycle
    # or top-sort failure. We translate the message to a structured
    # issue with the most generic code (cycle vs dangling).
    try:
        from app.core.graph import validate_workflow as _validate_graph_full
        _validate_graph_full(new_nodes, new_edges)
    except GraphError as exc:
        msg = str(exc)
        # Heuristic: cycle messages mention "cycle"; dangling messages
        # mention "missing" or "orphan". Top-sort failures are usually
        # cycle too.
        if "cycle" in msg.lower():
            issues.append(Issue(
                path="edges",
                code=IssueCode.CYCLE,
                message=msg,
                hint=hint_for(IssueCode.CYCLE),
            ))
        else:
            issues.append(Issue(
                path="edges",
                code=IssueCode.DANGLING_OUTPUT,
                message=msg,
                hint=hint_for(IssueCode.DANGLING_OUTPUT),
            ))
    except Exception as exc:
        # Catch-all so plan validation NEVER raises to the LLM — every
        # failure becomes structured data.
        issues.append(Issue(
            path="(graph)",
            code=IssueCode.INVALID_CONFIG,
            message=f"unexpected validation error: {exc}",
            hint="",
        ))

    return issues

def _path_for_conn_error(
    cerr: _ConnError,
    new_nodes: list[dict],
    new_edges: list[dict],
) -> str:
    """Translate a ConnectionError to a JSONPath-style string.

    ConnectionError carries `node_id` / `edge_id` / `source_id` /
    `target_id`. We try to express the issue in terms of the edge
    first (most connection errors are about edges), falling back to
    the node if no edge is referenced.
    """
    if cerr.edge_id is not None:
        # Find the edge's index in `new_edges`.
        for idx, e in enumerate(new_edges):
            if e.get("id") == cerr.edge_id:
                if cerr.source_id is None and cerr.target_id is not None:
                    return f"edges[{idx}].target"
                if cerr.target_id is None and cerr.source_id is not None:
                    return f"edges[{idx}].source"
                return f"edges[{idx}]"
        return f"edges[?id={cerr.edge_id}]"
    if cerr.node_id is not None:
        for idx, n in enumerate(new_nodes):
            if n.get("id") == cerr.node_id:
                return f"nodes[{idx}]"
        return f"nodes[?id={cerr.node_id}]"
    return "(connection)"

# ─────────────────────────────────────────────────────────────────
# PlanResult — the LLM-facing return shape
# ─────────────────────────────────────────────────────────────────
@dataclass
class PlanResult:
    """Structured return from `plan_workflow` / `replace_workflow`.

    `ok=True` → plan committed. `applied` carries summary counts.
    `config_echo` mirrors the post-coercion config of every node
    added or updated, so the LLM can see what the validator kept.

    `ok=False` → plan rejected. `issues` lists every validation
    failure (not just the first). `state_unchanged` is always True
    in this case — F0.2 / F1 atomicity contract.
    """
    ok: bool
    applied: dict[str, int] = field(default_factory=dict)
    config_echo: dict[str, dict] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    state_unchanged: bool = True
    # F6 : optional escalation hint surfaced when
    # the per-turn rejection budget is exhausted. Empty when
    # no escalation is needed. Set by `_plan_workflow` /
    # `_replace_workflow` after they increment the session
    # counter. The LLM tooling renders it as a sibling to
    # `issues` so the LLM sees both the structured errors AND
    # the "stop and call a diagnostic" nudge.
    next_step: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "applied": self.applied,
            "config_echo": self.config_echo,
            "issues": [i.to_dict() for i in self.issues],
            "state_unchanged": self.state_unchanged,
        }
        if self.next_step:
            out["next_step"] = self.next_step
        return out

def execute_plan(
    base_nodes: list[dict],
    base_edges: list[dict],
    plan: WorkflowPlan,
) -> tuple[list[dict], list[dict], list[Issue]]:
    """Apply the plan and validate the result. Returns
    `(new_nodes, new_edges, issues)` — caller decides whether to
    commit (issues empty) or roll back (issues non-empty).

    This is the core F1 primitive: one call covers the entire
    apply-then-validate flow. The caller wraps it with the
    snapshot-commit logic specific to ChatSession.
    """
    new_nodes, new_edges = apply_plan_to_snapshot(base_nodes, base_edges, plan)
    issues = validate_plan(new_nodes, new_edges)
    return new_nodes, new_edges, issues

__all__ = [
    "IssueCode",
    "Issue",
    "PlanNode",
    "PlanEdge",
    "WorkflowPlan",
    "PlanResult",
    "apply_plan_to_snapshot",
    "validate_plan",
    "validate_node_config_for_llm",
    "execute_plan",
]