"""5-locals scope shared by condition + router evaluators.

Single source of truth for the in-scope names both node kinds expose
to user-written expressions. Consumed by:

  - `compile.condition._compile_evaluator`  (runtime scope build + the
    `_eval` wrapper signature that gets exec'd)
  - `compile.condition._build_scope`         (runtime scope dict)
  - `strategies.router.to_source`            (codegen source string
    for the `Router` selector function)
  - `strategies.condition.to_source`         (codegen source string
    for the `Condition` evaluator function)

If you ever need to add a 6th local (e.g. `now`), do it here once —
all four sites pick it up automatically.
"""

from typing import Any

# Ordered (name, default) pairs — order matters: it's the declaration
# order in the generated `_eval` signature and the iteration order of
# the live runtime scope dict.
SELECTOR_LOCALS: tuple[tuple[str, Any], ...] = (
    ("previous_step_content", None),
    ("previous_step_outputs", {}),
    ("input", None),
    ("additional_data", {}),
    ("session_state", {}),
)

def selector_locals_names() -> tuple[str, ...]:
    """Names in declaration order — used to build the `_eval` signature."""
    return tuple(name for name, _ in SELECTOR_LOCALS)

def build_selector_scope(step_input: object) -> dict[str, Any]:
    """Extract the 5 evaluator locals from a `StepInput` (or duck-type).

    Mirrors the legacy `_build_scope` body: dict defaults get `or {}`
    coercion so `None` from a malformed step_input doesn't blow up the
    user expression downstream.
    """
    scope: dict[str, Any] = {}
    for name, default in SELECTOR_LOCALS:
        value = getattr(step_input, name, default)
        # Dict defaults: coalesce None / falsy → empty dict.
        if isinstance(default, dict) and not value:
            value = {}
        scope[name] = value
    return scope

def emit_selector_locals_source(indent: str = "    ") -> str:
    """Return the 5-line `name = getattr(step_input, ...)` block that
    codegen emits at the top of every `_eval` body. Lines are joined
    with `\\n`; the caller is responsible for the trailing newline.
    """
    lines = []
    for name, default in SELECTOR_LOCALS:
        default_repr = "{}" if isinstance(default, dict) else repr(default)
        if isinstance(default, dict):
            lines.append(
                f"{indent}{name} = getattr(step_input, '{name}', "
                f"{default_repr}) or {default_repr}"
            )
        else:
            lines.append(
                f"{indent}{name} = getattr(step_input, '{name}', {default_repr})"
            )
    return "\n".join(lines)