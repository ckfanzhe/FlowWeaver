"""Compile orchestration — the multi-pass builder.

Single-engine runtime entry point: turns `(nodes, edges)` into an
in-memory `agno.workflow.Workflow` object. The same object is the
runtime + the export's source of truth.

The pipeline runs four passes, mirroring the generator's source-
emission order but operating on Python objects:

  - **pass 0**  Tool-source objects (`tools` / `http` / `mcp`) — these
                are NOT in `wf.steps`. They live in `ctx.tool_objects`
                and are stitched into agents in pass 3.
  - **pass 0b** Knowledge-source objects (`knowledge`) — `Knowledge(...)`
                instances live in `ctx.knowledge_objects` and get wired
                into agents' `knowledge=...` parameter in pass 3b. New
                in [[gleaming-munching-grove]] — parallels tool sources
                but for agno's separate `knowledge` parameter (NOT
                `tools=[...]`).
  - **pass 1**  Object declarations per node:
                  - `agent`           → `Agent(...)`
                  - `ask`             → `Step(requires_user_input=True, ...)`
                  - `branch` / `flow` / `loop` are deferred to pass 2
                    (they need downstream targets).
  - **pass 1.5** `Step(name=..., agent=...)` wrappers for non-compound
                  nodes (currently only `agent`).
  - **pass 2**  Compound nodes built from their downstream targets.
  - **pass 3**  Wire each agent's `tools=[...]` from `ir.tool_attachments`.
  - **pass 3b** Wire each agent's `knowledge=...` from
                `ir.knowledge_attachments` (NEW ).
  - **assemble** `Workflow(steps=[...])`.

Adding a new node type means dropping a strategy class under
`app.core.strategies/<name>.py` and a manifest entry whose
`runtime.builder` names it. No edits here, no edits to
`tool_factories.py`, no edits to `serialize.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.compile._helpers.ir_helpers import nodes_by_id_from_ir
from app.core.graph import validate_workflow
from app.core.ir import WorkflowIR, build_ir
from app.core.node_types import NODE_TYPES

@dataclass
class CompileCtx:
    """Shared state across the four passes.

    `objects` is populated lazily by each strategy's `build()`.
    Compound strategies read it after pass 1.5 has wrapped agent
    nodes. The serializer reads `tool_objects` (populated in pass 0)
    when emitting pass-3 wiring.

    `user_id` : the workflow owner's id (matches the
    `X-User-Id` of whoever created/ran the workflow). Strategies
    that need to resolve user-scoped resources (the LLM preset
    lookup, the MCP server picker) read it from here so a workflow
    belonging to alice doesn't accidentally run against bob's API
    keys. `None` means "no caller scope" — the legacy behaviour
    (system rows only) — which keeps background tasks and pre-
    tests happy.
    """
    ir: WorkflowIR
    # Dict-of-dicts view of `ir.node_map` (raw shape the strategies
    # expect: `node["data"]["config"]`). Built once at construction
    # so every pass can read it cheaply.
    nodes_by_id: dict[str, dict] = field(default_factory=dict)
    objects: dict[str, Any] = field(default_factory=dict)
    # tool-source pool (nid → list of agno tool instances or source blocks)
    tool_objects: dict[str, list[Any]] = field(default_factory=dict)
    # knowledge-source pool (nid → agno `Knowledge` instance).
    # Parallel to `tool_objects` — but knowledge nodes produce a
    # SINGLE object (not a list) because agno's `Knowledge` is not a
    # tool-call target, just a retrieval context attached via
    # `Agent(knowledge=...)`. See plan [[gleaming-munching-grove]].
    knowledge_objects: dict[str, Any] = field(default_factory=dict)
    # HTTP wrapper metadata (the runtime builds the function, the
    # exporter emits the source — both use the same metadata).
    http_wrappers: dict[str, dict] = field(default_factory=dict)
    user_id: str | None = None

def _pass2_compound_order() -> tuple[str, ...]:
    """Return compound node types in manifest-defined pass order.

    Each compound strategy declares a `COMPOUND_PASS` integer in
    the manifest's `capabilities` block (parallel=10, condition=20,
    loop=30, router=40 today). Pass 2 walks types in ascending
    order so a `Parallel` containing a `Router` resolves correctly:
    the `Router` branch targets exist as objects by the time pass 2
    gets to it.

    Replacing the previous hardcoded
    `pass2_order = ("parallel", "condition", "loop", "router")` tuple
    means adding a new compound type is a one-class change — the
    manifest declares its `compoundPass`, and the pipeline picks it
    up automatically.
    """
    compound = [
        (name, spec.strategy.COMPOUND_PASS or 0)
        for name, spec in NODE_TYPES.items()
        if spec.kind == "compound"
    ]
    # phase : deterministic tie-break — when two
    # compound types share the same COMPOUND_PASS (or both are 0),
    # fall back to the type name so the order is byte-stable across
    # Python dict-iteration order changes. The previous lambda only
    # sorted on the integer, which let Python's set-dict iteration
    # order leak into the output.
    return tuple(
        name for name, _ in sorted(compound, key=lambda x: (x[1], x[0]))
    )

def _topo_top_level(ir: WorkflowIR) -> list[str]:
    """Top-level executable nodes in topo order, with nested children
    excluded. Mirrors the generator's `assemble_workflow` selection."""
    return ir.top_level_step_ids()

def build_workflow(
    workflow_id: str,
    name: str,
    db_nodes: list[dict],
    db_edges: list[dict],
    *,
    db: Any = None,
    session_id: str | None = None,
    start_node_id: str | None = None,
    user_id: str | None = None,
) -> "agno.workflow.Workflow":
    """Build an agno `Workflow` object from `(nodes, edges)`.

    Public entry point. Replaces the legacy `core.workflow_builder.build_workflow`.

    `start_node_id` — when set, the assembled `Workflow(steps=[...])`
    begins at this node. agno 2.8.7's `Wf.run(start_node_id=...)` was
    rejected at the type level, so we slice the top-level step list
    here instead. The downstream nodes still run in their original
    topo order; the only thing that's different is which prefix of
    `steps=` is included.

    `user_id`  — the workflow owner's id. Threaded into
    `CompileCtx` so the agent / MCP emitters scope their LLM preset
    and MCP server lookups against the owner's resources. The runtime
    service passes `workflow.created_by` here; the export path
    leaves it as None (exported code reads from the env, not the DB).
    """
    from agno.workflow import Workflow

    validate_workflow(db_nodes, db_edges)
    ir = build_ir(db_nodes, db_edges)
    ctx = CompileCtx(
        ir=ir,
        nodes_by_id=nodes_by_id_from_ir(ir),
        user_id=user_id,
    )

    _pass0_tool_sources(ctx, db_nodes)
    _pass0_knowledge_sources(ctx, db_nodes)
    _pass1_objects(ctx)
    _pass1_5_step_wrappers(ctx)
    _pass2_compounds(ctx)
    _pass3_tool_wiring(ctx)
    _pass3_knowledge_wiring(ctx)
    return _assemble(workflow_id, name, ir, ctx, start_node_id=start_node_id)

def _pass0_tool_sources(ctx: CompileCtx, db_nodes: list[dict]) -> None:
    """Build the tool-source pool. These never appear in `wf.steps` —
    they get attached to agents in pass 3."""
    for nid, node in ctx.nodes_by_id.items():
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        strategy = spec.strategy
        if not strategy.IS_TOOL_SOURCE:
            continue
        objs = strategy.build(nid, node, ctx)
        if not isinstance(objs, list):
            objs = [objs]
        ctx.tool_objects[nid] = list(objs or [])


def _pass0_knowledge_sources(ctx: CompileCtx, db_nodes: list[dict]) -> None:
    """Build the knowledge-source pool — parallel to `_pass0_tool_sources`
    but for agno's `knowledge=...` parameter (not `tools=[...]`).

    Knowledge nodes produce a SINGLE object (an agno `Knowledge`
    instance) rather than a list — there's no concept of "multiple
    tools in one tool-source node" for RAG, so the pool is shaped as
    `dict[nid, Knowledge]` instead of `dict[nid, list[Any]]`. The
    wiring pass (`_pass3_knowledge_wiring`) attaches it to an agent
    via `agent_obj.knowledge = kb` (singular, not `tools=[...]`).
    """
    for nid, node in ctx.nodes_by_id.items():
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        strategy = spec.strategy
        if not strategy.IS_KNOWLEDGE_SOURCE:
            continue
        obj = strategy.build(nid, node, ctx)
        ctx.knowledge_objects[nid] = obj

def _pass1_objects(ctx: CompileCtx) -> None:
    """Build the agent / ask objects. Compound nodes are
    deferred to pass 2.

    `nested_children` are STILL built here — pass-2 compound strategies
    read them out of `ctx.objects` by id when assembling their branches.
    Excluding them from `wf.steps` is the assembly's job (see
    `ir.top_level_step_ids()` which drops `nested_children`).
    """
    for nid in ctx.ir.topo_order:
        node = ctx.nodes_by_id.get(nid)
        if node is None:
            continue
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        strategy = spec.strategy
        # Compound nodes need their downstream targets resolved first
        # (pass 1.5 wraps agents, then pass 2 assembles). Tool-source
        # nodes don't appear in `wf.steps` — pass 0 / pass 3 own them.
        if strategy.COMPOUND_PASS is not None:
            continue
        if strategy.IS_TOOL_SOURCE:
            continue
        obj = strategy.build(nid, node, ctx)
        ctx.objects[nid] = obj

def _pass1_5_step_wrappers(ctx: CompileCtx) -> None:
    """Wrap every non-compound object in a `Step(name=..., agent=...)`.

    This is the only `Step` wrapper we'll emit for executable types —
    compound nodes are already their own agno object (Router /
    Parallel / Condition / Loop).
    """
    from agno.workflow import Step

    for nid, obj in list(ctx.objects.items()):
        node = ctx.nodes_by_id.get(nid)
        if node is None:
            continue
        # The Step wrapper is owned by the agent strategy's
        # STEP_WRAPPER ClassVar; ask builds its own Step
        # already so we don't double-wrap.
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        if spec.strategy.STEP_WRAPPER == "agent":
            label = node["data"].get("label") or nid
            ctx.objects[nid] = Step(name=label, agent=obj, step_id=nid)

def _pass2_compounds(ctx: CompileCtx) -> None:
    """Build compound nodes in manifest order. They reference
    `ctx.objects` for their children, which is why this runs after
    pass 1.5 wraps agents.

    Compound nodes that are themselves nested under another compound
    (e.g. a `Parallel` that is a branch of a `Router`) are STILL built
    here — the outer compound's `build()` reads them out of
    `ctx.objects` by id. `nested_children` only governs exclusion from
    the top-level `_steps` assembly in `assemble_workflow`.
    """
    for ntype in _pass2_compound_order():
        spec = NODE_TYPES.get(ntype)
        if spec is None:
            continue
        strategy = spec.strategy
        for nid in ctx.ir.topo_order:
            node = ctx.nodes_by_id.get(nid)
            if node is None:
                continue
            if node["type"] != ntype:
                continue
            ctx.objects[nid] = strategy.build(nid, node, ctx)

def _pass3_tool_wiring(ctx: CompileCtx) -> None:
    """Replace each agent's `tools=[]` with the union of attached
    tool-source nodes (real agno `Function` / `MCPTools` / `Function.from_callable(...)`).

    Source of truth: `ir.tool_attachments[agent_id]`. Legacy
    `ir.tool_refs[agent_id]` is the back-compat shim — the pipeline
    mirrors whatever `ir` resolved.
    """
    for agent_id, refs in ctx.ir.tool_attachments.items():
        agent_step = ctx.objects.get(agent_id)
        if agent_step is None:
            continue
        agent_obj = getattr(agent_step, "agent", None)
        if agent_obj is None:
            continue
        tools: list = []
        for ref in refs:
            for tool in ctx.tool_objects.get(ref, []):
                tools.append(tool)
        if tools:
            agent_obj.tools = tools


def _pass3_knowledge_wiring(ctx: CompileCtx) -> None:
    """Attach each agent's `knowledge=...` to the agno `Knowledge`
    instance built in pass 0b.

    Parallel to `_pass3_tool_wiring` but for the `knowledge=...`
    parameter (singular, not `tools=[...]`). Source of truth:
    `ir.knowledge_attachments[agent_id]`. Connection rule
    `max_incoming: 1` keeps each bucket at length ≤ 1; we still index
    `refs[0]` defensively (an empty list = nothing to wire).

    Setting `agent.knowledge = kb` directly mirrors how pass 3 sets
    `agent.tools = [...]` — the strategy's `build()` doesn't need to
    know about knowledge at all, keeping the signature stable for the
    5 other strategy classes that don't care.
    """
    for agent_id, refs in ctx.ir.knowledge_attachments.items():
        if not refs:
            continue
        agent_step = ctx.objects.get(agent_id)
        if agent_step is None:
            continue
        agent_obj = getattr(agent_step, "agent", None)
        if agent_obj is None:
            continue
        kb = ctx.knowledge_objects.get(refs[0])
        if kb is not None:
            agent_obj.knowledge = kb

def _assemble(
    workflow_id: str,
    name: str,
    ir: WorkflowIR,
    ctx: CompileCtx,
    *,
    start_node_id: str | None = None,
) -> "agno.workflow.Workflow":
    """Build the top-level `Workflow(steps=[...])`.

    `start_node_id` truncates the top-level step list so the assembled
    workflow begins at that node. We don't slice the underlying IR
    because compound-node bodies (Router/Parallel/Condition/Loop
    children) still need to be built by pass 2 — they're just not
    included at the top level when the start point skips them.
    """
    from agno.workflow import Workflow

    step_ids = _topo_top_level(ir)
    if start_node_id is not None:
        if start_node_id not in step_ids:
            raise ValueError(
                f"start_node_id {start_node_id!r} is not a top-level step"
            )
        idx = step_ids.index(start_node_id)
        step_ids = step_ids[idx:]
    steps: list = []
    for nid in step_ids:
        obj = ctx.objects.get(nid)
        if obj is None:
            continue
        steps.append(obj)
    # agno's `Wf.continue_run(...)` looks up the persisted session via
    # `self.get_session(session_id=...)`, which in turn requires
    # `self.db` to be set. Without a db the call raises "Could not find
    # session with id …" — even when `cache_session=True` keeps
    # `self._workflow_session` populated in memory. So we always pass
    # an in-process SQLite db here, backed by a StaticPool so every
    # connection sees the same data (default `:memory:` creates a fresh
    # DB per connection). The runtime + tests share the same compiled
    # workflow instance across legs, so the persisted session row
    # survives the pause/resume round-trip. A production deployment
    # can swap this out for a persistent db by passing `db=` to
    # `compile.build_workflow(...)`.
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from agno.db.sqlite import SqliteDb
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db = SqliteDb(db_engine=engine)
    return Workflow(
        id=workflow_id,
        name=name or workflow_id,
        steps=steps,
        cache_session=True,
        db=db,
    )

__all__ = ["build_workflow", "CompileCtx", "_pass2_compound_order"]
