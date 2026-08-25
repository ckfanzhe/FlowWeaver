"""BranchStrategy — .

Merges the prior standalone `router` + `condition` strategies into a
single mode-aware class. The manifest's `config.mode` discriminator
selects between:

  - `switch` — N-ary routing via `agno.workflow.router.Router(
    selector=fn|str|None, choices=[...])`. Three selector modes:
    `function` (Python expression), `cel` (CEL string), `hitl`
    (agno pauses + asks the user). No LLM picker (phase.1 / P1.3
    decision).
  - `if-else` — binary condition via `agno.workflow.condition.Condition(
    evaluator=fn|str|bool, steps=[then], else_steps=[else])`. Three
    evaluator modes: `function`, `cel`, `literal`. Optional HITL via
    `requires_confirmation` / `confirmation_message`.

The emitted object name is mode-aware — `object_suffix()` returns
`_router` for `mode='switch'` (matches the prior `Router` primitive's
shape) and `_condition` for `mode='if-else'` (matches the prior
`Condition` primitive's shape). That preserves every prior exported
`.py` byte-stable so users' existing files keep working.

Contract: function-mode selectors MUST return a
branch STEP OBJECT (`<branch_id>_step`), NOT a label string. The
diagnostic export on the bug case surfaced the LLM emitting "yes"
(label) instead of `yes_agent_step`; the chat-builder prompt now
explicitly teaches the rule.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Literal, Optional

from app.core.compile.expressions import (
    emit_selector_locals_source,
    selector_locals_names,
)

from .base import NodeStrategy

_log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# switch-mode helpers (carried over from the prior `router.py`)
# ─────────────────────────────────────────────────────────────────
def _normalize_cfg(cfg: dict) -> dict:
    """Run the raw config dict through `BranchNodeConfig.model_validate`
    so the `mode='before'` `_migrate_legacy_condition` validator fires
    (phase.1 legacy DSL → `evaluator` migration + alias coercion +
    default fill-in). Returns the dumped dict so the caller's downstream
    field reads (`cfg["evaluator"]["expression"]`) work as expected.

    : both `build()` and `to_source()` route through
    this so an envelope carrying `condition: "contains:hi"` (the legacy
    DSL string) produces the same `'hi' in previous_step_content` runtime
    primitive whether the workflow was saved via the API or hand-built
    by a test fixture.
    """
    from app.schemas.node_configs import BranchNodeConfig
    return BranchNodeConfig.model_validate(cfg).model_dump(by_alias=True)

def _resolve_selector_cfg(cfg: dict) -> dict[str, str]:
    """Return `{mode, expression, fallback_message}` for a branch's switch mode."""
    sel_cfg = (cfg.get("selector") or {})
    return {
        "mode": (sel_cfg.get("mode") or "function").lower(),
        "expression": sel_cfg.get("expression") or "",
        "fallback_message": (
            sel_cfg.get("fallback_message")
            or sel_cfg.get("fallbackMessage")
            or ""
        ),
    }

def _make_function_selector(mode: str, expression: str, nid: str):
    """Build a `selector(step_input) -> step_or_list` callable for switch / function mode.

    Mirrors the prior `RouterStrategy._make_function_selector`. Runtime
    failures return `[]` (fail-open — matches the existing safety net).
    """
    expr = expression or "None"

    def selector(step_input):
        scope = _build_router_scope(step_input)
        try:
            return _eval_router_expression(expr, step_input, scope)
        except Exception as exc:  # noqa: BLE001 — fail-open to no branch
            _log.warning(
                "Branch %s (mode=switch) selector raised %s; returning [].",
                nid, exc,
            )
            return []

    return selector

def _render_function_selector(nid: str, expression: str) -> str:
    """Render `def {nid}_selector(step_input)` for switch / function mode export."""
    from app.core.compile._helpers.utils import docstring

    expr_repr = expression or "None"
    d = docstring(
        f"Branch selector: pick a branch for {nid}.\n"
        f"Expression: {expr_repr}"
    )
    locals_block = emit_selector_locals_source()
    return (
        f"def {nid}_selector(step_input):\n"
        f"    {d}\n"
        f"{locals_block}\n"
        f"    return ({expr_repr})\n"
    )

def _build_router_scope(step_input) -> dict[str, Any]:
    """Mirror `compile.condition._build_scope` for the branch (switch) evaluator.

    Same 5 in-scope locals so users have one mental model for both
    `if-else` and `switch` expressions.
    """
    from app.core.compile.condition import _build_scope
    return _build_scope(step_input)

def _eval_router_expression(expr: str, step_input, scope: dict):
    """Compile + eval the function-mode expression with the scope locals.

    The expression returns a step object (or list) instead of bool —
    that's the shape agno's `Router.selector` expects.
    """
    src = (
        f"def _eval(step_input, {', '.join(selector_locals_names())}):\n"
        f"    return ({expr})\n"
    )
    gns: dict[str, Any] = {"__builtins__": __builtins__}
    lcls: dict[str, Any] = {}
    exec(compile(src, "<branch-selector>", "exec"), gns, lcls)
    return lcls["_eval"](step_input, **scope)

# ─────────────────────────────────────────────────────────────────
# if-else-mode helpers (carried over from the prior `condition.py`)
# ─────────────────────────────────────────────────────────────────
def _resolve_evaluator_cfg(cfg: dict) -> tuple[str, str]:
    """Return `(mode, expression)` for a branch's if-else mode.

    Reads the `evaluator` field directly — `BranchNodeConfig`'s
    `mode='before'` validator has already migrated any legacy
    `condition: 'always'` strings to `evaluator`.
    """
    evaluator_cfg = cfg.get("evaluator") or {}
    mode = (evaluator_cfg.get("mode") or "function").lower()
    expression = evaluator_cfg.get("expression") or ""
    return mode, expression

def _step_ref(tid: str | None) -> str:
    if not tid:
        return "None"
    return f"{tid}_step"

# ─────────────────────────────────────────────────────────────────
# BranchStrategy — mode-aware dispatcher
# ─────────────────────────────────────────────────────────────────
class BranchStrategy(NodeStrategy):
    """`Router(...)` when mode='switch', `Condition(...)` when mode='if-else'."""

    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "compound"
    # Matches the prior `condition` strategy (20) so the IR builder's
    # compound-pass ordering keeps the same semantics. The prior
    # `router` was 40; the collapse to a single pass is intentional —
    # the two runtime primitives share the same lifecycle phase
    # (children inlined into the parent workflow before loop body).
    COMPOUND_PASS: ClassVar[Optional[int]] = 20
    IS_TOOL_SOURCE: ClassVar[bool] = False
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "none"

    # ─────────────────────────────────────────────────────────────
    # Runtime build
    # ─────────────────────────────────────────────────────────────
    def build(self, nid: str, node: dict, ctx: Any) -> Any:
        cfg = _normalize_cfg((node.get("data") or {}).get("config") or {})
        label = (node.get("data") or {}).get("label") or nid
        mode = (cfg.get("mode") or "switch").lower()

        if mode == "switch":
            return self._build_switch(nid, node, cfg, label, ctx)
        if mode == "if-else":
            return self._build_if_else(nid, node, cfg, label, ctx)
        raise RuntimeError(
            f"branch {nid!r}: unknown mode {mode!r}; expected 'switch' or 'if-else'"
        )

    def _build_switch(self, nid: str, node: dict, cfg: dict, label: str, ctx: Any):
        """Build `agno.workflow.router.Router(selector=fn|str|None, choices=[...])`."""
        from agno.workflow.router import Router

        sel = _resolve_selector_cfg(cfg)
        mode = sel["mode"]
        expression = sel["expression"]
        fallback_message = sel["fallback_message"]

        ir = ctx.ir
        branch_ids = list(ir.branch_branches.get(nid, []))
        if not branch_ids:
            raise RuntimeError(f"branch {nid!r} (mode=switch) has no branches")

        choice_steps: list = []
        for tgt in branch_ids:
            if tgt in ctx.objects:
                choice_steps.append(ctx.objects[tgt])

        kwargs: dict[str, Any] = {"name": label, "choices": choice_steps}

        if mode == "hitl":
            kwargs["requires_user_input"] = True
            if fallback_message:
                kwargs["user_input_message"] = fallback_message
        elif mode == "cel":
            kwargs["selector"] = expression or "True"
        else:  # "function" (default)
            kwargs["selector"] = _make_function_selector(mode, expression, nid)

        return Router(**kwargs)

    def _build_if_else(self, nid: str, node: dict, cfg: dict, label: str, ctx: Any):
        """Build `agno.workflow.condition.Condition(evaluator, steps, else_steps)`."""
        from agno.workflow.condition import Condition
        from app.core.compile.condition import make_evaluator

        mode, expression = _resolve_evaluator_cfg(cfg)
        requires_confirmation = bool(
            cfg.get("requiresConfirmation") or cfg.get("requires_confirmation")
        )
        confirmation_message = (
            cfg.get("confirmationMessage")
            or cfg.get("confirmation_message")
            or ""
        )

        ir = ctx.ir
        branches = ir.get_branch_branches(nid)
        then_id, else_id = branches[0], branches[1]
        if not then_id:
            raise RuntimeError(
                f"branch {nid!r} (mode=if-else) has no 'then' target — connect an edge"
            )
        then_obj = ctx.objects.get(then_id)
        if then_obj is None:
            raise RuntimeError(f"branch {nid!r} then-target not in graph objects")
        else_obj = ctx.objects.get(else_id) if else_id else None

        kwargs: dict[str, Any] = {
            "name": label,
            "steps": [then_obj],
        }

        if mode == "cel":
            kwargs["evaluator"] = expression or "True"
        else:
            evaluator = make_evaluator(mode, expression)
            kwargs["evaluator"] = evaluator if evaluator is not None else (lambda _si: True)

        if else_obj is not None:
            kwargs["else_steps"] = [else_obj]

        if requires_confirmation:
            kwargs["requires_confirmation"] = True
            if confirmation_message:
                kwargs["confirmation_message"] = confirmation_message

        return Condition(**kwargs)

    # ─────────────────────────────────────────────────────────────
    # Source emission
    # ─────────────────────────────────────────────────────────────
    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        cfg = _normalize_cfg((node.get("data") or {}).get("config") or {})
        label = (node.get("data") or {}).get("label") or nid
        mode = (cfg.get("mode") or "switch").lower()

        if mode == "switch":
            return self._to_source_switch(nid, cfg, label, ctx)
        if mode == "if-else":
            return self._to_source_if_else(nid, cfg, label, ctx)
        raise RuntimeError(
            f"branch {nid!r}: unknown mode {mode!r}; expected 'switch' or 'if-else'"
        )

    def _to_source_switch(self, nid: str, cfg: dict, label: str, ctx: Any) -> str:
        """Emit `<nid>_selector` (function mode) + `<nid>_router = Router(...)`."""
        from app.core.compile._helpers.utils import q

        sel = _resolve_selector_cfg(cfg)
        mode = sel["mode"]
        expression = sel["expression"]
        fallback_message = sel["fallback_message"]
        label_repr = q(label)

        ir = ctx.ir
        branch_ids = list(ir.branch_branches.get(nid, []))

        out_lines: list[str] = []

        if mode == "function":
            out_lines.append(_render_function_selector(nid, expression))
            selector_kwarg = f"{nid}_selector"
        elif mode == "cel":
            selector_kwarg = q(expression or "True")
        else:  # "hitl"
            selector_kwarg = "None"

        choices_parts: list[str] = []
        for tgt in branch_ids:
            # H : the cross-type ref is unified through
            # `ir.object_suffix(tgt)` — covers `agent`, `branch` (any
            # mode), `flow` (mode-aware), `loop`, etc.
            choices_parts.append(f"{tgt}{ir.object_suffix(tgt)}")
        choices_repr = "[" + ", ".join(choices_parts) + "]"

        router_lines = [f"{nid}_router = Router(\n", f"    name={label_repr},\n"]
        if mode == "hitl":
            router_lines.append(f"    choices={choices_repr},\n")
            router_lines.append("    requires_user_input=True,\n")
            if fallback_message:
                router_lines.append(f"    user_input_message={q(fallback_message)},\n")
        else:
            router_lines.append(f"    choices={choices_repr},\n")
            router_lines.append(f"    selector={selector_kwarg},\n")
        router_lines.append(")\n")
        out_lines.append("".join(router_lines))
        return "".join(out_lines)

    def _to_source_if_else(self, nid: str, cfg: dict, label: str, ctx: Any) -> str:
        """Emit `<nid>_evaluator` (function/literal) + `<nid>_condition = Condition(...)`."""
        from app.core.compile._helpers.utils import docstring, q

        mode, expression = _resolve_evaluator_cfg(cfg)
        requires_confirmation = bool(
            cfg.get("requiresConfirmation") or cfg.get("requires_confirmation")
        )
        confirmation_message = (
            cfg.get("confirmationMessage")
            or cfg.get("confirmation_message")
            or ""
        )
        label_repr = q(label)

        ir = ctx.ir
        branches = ir.get_branch_branches(nid)
        then_id, else_id = branches[0], branches[1]
        if not then_id:
            raise RuntimeError(
                f"branch {nid!r} (mode=if-else) has no 'then' target — connect an edge"
            )

        out_lines: list[str] = []

        if mode == "function":
            expr_repr = expression or "True"
            d = docstring(f"Evaluate condition: {expr_repr}")
            locals_block = emit_selector_locals_source()
            out_lines.append(
                f"def {nid}_evaluator(step_input):\n"
                f"    {d}\n"
                f"{locals_block}\n"
                f"    return ({expr_repr})\n"
            )
            evaluator_kwarg = f"{nid}_evaluator"
        elif mode == "cel":
            evaluator_kwarg = q(expression or "True")
        else:  # literal
            normalised = (expression or "").strip().lower()
            evaluator_kwarg = "True" if normalised != "false" else "False"

        cond_lines = [
            f"{nid}_condition = Condition(\n",
            f"    name={label_repr},\n",
            f"    steps=[{_step_ref(then_id)}],\n",
            f"    evaluator={evaluator_kwarg},\n",
        ]
        if else_id:
            cond_lines.append(f"    else_steps=[{_step_ref(else_id)}],\n")
        if requires_confirmation:
            cond_lines.append(f"    requires_confirmation=True,\n")
            if confirmation_message:
                cond_lines.append(f"    confirmation_message={q(confirmation_message)},\n")
        cond_lines.append(")\n")
        out_lines.append("".join(cond_lines))
        return "".join(out_lines)

__all__ = ["BranchStrategy"]