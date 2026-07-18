"""ChatBuilder API — LLM-driven workflow-creation chat.

Endpoints:
  POST /api/v1/chat/builder        -> SSE stream of BuilderEvent
  POST /api/v1/chat/builder/apply  -> commit the staged diff
  POST /api/v1/chat/builder/cancel -> discard the staged diff

The two-mutating-endpoint split (`apply` / `cancel`) keeps the
chat interactions safe: the LLM may produce a diff, but the
DB isn't touched until the user explicitly clicks Apply.

SSE payload format matches the runtime endpoint:
  data: {"type": "start", "session_id": "..."}
  data: {"type": "tool_call", ...}
  data: {"type": "tool_result", ...}
  data: {"type": "diff", ...}
  data: {"type": "completed", "output": ""}

The stream always ends with `data: [DONE]`.

Streaming. The service is a **generator** — each `BuilderEvent`
is yielded as the LLM produces it, so the client sees the diff
card grow tool call by tool call rather than waiting for the
whole turn to finish. The `StreamingResponse` simply iterates
the generator and serializes each event to SSE.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Iterable

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.db.session import get_db
from app.schemas.chat_builder import (
    ChatBuilderApplyRequest,
    ChatBuilderRequest,
)
from app.schemas.workflow import WorkflowRead
from app.services import chat_builder_service

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# ─────────────────────────────────────────────────────────────────
# SSE formatting
# ─────────────────────────────────────────────────────────────────
def _format_sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")

def _format_done() -> bytes:
    return b"data: [DONE]\n\n"

def _stream_response(events: Iterable) -> StreamingResponse:
    """Wrap an iterable / generator of `BuilderEvent`s as an SSE
    response.

    Accepts either a generator (real streaming run — events land
    on the wire as the LLM emits them) or a list (legacy batched
    path; still honored so non-streaming providers keep working).
    The `for` loop pulls from the iterator lazily, so a streaming
    generator that blocks on the next LLM token will not block
    the whole response — only the next chunk.
    """
    def gen() -> AsyncIterator[bytes]:
        for ev in events:
            payload = ev.model_dump() if hasattr(ev, "model_dump") else ev
            yield _format_sse(payload)
        yield _format_done()
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

# ─────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────
@router.post("/builder")
def chat_builder(
    payload: ChatBuilderRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Run one chat turn against the workflow.

    The client sends the full conversation history each request so
    the LLM picks up where the user left off. The response is an
    SSE stream of `BuilderEvent`s; the first event is always a
    `start` event with the session id. Events arrive incrementally
    as the LLM produces them — the user sees the diff card grow
    in real time.
    """
    events = chat_builder_service.run_chat_turn(
        db,
        workflow_id=payload.workflow_id,
        messages=[m.model_dump() for m in payload.messages],
        user=user,
        preset_id=payload.preset_id,
    )
    return _stream_response(events)

@router.post("/builder/apply")
def chat_builder_apply(
    payload: ChatBuilderApplyRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Commit the staged diff to the workflow row.

    Server-side re-validation runs against the LATEST workflow row
    (race-safe against edits from another tab). On success, the
    chat session is discarded and the new workflow state is
    returned.
    """
    row = chat_builder_service.apply_pending_changes(
        db,
        workflow_id=payload.workflow_id,
        session_id=payload.session_id,
        user=user,
    )
    return JSONResponse(jsonable_encoder(WorkflowRead.from_orm_row(row).model_dump()))

@router.post("/builder/cancel")
def chat_builder_cancel(
    payload: ChatBuilderApplyRequest,
    user: CurrentUser = Depends(current_user),
):
    """Discard a chat session without applying.

    Idempotent — calling cancel on an unknown session returns 200
    with `discarded: false`. The client uses this to clear the
    diff card when the user clicks "Cancel".
    """
    before = chat_builder_service.get_session(payload.session_id)
    if before is None:
        return JSONResponse({"discarded": False})
    chat_builder_service.cancel_session(payload.session_id, user=user)
    return JSONResponse({"discarded": True})
