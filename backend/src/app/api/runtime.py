"""Runtime API — start/resume/cancel workflow execution. Thin shim over
`app.services.runtime_service`.

Endpoints:
  POST /api/v1/runtime/run                    -> SSE stream of events, ends on pause/completion/error
  POST /api/v1/runtime/continue               -> SSE stream resuming a paused session
  POST /api/v1/runtime/run-from               -> SSE stream starting at a chosen node
  POST /api/v1/runtime/{session_id}/cancel    -> set agno cancel flag; SSE stream emits ErrorEvent('workflow cancelled')
  GET  /api/v1/runtime/sessions/{id}          -> session inspection (debug)

SSE payload format (one event per `data:` line):
  data: {"type":"text","content":"..."}

The stream always ends with a `data: [DONE]` sentinel.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.events import RuntimeEvent
from app.auth import CurrentUser, current_user
from app.db.session import get_db
from app.schemas.workflow import ContinueRequest, RunFromRequest, RunRequest
from app.services import runtime_service

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])

# ─────────────────────────────────────────────────────────────────
# SSE formatting — HTTP concern, stays in the API layer
# ─────────────────────────────────────────────────────────────────
def _format_sse(event: RuntimeEvent | dict) -> bytes:
    payload = event.model_dump() if hasattr(event, "model_dump") else event
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

def _format_done() -> bytes:
    return b"data: [DONE]\n\n"

def _stream_response(session_id: str, events: list[RuntimeEvent]):
    """Wrap an event list + session id into a StreamingResponse."""
    def gen() -> AsyncIterator[bytes]:
        for ev in events:
            yield _format_sse(ev)
        yield _format_done()
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )

# ─────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────
@router.post("/run")
def run_workflow(
    payload: RunRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Start a workflow run. Streams SSE events; pauses on confirmation."""
    session_id, events = runtime_service.run_workflow(
        db,
        workflow_id=payload.workflow_id,
        input=payload.input,
        session_id=payload.session_id,
        user=user,
    )
    return _stream_response(session_id, events)

@router.post("/continue")
def continue_workflow(
    payload: ContinueRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Resume a paused session with the user's response.

     multi-user: the caller must supply a `user` and that
    user must own the session. Cross-user resume attempts get 404
    (same shape as "no such session" — we don't leak existence to
    a non-owner). The runtime service enforces the check.

    / session : `db` is injected so the
    service can recompile the slim session's lost `wf` handle
    (cross-restart path) — recompile reads the workflow row from
    SQLite via the existing `Workflow` table.
    """
    session_id, events = runtime_service.continue_workflow(
        db, payload.session_id, payload.response, user=user
    )
    return _stream_response(session_id, events)

@router.post("/run-from")
def run_workflow_from(
    payload: RunFromRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Start a NEW workflow run but begin execution at `start_node_id`
    instead of the entry node. Returns the same SSE stream as `/run`."""
    session_id, events = runtime_service.run_from(
        db,
        workflow_id=payload.workflow_id,
        input=payload.input,
        start_node_id=payload.start_node_id,
        user=user,
    )
    return _stream_response(session_id, events)

@router.get("/sessions/metrics")
def sessions_metrics():
    """/ session: operational metrics over the live
    in-process session store. Counts (total + per-status),
    unique-user count, oldest session's age — enough for a
    health check / admin panel to know "is the store growing?
    are sessions stuck in `running`?" without scraping logs.

    No persistence yet (session is v1.5) so these numbers
    reset on process restart. That's fine for v1 monitoring —
    the relevant signal is "trend over a single process
    lifetime", not "absolute count across restarts".

    Must be declared BEFORE the `/sessions/{session_id}` route —
    FastAPI matches in declaration order, and `{session_id}`
    would otherwise shadow the literal `metrics` segment.
    """
    return runtime_service.session_metrics()

@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    user: CurrentUser = Depends(current_user),
):
    """Inspect a session's state (status, history). Useful for debugging.

     multi-user: scoped to the caller's own sessions —
    cross-user reads return 404 so a non-owner can't enumerate sids
    by probing this endpoint.
    """
    return runtime_service.get_session(session_id, user=user)

@router.get("/sessions")
def list_sessions(
    workflow_id: str,
    user: CurrentUser = Depends(current_user),
):
    """/ session: list active slim sessions for a
    workflow. Sorted by `last_seen_at` desc so the most-recently-
    active session is first. Powers the frontend's debug /
    navigation surface (e.g. "Resume previous run" link)."""
    return runtime_service.list_sessions(workflow_id, user=user)

@router.post("/{session_id}/cancel")
def cancel_workflow(
    session_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Cancel a running workflow.

    agno 2.8.7 ships `Workflow.cancel_run(run_id)` backed by a
    process-wide `InMemoryRunCancellationManager` (RLock-guarded
    dict). The current leg's `for ev in agno_events:` loop calls
    `raise_if_cancelled(run_id)` between every agent chunk, so the
    cancellation flag fires near-immediately. The
    `EventAdapter` maps `WorkflowCancelledEvent` to
    `ErrorEvent(message="workflow cancelled")` and the in-flight
    SSE stream ends naturally — the client doesn't need to abort
    its `fetch` to surface the cancellation.

    Idempotent. Returns `{cancelled: false}` for sessions with no
    active run (already-completed, never-started, or already-
    cancelled). 404 on cross-user attempts (same shape as the
    other session endpoints — the multi-user invariant).

    / session : `db` is injected so the
    service can recompile the slim session's lost `wf` handle
    (cross-restart path) before calling `wf.cancel_run(run_id)`.
    """
    return runtime_service.cancel_session(db, session_id, user=user)