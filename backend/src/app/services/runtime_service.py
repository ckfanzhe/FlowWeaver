"""Runtime service — single-engine orchestrator.

Compile + run + adapt. Every call to `Wf.run(...)` / `Wf.continue_run(...)`
is the only engine we use; the legacy hand-rolled state machine that
sat on top of agno's primitives (`runtime/session.cursor`,
`context["_last_text"]`, `_pending_human_choices`, `_router_announce`,
…) is gone.

Lifecycle (single leg):

  1. Load the workflow row + validate non-empty.
  2. `compile.build_workflow(...)` → compiled `agno.Workflow`.
     The same object is reused across pause/resume legs; recompiling
     would force a fresh `WorkflowSession` and lose the persisted
     `WorkflowRunOutput` agno stores at each pause.
  3. `Wf.run(input=..., session_id=..., stream=True, stream_events=True,
     stream_executor_events=True)` — agno yields a
     `WorkflowRunOutputEvent` stream. The EventAdapter captures the
     last `run_id` (the orchestrator reads it from
     `last_run_id_for_session(...)` after the stream ends) and the
     last pause event's `step_requirements`.
  4. `EventAdapter.adapt(...)` — translate agno events into our SSE
     wire contract. Identical translation rules as before.
  5. Mirror `status` / `output` onto the slim `RuntimeSession` so the
     API layer can inspect it via `GET /sessions/{id}`.

Resume leg (`/runtime/continue`):

  1. Look up the slim session; verify `status == waiting_confirmation`.
  2. Walk `sess.pending_requirements` and apply the user's response
     via `set_user_input(...)` on the active requirement.
  3. `Wf.continue_run(run_id=sess.run_id, session_id=sess.id,
     step_requirements=updated, stream=True, stream_events=True,
     stream_executor_events=True)` — agno resumes where the pause
     left off, no recompile, no mirror channels.

Why drop `Wf.run(start_node_id=...)` for `/runtime/run-from`?
  agno 2.8.7 doesn't accept a `start_node_id` parameter on `run()`.
  The cleanest equivalent is to compile a fresh `Workflow(steps=[...])`
  that begins at the requested node — `compile.build_workflow` already
  honours `start_node_id=` for that.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.compile import build_workflow, extract_node_types
from app.core.event_adapter import EventAdapter
from app.core.events import CompletedEvent, ConfirmationEvent, ErrorEvent, RuntimeEvent
from app.core.http_constants import HTTP_422
from app.core.ir import build_ir
from app.db.models import Workflow
from app.runtime.session import RuntimeSession, session_store

# RBAC for the run endpoints. `CurrentUser` is imported lazily
# under TYPE_CHECKING so module import doesn't drag the FastAPI
# dependency machinery into the runtime test path.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.auth import CurrentUser

from app.services import member_service

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Workflow loading
# ─────────────────────────────────────────────────────────────────
def _load_workflow(db: Session, workflow_id: str) -> Workflow:
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    return row

def _require_non_empty(workflow: Workflow) -> list:
    """Return the workflow's nodes, raising 422 if the row is empty."""
    nodes = workflow.nodes or []
    if not nodes:
        raise HTTPException(
            HTTP_422,
            "Workflow is empty. Drag a node onto the canvas first.",
        )
    return nodes

# ─────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────
def run_workflow(
    db: Session,
    workflow_id: str,
    input: str,
    session_id: str | None = None,
    user: "CurrentUser | None" = None,
) -> Tuple[str, list[RuntimeEvent]]:
    """Start a workflow run. Streams SSE events; pauses on confirmation.

    `user` is forwarded by the HTTP layer. We enforce viewer access
    here so a non-member can't even run a workflow they can't see —
    the access check happens BEFORE the agno runtime starts so a
    rejected call costs the caller only a 403, not a half-built
    `WorkflowRunOutput`.
    """
    wf_row = _load_workflow(db, workflow_id)
    if user is not None and not bool(getattr(wf_row, "is_template", False)):
        member_service.require_role(db, workflow_id, user, "viewer")
    nodes = _require_non_empty(wf_row)
    return _run_leg(
        workflow_id=wf_row.id,
        workflow_name=wf_row.name,
        db_nodes=nodes,
        db_edges=wf_row.edges or [],
        input=input,
        session_id=session_id,
        start_node_id=None,
        user_id=getattr(user, "id", None),
        workflow_updated_at=getattr(wf_row, "updated_at", None),
    )

def continue_workflow(
    db: Session,
    session_id: str,
    response: Any,
    user: "CurrentUser | None" = None,
) -> Tuple[str, list[RuntimeEvent]]:
    """Resume a paused session with the user's response.

    The `Wf.continue_run(...)` call IS the resume — agno rebuilds its
    internal cursor from the persisted `WorkflowRunOutput` and picks up
    exactly where the pause left off. We just have to apply the user's
    answer to the active `StepRequirement` first.

     multi-user: the caller must supply a `user` and that
    user must own the session. Cross-user resume attempts get 403 so
    an attacker can't hijack a paused run by guessing the sid. We
    also re-check `member_service.require_role(..., "viewer")` on the
    underlying workflow — covers the case where ownership flipped
    while the session was paused (admin demoted the user, etc.).

    / session (, commit 2): the slim session
    survives process restart via SQLite, but the compiled
    `agno.Workflow` (`sess.wf`) is transient — it's rebuilt via
    `build_workflow(...)` on demand when the cache comes back
    empty (cache miss on `get_for_user`).

    / session cross-restart caveat: the
    `pending_requirements` carried in the slim session are agno's
    `StepRequirement` objects (in-process) but only their JSON-safe
    view survives the trip through SQLite (commit 1's
    `_serialize_reqs`). On restart, `_row_to_session` populates
    them as a list of dicts. The frontend rehydrate path (session
    step 2) reads them via `GET /sessions/{id}` to restore the
    pause UI — dicts are fine for that. The backend resume path
    here needs real objects to call `req.set_user_input(...)` on
    the active requirement, so a cross-restart resume raises 409
    with a clear message instead of crashing on `AttributeError`.
    """
    store = session_store()
    caller_id = getattr(user, "id", None) if user is not None else None
    sess = store.get_for_user(session_id, caller_id)
    if sess is None:
        # Don't leak "this sid exists but isn't yours" — same wire
        # shape for "no such session" and "someone else's session"
        # so a non-owner can't enumerate sids by status code.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if sess.status != "waiting_confirmation":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"session {session_id} not waiting for confirmation "
            f"(status={sess.status})",
        )

    # / session (commit 2): cross-restart pause-resume
    # lost the in-memory `StepRequirement` objects — the DB only
    # carries their JSON-safe view (dicts). Detect and surface a
    # clear 409 instead of letting `req.set_user_input` crash on
    # `AttributeError`. The frontend's rehydrate UI shows the pause
    # state from the dict view; only the backend resume path is
    # affected.
    if (
        sess.pending_requirements
        and isinstance(sess.pending_requirements[0], dict)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "session paused state lost across process restart — "
            "please re-trigger the workflow",
        )

    # / session (commit 2): recompile on cache miss.
    # `sess.wf` is None when the slim session came back from
    # SQLite (post-restart) — the compiled `agno.Workflow` is
    # transient. The workflow row's `nodes` + `edges` are still
    # in DB; we rebuild the same workflow object. Compiled
    # `wf.cancel_run` and the cached `wf.get_run_output` calls
    # below both need the live `wf`.
    if sess.wf is None:
        wf_row = _load_workflow(db, sess.workflow_id)
        try:
            sess.wf = build_workflow(
                workflow_id=wf_row.id,
                name=wf_row.name or wf_row.id,
                db_nodes=wf_row.nodes or [],
                db_edges=wf_row.edges or [],
                session_id=sess.id,
                user_id=caller_id,
            )
            sess.node_types = extract_node_types(sess.wf)
        except Exception as e:  # noqa: BLE001
            log.exception("recompile-on-load failed for %s", sess.id)
            sess.status = "error"
            sess.append_event(
                ErrorEvent(
                    message=f"workflow recompile failed: {type(e).__name__}: {e}"
                )
            )
            sess.flush()
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "workflow recompile failed",
            )

    # Apply the user's answer to the active requirement(s).
    updated = list(sess.pending_requirements or [])
    if updated:
        for req in updated:
            if getattr(req, "requires_user_input", False):
                field_name, value = _coerce_response_for_requirement(req, response)
                try:
                    req.set_user_input(validate=False, **{field_name: value})
                except Exception:  # noqa: BLE001
                    # Defensive: if validation rejects, fall back to
                    # raw payload — better than crashing the resume leg.
                    req.user_input = response
                break

    sess.status = "running"
    sess.pending_requirements = []

    # Resolve the run_id. The EventAdapter captures it during the
    # stream; if for some reason it's missing, ask agno's workflow
    # cache directly (cache_session=True makes this cheap).
    run_id = sess.get_last_run_id()
    if not run_id and sess.wf is not None:
        ro = sess.wf.get_run_output(session_id=sess.id)  # type: ignore[arg-type]
        run_id = getattr(ro, "run_id", None) if ro else None
    if not run_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "session has no resumable run",
        )
    sess.set_last_run_id(run_id)

    # Wf.continue_run expects `session_id` to match the run's
    # `WorkflowSession.session_id` (the ID agno uses internally — NOT
    # our slim `RuntimeSession.id`). Both ids happen to be identical
    # here (we pass `session_id=sess.id` to `Wf.run(...)`), but the
    # persisted run may carry its own `run_response.session_id`. Use
    # what the run actually carries so agno's `Wf.get_session(...)`
    # cache hit doesn't 404.
    run_session_id = sess.id
    if sess.wf is not None:
        cached_run = sess.wf.get_run_output(run_id=run_id, session_id=sess.id)
        if cached_run is not None and getattr(cached_run, "session_id", None):
            run_session_id = cached_run.session_id

    try:
        agno_events = sess.wf.continue_run(
            run_id=run_id,
            session_id=run_session_id,
            step_requirements=updated or None,
            stream=True,
            stream_events=True,
            stream_executor_events=True,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("wf.continue_run failed")
        sess.status = "error"
        err = ErrorEvent(
            message=f"workflow resume failed: {type(e).__name__}: {e}"
        )
        sess.append_event(err)
        sess.flush()  # / session (commit 2)
        return sess.id, [err]

    return _finalise_leg(sess, agno_events)

def run_from(
    db: Session,
    workflow_id: str,
    input: str,
    start_node_id: str,
    user: "CurrentUser | None" = None,
) -> Tuple[str, list[RuntimeEvent]]:
    """Start a NEW workflow run but begin at `start_node_id`.

    `user` is forwarded by the HTTP layer. Same viewer check as
    `run_workflow` — non-members can't start a run-from either.
    """
    wf_row = _load_workflow(db, workflow_id)
    if user is not None and not bool(getattr(wf_row, "is_template", False)):
        member_service.require_role(db, workflow_id, user, "viewer")
    nodes = _require_non_empty(wf_row)
    edges = wf_row.edges or []

    ir = build_ir(nodes, edges)
    if start_node_id not in ir.node_map:
        err = ErrorEvent(
            message=f"start node {start_node_id!r} not in workflow"
        )
        # Route through SessionStore.create() so the user_id is
        # recorded and the by-user index stays consistent. Previously
        # this path poked `_sessions[sid] = sess` directly, bypassing
        # the store API and leaving the row ownerless.
        sess = session_store().create(
            workflow_id=workflow_id,
            input=input,
            user_id=getattr(user, "id", None),
            session_id=f"run-{start_node_id[:8]}",
        )
        sess.status = "error"
        sess.append_event(err)
        sess.flush()  # / session (commit 2)
        return sess.id, [err]

    return _run_leg(
        workflow_id=wf_row.id,
        workflow_name=wf_row.name,
        db_nodes=nodes,
        db_edges=edges,
        input=input,
        session_id=None,
        start_node_id=start_node_id,
        user_id=getattr(user, "id", None),
        workflow_updated_at=getattr(wf_row, "updated_at", None),
    )

# ─────────────────────────────────────────────────────────────────
# Internal: compile + run + adapt
# ─────────────────────────────────────────────────────────────────
def _run_leg(
    *,
    workflow_id: str,
    workflow_name: str,
    db_nodes: list[dict],
    db_edges: list[dict],
    input: str,
    session_id: str | None,
    start_node_id: str | None,
    user_id: str | None = None,
    workflow_updated_at: "datetime | None" = None,
) -> Tuple[str, list[RuntimeEvent]]:
    """Single leg: compile, run, adapt.

    `user_id` : the workflow owner's id, threaded into
    `build_workflow(...)` so the agent / MCP emitters scope their
    resource lookups against the owner's presets / MCP servers. The
    caller layer (run_workflow / run_from) pulls this off the
    `CurrentUser` dependency.

    `workflow_updated_at` (/ session, ): if the
    incoming workflow was edited after the slim session was
    created (i.e. `workflow_updated_at > sess.workflow_updated_at`),
    the slim session is dropped BEFORE the reuse branch — the
    next call creates a fresh `AgentSession` so the agent
    doesn't try to call tools / nodes that no longer exist.
    The previous behaviour (re-compile the workflow but keep
    the prior agent context) silently left stale tool messages
    in `AgentSession.messages` after every workflow edit; the
    user-visible symptom was the agent calling a deleted tool.
    """
    store = session_store()
    sess: "RuntimeSession | None" = None
    if session_id is not None:
        # / session : ownership check on reuse. The
        # prior `store.get(session_id)` was an unscoped lookup —
        # any caller who happened to know another user's
        # `session_id` (e.g. via a leaked URL, browser history,
        # or a brute-force probe against the 8-char uuid4 hex
        # namespace) could resume THAT user's session and see its
        # conversation history. The slim session itself is
        # already partitioned by `user_id` (line 233 in
        # runtime/session.py), and `get_for_user(...)` enforces
        # that partition — we just weren't using it on this
        # code path. Switch to the owner-scoped lookup. If the
        # session belongs to a different user, `get_for_user`
        # returns None — and we fall through to `create(...)`,
        # which mints a fresh session for THIS caller (so the
        # attack surfaces as "you got a fresh session" rather
        # than "you accessed the other user's data"). The
        # security property is the partition: cross-user reads
        # cannot resume a foreign session.
        sess = store.get_for_user(session_id, user_id)
        # / session: stale-session_id also signals "the slim
        # session was deleted server-side" (cancel / Clear / prior
        # workflow-edit invalidation). If `get_for_user(...)`
        # returns None for any reason, we MUST mint a fresh sid
        # below — passing the stale `session_id` to `create(...)`
        # would resurrect the prior id under a fresh
        # AgentSession, a subtle source of bugs for any
        # introspection / session-list UX. The two causes (cross-
        # user vs server-deleted) collapse to the same fall-
        # through, but the security partition is still enforced
        # by `get_for_user` — see the comment above.
        if sess is None:
            session_id = None
        if (
            sess is not None
            and workflow_updated_at is not None
            and sess.workflow_updated_at is not None
            and workflow_updated_at > sess.workflow_updated_at
        ):
            log.info(
                "workflow %s edited (was %s, now %s) — invalidating "
                "slim session %s (will mint fresh sid for fresh "
                "AgentSession context)",
                workflow_id,
                sess.workflow_updated_at.isoformat(),
                workflow_updated_at.isoformat(),
                session_id,
            )
            store.delete(session_id)
            # Drop the prior sid so `create(...)` below mints a fresh
            # one — keeps the frontend's `sessionId` in lockstep
            # with the slim session lifecycle (session: a stale
            # session_id pointing at a fresh session would be a
            # subtle source of bugs for any introspection / future
            # session-list UX).
            session_id = None
            sess = None
        elif sess is not None:
            sess.input = input
    if sess is None:
        sess = store.create(
            workflow_id,
            input,
            user_id=user_id,
            session_id=session_id,
            workflow_updated_at=workflow_updated_at,
        )

    try:
        wf = build_workflow(
            workflow_id=workflow_id,
            name=workflow_name or workflow_id,
            db_nodes=db_nodes,
            db_edges=db_edges,
            session_id=sess.id,
            start_node_id=start_node_id,
            user_id=user_id,
        )
    except ValueError as e:
        log.warning("build_workflow rejected: %s", e)
        sess.status = "error"
        err = ErrorEvent(message=str(e))
        sess.append_event(err)
        sess.flush()  # / session (commit 2)
        return sess.id, [err]
    except Exception as e:  # noqa: BLE001
        log.exception("build_workflow failed")
        sess.status = "error"
        err = ErrorEvent(
            message=f"workflow compile failed: {type(e).__name__}: {e}"
        )
        sess.append_event(err)
        sess.flush()  # / session (commit 2)
        return sess.id, [err]

    sess.wf = wf

    # Capture node-type mapping for the EventAdapter so NodeStartEvent
    # events can surface the original node type (the trace panel uses
    # this to pick the right icon).
    sess.node_types = extract_node_types(wf)

    try:
        agno_events = wf.run(
            input=input,
            stream=True,
            stream_events=True,
            stream_executor_events=True,
            session_id=sess.id,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("wf.run failed")
        sess.status = "error"
        err = ErrorEvent(
            message=f"workflow run failed: {type(e).__name__}: {e}"
        )
        sess.append_event(err)
        sess.flush()  # / session (commit 2)
        return sess.id, [err]

    return _finalise_leg(sess, agno_events)

def _finalise_leg(
    sess: RuntimeSession,
    agno_events,
) -> Tuple[str, list[RuntimeEvent]]:
    """Translate agno events, mirror status/output, capture resume state."""
    adapter = EventAdapter(session_id=sess.id)
    events = adapter.adapt(agno_events)

    for ev in events:
        sess.append_event(ev)

    # Pull resume bookkeeping out of the EventAdapter's capture.
    # ( multi-user refactor: state lives on the session,
    # not on a module-level dict keyed by sid.)
    run_id = sess.get_last_run_id()
    if run_id:
        sess.set_last_run_id(run_id)

    reqs = sess.get_last_step_requirements()
    if reqs:
        sess.set_last_step_requirements(reqs)

    if any(isinstance(ev, CompletedEvent) for ev in events):
        comp = next(ev for ev in events if isinstance(ev, CompletedEvent))
        if comp.output:
            sess.output = comp.output
        sess.status = "completed"
    elif any(isinstance(ev, ConfirmationEvent) for ev in events):
        sess.status = "waiting_confirmation"
        pause_ev = next(ev for ev in events if isinstance(ev, ConfirmationEvent))
        sess.history.append({"confirmation": pause_ev.model_dump()})
    elif any(isinstance(ev, ErrorEvent) for ev in events):
        sess.status = "error"

    # / session (commit 1): mirror final state to SQLite
    # so list_sessions / metrics (which now query the DB) see the
    # completed / waiting_confirmation / error row. Commit 2
    # extends the flush wiring to the error paths in `_run_leg`
    # (build_workflow / wf.run failures) — those currently drop
    # without a flush, but the existing tests don't cover that
    # path yet.
    sess.flush()

    return sess.id, events

# ─────────────────────────────────────────────────────────────────
# Session inspection (debug)
# ─────────────────────────────────────────────────────────────────
def get_session(
    session_id: str,
    user: "CurrentUser | None" = None,
) -> dict:
    """Inspect a session's state (status, history, pending_requirements).
    Useful for debugging AND for the / session frontend
    rehydration path: a user who refreshes mid-pause calls
    `GET /runtime/sessions/{sid}` to learn that the session is
    `waiting_confirmation` AND which `pending_requirements` are
    outstanding, then restores the `pendingConfirmation` chat
    state from the first pending requirement.

     multi-user: scoped to the caller's own sessions, same
    shape as `continue_workflow` — `404 Session not found` for both
    "no such sid" and "not yours" so a non-owner can't enumerate.
    """
    store = session_store()
    caller_id = getattr(user, "id", None) if user is not None else None
    sess = store.get_for_user(session_id, caller_id)
    if sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return {
        "id": sess.id,
        "workflow_id": sess.workflow_id,
        "status": sess.status,
        "input": sess.input,
        "output": sess.output,
        "history": sess.history,
        # / session: surface the pending HITL gate so the
        # frontend can reconstruct the `pendingConfirmation`
        # state on page load. The most recent requirement is what
        # the EventAdapter would have translated into a
        # `ConfirmationEvent`; we mirror that shape here so the
        # frontend can rehydrate from either path identically.
        "pending_requirements": list(sess.pending_requirements or []),
    }

def list_sessions(
    workflow_id: str,
    user: "CurrentUser | None" = None,
) -> list[dict]:
    """/ session + session: list active slim sessions
    for a workflow. SQL query over the `runtime_sessions` table
    (session) so the list survives process restart — the
    pre-persistence in-memory dict was bounded by the cleanup cron
    and lost on every restart.

    Returns a summary (no full `history`) of every session that
    targets `workflow_id` AND belongs to `user.id`. The list is
    sorted by `last_seen_at` descending so the most-recently-
    active session comes first — that's the one the user is most
    likely to want to inspect.

     multi-user: ownership-scoped — the SQL filter
    `user_id = :caller OR user_id IS NULL` excludes other users'
    sessions, same shape as `get_session` / `continue_workflow`.
    Anonymous callers see only their own anonymous sessions.
    """
    from sqlalchemy import or_

    from app.db.models import RuntimeSessionRow

    store = session_store()
    caller_id = getattr(user, "id", None) if user is not None else None
    with store._scope() as db:
        q = db.query(RuntimeSessionRow).filter(
            RuntimeSessionRow.workflow_id == workflow_id
        )
        if caller_id is not None:
            q = q.filter(
                or_(
                    RuntimeSessionRow.user_id == caller_id,
                    RuntimeSessionRow.user_id.is_(None),
                )
            )
        rows = q.all()
    rows.sort(key=lambda r: r.last_seen_at, reverse=True)
    return [
        {
            "id": r.id,
            "status": r.status,
            "last_seen_at": r.last_seen_at,
            "started_at": r.started_at,
            "input": r.input,
            "has_pending_requirements": bool(r.pending_requirements),
        }
        for r in rows
    ]

def session_metrics() -> dict:
    """/ session: operational metrics over the live
    in-process session store. Thin pass-through to
    `SessionStore.metrics()` so the route handler can stay
    declarative (no business logic at the API layer)."""
    return session_store().metrics()

def cancel_session(
    db: Session,
    session_id: str,
    user: "CurrentUser | None",
) -> dict:
    """Cancel a running workflow via agno's `Workflow.cancel_run`.

    The agno loop calls `raise_if_cancelled(run_id)` between every
    agent chunk, so cancellation is near-immediate once the flag is
    set. The `EventAdapter` already maps `WorkflowCancelledEvent` to
    `ErrorEvent(message="workflow cancelled")`, so the SSE stream
    just naturally ends with the error event + DONE — no extra
    streaming glue needed.

    Multi-user invariant: 404 on cross-user attempts (same shape as
    `get_session`). Idempotent on already-completed / unknown
    sessions — returns `{cancelled: false}` so the client can clear
    its optimistic UI without surfacing a stale 4xx.

    / session : on a successful cancel, the slim
    session is DELETED (not just left in `_sessions`). The
    previous behaviour kept the row around so the frontend could
    POST a fresh `/runtime/run` against the same `session_id` —
    but that meant the backend held an orphaned slim session
    indefinitely. Deleting on cancel means a follow-up turn
    starts a fresh `AgentSession` (which is the right semantics
    anyway — the user just cancelled, they probably want a
    clean slate) and the `_SESSION` dict stays bounded. The
    frontend's stale `session_id` is reconciled by the HTTP
    layer's `X-Session-Id` response header on the next turn.

    / session (, commit 2): the compiled
    `wf` is transient. After a process restart, `sess.wf` is
    None; we recompile via the workflow row so
    `wf.cancel_run(run_id)` has an instance to call on. The
    `InMemoryRunCancellationManager` is process-wide (not on
    the workflow instance), so this still cancels the live
    agno loop if it's still streaming — but if the workflow
    was paused and the process restarted before any leg
    re-streamed, the cancel is a no-op (`run_id` not
    registered). Either way: idempotent + safe.
    """
    store = session_store()
    caller_id = getattr(user, "id", None) if user is not None else None
    sess = store.get_for_user(session_id, caller_id)
    if sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    # Only meaningful when the session is actually in-flight. After
    # completion the agno loop has already torn down; flagging it now
    # would either resurrect a stale row in the cancel manager or
    # silently affect a future unrelated run that happens to share
    # the recycled run_id.
    if sess.status not in ("running", "waiting_confirmation"):
        # Idempotent on completed / unknown — also drop the slim
        # session if it's still hanging around (covers the
        # post-completion stale-row case the history
        # called out).
        store.delete(session_id)
        return {"cancelled": False}

    # / session (commit 2): recompile on cache miss.
    if sess.wf is None:
        try:
            wf_row = _load_workflow(db, sess.workflow_id)
            sess.wf = build_workflow(
                workflow_id=wf_row.id,
                name=wf_row.name or wf_row.id,
                db_nodes=wf_row.nodes or [],
                db_edges=wf_row.edges or [],
                session_id=sess.id,
                user_id=caller_id,
            )
            sess.node_types = extract_node_types(sess.wf)
        except Exception as e:  # noqa: BLE001
            log.exception("recompile-on-load failed for %s", sess.id)
            # Recompile failed — drop the slim session and report
            # idempotently. The user can re-trigger from the UI.
            store.delete(session_id)
            return {"cancelled": False}

    run_id = sess.run_id
    wf = sess.wf
    cancelled = False
    if run_id is not None and wf is not None:
        # `Workflow.cancel_run` returns False when the run_id isn't yet
        # registered with the cancellation manager (cancel-before-start
        # race), but it still records the intent. Both outcomes count as
        # "cancelled" from the client's perspective.
        wf.cancel_run(run_id)
        cancelled = True
    # / session: drop the slim session so the in-process
    # `_SESSIONS` dict stays bounded. See docstring.
    store.delete(session_id)
    return {"cancelled": cancelled}

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _coerce_response_for_requirement(req, response: Any) -> tuple[str, Any]:
    """Map a frontend-posted `response` onto the schema field name.

    Frontend contract:
      - text prompt    →  bare string              →  schema field `response`
      - choice prompt  →  `{"selection": ""}`  →  schema field `selection`
      - confirm prompt →  bare bool                →  schema field `confirmation`
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