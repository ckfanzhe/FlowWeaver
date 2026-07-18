"""Runtime event types — produced by the executor, consumed by the SSE layer
and the frontend chat panel.

Mirrors frontend/src/types/workflow.ts `RuntimeEvent`.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

class TextEvent(BaseModel):
    type: Literal["text"] = "text"
    content: str

class ToolCallEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

class ToolResultEvent(BaseModel):
    """Outcome of a tool call.

    `ok` is True when the tool's call succeeded (no exception escaped
    agno's `Function.execute`). `False` either means the tool raised
    OR agno surfaced a `tool_call_error` for another reason (e.g.
    network timeout, schema-validation failure). The frontend uses
    this to render ✓ vs ✗; without it the chat panel defaults to
    ✗ for every tool call (the  `dispatch_task` export
    bug — the dispatch returned `{success: True, ...}` but the
    runtime emitted `tool_result` with no `ok` field, so the
    frontend's `?? false` fallback fired `isError: true`).

    Defaults to True so manually-constructed events in tests don't
    have to opt in; the SSE stream always sets it explicitly.
    """
    type: Literal["tool_result"] = "tool_result"
    tool: str
    result: Any = None
    ok: bool = True

class ConfirmationEvent(BaseModel):
    type: Literal["confirmation"] = "confirmation"
    kind: Literal["tool_confirm", "ask"]
    prompt: str
    choices: Optional[list[str]] = None
    toolCallId: Optional[str] = None

class CompletedEvent(BaseModel):
    type: Literal["completed"] = "completed"
    output: str

class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: str

class NodeStartEvent(BaseModel):
    """Emitted the instant the executor begins handling a node.

    `t` is a client-relative monotonic timestamp in milliseconds from
    session start (or `time.monotonic()`-based since session creation),
    so the frontend can render a per-node timeline without dealing with
    server clocks. `nodeId` is the workflow's node id, `nodeType` is one
    of the supported types, `label` is what the user named the node.
    """
    type: Literal["node_start"] = "node_start"
    nodeId: str
    nodeType: str
    label: str
    t: int

class NodeEndEvent(BaseModel):
    """Emitted after a node handler finishes (success or failure).

    `durationMs` is measured by the executor wrapping the handler call —
    LLM call + tool exec + everything inside is included. `tokens` is
    populated for LLM-backed nodes (agent, router) when agno's
    `RunOutput.metrics` reports them; otherwise None.
    """
    type: Literal["node_end"] = "node_end"
    nodeId: str
    status: Literal["ok", "error"]
    durationMs: int
    error: Optional[str] = None
    tokens: Optional[dict[str, int]] = None
    t: int

RuntimeEvent = (
    TextEvent
    | ToolCallEvent
    | ToolResultEvent
    | ConfirmationEvent
    | CompletedEvent
    | ErrorEvent
    | NodeStartEvent
    | NodeEndEvent
)

def event_to_dict(event: RuntimeEvent) -> dict:
    return event.model_dump()