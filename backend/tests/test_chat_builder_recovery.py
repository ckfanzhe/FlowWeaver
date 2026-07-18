"""Tests for F6 self-correction surface.

F6 introduces a per-turn rejection budget so the LLM gets a
visible "STOP and call a diagnostic" nudge once it has burned
through REJECTION_BUDGET_PER_TURN consecutive rejections. This
file exercises the surface in three layers:

  1. Pure-function tests — `hint_for()` (the per-IssueCode hint
     mapper) and `_budget_exhausted_message()` (the budget
     escalation text). These tests pin the WIRE so a future hint
     rewrite can't silently break the LLM-facing contract.

  2. Session-bound tests — `ChatSession.turn_rejection_count`
     lifecycle: starts at 0, increments on every rejection
     helper, resets on the next chat turn. The test asserts the
     dataclass field exists and behaves, which is what the rest
     of the chat builder relies on.

  3. Tool-surface tests — the actual `_rejection_result(exc,
     session)` path used by the wrapped tool wrappers, plus the
     `PlanResult.next_step` field that carries the escalation
     hint through the JSON shape. These are the contracts the
     LLM tooling depends on.
"""
from __future__ import annotations

import copy
import json
import uuid

import pytest

from app.auth import CurrentUser
from app.db.models import User, Workflow
from app.services import chat_builder_service as cbs
from app.services import member_service
from app.services.chat_builder_plan import (
    Issue,
    IssueCode,
    PlanResult,
    hint_for,
)

# ─────────────────────────────────────────────────────────────────
# Pure-function tests — `hint_for` + `_budget_exhausted_message`
# ─────────────────────────────────────────────────────────────────
class TestHintFor:
    """The hint mapper must cover every IssueCode the platform
    can emit. A missing entry means the LLM sees a blank hint —
    which was the bug that F6.1 was created to fix."""

    def test_all_issue_codes_have_hints(self):
        """Every member of `IssueCode` must have a non-empty
        template in `_HINT_TEMPLATES`. Missing entries surface
        as `""` at runtime, which the LLM can't act on."""
        from app.services.chat_builder_plan import _HINT_TEMPLATES
        missing = [
            code.value
            for code in IssueCode
            if not _HINT_TEMPLATES.get(code, "").strip()
        ]
        assert missing == [], (
            f"IssueCodes without hint templates: {missing}. "
            "Add a hint in chat_builder_plan._HINT_TEMPLATES."
        )

    def test_each_hint_is_a_concrete_next_step(self):
        """Hints should mention a tool name or a concrete
        action ('add', 'set', 'call', 'remove', 'use'). Vague
        hints ('see docs', 'invalid') waste the LLM's budget."""
        verbs = ("call ", "add ", "set ", "remove ", "use ",
                 "drop ", "connect ", "wire ", "break ", "split ",
                 "merge ", "call ", "route ", "check ")
        for code in IssueCode:
            hint = hint_for(code)
            if not hint:
                continue  # (covered by `test_all_issue_codes_have_hints`)
            lowered = hint.lower()
            # Every hint should reference at least one verb that
            # implies an action. This isn't perfect NLP — we
            # allow exceptions for codes that ARE the verb (e.g.
            # PLAN_ATOMIC_REJECTED is structural).
            assert any(v in lowered for v in verbs) or code in (
                IssueCode.PLAN_ATOMIC_REJECTED,
            ), (
                f"hint for {code.value!r} has no concrete next "
                f"step: {hint!r}"
            )

    def test_plan_atomic_rejected_substitutes_cap(self):
        """The PLAN_ATOMIC_REJECTED hint embeds the cap value;
        a typo there breaks the contract."""
        h = hint_for(IssueCode.PLAN_ATOMIC_REJECTED, cap=42)
        assert "42" in h
        assert "STOP" in h or "Apply" in h  # reasonable action verbs

class TestBudgetExhaustedMessage:
    """`_budget_exhausted_message` returns `""` under the budget
    and a multi-line escalation over it. Tested as a static
    (no-session) function is fine — it's a pure projection of
    the rejection count."""

    def test_under_budget_returns_empty(self):
        session = cbs.ChatSession(
            session_id="s", workflow_id="w", user_id="u",
        )
        # Fresh session: counter is 0, well under budget.
        msg = cbs._budget_exhausted_message(session)
        assert msg == ""

    def test_at_budget_returns_empty(self):
        """Counter == budget still returns "" — the escalation
        fires when the counter has just CROSSED the budget, i.e.
        on rejection N+1."""
        session = cbs.ChatSession(
            session_id="s", workflow_id="w", user_id="u",
            turn_rejection_count=cbs.REJECTION_BUDGET_PER_TURN,
        )
        msg = cbs._budget_exhausted_message(session)
        assert msg == ""

    def test_over_budget_returns_escalation(self):
        session = cbs.ChatSession(
            session_id="s", workflow_id="w", user_id="u",
            turn_rejection_count=cbs.REJECTION_BUDGET_PER_TURN + 1,
        )
        msg = cbs._budget_exhausted_message(session)
        assert msg  # non-empty
        # Must reference at least one diagnostic tool the LLM can call.
        assert "preview_workflow" in msg
        assert "get_node_types" in msg

    def test_escalation_includes_count(self):
        """The LLM can see its own count — useful when it
        self-corrects and wonders why a hint appeared."""
        session = cbs.ChatSession(
            session_id="s", workflow_id="w", user_id="u",
            turn_rejection_count=7,
        )
        msg = cbs._budget_exhausted_message(session)
        assert "7" in msg

# ─────────────────────────────────────────────────────────────────
# Session-bound tests — turn_rejection_count lifecycle
# ─────────────────────────────────────────────────────────────────
class TestTurnRejectionCountField:
    """The dataclass field is what every wrapped tool wrapper
    increments. Adding it was a backwards-compatible change (new
    field with default) but the runtime contract is that every
    rejection helper bumps it and every chat turn resets it."""

    def test_default_zero(self):
        s = cbs.ChatSession(
            session_id="x", workflow_id="y", user_id="z",
        )
        assert s.turn_rejection_count == 0

    def test_rejection_result_increments_with_session(self):
        """`_rejection_result(exc, session)` must bump the
        counter. Without `session` the helper is a pure
        formatter (and stays one for testability)."""
        s = cbs.ChatSession(
            session_id="x", workflow_id="y", user_id="z",
        )
        exc = cbs.ToolCallRejected(
            tool="add_node", message="boom", hint="fix it",
        )
        cbs._rejection_result(exc, s)
        assert s.turn_rejection_count == 1
        cbs._rejection_result(exc, s)
        assert s.turn_rejection_count == 2

    def test_rejection_result_without_session_does_not_track(self):
        """The no-session form is the pure formatter path —
        it's used in tests that don't care about the budget."""
        exc = cbs.ToolCallRejected(
            tool="add_node", message="boom", hint="fix it",
        )
        # No exception; just verify it doesn't crash.
        out = cbs._rejection_result(exc)
        assert "boom" in out
        assert "fix it" in out

    def test_escalation_fires_only_after_threshold(self):
        """Five rejections pass silently; the sixth adds the
        escalation. The threshold is REJECTION_BUDGET_PER_TURN."""
        s = cbs.ChatSession(
            session_id="x", workflow_id="y", user_id="z",
        )
        exc = cbs.ToolCallRejected(
            tool="add_node", message="boom", hint="fix",
        )
        for _ in range(cbs.REJECTION_BUDGET_PER_TURN):
            out = cbs._rejection_result(exc, s)
            assert "STOP" not in out  # under threshold
        # Sixth rejection pushes it over.
        out = cbs._rejection_result(exc, s)
        assert "STOP" in out
        assert s.turn_rejection_count == cbs.REJECTION_BUDGET_PER_TURN + 1

# ─────────────────────────────────────────────────────────────────
# Tool-surface tests — full path through PlanResult.next_step
# ─────────────────────────────────────────────────────────────────
def _setup(db):
    """Stand in for the empty_workflow fixture (kept self-
    contained across test modules)."""
    db.add(User(id="alice@example.com", tenant_id="tenant-default"))
    db.commit()
    wid = f"wf-{uuid.uuid4().hex[:8]}"
    db.add(Workflow(
        id=wid, name="seed", description="seed",
        nodes=[{
            "id": "a1", "type": "agent",
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": "A1", "config": {"instructions": ""}},
        }],
        edges=[],
        created_by="alice@example.com",
    ))
    db.commit()
    member_service.bootstrap_owner(db, wid, "alice@example.com")
    db.commit()
    user = CurrentUser(id="alice@example.com", tenant_id="tenant-default")
    return cbs._load_or_create_session(db, wid, user), wid

class TestPlanResultNextStep:
    """The PlanResult JSON shape picked up `next_step` in F6
    so the per-turn budget escalation rides along with the
    structured issues. Tests pin both the to_dict() projection
    AND the session-bound emission through the wrapped
    `plan_workflow` tool."""

    def test_to_dict_omits_next_step_when_empty(self):
        """Empty next_step must NOT appear in the JSON — the
        LLM tooling keys off presence (`if "next_step" in
        out`). Adding empty would create a downstream
        regression in the frontend."""
        r = PlanResult(ok=False, issues=[
            Issue(path="(plan)", code=IssueCode.INVALID_CONFIG,
                  message="x", hint=""),
        ])
        d = r.to_dict()
        assert "next_step" not in d

    def test_to_dict_includes_next_step_when_set(self):
        r = PlanResult(
            ok=False,
            issues=[Issue(path="(plan)", code=IssueCode.INVALID_CONFIG,
                          message="x", hint="")],
            next_step="STOP — call preview_workflow",
        )
        d = r.to_dict()
        assert d["next_step"] == "STOP — call preview_workflow"

    def test_plan_rejection_path_attaches_next_step(self, db):
        """Drive the wrapped `plan_workflow` tool with a
        workflow_id that doesn't match the session — the inner
        `_check_same_workflow` raises `ToolCallRejected` before
        any JSON parsing, so we get the string-return path with
        the budget escalation appended."""
        session, wf_id = _setup(db)
        # Force the session into "over budget" first.
        for _ in range(cbs.REJECTION_BUDGET_PER_TURN + 1):
            session.turn_rejection_count += 1
        funcs = cbs._build_tools_for_session(session)
        plan_tool = next(f for f in funcs if f.name == "plan_workflow")
        result = plan_tool.entrypoint(
            plan={"nodes": [], "edges": []},
            workflow_id="wf-does-not-exist",
        )
        # String return: rejection message + budget escalation.
        assert "STOP" in result
        assert "preview_workflow" in result
        # Counter incremented again.
        assert session.turn_rejection_count > cbs.REJECTION_BUDGET_PER_TURN + 1

    def test_validation_issues_attach_next_step_in_json(self, db):
        """For SCHEMA-level rejections (Pydantic `model_validate`
        failures), the wrapped tool returns a JSON dict with
        `next_step`. We trigger it with a structurally valid
        but semantically broken plan (missing `type` field)."""
        session, wf_id = _setup(db)
        for _ in range(cbs.REJECTION_BUDGET_PER_TURN + 1):
            session.turn_rejection_count += 1
        funcs = cbs._build_tools_for_session(session)
        plan_tool = next(f for f in funcs if f.name == "plan_workflow")
        result = plan_tool.entrypoint(plan={
            "nodes": [{"id": "x"}],  # missing 'type'
        })
        out = json.loads(result)
        assert out["ok"] is False
        assert "issues" in out
        assert "next_step" in out
        assert "STOP" in out["next_step"]

class TestRecoverySectionInSystemPrompt:
    """F6.4 added a recovery section to `_BUILDER_SYSTEM_PROMPT_HEADER`
    pointing at the diagnostic tools. We assert the section is
    present and references each tool — without this the LLM
    wouldn't know `explain_failure` is callable from the chat."""

    def test_recovery_section_present(self):
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        assert "Self-correction" in prompt
        # Each diagnostic tool should be mentioned.
        for tool in (
            "preview_workflow",
            "get_node_types",
            "get_connection_rules",
            "get_graph_state",
            "inspect_run",
            "explain_failure",
        ):
            assert tool in prompt, f"recovery section missing {tool!r}"

    def test_recovery_section_mentions_loop_warning(self):
        """The section must explicitly tell the LLM not to
        loop. This is the 'what NOT to do' half of the
        guidance."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        assert "loop" in prompt.lower() or "retry" in prompt.lower()