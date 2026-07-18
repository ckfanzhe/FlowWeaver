"""Declarative JSON test harness for the workflow executor.

Background
----------
Before this harness, every executor test was a hand-rolled sequence of
`execute(...)` calls + ad-hoc list filtering on the returned events
(see `test_runtime.py`). That worked for simple flows but became
opaque as soon as the test needed to assert on the *shape* of the
event stream — e.g. "exactly 3 NodeStartEvents, in this order,
followed by a CompletionEvent whose output contains X".

This module replaces that pattern with three things:

  1. JSON fixtures under `tests/fixtures/workflows/` that use the
     SAME envelope format as the frontend's `importJsonWorkflow`
     (see `app/core/workflow_io.py`). So a fixture is also a valid
     import file — what we test against is what users can drag into
     the canvas.

  2. `run_fixture(name, input, ...)` — loads the JSON, runs the
     workflow via `executor.execute()`, returns the raw event list
     AND a handle to the `RuntimeSession` so callers can inspect
     `session.status`, `session.output`, etc.

  3. `assert_run_matches(events, expected)` — declarative assertions
     over the event stream (`text_contains`, `nodes_started`,
     `nodes_ended_ok`, `paused_kind`, `completed_output_contains`,
     `error_contains`). Prints a clear diff on failure instead of
     dumping the raw event list.

The harness is intentionally thin: it does NOT replace the existing
`execute()` / `continue_session()` API. It's a convenience layer on
top of them. Tests that need fine-grained control still call
`executor` directly.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.compile import continue_leg, run_leg
from app.core.events import (
    CompletedEvent,
    ConfirmationEvent,
    ErrorEvent,
    NodeEndEvent,
    NodeStartEvent,
    TextEvent,
)
from app.core.workflow_io import parse as parse_envelope
from app.runtime.session import RuntimeSession, session_store

# Side table: session_id → compiled `agno.Workflow`. We need the
# `wf` handle on the resume leg so we can call `Wf.continue_run(...)`
# against the same compiled object the first leg produced. The
# runtime service keeps this on its own `RuntimeSession.wf`; the
# harness mirrors it here so tests can resume without going through
# the HTTP layer.
_WF_BY_SESSION: dict[str, Any] = {}

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "workflows"

# ─────────────────────────────────────────────────────────────────
# ExpectedRun — declarative assertion spec
# ─────────────────────────────────────────────────────────────────
@dataclass
class ExpectedRun:
    """What the harness should assert about a single workflow run.

    All fields are optional; unspecified fields are not asserted. The
    order of fields matches the order of fields in the typical
    "happy-path" assertion (input → handlers → completion), so a
    `repr()` reads top-to-bottom like the workflow itself.

    Use `to_continue()` to chain a second `ExpectedRun` for the
    resume leg of a human_input flow.
    """

    # Last text payload produced should contain this substring.
    text_contains: Optional[str] = None
    # Every id in this list must appear in a NodeStartEvent (in order).
    nodes_started: list[str] = field(default_factory=list)
    # Every id in this list must appear in a NodeEndEvent with status="ok".
    nodes_ended_ok: list[str] = field(default_factory=list)
    # If set, expect a ConfirmationEvent of this kind ("human_input" or
    # "confirm") to be emitted.
    paused_kind: Optional[str] = None
    # Expect a CompletedEvent whose `output` contains this substring.
    completed_output_contains: Optional[str] = None
    # Expect an ErrorEvent whose `message` contains this substring.
    error_contains: Optional[str] = None

    def to_continue(self, response: Any, follow: "ExpectedRun") -> "_ContinuePlan":
        return _ContinuePlan(response=response, follow=follow)

@dataclass
class _ContinuePlan:
    """Marker returned by `ExpectedRun.to_continue()` — keeps the
    harness API explicit: `run_fixture(...)` returns either a single
    `RunResult` or a `(RunResult, ContinuePlan)` tuple."""
    response: Any
    follow: ExpectedRun

# ─────────────────────────────────────────────────────────────────
# RunResult — what `run_fixture` returns
# ─────────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    events: list
    session: Optional[RuntimeSession]
    fixture_name: str
    input: str

# ─────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────
def load_fixture(name: str, *, skip_validation: bool = False) -> dict:
    """Load a JSON fixture by name (with or without `.json`) and
    return the parsed `workflow` block (matches the shape
    `workflow_io.parse` produces).

    `skip_validation=True` bypasses the `parse_envelope` type check —
    used by fixtures that intentionally reference an unknown node
    type (e.g. to assert the executor's no-handler error path).

    Raises `FileNotFoundError` if the fixture is missing.
    """
    p = Path(name)
    if not p.suffix:
        p = p.with_suffix(".json")
    if not p.is_absolute():
        p = FIXTURES_DIR / p
    with p.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if skip_validation:
        body = payload["workflow"]
        return {
            "name": body["name"],
            "description": body.get("description") or None,
            "nodes": body["nodes"],
            "edges": body["edges"],
        }
    return parse_envelope(payload)

def run_fixture(
    name: str,
    input: str,
    *,
    session_id: Optional[str] = None,
    expected: Optional[ExpectedRun] = None,
    skip_validation: bool = False,
) -> RunResult | tuple[RunResult, _ContinuePlan]:
    """Load a fixture, run it through the executor, return events +
    session. Optionally asserts against an `ExpectedRun`.

    `skip_validation=True` bypasses envelope type validation — useful
    for fixtures that intentionally reference unknown node types.

    If `expected` is provided and is a plain `ExpectedRun`, the harness
    asserts before returning. On assertion failure, a clear diff is
    printed (event summary, missing/extra ids, actual text vs expected
    substring) so the failure message tells you WHY the run didn't
    match, not just that it didn't.

    `ExpectedRun.to_continue()` chains a resume: pass that
    `_ContinuePlan` directly as `expected`. The harness will run the
    first leg, then return `(RunResult, ContinuePlan)` so the caller
    can hand it to `run_continue(...)` to assert the resume leg.
    """
    wf_body = load_fixture(name, skip_validation=skip_validation)
    sid, events, wf = run_leg(
        workflow_id=f"fixture-{name}",
        name=name,
        db_nodes=wf_body["nodes"],
        db_edges=wf_body["edges"],
        input=input,
        session_id=session_id,
    )
    _WF_BY_SESSION[sid] = wf
    sess = session_store().get(sid)
    if sess is not None:
        sess.wf = wf
    result = RunResult(
        events=events,
        session=sess,
        fixture_name=name,
        input=input,
    )
    if isinstance(expected, _ContinuePlan):
        # The caller already declared "I expect a pause + resume" via
        # `ExpectedRun(...).to_continue(response, follow)`. We just
        # verify the first leg produced a pause, then return the plan.
        plan = expected
        pauses = [e for e in events if isinstance(e, ConfirmationEvent)]
        if not pauses:
            raise AssertionError(
                f"fixture {name!r} (input={input!r}): expected a pause "
                f"(to_continue), but no ConfirmationEvent was emitted. "
                f"events: {[type(e).__name__ for e in events]}"
            )
        return result, plan
    if isinstance(expected, ExpectedRun):
        assert_run_matches(result, expected)
    return result

def run_continue(
    session_id: str,
    response: Any,
    expected: ExpectedRun,
) -> RunResult:
    """Resume a paused session and assert against `expected`.

    Pairs with `run_fixture(..., expected=ExpectedRun(..., paused_kind=...).to_continue(...))`.

    We look up the compiled `agno.Workflow` from our side table,
    pull the persisted `WorkflowRunOutput` via `Wf.get_run_output(...)`,
    apply the user's `response` to the active `StepRequirement`, and
    call `Wf.continue_run(...)`. No more `_last_user_response` mirror.
    """
    from app.core.compile.run import continue_leg as _continue_leg

    wf = _WF_BY_SESSION.get(session_id)
    if wf is None:
        sess = session_store().get(session_id)
        wf = getattr(sess, "wf", None) if sess else None
    if wf is None:
        raise RuntimeError(
            f"no compiled Workflow handle for session {session_id!r} "
            f"— was run_fixture() called first?"
        )

    # Discover the persisted run_output for this session.
    ro = None
    for sess in wf.storage_sessions.values() if hasattr(wf, "storage_sessions") else []:
        ro = sess
        break
    # Fallback: read the WorkflowSession via the workflow's own cache.
    if ro is None:
        # agno's API: `wf._workflow_session` is the cached session
        # (when `cache_session=True`, the default). Its `runs` dict
        # holds persisted `WorkflowRunOutput` rows.
        ws = getattr(wf, "_workflow_session", None)
        if ws is not None and getattr(ws, "runs", None):
            # runs is dict[run_id, WorkflowRunOutput]
            ro = list(ws.runs.values())[-1] if ws.runs else None

    if ro is None:
        raise RuntimeError(
            f"no WorkflowRunOutput found for session {session_id!r}"
        )

    # Apply the user's response to the active requirement.
    updated_reqs = list(ro.step_requirements or [])
    for req in updated_reqs:
        if getattr(req, "requires_user_input", False):
            field_name, value = _coerce(req, response)
            try:
                req.set_user_input(validate=False, **{field_name: value})
            except Exception:  # noqa: BLE001
                req.user_input = response
            break

    sid, events = _continue_leg(
        wf,
        session_id=session_id,
        run_id=ro.run_id,
        step_requirements=updated_reqs,
    )
    sess = session_store().get(sid)
    result = RunResult(
        events=events,
        session=sess,
        fixture_name="(continued)",
        input=sess.input if sess else "",
    )
    assert_run_matches(result, expected)
    return result

def _coerce(req, response: Any) -> tuple[str, Any]:
    """Map a frontend-posted `response` onto the schema field name.

    Mirror of `runtime_service._coerce_response_for_requirement`.
    Kept here so the harness doesn't need to import private service
    helpers.
    """
    schema = getattr(req, "user_input_schema", None) or []
    field_names: list[str] = []
    for f in schema:
        if isinstance(f, dict):
            name = f.get("name")
        else:
            name = getattr(f, "name", None)
        if name:
            field_names.append(name)

    if isinstance(response, dict) and "selection" in response:
        field = "selection" if "selection" in field_names else (field_names[0] if field_names else "selection")
        return field, response["selection"]
    if isinstance(response, str):
        field = "response" if "response" in field_names else (field_names[0] if field_names else "response")
        return field, response
    if isinstance(response, bool):
        field = "confirmation" if "confirmation" in field_names else (field_names[0] if field_names else "confirmation")
        return field, response
    field = field_names[0] if field_names else "response"
    return field, response

# ─────────────────────────────────────────────────────────────────
# Declarative assertion
# ─────────────────────────────────────────────────────────────────
def assert_run_matches(result: RunResult, expected: ExpectedRun) -> None:
    """Assert that `result.events` matches `expected`.

    Each field on `expected` is checked independently; the first
    failure raises `AssertionError` with a short diff. Tests that
    want multiple errors at once can call `run_fixture(...)` with
    `expected=None` and then call this helper inside a try/except.
    """
    evs = result.events
    failures: list[str] = []

    # text_contains
    if expected.text_contains is not None:
        texts = [e.content for e in evs if isinstance(e, TextEvent)]
        joined = "\n".join(texts)
        if expected.text_contains not in joined:
            failures.append(
                f"text_contains={expected.text_contains!r} not found in "
                f"TextEvents:\n  {joined!r}"
            )

    # nodes_started (ordered, must all appear)
    if expected.nodes_started:
        actual_starts = [e.nodeId for e in evs if isinstance(e, NodeStartEvent)]
        missing = [
            nid for nid in expected.nodes_started if nid not in actual_starts
        ]
        if missing:
            failures.append(
                f"nodes_started missing={missing!r}; actual order={actual_starts!r}"
            )
        # Order check (subsequence): expected ids must appear in `actual_starts`
        # in the same relative order.
        if not missing:
            it = iter(actual_starts)
            try:
                for nid in expected.nodes_started:
                    while next(it) != nid:
                        pass
            except StopIteration:
                failures.append(
                    f"nodes_started order mismatch; expected {expected.nodes_started!r} "
                    f"in {actual_starts!r}"
                )

    # nodes_ended_ok
    if expected.nodes_ended_ok:
        actual_ends_ok = {
            e.nodeId for e in evs
            if isinstance(e, NodeEndEvent) and e.status == "ok"
        }
        missing = [nid for nid in expected.nodes_ended_ok if nid not in actual_ends_ok]
        if missing:
            failures.append(
                f"nodes_ended_ok missing={missing!r}; actual ok set={sorted(actual_ends_ok)!r}"
            )

    # paused_kind
    if expected.paused_kind is not None:
        pauses = [e for e in evs if isinstance(e, ConfirmationEvent)]
        if not pauses:
            failures.append(
                f"expected ConfirmationEvent(kind={expected.paused_kind!r}); got none"
            )
        elif pauses[0].kind != expected.paused_kind:
            failures.append(
                f"paused_kind: expected {expected.paused_kind!r}, "
                f"got {pauses[0].kind!r}"
            )

    # completed_output_contains
    if expected.completed_output_contains is not None:
        comps = [e for e in evs if isinstance(e, CompletedEvent)]
        if not comps:
            failures.append(
                f"completed_output_contains={expected.completed_output_contains!r} "
                "but no CompletedEvent emitted"
            )
        elif expected.completed_output_contains not in (comps[0].output or ""):
            failures.append(
                f"completed_output_contains={expected.completed_output_contains!r} "
                f"not in {comps[0].output!r}"
            )

    # error_contains
    if expected.error_contains is not None:
        errs = [e for e in evs if isinstance(e, ErrorEvent)]
        if not errs:
            failures.append(
                f"error_contains={expected.error_contains!r} but no ErrorEvent"
            )
        elif not any(
            expected.error_contains in (e.message or "") for e in errs
        ):
            failures.append(
                f"error_contains={expected.error_contains!r} not in "
                f"{[e.message for e in errs]!r}"
            )

    if failures:
        def _one_line(e):
            label = ""
            if hasattr(e, "nodeId") and getattr(e, "nodeId"):
                label = f"{e.nodeId}: "
            elif isinstance(e, TextEvent):
                label = f"{getattr(e, 'content', '')[:40]!r}: "
            elif isinstance(e, CompletedEvent):
                label = f"output={(getattr(e, 'output', '') or '')[:40]!r}: "
            elif isinstance(e, ConfirmationEvent):
                label = f"prompt={getattr(e, 'prompt', '')[:40]!r}: "
            elif isinstance(e, ErrorEvent):
                label = f"{getattr(e, 'message', '')[:40]!r}: "
            return f"  {label}{type(e).__name__}"

        summary = "\n".join(
            _one_line(e)
            for e in evs
            if isinstance(e, (NodeStartEvent, NodeEndEvent, TextEvent,
                              CompletedEvent, ConfirmationEvent, ErrorEvent))
        )
        raise AssertionError(
            f"fixture {result.fixture_name!r} (input={result.input!r}) "
            "did not match expected:\n  - "
            + "\n  - ".join(failures)
            + "\n\nevent summary:\n"
            + summary
        )