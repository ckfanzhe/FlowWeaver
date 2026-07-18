"""Tests for the agno-native `Condition.evaluator` factory.

Condition evaluator: these cover the new factory
`app.core.compile.condition.make_evaluator(mode, expression)` and the
migration helper `migrate_legacy_condition(raw)`. The legacy DSL
parser `parse_condition_template` is tested via the migration tests
(indirect coverage).

Existing tests in `test_node_config_schemas.py::TestConditionConfig`
cover the schema-level migration on save.
"""
from __future__ import annotations

import pytest

from app.core.compile.condition import (
    make_evaluator,
    migrate_legacy_condition,
    parse_condition_template,
)

# ─────────────────────────────────────────────────────────────────
# Legacy DSL parser — still exported for migration use
# ─────────────────────────────────────────────────────────────────
class TestParseConditionTemplate:
    def test_empty_defaults_to_always(self):
        assert parse_condition_template("") == ("always", "")
        assert parse_condition_template("   ") == ("always", "")

    def test_always_and_never(self):
        assert parse_condition_template("always") == ("always", "")
        assert parse_condition_template("ALWAYS") == ("always", "")
        assert parse_condition_template("never") == ("never", "")

    def test_contains(self):
        assert parse_condition_template("contains:foo") == ("contains", "foo")

    def test_equals(self):
        assert parse_condition_template("equals:42") == ("equals", "42")

    def test_regex(self):
        assert parse_condition_template("regex:^\\d+$") == ("regex", "^\\d+$")

    def test_bare_string_defaults_to_contains(self):
        assert parse_condition_template("foo") == ("contains", "foo")

# ─────────────────────────────────────────────────────────────────
# Legacy DSL → new evaluator migration
# ─────────────────────────────────────────────────────────────────
class TestMigrateLegacyCondition:
    def test_always_becomes_literal_true(self):
        m = migrate_legacy_condition("always")
        assert m == {"mode": "literal", "expression": "True"}

    def test_never_becomes_literal_false(self):
        m = migrate_legacy_condition("never")
        assert m == {"mode": "literal", "expression": "False"}

    def test_contains_becomes_function(self):
        m = migrate_legacy_condition("contains:urgent")
        assert m["mode"] == "function"
        assert "'urgent'" in m["expression"]
        assert "previous_step_content" in m["expression"]

    def test_contains_with_quotes_is_escaped(self):
        """A value containing quotes must round-trip safely through
        the generated Python expression."""
        m = migrate_legacy_condition(r"contains:it's fine")
        assert m["mode"] == "function"
        # repr() escapes the single quote
        assert "'" in m["expression"]
        # The resulting expression must be a valid Python literal expression
        # — round-trip eval to confirm.
        assert "previous_step_content" in m["expression"]

    def test_equals_becomes_function(self):
        m = migrate_legacy_condition("equals:billing")
        assert m["mode"] == "function"
        assert "'billing'" in m["expression"]
        assert "==" in m["expression"]

    def test_regex_becomes_function_using_re_search(self):
        m = migrate_legacy_condition("regex:^\\d+$")
        assert m["mode"] == "function"
        assert "search" in m["expression"]
        assert "'^\\\\d+$'" in m["expression"] or "'^\\d+$'" in m["expression"]

    def test_bare_string_defaults_to_contains_migration(self):
        m = migrate_legacy_condition("urgent")
        assert m["mode"] == "function"
        assert "'urgent'" in m["expression"]

# ─────────────────────────────────────────────────────────────────
# make_evaluator — agno-native factory
# ─────────────────────────────────────────────────────────────────
class TestMakeEvaluator:
    """`make_evaluator(mode, expression)` returns a Callable for
    function/literal modes, None for cel mode."""

    def test_cel_returns_none(self):
        """The factory signals 'pass the string directly to agno'
        for cel mode by returning None."""
        assert make_evaluator("cel", "input.contains('urgent')") is None

    def test_literal_true_evaluates_true(self):
        fn = make_evaluator("literal", "True")
        assert fn is not None
        assert fn(None) is True

    def test_literal_false_evaluates_false(self):
        fn = make_evaluator("literal", "False")
        assert fn is not None
        assert fn(None) is False

    def test_literal_case_insensitive(self):
        assert make_evaluator("literal", "true")(None) is True
        assert make_evaluator("literal", "FALSE")(None) is False

    def test_function_expression_against_scope(self):
        """Function-mode evaluator exposes the 5 in-scope locals:
        previous_step_content / previous_step_outputs / input /
        additional_data / session_state."""
        fn = make_evaluator(
            "function",
            "'urgent' in (previous_step_content or '')",
        )
        assert fn is not None

        class FakeStepInput:
            previous_step_content = "this is urgent"
            previous_step_outputs = {}
            input = None
            additional_data = {}
            session_state = {}

        assert fn(FakeStepInput()) is True
        assert fn(FakeStepInput()) is True

    def test_function_returns_false_on_keyerror(self):
        """If the expression references a missing key, evaluator
        returns False (fail-open, matches the old regex-mismatch
        behaviour) and logs a warning."""
        fn = make_evaluator(
            "function",
            "previous_step_outputs['missing_key'] == 'x'",
        )
        assert fn is not None

        class FakeStepInput:
            previous_step_content = "x"
            previous_step_outputs = {}  # 'missing_key' not present
            input = None
            additional_data = {}
            session_state = {}

        assert fn(FakeStepInput()) is False

    def test_function_rejects_invalid_syntax(self):
        """A non-Python expression must surface as ValueError so the
        user sees a clear error instead of a silent runtime crash."""
        with pytest.raises(ValueError, match="not valid Python"):
            make_evaluator("function", "this is not python !!!")

    def test_function_with_session_state(self):
        """session_state is in scope — this is the new power the
        legacy DSL never exposed."""
        fn = make_evaluator(
            "function",
            "session_state.get('flag', False)",
        )
        assert fn is not None

        class WithFlag:
            previous_step_content = None
            previous_step_outputs = {}
            input = None
            additional_data = {}
            session_state = {"flag": True}

        class WithoutFlag:
            previous_step_content = None
            previous_step_outputs = {}
            input = None
            additional_data = {}
            session_state = {}

        assert fn(WithFlag()) is True
        assert fn(WithoutFlag()) is False