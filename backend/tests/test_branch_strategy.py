"""Unit tests for `BranchStrategy` — the mode-aware merger of the prior
`router` + `condition` strategies.

Covers:

  - switch × {function, cel, hitl} selector modes
  - if-else × {function, cel, literal} evaluator modes
  - the legacy `condition: "contains:hi"` DSL migration through
    `BranchNodeConfig._migrate_legacy_condition`
  - the mode-discriminator error path (unknown mode → RuntimeError)
  - the empty-branches error path (switch) and missing-then error
    path (if-else)
  - `to_source` emission for both modes

Why a fresh file (vs extending `test_condition_evaluator.py`):
`test_condition_evaluator.py` tests the standalone `make_evaluator`
factory — the building block. This file tests the FULL strategy
(Router/Condition primitives + label/choices/evaluator assembly + the
legacy-DSL hookup). Two layers, two files.

Circular import workaround: `app.core.strategies.branch` cannot be
imported directly (it triggers the `strategies/__init__.py` registry
walk, which fails when not all node-type modules are loaded). Going
through `app.main` first loads the full app graph and registers the
strategy in sys.modules. See `app.core.strategies.__init__.py::_instantiate_one`.
"""
from __future__ import annotations

from typing import Any

import pytest

# IMPORTANT: import order matters — `app.main` must come first to
# register the strategies registry before `branch` can be imported.
import app.main  # noqa: F401
from app.core.ir import IRNode, WorkflowIR
from app.core.strategies.branch import BranchStrategy


# ─────────────────────────────────────────────────────────────────
# fixtures / helpers
# ─────────────────────────────────────────────────────────────────


def _ctx(
    branch_id: str,
    branch_targets: list[str],
    target_objs: dict[str, Any] | None = None,
    *,
    branch_type: str = "branch",
) -> Any:
    """Build a minimal ctx with `ir` + `objects` for a single branch node.

    `objects` is the IR→runtime map: branch_id → branch object,
    target_id → step object (we use a sentinel marker so we can assert
    the step passed through to Router.choices / Condition.steps).
    """
    node = IRNode(
        id=branch_id,
        type=branch_type,
        data={"config": {"mode": "switch"}},
    )
    ir = WorkflowIR(
        node_map={branch_id: node},
        outgoing={branch_id: list(branch_targets)},
        incoming={tgt: [branch_id] for tgt in branch_targets},
        topo_order=[branch_id, *branch_targets],
        entry_id=branch_id,
        flow_branches={},
        branch_branches={branch_id: list(branch_targets)},
        loop_bodies={},
    )
    return type("Ctx", (), {"ir": ir, "objects": target_objs or {}})()


def _node(branch_id: str, **cfg_overrides) -> dict:
    """Build the raw `node` dict that goes into `BranchStrategy.build()`.

    Config defaults match the manifest's `branch.defaultConfig` so
    every test's cfg is one valid merge — except the bits the test
    actually wants to override.
    """
    cfg = {
        "mode": "switch",
        "selector": {"mode": "function", "expression": "", "fallbackMessage": ""},
        "evaluator": {"mode": "function", "expression": "", "migratedFromLegacy": False},
        "branches": [],
        "elseTarget": "",
        "requiresConfirmation": False,
        "confirmationMessage": "",
    }
    cfg.update(cfg_overrides)
    return {"id": branch_id, "type": "branch", "data": {"label": branch_id, "config": cfg}}


class _Marker:
    """Stand-in for an agno Step / Router / Condition. Identity-matters
    only — `Router.choices` accepts any list, and the strategy reads
    the same list back to assemble the runtime primitive."""


# ─────────────────────────────────────────────────────────────────
# switch mode × {function, cel, hitl}
# ─────────────────────────────────────────────────────────────────


def test_switch_function_emits_router_with_callable_selector():
    """function-mode switch builds `Router(selector=callable, choices=[...])`.

    The selector must be a Python callable (the function-mode
    factory wraps the user's expression in a closure that returns a
    step object, NOT a label string). Locked in so a future "let's
    pass the expression string directly" patch doesn't silently
    change the runtime contract.
    """
    from agno.workflow.router import Router

    t1 = _Marker()
    t2 = _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="switch",
        selector={"mode": "function", "expression": "previous_step_content",
                  "fallbackMessage": ""},
    ), ctx)
    assert isinstance(obj, Router)
    assert callable(obj.selector)
    assert obj.choices == [t1, t2]


def test_switch_function_selector_evaluates_expression():
    """Round-trip: the function-mode selector actually evaluates the
    configured expression against the in-scope locals.

    `previous_step_content` is the most-used local — calling the
    callable with a StepInput whose `.previous_step_content` is "yes"
    must return the result of that expression, which evaluates to
    a truthy string.
    """
    from agno.workflow.router import Router

    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="switch",
        selector={"mode": "function", "expression": "previous_step_content",
                  "fallbackMessage": ""},
    ), ctx)
    # StepInput duck type — `previous_step_content` is a property the
    # scope-builder reads.
    fake_input = type("SI", (), {"previous_step_content": "yes"})()
    assert obj.selector(fake_input) == "yes"


def test_switch_cel_passes_expression_string_as_selector():
    """CEL mode passes the raw expression STRING through to
    `Router(selector=...)` — agno evaluates it natively, no callable
    wrapping on our side. Pin the contract so a future patch that
    wraps CEL in a Python closure (which would break agno's native
    CEL evaluator) is caught early.
    """
    from agno.workflow.router import Router

    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="switch",
        selector={"mode": "cel", "expression": "input == 'go'",
                  "fallbackMessage": ""},
    ), ctx)
    assert isinstance(obj, Router)
    assert obj.selector == "input == 'go'"  # raw string, NOT a callable
    assert obj.choices == [t1, t2]


def test_switch_hitl_emits_router_with_user_input_flag():
    """HITL mode sets `requires_user_input=True` and (optionally)
    `user_input_message=fallbackMessage`. The selector itself is
    None — agno's HITL primitive asks the user to pick.
    """
    from agno.workflow.router import Router

    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="switch",
        selector={"mode": "hitl", "expression": "",
                  "fallbackMessage": "Pick a branch"},
    ), ctx)
    assert isinstance(obj, Router)
    assert obj.requires_user_input is True
    assert obj.user_input_message == "Pick a branch"
    assert obj.selector is None
    assert obj.choices == [t1, t2]


def test_switch_hitl_no_fallback_message_omits_kwarg():
    """Empty fallbackMessage → `user_input_message` is NOT set (agno
    uses its built-in default prompt). Pinned so a future "always
    set user_input_message" patch doesn't break the prompt UX.
    """
    from agno.workflow.router import Router

    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="switch",
        selector={"mode": "hitl", "expression": "",
                  "fallbackMessage": ""},
    ), ctx)
    assert obj.requires_user_input is True
    # agno's Router has no user_input_message attr when not set, OR
    # it's None — both are acceptable.
    assert getattr(obj, "user_input_message", None) in (None, "")


def test_switch_function_selector_fail_open_returns_empty():
    """Runtime failure inside a function-mode selector (typo in
    expression, missing local, etc.) → selector returns `[]` (no
    branch). This is the documented fail-open behaviour: agno
    treats `[]` as "no match", the workflow skips the branch and
    falls through to the next sibling.

    We trigger a failure by giving an expression that references
    a name not in scope.
    """
    t1 = _Marker()
    ctx = _ctx("b1", ["t1"], {"t1": t1})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="switch",
        selector={"mode": "function",
                  "expression": "this_name_does_not_exist",
                  "fallbackMessage": ""},
    ), ctx)
    # Empty StepInput — enough scope to not raise on lookup itself,
    # but the expression references an unknown name.
    fake_input = type("SI", (), {"previous_step_content": ""})()
    assert obj.selector(fake_input) == []


# ─────────────────────────────────────────────────────────────────
# if-else mode × {function, cel, literal}
# ─────────────────────────────────────────────────────────────────


def test_if_else_function_emits_condition_with_callable_evaluator():
    """function-mode if-else builds `Condition(evaluator=callable,
    steps=[then], else_steps=[else])`.

    `evaluator` is the result of `make_evaluator('function', expr)` —
    a callable that takes `step_input` and returns bool.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "function", "expression": "previous_step_content",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    assert isinstance(obj, Condition)
    assert callable(obj.evaluator)
    assert obj.steps == [then_step]
    assert obj.else_steps == [else_step]


def test_if_else_function_evaluator_returns_bool():
    """Round-trip: a function-mode evaluator actually evaluates the
    expression against the scope and returns a bool.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "function", "expression": "previous_step_content",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    fake_input = type("SI", (), {"previous_step_content": "go"})()
    assert obj.evaluator(fake_input) is True
    fake_input = type("SI", (), {"previous_step_content": ""})()
    assert obj.evaluator(fake_input) is False


def test_if_else_cel_passes_expression_string_as_evaluator():
    """CEL mode passes the raw string through to
    `Condition(evaluator=<str>)` — agno evaluates natively. No
    Python callable wrapper on our side.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "cel", "expression": "input == 'go'",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    assert isinstance(obj, Condition)
    assert obj.evaluator == "input == 'go'"  # raw string


def test_if_else_literal_true_routes_to_then():
    """`literal` mode normalises the expression to a bool. `True` →
    evaluator returns True → agno runs the `then` branch.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "literal", "expression": "True",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    assert isinstance(obj, Condition)
    fake_input = type("SI", (), {})()
    assert obj.evaluator(fake_input) is True


def test_if_else_literal_false_routes_to_else():
    """`literal` mode with `False` (case-insensitive) → evaluator
    returns False. Pinned so a future patch that lower-cases
    differently (e.g. `.capitalize()`) is caught early.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "literal", "expression": "false",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    fake_input = type("SI", (), {})()
    assert obj.evaluator(fake_input) is False


def test_if_else_literal_unknown_string_treated_as_function():
    """`literal` mode with a non-`True`/`False` string falls through
    to function mode (per `make_evaluator` friendliness). This
    means a user who picks the literal radio but types a Python
    expression gets sensible behaviour instead of a hard error.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "literal", "expression": "previous_step_content",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    # The result is a callable (function-mode fallback).
    assert callable(obj.evaluator)


def test_if_else_no_else_branch_omits_else_steps_kwarg():
    """An if-else with no else target (no second edge AND empty
    `elseTarget`) → `Condition` is built WITHOUT `else_steps`.
    agno treats that as "no else", the workflow skips the
    condition entirely if the evaluator returns False.
    """
    from agno.workflow.condition import Condition

    then_step = _Marker()
    # Only one branch in branch_branches — no second edge.
    ctx = _ctx("b1", ["t1"], {"t1": then_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "function", "expression": "previous_step_content",
                   "migratedFromLegacy": False},
        elseTarget="",
    ), ctx)
    assert isinstance(obj, Condition)
    assert obj.steps == [then_step]
    # else_steps is either absent (default) or an empty list — both
    # are acceptable for "no else branch".
    assert not getattr(obj, "else_steps", None)


def test_if_else_requires_confirmation_sets_kwarg():
    """`requiresConfirmation=True` flows into
    `Condition(requires_confirmation=True)`. Block-level HITL.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    obj = strat.build("b1", _node("b1", mode="if-else",
        evaluator={"mode": "function", "expression": "previous_step_content",
                   "migratedFromLegacy": False},
        elseTarget="t2",
        requiresConfirmation=True,
        confirmationMessage="Are you sure?",
    ), ctx)
    assert obj.requires_confirmation is True
    assert obj.confirmation_message == "Are you sure?"


# ─────────────────────────────────────────────────────────────────
# error paths
# ─────────────────────────────────────────────────────────────────


def test_unknown_mode_raises_pydantic_validation_error():
    """mode discriminator rejects anything other than 'switch' or
    'if-else'. The `BranchNodeConfig` Pydantic schema is the first
    line of defense — it catches the bad mode BEFORE the strategy's
    own RuntimeError can fire. (The RuntimeError is a backstop for
    future schema-less call paths; in practice the schema rejects
    first.)

    Pinned so a future patch that widens the Literal to `str` (or
    drops validation entirely) is caught — the strategy's built-in
    `RuntimeError` becomes the sole defense and silently regresses
    on every test that passes bad data through it.
    """
    from pydantic import ValidationError

    t1 = _Marker()
    ctx = _ctx("b1", ["t1"], {"t1": t1})
    strat = BranchStrategy()
    with pytest.raises(ValidationError, match="Input should be 'switch' or 'if-else'"):
        strat.build("b1", _node("b1", mode="loop"), ctx)


def test_switch_no_branches_raises():
    """switch mode with zero branches → RuntimeError. agno's Router
    would build but with empty choices, the runtime would silently
    never route anything — better to fail loud at build time.
    """
    ctx = _ctx("b1", [], {})
    strat = BranchStrategy()
    with pytest.raises(RuntimeError, match="has no branches"):
        strat.build("b1", _node("b1", mode="switch"), ctx)


def test_if_else_missing_then_raises():
    """if-else with no `then` target (first branch slot empty) →
    RuntimeError. The 'then' branch is mandatory; the 'else' is
    optional. Pinned so a future "allow empty then" patch is
    caught early.
    """
    ctx = _ctx("b1", [], {})
    strat = BranchStrategy()
    with pytest.raises(RuntimeError, match="no 'then' target"):
        strat.build("b1", _node("b1", mode="if-else",
            evaluator={"mode": "function", "expression": "True",
                       "migratedFromLegacy": False},
            elseTarget="",
        ), ctx)


def test_if_else_then_target_not_in_objects_raises():
    """`then_id` resolves to a target id that's NOT in
    `ctx.objects` → RuntimeError. Catches compile-vs-runtime
    drift where an edge was added but the target node was deleted.
    """
    ctx = _ctx("b1", ["t1", "t2"], {})  # no t1/t2 in objects
    strat = BranchStrategy()
    with pytest.raises(RuntimeError, match="then-target not in graph objects"):
        strat.build("b1", _node("b1", mode="if-else",
            evaluator={"mode": "function", "expression": "True",
                       "migratedFromLegacy": False},
            elseTarget="t2",
        ), ctx)


# ─────────────────────────────────────────────────────────────────
# legacy DSL migration (phase.1 of the merge)
# ─────────────────────────────────────────────────────────────────


def test_legacy_contains_dsl_migrates_to_function_evaluator():
    """A legacy `condition: 'contains:hi'` string (the pre-merge
    DSL) migrates through `BranchNodeConfig._migrate_legacy_condition`
    into a function-mode evaluator that uses Python `in` semantics.

    Pinned so a future regression in the migration shim doesn't
    silently break users' saved workflows that still carry the
    old envelope.
    """
    from agno.workflow.condition import Condition

    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    raw_node = {
        "id": "b1",
        "type": "branch",
        "data": {
            "label": "b1",
            "config": {
                "mode": "if-else",
                # Legacy DSL — what the pre-merge API emitted when
                # the user picked the "contains" radio.
                "condition": "contains:hi",
                "elseTarget": "t2",
            },
        },
    }
    obj = strat.build("b1", raw_node, ctx)
    assert isinstance(obj, Condition)
    assert callable(obj.evaluator)
    # Round-trip the evaluator: 'hi' is in 'say hi there' → True.
    fake_input = type("SI", (), {"previous_step_content": "say hi there"})()
    assert obj.evaluator(fake_input) is True
    # Round-trip: 'hi' is NOT in 'goodbye' → False.
    fake_input = type("SI", (), {"previous_step_content": "goodbye"})()
    assert obj.evaluator(fake_input) is False


# ─────────────────────────────────────────────────────────────────
# to_source emission
# ─────────────────────────────────────────────────────────────────


def test_to_source_switch_function_emits_selector_and_router():
    """`to_source` for switch / function mode emits BOTH a Python
    `def` block (the selector closure) AND the `Router(...)` line.

    Pinned so a future "skip the def if expression is empty"
    optimisation doesn't silently change the emitted shape (which
    would break downstream tooling that greps the export for
    `<nid>_selector`).
    """
    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    src = strat.to_source("b1", _node("b1", mode="switch",
        selector={"mode": "function", "expression": "previous_step_content",
                  "fallbackMessage": ""},
    ), ctx)
    assert "def b1_selector(step_input):" in src
    assert "b1_router = Router(" in src
    assert 'selector=b1_selector' in src


def test_to_source_switch_cel_emits_router_with_string_selector():
    """`to_source` for switch / CEL mode emits `Router(...)` with a
    quoted expression string, NO `def` block.
    """
    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    src = strat.to_source("b1", _node("b1", mode="switch",
        selector={"mode": "cel", "expression": "input == 'go'",
                  "fallbackMessage": ""},
    ), ctx)
    assert "def b1_selector" not in src
    assert "b1_router = Router(" in src
    assert 'selector="input == \'go\'"' in src


def test_to_source_switch_hitl_emits_router_with_user_input_flag():
    """`to_source` for switch / HITL emits `Router(...,
    requires_user_input=True, user_input_message=...)` and NO
    `def` block.
    """
    t1, t2 = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": t1, "t2": t2})
    strat = BranchStrategy()
    src = strat.to_source("b1", _node("b1", mode="switch",
        selector={"mode": "hitl", "expression": "",
                  "fallbackMessage": "Pick"},
    ), ctx)
    assert "def b1_selector" not in src
    assert "b1_router = Router(" in src
    assert "requires_user_input=True" in src
    assert 'user_input_message="Pick"' in src


def test_to_source_if_else_function_emits_evaluator_and_condition():
    """`to_source` for if-else / function mode emits BOTH a Python
    `def` block (the evaluator closure) AND the `Condition(...)`
    line. Object name uses `_condition` suffix (mode-aware — NOT
    `_router` which would be wrong).
    """
    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    src = strat.to_source("b1", _node("b1", mode="if-else",
        evaluator={"mode": "function", "expression": "previous_step_content",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    assert "def b1_evaluator(step_input):" in src
    assert "b1_condition = Condition(" in src
    assert "evaluator=b1_evaluator" in src
    # Mode-aware suffix: `_condition` not `_router`.
    assert "b1_router" not in src


def test_to_source_if_else_literal_emits_python_bool():
    """`to_source` for if-else / literal mode emits the literal
    expression as a Python `True` / `False` (NOT a quoted string
    — agno's Condition(evaluator=<str>) would evaluate the string
    itself, which is wrong for non-CEL).
    """
    then_step, else_step = _Marker(), _Marker()
    ctx = _ctx("b1", ["t1", "t2"], {"t1": then_step, "t2": else_step})
    strat = BranchStrategy()
    src = strat.to_source("b1", _node("b1", mode="if-else",
        evaluator={"mode": "literal", "expression": "True",
                   "migratedFromLegacy": False},
        elseTarget="t2",
    ), ctx)
    assert "b1_condition = Condition(" in src
    # Raw `True` token, not a quoted "True".
    assert "evaluator=True," in src
    assert 'evaluator="True"' not in src


def test_to_source_unknown_mode_raises_pydantic_validation_error():
    """`to_source` mirrors `build`'s mode discriminator — the
    schema validation is the same Pydantic call, so the same
    ValidationError fires. See `test_unknown_mode_raises_pydantic_validation_error`
    for the rationale on why the schema catches it first."""
    from pydantic import ValidationError

    t1 = _Marker()
    ctx = _ctx("b1", ["t1"], {"t1": t1})
    strat = BranchStrategy()
    with pytest.raises(ValidationError, match="Input should be 'switch' or 'if-else'"):
        strat.to_source("b1", _node("b1", mode="loop"), ctx)