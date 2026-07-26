"""Workflow → Python source serializer.

The export pipeline. Reads the same `CompileCtx` the runtime pipeline
built, walks each strategy's `to_source()` method, and stitches the
result into a runnable Python file.

The contract is simple: the bytes of `to_python_source(wf)` MUST be
runnable as `python workflow.py` and produce the same dispatch as
`Wf.run(...)` does in-process. The runtime + the export share the
same `CompileCtx` values, so the deterministic builder logic can't
drift.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core.compile._helpers.imports import collect_imports
from app.core.compile._helpers.assembly import assemble_workflow
from app.core.compile._helpers.utils import safe_name
from app.core.ir import build_ir
from app.core.graph import validate_workflow
from app.core.node_types import NODE_TYPES

from .errors import CompileError
from .pipeline import CompileCtx, _pass2_compound_order

_TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "templates"

def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )

def to_python_source(
    workflow: dict,
    name: str = "workflow",
    *,
    user_id: str | None = None,
) -> str:
    """Render the workflow dict into a Python source string.

    Same entry point the legacy `core.generator.pipeline.render_python`
    exposed. Internally builds the IR and runs the same multi-pass
    pipeline as the runtime, but emits source fragments instead of
    objects.

    `user_id` : scopes the MCP server lookup during
    pass-0 so the exported `.py` can't reach into a private MCP
    server the workflow owner hasn't shared. When None (the default,
    used by legacy tests), the lookup falls back to the
    pre-binding behaviour: any visible row.
    """
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    if not nodes:
        raise CompileError("workflow has no nodes")
    # Migrate legacy type aliases
    # (`parallel` / `steps` → `flow`; `router` / `condition` →
    # `branch`) in place before `validate_workflow` runs. Tests
    # that hand-build workflows with the old type names still
    # produce the new compiled output without going through the
    # API read path.
    from app.core._compat import migrate_envelope
    migrate_envelope(workflow)
    try:
        validate_workflow(nodes, edges)
    except Exception as e:
        # Re-raise as CompileError so callers only need to catch the
        # one canonical exception type. `validate_workflow` raises
        # `GraphError` for cycles / dangling edges / unknown types.
        raise CompileError(str(e)) from e
    ir = build_ir(nodes, edges)

    # Build a context that mirrors the runtime's `CompileCtx` so we
    # can use the same `is_compound()` / `is_tool_source()` checks.
    from app.core.compile._helpers.ir_helpers import nodes_by_id_from_ir
    nodes_by_id = nodes_by_id_from_ir(ir)
    ctx = CompileCtx(ir=ir, nodes_by_id=nodes_by_id, user_id=user_id)

    # We don't need to actually build the agno objects to serialize
    # — `to_source()` is fully text-driven. We just need the IR + the
    # tool-source metadata for the agent wiring pass.
    from app.core.compile._helpers.http_wrappers import http_wrappers_metadata
    http_wrappers = http_wrappers_metadata(nodes_by_id)
    ctx.http_wrappers = {w["node_id"]: w for w in http_wrappers}

    safe = safe_name(workflow.get("name") or name)

    # Run the same passes the runtime does, but emit source.
    pass0 = _pass0_tool_sources_source(ctx)
    pass0b = _pass0_knowledge_sources_source(ctx)
    pass1 = _pass1_objects_source(ctx)
    pass1_5 = _pass1_5_step_wrappers_source(ctx)
    pass2 = _pass2_compounds_source(ctx)
    pass3 = _pass3_tool_wiring_source(ctx)
    pass3b = _pass3_knowledge_wiring_source(ctx)
    pass4 = assemble_workflow(safe, ir)
    main = _main_entry()

    imports_extra = collect_imports(
        nodes_by_id, has_http=any(n["type"] == "http" for n in nodes_by_id.values())
    )
    header = _env().get_template("workflow.py.jinja").render(
        filename=f"{safe}.py",
        imports_extra=imports_extra,
    )
    extra_block = "\n".join(imports_extra) + "\n\n" if imports_extra else ""
    return (
        header + extra_block + pass0 + pass0b + pass1 + pass1_5 + pass2 + pass3 + pass3b + pass4 + main
    )

def _pass0_tool_sources_source(ctx: CompileCtx) -> str:
    """Same as the runtime's pass 0 — but emit source for tool-source
    nodes (raw function bodies, HTTP wrappers, MCPTools).

    The order matters: tools function bodies come first (the agent
    that references them is rendered later in pass 1, but Python's
    name resolution needs the function defs to exist at module
    scope before the agent's `tools=[...]` references them).
    """
    blocks: list[str] = []
    nodes_by_id = ctx.nodes_by_id
    for nid in ctx.ir.topo_order:
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        if not spec.strategy.IS_TOOL_SOURCE:
            continue
        blocks.append(spec.strategy.to_source(nid, node, ctx))
    return "".join(blocks)


def _pass0_knowledge_sources_source(ctx: CompileCtx) -> str:
    """Emit `<nid>_kb = Knowledge(...)` blocks at module scope.

    Parallel to `_pass0_tool_sources_source` — knowledge sources also
    get defined at module scope (before agents) so Python's name
    resolution sees them by the time the agent's `knowledge=<ref>_kb`
    reference is evaluated in pass 3b. The order matches the runtime:
    pass 0 (tools) → pass 0b (knowledge) → pass 1 (agents).

    Each block is 3 lines (embedder → vector_db → Knowledge) per
    `knowledge_expr.knowledge_block`. See plan
    [[gleaming-munching-grove]] for the build-order rationale.
    """
    blocks: list[str] = []
    nodes_by_id = ctx.nodes_by_id
    for nid in ctx.ir.topo_order:
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        if not spec.strategy.IS_KNOWLEDGE_SOURCE:
            continue
        blocks.append(spec.strategy.to_source(nid, node, ctx))
    return "".join(blocks)

def _pass1_objects_source(ctx: CompileCtx) -> str:
    """Pass 1: render every node's main declaration object in topo order.

    Compound nodes are deferred to pass 2. Nested children ARE rendered
    at module level — Parallel/Router/Condition/Loop strategies inline
    them by variable name (e.g. `na_a1_agent`), and Python's name
    resolution requires them to exist in the module scope. Excluding
    nested children from the top-level `_steps` list is the
    `assemble_workflow` job — same as the runtime pipeline.
    """
    blocks: list[str] = []
    nodes_by_id = ctx.nodes_by_id
    for nid in ctx.ir.topo_order:
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        strategy = spec.strategy
        # Compound nodes are deferred to pass 2; tool-source nodes
        # are handled by pass 0 / pass 3 (their `to_source()` lives
        # in the wrapper-function / MCPTools source). Knowledge-source
        # nodes are handled by pass 0b / pass 3b (their `to_source()`
        # emits the `Knowledge(...)` constructor block).
        if strategy.COMPOUND_PASS is not None:
            continue
        if strategy.IS_TOOL_SOURCE:
            continue
        if strategy.IS_KNOWLEDGE_SOURCE:
            continue
        blocks.append(strategy.to_source(nid, node, ctx))
    return "".join(blocks)

def _pass1_5_step_wrappers_source(ctx: CompileCtx) -> str:
    """Pass 1.5: `<nid>_step = Step(...)` wrappers for non-compound types.

    The agent emitter already emits the Step wrapper inline — so this
    pass is currently a no-op (kept as a placeholder for types that
    might want a separate wrapper step in the future).
    """
    return ""

def _pass2_compounds_source(ctx: CompileCtx) -> str:
    """Pass 2: compound nodes in manifest-defined pass order.

    Compound nodes whose targets are nested (e.g. a `Parallel` that is
    itself a branch of a `Router`) are STILL emitted here — they need a
    module-level name so the outer compound's `to_source` can reference
    them. `nested_children` only governs exclusion from the top-level
    `_steps` assembly (see `assemble_workflow`).
    """
    blocks: list[str] = []
    nodes_by_id = ctx.nodes_by_id
    for ntype in _pass2_compound_order():
        spec = NODE_TYPES.get(ntype)
        if spec is None:
            continue
        strategy = spec.strategy
        for nid in ctx.ir.topo_order:
            node = nodes_by_id.get(nid)
            if node is None:
                continue
            if node["type"] != ntype:
                continue
            blocks.append(strategy.to_source(nid, node, ctx))
    return "".join(blocks)

def _pass3_tool_wiring_source(ctx: CompileCtx) -> str:
    """Pass 3: wire each agent's `tools=[...]` (the source equivalent)."""
    from app.core.compile._helpers.tools_expr import tools_list

    blocks: list[str] = []
    nodes_by_id = ctx.nodes_by_id
    for nid in ctx.ir.topo_order:
        if nid in ctx.ir.nested_children:
            continue
        node = nodes_by_id.get(nid)
        if node is None:
            continue
        spec = NODE_TYPES.get(node["type"])
        if spec is None:
            continue
        if not spec.strategy.NEEDS_TOOL_WIRING:
            continue
        refs = (
            ctx.ir.tool_attachments.get(nid)
            or ctx.ir.tool_refs.get(nid)
            or []
        )
        refs = [t for t in refs if t in nodes_by_id]
        if not refs:
            continue
        expr = tools_list(refs, nodes_by_id, ctx.http_wrappers)
        blocks.append(f"{nid}_agent.tools = {expr}\n")
    return "".join(blocks)


def _pass3_knowledge_wiring_source(ctx: CompileCtx) -> str:
    """Pass 3b: wire each agent's `knowledge=...` (the source equivalent).

    Emits `<nid>_agent.knowledge = <ref>_kb` lines — the runtime pass
    sets the attribute directly via `setattr`; the source path
    mirrors that with a literal assignment. `knowledge_ref(ref)`
    returns the variable expression (`<ref>_kb`) emitted in pass 0b.

    Empty `refs` / dangling refs are silently skipped (the connection
    validator rejects dangling edges on save, so this is a belt-and-
    braces defensive skip — matches `_pass3_tool_wiring_source`).
    """
    from app.core.compile._helpers.knowledge_expr import knowledge_ref

    blocks: list[str] = []
    nodes_by_id = ctx.nodes_by_id
    for agent_id, refs in ctx.ir.knowledge_attachments.items():
        if not refs:
            continue
        ref = refs[0]
        if ref not in nodes_by_id:
            continue
        blocks.append(f"{agent_id}_agent.knowledge = {knowledge_ref(ref)}\n")
    return "".join(blocks)

def _main_entry() -> str:
    """The `def main()` + `if __name__ == '__main__':` footer."""
    return (
        "\n\ndef main():\n"
        "    user_input = input('Enter your input: ')\n"
        "    workflow.print_response(user_input)\n"
        "\n\nif __name__ == '__main__':\n    main()\n"
    )

__all__ = ["to_python_source"]
