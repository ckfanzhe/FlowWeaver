"""Compile + run + adapt — the single-engine core.

Three callers consume this:

  - `app.services.runtime_service` for the HTTP SSE stream. Adds
    session lifecycle (DB row load, session_store CRUD, status
    mirroring for `GET /sessions/{id}`).
  - `app.services.chat_builder_run` for the LLM-driven debug tool
    (F5). Uses `drive_leg_with_trace` which adds a per-step
    accumulator + HITL injection + wall-clock timeout. The
    RunTrace envelope stays in `chat_builder_run` (it's the store
    owner); the leg machinery is shared.
  - `tests/harness.py` for declarative fixture runs. No DB, no
    session_store — just compile, run, translate, return events.

The reason this lives in `compile/` rather than `services/`: it is
the runtime embodiment of the same `CompileCtx` that powers
`to_python_source()`. Calling `run_leg` returns the same kind of
`RuntimeEvent` stream the export code would emit if we ran the
rendered Python file.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Tuple

from app.core.compile import build_workflow
from app.core.event_adapter import EventAdapter
from app.core.events import (
    CompletedEvent,
    ConfirmationEvent,
    ErrorEvent,
    NodeEndEvent,
    NodeStartEvent,
    RuntimeEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# LegStep — generic per-step accumulator (row B, )
# ─────────────────────────────────────────────────────────────────
# `RunTrace.steps[]` and `chat_builder_run.RunStep` used to be the
# same shape defined in two files. We promote it here as the
# canonical leg-step dataclass; `chat_builder_run.RunStep` is now a
# re-export alias so existing import paths (`from app.services
# .chat_builder_run import RunStep`) keep working.
@dataclass
class LegStep:
    """One execution step inside a single leg.

    Fields match the JSON shape `inspect_run` returns under
    `steps[]` (snake_case). The `output` field is filled by the
    caller's wrapper if needed — the facade doesn't read it.
    """
    step_id: str
    node_id: str
    node_type: str
    label: str = ""
    status: str = "running"  # "ok" | "error" | "skipped"
    input: Optional[Any] = None
    output: Optional[Any] = None
    tool_calls: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    started_at_ms: int = 0
    error: Optional[str] = None

# Default wall-clock timeout for `drive_leg_with_trace`. Generous
# (60s) because real workflows can take a while; the chat builder
# defaults to this value (see `chat_builder_run.DEFAULT_RUN_TIMEOUT_SEC`).
DEFAULT_RUN_TIMEOUT_SEC = 60.0

def run_leg(
    *,
    workflow_id: str,
    name: str,
    db_nodes: list[dict],
    db_edges: list[dict],
    input: str,
    session_id: str | None = None,
    start_node_id: str | None = None,
) -> Tuple[str, list[RuntimeEvent], Any]:
    """Compile, run, adapt. Returns `(session_id, events, workflow)`.

    `workflow` is the compiled `agno.Workflow` — the caller holds
    onto it for `Wf.continue_run(...)` on resume. We don't return it
    from `runtime_service.run_workflow()` because that signature is
    frozen by the API tests, but `tests/harness.py` uses it to drive
    multi-leg fixture runs.
    """
    try:
        wf = build_workflow(
            workflow_id=workflow_id,
            name=name or workflow_id,
            db_nodes=db_nodes,
            db_edges=db_edges,
            session_id=session_id,
            start_node_id=start_node_id,
        )
    except Exception as e:  # noqa: BLE001
        # Mirror the runtime service contract: a build failure becomes
        # a single ErrorEvent in the SSE stream. Without this, callers
        # that go straight through `run_leg` (tests, harnesses) would
        # see a raw exception, which is unobservable from the SSE
        # contract the frontend depends on.
        from app.core.events import ErrorEvent
        from app.core.compile.errors import CompileError
        msg = str(e) if isinstance(e, CompileError) else f"{type(e).__name__}: {e}"
        sid = session_id or f"run-{workflow_id[:8]}"
        return sid, [ErrorEvent(message=msg)], None  # type: ignore[return-value]
    sid = session_id or f"run-{id(wf) & 0xffff:04x}"

    # Make sure the slim RuntimeSession exists for the EventAdapter to
    # consult (status / node_types). `runtime_service` already does this
    # before calling `run_leg`; for direct harness use we add the
    # fallback here. Route through `SessionStore.create()` (rather than
    # poking `_sessions[sid] = sess`) so the by-user index stays
    # consistent — anonymous harness rows live in `_sessions` without
    # being keyed to any user.
    import time as _time
    from app.runtime.session import RuntimeSession, session_store
    store = session_store()
    sess = store.get(sid)
    if sess is None:
        sess = store.create(
            workflow_id=workflow_id,
            input=input,
            user_id=None,
            session_id=sid,
        )
        sess.wf = wf
        sess.started_at = _time.monotonic()
    sess.node_types = extract_node_types(wf)

    try:
        agno_events: Iterator = wf.run(
            input=input,
            stream=True,
            stream_events=True,
            stream_executor_events=True,
            session_id=sid,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("wf.run failed")
        err = ErrorEvent(
            message=f"workflow run failed: {type(e).__name__}: {e}"
        )
        return sid, [err], wf
    adapter = EventAdapter(session_id=sid)
    events = adapter.adapt(agno_events)
    # Mirror status on the session so direct callers (tests/harness)
    # see the same state the SSE API layer would surface.
    if any(isinstance(ev, CompletedEvent) for ev in events):
        comp = next(ev for ev in events if isinstance(ev, CompletedEvent))
        if comp.output:
            sess.output = comp.output
        sess.status = "completed"
    elif any(isinstance(ev, ConfirmationEvent) for ev in events):
        sess.status = "waiting_confirmation"
    elif any(isinstance(ev, ErrorEvent) for ev in events):
        sess.status = "error"
    # / session (commit 2): mirror the harness-path
    # final state to SQLite so direct callers (tests / debug
    # harnesses) see the same row visibility the SSE API layer
    # would. Without this, harness runs leave status="running"
    # in the DB and `list_sessions` / `metrics` would miss them.
    sess.flush()
    return sid, events, wf

def continue_leg(
    wf,
    *,
    session_id: str,
    run_id: str,
    step_requirements: list | None = None,
) -> Tuple[str, list[RuntimeEvent]]:
    """Resume a paused workflow via `Wf.continue_run(...)`.

    The caller is responsible for mutating `step_requirements[i].user_input`
    BEFORE calling this — agno reads the resolved `user_input` dict
    off each active requirement.
    """
    try:
        agno_events: Iterator = wf.continue_run(
            run_id=run_id,
            session_id=session_id,
            step_requirements=step_requirements or None,
            stream=True,
            stream_events=True,
            stream_executor_events=True,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("wf.continue_run failed")
        err = ErrorEvent(
            message=f"workflow resume failed: {type(e).__name__}: {e}"
        )
        return session_id, [err]
    adapter = EventAdapter(session_id=session_id)
    return session_id, adapter.adapt(agno_events)

# ─────────────────────────────────────────────────────────────────
# drive_leg_with_trace — trace-oriented facade shared by chat builder
# (F5) and any future trace-aware runner (row B, )
# ─────────────────────────────────────────────────────────────────
# `run_leg` is the SSE-oriented leg: returns the raw `RuntimeEvent`
# stream for the HTTP layer to forward. `drive_leg_with_trace`
# extends that contract with a per-step accumulator (`LegStep[]`),
# a HITL injection loop, and a wall-clock timeout — the same
# machinery `chat_builder_run.run_workflow` used to maintain on its
# own. The chat builder now calls this facade and folds the result
# into its `RunTrace` envelope (the store lives in `chat_builder_run`).
#
# HITL splicing is delegated to `on_hitl(reqs, response)` — the caller
# owns the field-name decision (`response` vs `selection` vs
# `confirmation`). The facade only does the leg iteration: pull an
# event, fold into LegStep / pending_requirements, on
# `ConfirmationEvent` call `on_hitl` and `Wf.continue_run` if the
# caller has a queued response.
#
# Returns `(session_id, events, steps, pending_requirements,
# output, error, status)`. The chat builder builds a `RunTrace` from
# these — the trace envelope (store, JSON shape, diagnostics rules)
# stays in `chat_builder_run`; the leg machinery is here.
def drive_leg_with_trace(
    wf,
    *,
    session_id: str,
    input: str,
    hitl_responses: Optional[list[Any]] = None,
    timeout_sec: float = DEFAULT_RUN_TIMEOUT_SEC,
    on_hitl: Optional[Callable[[list[Any], Any], Optional[list[Any]]]] = None,
) -> Tuple[
    str,
    list[RuntimeEvent],
    list[LegStep],
    list[dict],
    Optional[str],
    Optional[str],
    str,
]:
    """Drive one workflow through `Wf.run` + (optional) HITL resumes.

    Args:
        wf: a compiled `agno.Workflow` from `build_workflow(...)`.
        session_id: the run session id (passed to agno).
        input: the user message / initial input.
        hitl_responses: queue of answers, one per paused
            `ConfirmationEvent`. When a pause happens and the queue
            has a response for it, the facade calls `on_hitl(...)`
            and then `Wf.continue_run(...)` with the mutated
            requirements. When the queue runs dry the leg ends with
            `status='paused'`.
        timeout_sec: wall-clock cap on the whole leg (initial run +
            all HITL resumes). The agno runtime itself doesn't
            time out — a malformed workflow can loop forever.
        on_hitl: callable `(reqs, response) -> Optional[mutated_reqs]`.
            When None, the default splice just sets
            `req["user_input"] = response`. The chat builder passes
            its own (`_apply_hitl_response`) to keep field-name
            routing decisions in the chat builder.

    Returns:
        `(session_id, events, steps, pending_requirements, output,
        error, status)`. `status` is one of `"completed"` |
        `"paused"` | `"failed"`, derived from terminal events
        (CompletedEvent / ConfirmationEvent / ErrorEvent) — same
        rule `chat_builder_run._collect_leg` used to apply. The
        chat builder folds these into a `RunTrace`.
    """
    hitl_responses = list(hitl_responses or [])
    splice = on_hitl or _default_splice

    adapter = EventAdapter(session_id=session_id)
    all_steps: list[LegStep] = []
    pending_requirements: list[dict] = []
    all_events: list[RuntimeEvent] = []
    output: Optional[str] = None
    error_msg: Optional[str] = None
    status = "running"
    started = time.monotonic()
    response_cursor = 0

    # First leg: Wf.run
    try:
        agno_events = _run_with_timeout(
            wf.run(
                input=input,
                stream=True,
                stream_events=True,
                stream_executor_events=True,
                session_id=session_id,
            ),
            timeout_sec=timeout_sec,
            started=started,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            session_id, [], all_steps, pending_requirements,
            None, f"workflow run failed: {type(exc).__name__}: {exc}",
            "failed",
        )

    status, output, error_msg, pending_requirements, leg_events = _collect_leg_events(
        adapter, agno_events, all_steps, output, error_msg,
        pending_requirements,
    )
    all_events.extend(leg_events)

    # HITL loop: while the workflow is paused and the caller has
    # queued answers, keep resuming. The loop is bounded by the
    # queue size so a runaway pause-respond cycle terminates cleanly.
    while status == "paused" and response_cursor < len(hitl_responses):
        reqs = pending_requirements or []
        if not reqs:
            break
        response = hitl_responses[response_cursor]
        response_cursor += 1
        updated = splice(reqs, response)
        if updated is None:
            error_msg = (
                f"hitl response #{response_cursor} could not be "
                "applied to the active requirement"
            )
            status = "failed"
            break
        try:
            run_id = _resolve_run_id(wf, session_id)
            agno_events = _run_with_timeout(
                wf.continue_run(
                    run_id=run_id,
                    session_id=session_id,
                    step_requirements=updated,
                    stream=True,
                    stream_events=True,
                    stream_executor_events=True,
                ),
                timeout_sec=timeout_sec,
                started=started,
            )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"workflow resume failed: {type(exc).__name__}: {exc}"
            status = "failed"
            break
        status, output, error_msg, pending_requirements, leg_events = _collect_leg_events(
            adapter, agno_events, all_steps, output, error_msg,
            pending_requirements,
        )
        all_events.extend(leg_events)

    return (
        session_id, all_events, all_steps, pending_requirements,
        output, error_msg, status,
    )

def _run_with_timeout(events: Iterator, *, timeout_sec: float, started: float):
    """Wrap an agno event iterator with a wall-clock timeout.

    We don't kill the underlying process; we stop pulling events.
    The runtime keeps going in the background but won't surface more
    events to the chat. Good enough for v1: a runaway workflow is
    detected and surfaced as a timeout error.
    """
    deadline = started + timeout_sec
    for ev in events:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"workflow exceeded timeout of {timeout_sec:.1f}s"
            )
        yield ev

def _collect_leg_events(
    adapter: EventAdapter,
    agno_events: Iterator,
    all_steps: list[LegStep],
    output: Optional[str],
    error_msg: Optional[str],
    pending_requirements: list[dict],
) -> Tuple[str, Optional[str], Optional[str], list[dict], list[RuntimeEvent]]:
    """Translate agno events via EventAdapter and fold them into
    the running trace. Returns `(status, output, error,
    pending_requirements, events)` — Python's lack of inout for
    these locals means we pass them through explicitly.

    Bookkeeping:
      - `NodeStartEvent` → create-or-find a `LegStep` placeholder.
      - `NodeEndEvent`   → update its status / duration / error.
      - `ToolCallEvent`  → stash args in `pending_tool_calls`.
      - `ToolResultEvent`→ pop + append to the most-recent active step.
      - `TextEvent`      → overwrite `output` (last wins; `CompletedEvent` overrides).
      - `CompletedEvent` → authoritative output + status='completed'.
      - `ConfirmationEvent` → status='paused' + capture requirement.
      - `ErrorEvent`     → status='failed' + capture message.
    """
    events = adapter.adapt(agno_events)
    pending_tool_calls: dict[str, dict] = {}

    for ev in events:
        if isinstance(ev, NodeStartEvent):
            sid = ev.nodeId
            existing = next(
                (s for s in all_steps if s.step_id == sid), None,
            )
            if existing is None:
                all_steps.append(LegStep(
                    step_id=sid,
                    node_id=ev.nodeId,
                    node_type=ev.nodeType,
                    label=ev.label,
                    started_at_ms=ev.t,
                    status="running",
                ))
        elif isinstance(ev, NodeEndEvent):
            sid = ev.nodeId
            existing = next(
                (s for s in all_steps if s.step_id == sid), None,
            )
            if existing is None:
                # Defensive — a NodeEnd without a NodeStart is
                # possible with custom emitters. Create a stub.
                existing = LegStep(
                    step_id=sid,
                    node_id=ev.nodeId,
                    node_type="?",
                    label="",
                )
                all_steps.append(existing)
            existing.status = ev.status
            existing.duration_ms = ev.durationMs
            existing.error = ev.error
        elif isinstance(ev, ToolCallEvent):
            import copy as _copy
            pending_tool_calls[ev.tool] = {
                "name": ev.tool,
                "args": _copy.deepcopy(ev.args),
            }
        elif isinstance(ev, ToolResultEvent):
            tc = pending_tool_calls.pop(ev.tool, {"name": ev.tool, "args": {}})
            tc["result"] = ev.result
            target = next(
                (s for s in reversed(all_steps) if s.status == "running"),
                None,
            )
            if target is not None:
                target.tool_calls.append(tc)
        elif isinstance(ev, TextEvent):
            # Last text bubble becomes the trace-level output IF
            # no CompletedEvent arrives. (CompletedEvent wins.)
            output = ev.content
        elif isinstance(ev, CompletedEvent):
            output = ev.output
        elif isinstance(ev, ErrorEvent):
            error_msg = ev.message
        elif isinstance(ev, ConfirmationEvent):
            pending_requirements.append(ev.model_dump())

    if any(isinstance(ev, CompletedEvent) for ev in events):
        status = "completed"
    elif any(isinstance(ev, ConfirmationEvent) for ev in events):
        status = "paused"
    elif any(isinstance(ev, ErrorEvent) for ev in events):
        status = "failed"
    else:
        # Stream ended without a terminal event. Conservatively
        # call it completed if we saw any step end OK.
        status = "completed" if any(
            s.status == "ok" for s in all_steps
        ) else "failed"

    return status, output, error_msg, pending_requirements, events

def _resolve_run_id(wf, session_id: str) -> str:
    """Pull the run_id agno cached for `session_id`.

    Mirrors `runtime_service._run_leg`'s fallback logic — if the
    captured `run_id` is missing, ask the workflow's cache.
    """
    try:
        ro = wf.get_run_output(session_id=session_id)
        if ro is not None and getattr(ro, "run_id", None):
            return ro.run_id  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(
        "could not resolve run_id for resume — agno dropped the "
        "session before continue_run was called"
    )

def _default_splice(reqs: list[Any], response: Any) -> Optional[list[Any]]:
    """Default HITL splice when the caller doesn't supply `on_hitl`.

    Sets `req["user_input"] = response` on the first pending
    requirement. Returns the (mutated) list, or None if there's
    nothing to splice.
    """
    if not reqs:
        return None
    req = reqs[0]
    if isinstance(req, dict):
        req["user_input"] = response
    return reqs

__all__ = [
    "run_leg",
    "continue_leg",
    "drive_leg_with_trace",
    "extract_node_types",
    "LegStep",
    "DEFAULT_RUN_TIMEOUT_SEC",
]

# ─────────────────────────────────────────────────────────────────
# Node-type capture for the EventAdapter
# ─────────────────────────────────────────────────────────────────
def extract_node_types(wf) -> dict[str, str]:
    """Walk a compiled `agno.Workflow` and return `{step_id: node_type}`.

    agno doesn't carry the original node type on `Step` objects
    directly, but our `Step(name=..., agent=..., step_id=...)` wrapper
    does include `step_id` and `name`. We recover the type by checking
    which compound primitive the step is an instance of (Router /
    Parallel / Steps / Condition / Loop) and otherwise inferring
    "agent" or "ask" from the wrapper's `agent` /
    `requires_user_input` fields.

    Both `Parallel` and `Steps` agno primitives map to the merged
    `flow` node type (the runtime primitive is a config-level choice,
    not a type-level distinction). The trace panel now consistently
    shows `flow` for both modes; the mode is surfaced separately on
    `RuntimeSession` for icon / label rendering. Prior to this fix,
    `Steps` instances fell through to the generic `Step` branch and
    were reported as `"step"` — silently masking the nested flow
    structure.

    Both `Router` and `Condition` agno primitives map to the merged
    `branch` node type — mode is surfaced separately on
    `RuntimeSession`. Children: `choices` (Router) OR `steps` +
    `else_steps` (Condition) — both walked uniformly here.

    The EventAdapter reads this from `RuntimeSession.node_types` to
    populate `NodeStartEvent.nodeType` for the trace panel.
    """
    from agno.workflow.router import Router
    from agno.workflow.condition import Condition
    from agno.workflow.loop import Loop
    from agno.workflow.parallel import Parallel
    from agno.workflow.steps import Steps
    from agno.workflow import Step

    out: dict[str, str] = {}

    def _walk(s, top_level: bool = True) -> None:
        if s is None:
            return
        sid = getattr(s, "step_id", None) or getattr(s, "name", None)
        if isinstance(s, (Router, Condition)):
            # Both primitives map to the merged `branch` node type —
            # mode is surfaced separately on `RuntimeSession`. Walk
            # `choices` (Router) + `steps` + `else_steps` (Condition)
            # uniformly so a single `_walk` covers both runtime shapes.
            if sid:
                out[sid] = "branch"
            for c in (getattr(s, "choices", []) or []):
                _walk(c, top_level=False)
            for c in (getattr(s, "steps", []) or []):
                _walk(c, top_level=False)
            for c in (getattr(s, "else_steps", []) or []):
                _walk(c, top_level=False)
        elif isinstance(s, (Parallel, Steps)):
            # Both primitives map to the merged `flow` node type —
            # mode is surfaced separately on `RuntimeSession`.
            if sid:
                out[sid] = "flow"
            for c in (getattr(s, "steps", []) or []):
                _walk(c, top_level=False)
        elif isinstance(s, Loop):
            if sid:
                out[sid] = "loop"
            for c in (getattr(s, "steps", []) or []):
                _walk(c, top_level=False)
        elif isinstance(s, Step):
            if sid:
                if getattr(s, "agent", None) is not None:
                    out[sid] = "agent"
                elif getattr(s, "requires_user_input", False):
                    out[sid] = "ask"
                else:
                    out[sid] = "step"
        else:
            if sid:
                out[sid] = type(s).__name__.lower()

    for s in getattr(wf, "steps", []) or []:
        _walk(s)
    return out