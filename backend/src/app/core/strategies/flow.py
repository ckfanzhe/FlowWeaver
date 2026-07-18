"""FlowStrategy — `Parallel(*branches, name=...)` or `Steps(steps=[...], name=...)`.

: replaces the standalone `ParallelStrategy` and
`StepsStrategy` with a single `mode`-discriminated class. The two
runtime primitives (`agno.workflow.parallel.Parallel`,
`agno.workflow.steps.Steps`) had identical branch emission and differed
only in the wrapper class and the two HITL kwargs. Merging them is a
pure consolidation — no semantic divergence beyond what
`config.mode` already declares.

Branch resolution: edges are the canonical source (the IR exposes
`flow_branches[nid]`); the legacy `cfg.branches` list is a display-only
mirror carried in the exported JSON.

HITL kwargs (`requires_confirmation`, `confirmation_message`) only
take effect in `mode='sequential'`. `mode='parallel'` ignores them —
parallel branches with HITL are an oxymoron and `Loop` is the
recommended primitive for per-iteration gates.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from .base import NodeStrategy

class FlowStrategy(NodeStrategy):
    """`Parallel(...)` when `mode='parallel'`, `Steps(...)` otherwise."""

    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "compound"
    COMPOUND_PASS: ClassVar[Optional[int]] = 10
    IS_TOOL_SOURCE: ClassVar[bool] = False
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "none"

    @staticmethod
    def _branch_arg(tgt: str, ctx: Any) -> str:
        """Render a single branch arg as a Python expression.

        Dispatch by the target node's type — same shape as the prior
        `ParallelStrategy.to_source` and `StepsStrategy._coerce_arg`.
        Agent children get `Step(name=..., agent=<tid>_agent)`;
        compound children get `<tid><suffix>`; everything else falls
        through to a bare `Step` placeholder so the import doesn't
        crash on ask / unknown shapes. :
        `human_input` was renamed to `ask` (kind=`control_flow`);
        ask gets the same `<tid>_step` suffix as agent via
        `ir.object_suffix()`.
        """
        from app.core.compile._helpers.utils import q

        tgt_node = ctx.ir.node_map.get(tgt)
        if tgt_node is None:
            return f"Step(name={q(tgt)}, agent=None)"
        tt = tgt_node.type
        if tt == "agent":
            return f"Step(name={q(tgt_node.data.get('label') or tgt)}, agent={tgt}_agent)"
        # H: compound-vs-executable suffix comes from the IR's
        # `object_suffix()` — same table that drives
        # `compile._helpers.assembly.assemble_workflow`.
        suffix = ctx.ir.object_suffix(tgt)
        if suffix != "_step":
            return f"{tgt}{suffix}"
        return f"Step(name={q(tgt_node.data.get('label') or tgt)}, agent=None)"

    def build(self, nid: str, node: dict, ctx: Any) -> Any:
        """Build an agno `Parallel(...)` or `Steps(...)` whose branches
        are the children the IR nested under this node.

        Dispatch on `config.mode`:
          * `'parallel'`    → `Parallel(*branch_steps, name=label)`
          * `'sequential'`  → `Steps(name=label, steps=branch_steps,
                                     requires_confirmation=...,
                                     confirmation_message=...)`
        """
        cfg = node["data"].get("config") or {}
        mode = cfg.get("mode") or "parallel"
        label = node["data"].get("label") or nid
        ir = ctx.ir
        branch_ids = list(ir.flow_branches.get(nid, []))
        if not branch_ids:
            raise RuntimeError(
                f"flow {nid!r} has no branches — connect at least one edge"
            )
        branch_steps: list = [
            ctx.objects[tgt] for tgt in branch_ids if tgt in ctx.objects
        ]
        if mode == "parallel":
            from agno.workflow.parallel import Parallel
            return Parallel(*branch_steps, name=label)
        from agno.workflow.steps import Steps
        return Steps(
            name=label,
            steps=branch_steps,
            requires_confirmation=bool(cfg.get("requiresConfirmation") or False),
            confirmation_message=cfg.get("confirmationMessage") or None,
        )

    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit a `Parallel(...)` or `Steps(...)` source block at the top level.

        The branches are inlined — `Step(name=..., agent=<branch>_agent)`
        for agent children, or the compound object for nested compounds.
        HITL kwargs (`requires_confirmation`, `confirmation_message`)
        are only emitted in `mode='sequential'`.
        """
        from app.core.compile._helpers.utils import q

        cfg = node["data"].get("config") or {}
        mode = cfg.get("mode") or "parallel"
        label = node["data"].get("label") or nid
        label_repr = q(label)
        ir = ctx.ir
        branch_ids = list(ir.flow_branches.get(nid, []))
        branch_args: list[str] = [self._branch_arg(tgt, ctx) for tgt in branch_ids]

        if mode == "parallel":
            return (
                f"{nid}_parallel = Parallel("
                f"{', '.join(branch_args)}, name={label_repr})\n"
            )

        # mode == 'sequential' → Steps(...)
        extra_kwargs: list[str] = []
        if cfg.get("requiresConfirmation"):
            extra_kwargs.append("    requires_confirmation=True,")
            msg = (cfg.get("confirmationMessage") or "").strip()
            if msg:
                extra_kwargs.append(f"    confirmation_message={q(msg)},")
        extra_block = "\n".join(extra_kwargs)
        if extra_block:
            extra_block = extra_block + "\n"
        return (
            f"{nid}_steps = Steps(\n"
            f"    name={label_repr},\n"
            f"    steps=[{', '.join(branch_args)}],\n"
            f"{extra_block}"
            f")\n"
        )

__all__ = ["FlowStrategy"]