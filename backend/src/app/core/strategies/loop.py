"""LoopStrategy — `Loop(steps=[body], max_iterations=..., end_condition=...)`.

A `loop` node wraps a single body step and re-runs it up to
`max_iterations` times, stopping early when `end_condition` substring-
matches the body's last step output. The body id comes from the IR —
`ir.loop_bodies[nid]`.

phase.1 (, P1.5 Loop HITL): pass through two HITL knobs to
agno's `Loop.human_review` (built from the flat params
`requires_confirmation` / `requires_iteration_review`). These are NOT
context-manager nodes — they just gate execution.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from .base import NodeStrategy

class LoopStrategy(NodeStrategy):
    """`Loop(steps=[body], max_iterations=..., end_condition=...)`."""

    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "compound"
    COMPOUND_PASS: ClassVar[Optional[int]] = 30
    IS_TOOL_SOURCE: ClassVar[bool] = False
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "none"

    def build(self, nid: str, node: dict, ctx: Any) -> Any:
        """Build an agno `Loop` with the body step from the IR."""
        from agno.workflow.loop import Loop

        cfg = node["data"].get("config") or {}
        label = node["data"].get("label") or nid
        ir = ctx.ir
        body_id = ir.loop_bodies.get(nid)
        if not body_id or body_id not in ctx.objects:
            raise RuntimeError(
                f"loop {nid!r} has no body — pick a step to repeat in the property panel"
            )
        body_obj = ctx.objects[body_id]
        max_iter = int(cfg.get("maxIterations") or 3)
        forward = bool(cfg.get("forwardIterationOutput") or False)
        end_raw = (cfg.get("endCondition") or "").strip()

        def end_condition(step_outputs: list) -> bool:
            if not end_raw or not step_outputs:
                return False
            last = step_outputs[-1]
            content = getattr(last, "content", None)
            if content is None and isinstance(last, dict):
                content = last.get("content")
            if content is None:
                return False
            return end_raw in str(content)

        return Loop(
            name=label,
            steps=[body_obj],
            max_iterations=max_iter,
            end_condition=end_condition,
            forward_iteration_output=forward,
            # HITL — phase.1 (P1.5). Loop accepts both flat fields and a
            # HumanReview object; the flat fields are simpler and there's no
            # `human_review` override planned in the v1 shape.
            requires_confirmation=bool(cfg.get("requiresConfirmation") or False),
            confirmation_message=cfg.get("confirmationMessage") or None,
            requires_iteration_review=bool(
                cfg.get("requiresIterationReview") or False
            ),
            iteration_review_message=cfg.get("iterationReviewMessage") or None,
        )

    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit `<nid>_loop = Loop(...)`."""
        from app.core.compile._helpers.utils import q

        cfg = node["data"].get("config") or {}
        label = node["data"].get("label") or nid
        label_repr = q(label)
        ir = ctx.ir
        body_id = ir.loop_bodies.get(nid)
        try:
            max_iter = max(1, int(cfg.get("maxIterations") or 3))
        except (TypeError, ValueError):
            max_iter = 3
        forward = bool(cfg.get("forwardIterationOutput"))
        end_raw = (cfg.get("endCondition") or "").strip()
        end_repr = q(end_raw) if end_raw else "None"
        body_ref = f"{body_id}_step" if body_id else "None"

        # HITL — phase.1 (P1.5). Only emit the kwargs the user actually
        # set; otherwise the generated code stays short and matches the
        # pre-HITL snapshots (no churn for users who don't opt in).
        hitl_kwargs: list[str] = []
        if cfg.get("requiresConfirmation"):
            hitl_kwargs.append("    requires_confirmation=True,")
            msg = (cfg.get("confirmationMessage") or "").strip()
            if msg:
                hitl_kwargs.append(f"    confirmation_message={q(msg)},")
        if cfg.get("requiresIterationReview"):
            hitl_kwargs.append("    requires_iteration_review=True,")
            msg = (cfg.get("iterationReviewMessage") or "").strip()
            if msg:
                hitl_kwargs.append(f"    iteration_review_message={q(msg)},")
        hitl_block = "\n".join(hitl_kwargs)
        if hitl_block:
            hitl_block = hitl_block + "\n"

        return (
            f"{nid}_loop = Loop(\n"
            f"    name={label_repr},\n"
            f"    steps=[{body_ref}],\n"
            f"    max_iterations={max_iter},\n"
            f"    end_condition={end_repr},\n"
            f"    forward_iteration_output={'True' if forward else 'False'},\n"
            f"{hitl_block}"
            f")\n"
        )

__all__ = ["LoopStrategy"]