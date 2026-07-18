"""Workflow assembly — the `Workflow(steps=[...])` block.

The set of "top-level" step ids comes from `ir.top_level_step_ids()` —
the same authoritative set the runtime executor reads. We don't compute
child ids here; the IR does it once and both consumers stay in sync.
"""
from __future__ import annotations

from .utils import q
from app.core.ir import WorkflowIR

def assemble_workflow(name: str, ir: WorkflowIR) -> str:
    """Build the `Workflow(steps=[...])` source block.

    Convention:
      - Compound nodes (`parallel` / `router` / `condition` / `loop` /
        `steps`) emit a single object (`<nid>_parallel` etc.) — that's
        the only thing that goes into the top-level `steps` list. Their
        children are inlined into those compound objects in pass 2
        (handled by the per-type emitters, not here).
      - Everything else emits `<nid>_step`.
    """
    step_ids = ir.top_level_step_ids()

    lines: list[str] = []
    lines.append("_steps = []\n")
    for nid in step_ids:
        # H: compound-vs-executable suffix comes from the IR's
        # `object_suffix()` (single source of truth — also read by
        # `strategies.steps.StepsStrategy._branch_ref`).
        suffix = ir.object_suffix(nid)
        lines.append(f"_steps.append({nid}{suffix})\n")
    lines.append("\n")
    lines.append(
        f"workflow = Workflow(name={q(name)}, steps=_steps)\n"
    )
    return "".join(lines)