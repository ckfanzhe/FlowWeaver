"""Condition-template parser + evaluator factory.

Two surfaces live here:

  1. `parse_condition_template` + `migrate_legacy_condition` — the
     legacy DSL (`contains:/equals:/regex:/always/never`). Used only
     by the migration path on save (see
     `schemas.node_configs.ConditionNodeConfig._migrate_legacy_condition`).
     The runtime no longer consults the DSL.

  2. `make_evaluator(mode, expression)` — the new agno-native factory.
     Returns a `Callable[[StepInput], bool]` for `function` and
     `literal` modes. For `cel` mode the caller skips this function
     entirely — the string is passed verbatim to
     `Condition(evaluator="...")`.

The runtime and the export share these callables verbatim. The export
emits them as text in the generated `.py`; the runtime invokes them
as Python callables. Same semantics, no drift.

Function-mode expressions have 5 in-scope locals (matches agno's
`StepInput` shape):

  - `previous_step_content` (str | None)
  - `previous_step_outputs` (dict[str, str])
  - `input` (str | None) — workflow input
  - `additional_data` (dict[str, Any])
  - `session_state` (dict[str, Any])

Bad expressions fail loudly at import time (export) or at first
invocation (runtime) — we don't try to lint.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from app.core.compile.expressions import (
    build_selector_scope,
    selector_locals_names,
)

# ─────────────────────────────────────────────────────────────────
# Legacy DSL — only used by migration
# ─────────────────────────────────────────────────────────────────

def parse_condition_template(raw: str) -> tuple[str, str]:
    """Parse the v0 condition DSL `(op, value)`. Empty → `("always", "")`.

    Grammar (case-insensitive prefix):
      contains:foo  → substring `foo` in upstream text
      equals:42     → exact match (string equality)
      regex:^\\d+$  → `re.search` against the upstream text
      always        → always True
      never         → always False
      (anything else is treated as `contains:<raw>`)
    """
    s = (raw or "").strip()
    if not s:
        return "always", ""
    lower = s.lower()
    if lower == "always":
        return "always", ""
    if lower == "never":
        return "never", ""
    for prefix in ("contains:", "equals:", "regex:"):
        if lower.startswith(prefix):
            return prefix[:-1], s[len(prefix):]
    return "contains", s

def migrate_legacy_condition(raw: str) -> dict[str, str]:
    """Translate the v0 DSL string into a `{mode, expression}` dict.

    phase.1 : called by
    `ConditionNodeConfig._migrate_legacy_condition` whenever a saved
    workflow carries the legacy `condition` field. Returns the new
    representation; the caller wraps it in `ConditionEvaluator`.

    Mapping:
      always            → literal  | True
      never             → literal  | False
      contains:foo      → function | "'foo' in previous_step_content"
      equals:42         → function | "previous_step_content == '42'"
      regex:^\\d+$      → function | bool(__import__('re').search(...))
      foo               → function | "'foo' in previous_step_content"

    The expression values are escaped via `repr` for the substring
    part so embedded quotes / backslashes don't break the generated
    Python.
    """
    op, value = parse_condition_template(raw)
    if op == "always":
        return {"mode": "literal", "expression": "True"}
    if op == "never":
        return {"mode": "literal", "expression": "False"}
    if op == "contains":
        return {
            "mode": "function",
            "expression": f"{value!r} in (previous_step_content or '')",
        }
    if op == "equals":
        return {
            "mode": "function",
            "expression": f"(previous_step_content or '') == {value!r}",
        }
    if op == "regex":
        # Generated eval imports re lazily so the export doesn't pay
        # the import cost when the workflow never reaches this branch.
        return {
            "mode": "function",
            "expression": (
                f"(__import__('re').search({value!r}, "
                f"previous_step_content or '') is not None)"
            ),
        }
    # Defensive — parse_condition_template can't return anything else,
    # but be explicit so future grammars don't fall through silently.
    return {"mode": "literal", "expression": "True"}

# ─────────────────────────────────────────────────────────────────
# Agno-native evaluator factory (phase.1+)
# ─────────────────────────────────────────────────────────────────

def make_evaluator(
    mode: str,
    expression: str,
) -> Optional[Callable[[object], bool]]:
    """Build an agno `Condition.evaluator(step_input) -> bool`.

    Args:
      mode: `"function"` or `"literal"`. `"cel"` is NOT handled here —
        callers should pass the expression string directly to
        `Condition(evaluator=<str>)`.
      expression:
        - function mode: a Python expression string evaluated in a
          scope exposing `previous_step_content`,
          `previous_step_outputs`, `input`, `additional_data`, and
          `session_state`.
        - literal mode: the literal `"True"` or `"False"` (case-
          insensitive).

    Returns:
      A callable for `function`/`literal` modes; `None` for `cel`
      (the caller is expected to pass the raw string to agno).

    Runtime failures (typo in expression, KeyError on
    `previous_step_outputs["x"]`, etc.) are caught and treated as
    `False` — same fallback as the old regex-mismatch case. We log
    a warning so users can debug without crashing the workflow.
    """
    mode = (mode or "").lower()
    if mode == "cel":
        return None  # signal: caller should pass the string directly

    if mode == "literal":
        normalised = (expression or "").strip().lower()
        if normalised == "true":
            return lambda _step_input: True
        if normalised == "false":
            return lambda _step_input: False
        # Treat anything else as a function expression — friendlier for
        # users who type "True" / "False" without knowing the literal mode
        return _compile_function_evaluator(expression or "")

    # Default to function mode for safety
    return _compile_function_evaluator(expression or "")

def _compile_function_evaluator(
    expression: str,
) -> Callable[[object], bool]:
    """Wrap a Python expression in a runtime-safe evaluator.

    The compiled inner function takes all 6 names as positional/keyword
    args (`step_input` + the 5 scope locals). The outer evaluator
    binds them every call:

        def _eval(step_input, previous_step_content,
                  previous_step_outputs, input,
                  additional_data, session_state):
            return (<user expression>)

    The returned callable:
      - rebuilds the scope dict from the live StepInput every call
      - traps `Exception` (NameError, KeyError, AttributeError, etc.)
        and returns False (matches the old regex-error fallback)
      - emits a single warning per *distinct* expression per process
        (avoids log flooding on a 1000-iteration refinement loop)
    """
    import logging
    _log = logging.getLogger(__name__)

    # 6 params — step_input first, then the 5 locals from
    # `compile.expressions.SELECTOR_LOCALS` (single source of truth,
    # shared with the codegen emitters in `strategies.{router,condition}`).
    _sig = ", ".join(selector_locals_names())
    src = (
        f"def _eval(step_input, {_sig}):\n"
        f"    return ({expression})\n"
    )
    gns: dict[str, Any] = {"__builtins__": __builtins__}
    lcls: dict[str, Any] = {}
    try:
        exec(compile(src, "<condition-evaluator>", "exec"), gns, lcls)
    except SyntaxError as exc:
        raise ValueError(
            f"Condition evaluator expression is not valid Python: "
            f"{expression!r} ({exc.msg})"
        ) from exc
    fn = lcls["_eval"]

    seen_expressions: set[str] = set()

    def evaluator(step_input: object) -> bool:
        scope = _build_scope(step_input)
        try:
            return bool(fn(step_input, **scope))
        except Exception as exc:  # noqa: BLE001 — fail-open to False
            if expression not in seen_expressions:
                seen_expressions.add(expression)
                _log.warning(
                    "Condition evaluator raised %s; returning False. "
                    "Expression: %r. Update the condition node to fix.",
                    type(exc).__name__, expression,
                )
            return False

    return evaluator

def _build_scope(step_input: object) -> dict[str, Any]:
    """Extract the 5 evaluator locals from a `StepInput` (or duck-type).

    Thin wrapper — the actual extraction lives in
    `compile.expressions.build_selector_scope` (single source of truth,
    shared with `strategies.{router,condition}` codegen emitters).
    """
    return build_selector_scope(step_input)

def _takes_step_input(fn: Callable[..., Any]) -> bool:
    """Did the user reference `step_input` in their expression?

    Kept for back-compat with earlier reflection-based dispatch;
    no longer used by `_compile_function_evaluator` (which always
    binds step_input explicitly). The expression is wrapped in a
    fixed-signature closure that always takes `step_input`, so any
    caller code can still introspect this if they want.
    """
    import inspect
    try:
        return "step_input" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False

__all__ = [
    "parse_condition_template",
    "migrate_legacy_condition",
    "make_evaluator",
]