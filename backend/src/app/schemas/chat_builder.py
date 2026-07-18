"""Schemas for the LLM-driven workflow-creation chat.

`BuilderEvent` mirrors the runtime's `RuntimeEvent`, but for the
"create / edit" chat surface. The two streams are distinct on purpose:
  * `RuntimeEvent` describes a workflow EXECUTION (text, tool_call/conf
 irmation, completed, node_start/end).
  * `BuilderEvent` describes a workflow DESIGN session (LLM
  thinking, tool call against the workflow JSON, validation result,
  pending diff, apply confirmation).

The two unions are intentionally separate so the frontend can render
them with different UIs without a discriminator prefix. They share the
SSE plumbing (`data: ...\\n\\n`).

Mirrors frontend/src/types/chatBuilder.ts.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────
# Chat history shape (sent in by the client)
# ─────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    """One entry in the chat history.

    `role` is `user` for human input, `assistant` for LLM replies. The
    platform does NOT replay tool_call / tool_result messages back into
    the LLM context — the chat builder replays them by re-running the
    agno session with the original messages. This shape exists so the
    client can render the history UI without re-parsing the SSE stream.
    """
    role: Literal["user", "assistant"]
    content: str

class ChatBuilderRequest(BaseModel):
    """Input for `POST /api/v1/chat/builder`.

    `workflow_id` — the workflow being edited. The service loads the
    workflow JSON to seed the LLM context (the LLM sees the current
    nodes/edges so it can make precise edits).

    `messages` — the user's full conversation history. Each request
    carries the whole history so the LLM can pick up where the user
    left off.

    `preset_id` — optional override for the user's default LLM preset.
    Lets the chat UI swap to a stronger model for a complex build
    ("") without changing the user's system default. The service
    must validate that the preset exists AND belongs to the calling
    user (or is system-shared) before honouring the override — never
    trust an unverified client-supplied id. When absent or invalid,
    the service falls back to `_resolve_default_preset_id`.

    `pending_change_id` — when the user said "Apply" to a previous
    diff, the service also returns the final applied state. This
    field is currently informational (the service recomputes the
    pending diff from scratch) but is reserved for a future
    optimistic-apply flow.
    """
    workflow_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    preset_id: Optional[str] = None

class ChatBuilderApplyRequest(BaseModel):
    """Input for `POST /api/v1/chat/builder/apply`.

    `session_id` — the chat session that owns the pending changes.
    `pending_diff` — the diff that the user approved. Re-validated
    server-side before commit (never trust client-side validation).
    """
    workflow_id: str
    session_id: str
    # The pending change set the user approved. Structure mirrors
    # `PendingChange` from `chat_builder_service.py`.
    pending: list[dict[str, Any]]

# ─────────────────────────────────────────────────────────────────
# Builder events — streamed to the client
# ─────────────────────────────────────────────────────────────────
class BuilderStartEvent(BaseModel):
    """First event of every /builder response. Carries the session id
    so the client can attach Apply / Cancel controls to the diff."""
    type: Literal["start"] = "start"
    session_id: str

class BuilderThinkingEvent(BaseModel):
    """LLM is reasoning. The frontend renders this as a spinner or
    'thinking…' chip while waiting for the next event."""
    type: Literal["thinking"] = "thinking"

class BuilderTextEvent(BaseModel):
    """Plain assistant text (e.g. "I'll add a Router node after the
    entry agent and connect it to a fallback agent.").

    `delta=True` marks the event as a streaming fragment — the
    client should APPEND `content` to the last text bubble in
    the current turn (creating one if none exists) rather than
    starting a new bubble. This is how the chat shows the LLM
    "typing" character by character / token by token instead of
    waiting for the full response to land in one burst.

    `delta=False` (default) is the final, complete text — the
    client should start a new text bubble. The streaming path
    uses deltas; the batched fallback path uses the final form.
    """
    type: Literal["text"] = "text"
    content: str
    delta: bool = False

class BuilderToolCallEvent(BaseModel):
    """LLM emitted a tool call against the workflow JSON.

    `tool_call_id` is the platform's id for the call — used to
    correlate the eventual `tool_result`. `tool` is the function name
    (e.g. `add_node`, `update_node`). `args` is the LLM's argument
    dict; the client renders it formatted (not raw JSON)."""
    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

class BuilderToolResultEvent(BaseModel):
    """Outcome of the tool call after the platform ran it.

    `ok` is True when the Pydantic + graph validation passed and the
    change is staged in the pending diff. `False` means the change
    was REJECTED (the LLM will see the error message in the next
    turn and can self-correct). `message` is the human-readable
    message for the chat."""
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool: str
    ok: bool
    message: str
    # Optional: a snippet of the diff that landed, so the UI can fold
    # it into a single diff card without waiting for the `diff`
    # event.
    diff_summary: Optional[dict[str, Any]] = None

class BuilderDiffEvent(BaseModel):
    """Cumulative diff of all pending changes so far in this chat
    session. The UI renders this as a single expandable card with
    Apply / Cancel buttons.

    `summary` is a tiny machine-readable form (e.g.
    `{"added_nodes": 2, "removed_nodes": 0, "updated_nodes": 1,
    "added_edges": 1, "removed_edges": 0}`) for the chip row.
    `nodes` / `edges` are the full before/after shapes for the
    expandable detail."""
    type: Literal["diff"] = "diff"
    summary: dict[str, int]
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)

class BuilderCompletedEvent(BaseModel):
    """Stream ends with this. Mirrors the runtime's `completed`."""
    type: Literal["completed"] = "completed"
    output: str = ""

class BuilderErrorEvent(BaseModel):
    """Stream ends with this on unrecoverable failure (LLM network,
    model unconfigured, schema violation that no retry can fix)."""
    type: Literal["error"] = "error"
    message: str

class BuilderRetryEvent(BaseModel):
    """Mid-stream retry notice — the previous attempt hit a
    transient parse failure (typical: SSE-parser JSONDecodeError
    when the Anthropic SDK reads a partial chunk). The chat layer
    re-runs `agent.run()` from scratch; the user sees a tiny
    "stream interrupted, retrying…" notice and the new attempt's
    events flow in below it. Duplicate `tool_call_id`s on the
    retry are deduped by id (existing behaviour — each id is
    fresh per LLM run, so collisions only happen when the LLM
    legitimately replays the same call)."""
    type: Literal["retry"] = "retry"
    reason: str = ""

BuilderEvent = (
    BuilderStartEvent
    | BuilderThinkingEvent
    | BuilderTextEvent
    | BuilderToolCallEvent
    | BuilderToolResultEvent
    | BuilderDiffEvent
    | BuilderCompletedEvent
    | BuilderErrorEvent
    | BuilderRetryEvent
)
