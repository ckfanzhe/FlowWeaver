"""Regression tests for `EventAdapter` (agno → RuntimeEvent translation).

The adapter is the boundary between agno's `WorkflowRunOutputEvent`
dataclass stream and our Pydantic `RuntimeEvent` SSE wire format.
Bug history (chat export): `ToolCallEvent.tool`
was wired to `d.get("tool")` from `ev.to_dict()`. That dict comes
back as the full `ToolExecution` payload (tool_call_id, approval_id,
...). Our `ToolCallEvent.tool` is typed `str`, so Pydantic raised
`type=string_type` and the SSE stream blew up — the chat saw
the same error repeatedly until the cap ran out.

These tests pin the fix: the adapter MUST read `tool_name` /
`tool_args` / `result` off the `ToolExecution` dataclass directly,
not via `to_dict()` serialization.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.event_adapter import EventAdapter
from app.core.events import ToolCallEvent, ToolResultEvent

class ToolCallStartedEvent:
    """Stub matching agno's `ToolCallStartedEvent`.

    The adapter's `_translate_one` keys off `type(ev).__name__`, so the
    class MUST be named exactly `"ToolCallStartedEvent"`. `to_dict()`
    mirrors the actual agno 2.8.7 shape (the full `ToolExecution`
    serialised under the `tool` key)."""

    def __init__(self, te: SimpleNamespace):
        self.tool = te

    def to_dict(self):
        return {
            "event": "ToolCallStarted",
            "tool": self.tool.__dict__,
        }

class ToolCallCompletedEvent:
    """Stub matching agno's `ToolCallCompletedEvent`."""

    def __init__(self, te: SimpleNamespace):
        self.tool = te
        self.content = None

    def to_dict(self):
        return {
            "event": "ToolCallCompleted",
            "tool": self.tool.__dict__,
            "content": None,
        }

def _agno_started(tool_name: str, tool_args: dict, *, approval_id: str | None = None,
                  tool_call_id: str = "call_test"):
    """Build a stub `ToolCallStartedEvent` whose `to_dict()` returns
    the full ToolExecution dict (matching agno 2.8.7's actual shape)."""
    te = SimpleNamespace(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_call_error=None,
        result=None,
        approval_id=approval_id,
    )
    return ToolCallStartedEvent(te)

def _agno_completed(tool_name: str, result, *, tool_call_error=None):
    te = SimpleNamespace(
        tool_call_id="call_test",
        tool_name=tool_name,
        tool_args={"k": "v"},
        tool_call_error=tool_call_error,
        result=result,
        approval_id=None,
    )
    return ToolCallCompletedEvent(te)

def test_event_adapter_translates_tool_call_started_via_dataclass_not_dict():
    """The exact the original bug: `to_dict()` returns `tool` as a full
    ToolExecution dict. The adapter must extract `tool_name` /
    `tool_args` from the dataclass directly, NOT the dict under the
    `tool` key (which is a dict, not a string)."""
    started = _agno_started(
        tool_name="http_request",
        tool_args={"url": "https://api.example.com"},
        approval_id="appr-123",
        tool_call_id="chatcmp-call-001",
    )

    events = EventAdapter().adapt([started])

    assert len(events) == 1, f"expected 1 event, got {len(events)}"
    ev = events[0]
    assert isinstance(ev, ToolCallEvent), f"expected ToolCallEvent, got {type(ev).__name__}"
    # The contract: tool is a plain string, args is the LLM's arg dict.
    assert ev.tool == "http_request", (
        f"ToolCallEvent.tool must be a string (the tool name), got {ev.tool!r}"
    )
    assert ev.args == {"url": "https://api.example.com"}
    # Regression guard: the dict-shape payload must NOT leak into the
    # `tool` field (the original Pydantic ValidationError bug).
    assert not isinstance(ev.tool, dict), (
        "REGRESSION: ev.tool is a dict — to_dict() shape leaked through"
    )

def test_event_adapter_translates_tool_call_completed_with_result_payload():
    """Fix (chat export): agno 2.8.7 puts the
    tool's actual return value on `tool_obj.result` (str-wrapped)
    AND the auto-generated timing string `"<tool_name>(<args>)
    completed in <elapsed>s."` on `event.content`. The adapter MUST
    prefer `tool_obj.result` — the timing string is useless for
    the LLM (no observable success signal) and useless for the
    UI (no real data to render). Matches the chat builder's
    `_stringify_content(t.result)` contract."""
    completed = _agno_completed("http_request", {"status": 200, "body": "ok"})

    events = EventAdapter().adapt([completed])

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolResultEvent)
    assert ev.tool == "http_request"
    assert ev.result == {"status": 200, "body": "ok"}
    # No tool_call_error → ok=True (the default the SSE wire expects).
    assert ev.ok is True

def test_event_adapter_prefers_tool_obj_result_over_timing_content():
    """Pin the the recent priority order: `tool_obj.result` wins
    over `event.content`. Before the fix the adapter preferred
    `event.content` (the timing string) — every tool result on
    the SSE wire was the `"<tool_name>(<args>) completed in
    X.XXXXs."` placeholder, so the chat panel rendered nothing
    useful and the LLM had no success signal."""
    class ToolCallCompletedEvent:
        def __init__(self):
            self.tool = SimpleNamespace(
                tool_call_id="call_test",
                tool_name="query_substations",
                tool_args={"city": "x", "district": "y"},
                tool_call_error=None,
                # agno sets `result = str(function_call_result.content)`
                # — for an HTTP tool returning JSON, this is the JSON
                # body str-wrapped (e.g. `'{"substations": [...]}'`).
                result='{"substations": [{"id": "s1", "name": "..."}]}',
                approval_id=None,
            )
            # agno sets `event.content` to the timing string at
            # `models/base.py:2970`. This is the field the
            # pre-the recent adapter incorrectly preferred.
            self.content = (
                "query_substations(city=x, district=y) completed in 0.0049s. "
            )

        def to_dict(self):
            return {
                "event": "ToolCallCompleted",
                "tool": self.tool.__dict__,
                "content": self.content,
            }

    events = EventAdapter().adapt([ToolCallCompletedEvent()])
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolResultEvent)
    # The actual tool result, NOT the timing string.
    assert ev.result == '{"substations": [{"id": "s1", "name": "..."}]}'
    assert "completed in" not in str(ev.result)

def test_event_adapter_translates_tool_call_completed_with_ok_on_error():
    """The the original `dispatch_task` bug regression: when the tool
    raised and agno set `tool_call_error=True`, the adapter MUST
    surface `ok=False` so the chat panel renders ✗ instead of the
    default `?? false` fallback firing prematurely. The frontend
    `toolResultPart` (chatRuntimeAdapters.ts:366) reads `data.ok` —
    without this propagation, EVERY successful tool call shows ✗."""
    completed = _agno_completed(
        "http_request",
        result="boom: connection refused",
        tool_call_error=True,
    )

    events = EventAdapter().adapt([completed])

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolResultEvent)
    assert ev.ok is False
    assert ev.result == "boom: connection refused"

def test_event_adapter_prefers_tool_obj_result_over_timing_content():
    """Pin the the recent priority order INVERSION.

    PRE-FIX: this adapter preferred `event.content` over
    `tool_obj.result` based on the assumption that `event.content`
    carries the raw tool return. In agno 2.8.7 that's WRONG:
    `event.content` is the auto-generated timing string
    `"<tool_name>(<args>) completed in <elapsed>s."` (set at
    `agno/models/base.py:2970`), and `tool_obj.result` carries
    the actual str-wrapped tool return.

    This test inverts the previous expectation: when `event.content`
    is the timing string and `tool.result` is the real payload,
    the adapter MUST prefer `tool.result` so the UI sees
    structured data, not the timing placeholder."""
    te = SimpleNamespace(
        tool_call_id="call_test",
        tool_name="http_request",
        tool_args={"k": "v"},
        tool_call_error=None,
        # agno's ToolExecution.result = str(function_call_result.content)
        # — for an HTTP tool returning JSON, this is the JSON body
        # str-wrapped.
        result='{"success": true, "task_id": "T-1"}',
        approval_id=None,
    )
    completed = ToolCallCompletedEvent(te)
    # agno's ToolCallCompletedEvent.content = the timing string.
    # Test still keeps a "real-looking" content here so we exercise
    # the priority logic (tool.result wins over content).
    completed.content = (
        "http_request(k=v) completed in 0.0049s. "
    )

    events = EventAdapter().adapt([completed])

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolResultEvent)
    # tool_obj.result wins. The actual JSON, NOT the timing string.
    assert ev.result == '{"success": true, "task_id": "T-1"}'
    assert "completed in" not in str(ev.result)

def test_event_adapter_handles_missing_tool_name_gracefully():
    """Defensive: if `tool_obj.tool_name` is None (malformed event),
    the adapter must still produce a valid `ToolCallEvent` rather than
    blowing up. We fall back to `"tool"` as the lowest common
    denominator — the SSE consumer can decide to drop the event."""
    te = SimpleNamespace(
        tool_call_id="x",
        tool_name=None,
        tool_args={"a": 1},
        approval_id=None,
    )
    events = EventAdapter().adapt([ToolCallStartedEvent(te)])

    assert len(events) == 1
    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool == "tool"
    assert events[0].args == {"a": 1}

def test_event_adapter_handles_tool_args_none():
    """`tool_args` defaults to None on the dataclass — must default to
    `{}` on the wire."""
    te = SimpleNamespace(
        tool_call_id="x",
        tool_name="noop",
        tool_args=None,
        approval_id=None,
    )
    events = EventAdapter().adapt([ToolCallStartedEvent(te)])

    assert len(events) == 1
    assert events[0].args == {}

def test_event_adapter_unknown_event_type_is_silently_dropped():
    """Forward-compat: an event type the adapter doesn't know about
    must not crash the stream. Documented contract in `event_adapter.py`
    docstring."""
    class _UnknownEvent:
        def to_dict(self):
            return {"future_field": "x"}

    events = EventAdapter().adapt([_UnknownEvent()])
    assert events == []

# ─────────────────────────────────────────────────────────────────
# RunErrorEvent + WorkflowErrorEvent — surface LLM / runtime errors
# so the chat panel can show a real "what went wrong" banner
# instead of an empty `completed` event.
# ─────────────────────────────────────────────────────────────────
from app.core.events import ErrorEvent

def test_event_adapter_translates_run_error_event_to_error():
    """Pin the the recent fix: agno emits `RunErrorEvent` with the
    error message on `content` when the LLM call fails (e.g.
    vLLM not running → `APIConnectionError` → `ModelProviderError`
    → `RunErrorEvent(content="[ModelProviderError] API connection
    error: ...")`. PRE-FIX the adapter silently dropped this
    event; the workflow then emitted `WorkflowCompletedEvent` with
    `content=None`, and the user saw `CompletedEvent(output="")`
    with no indication of failure.

    The fix: surface the error message as an `ErrorEvent` with
    the `[error_type]` prefix so the chat panel renders a
    user-visible error banner instead of the silent empty
    `completed`."""
    class RunErrorEvent:
        def to_dict(self):
            return {
                "event": "RunError",
                "content": "API connection error from OpenAI API: Connection error.",
                "error_type": "ModelProviderError",
                "error_id": None,
                "additional_data": None,
            }

    events = EventAdapter().adapt([RunErrorEvent()])
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ErrorEvent)
    assert ev.message == "[ModelProviderError] API connection error from OpenAI API: Connection error."

def test_event_adapter_translates_workflow_error_event_to_error():
    """Workflow-level error (agno's broad `except Exception` in
    `_execute_stream`). PRE-FIX the adapter emitted a generic
    `"workflow error"` with no type / message detail. The fix
    surfaces `[error_type] message` so the chat panel shows a
    real banner."""
    class WorkflowErrorEvent:
        def to_dict(self):
            return {
                "event": "WorkflowError",
                "error": "Step 'inspection_agent' raised ModelProviderError: connection refused",
                "error_type": "WorkflowError",
                "error_id": None,
                "additional_data": None,
            }

    events = EventAdapter().adapt([WorkflowErrorEvent()])
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ErrorEvent)
    assert "connection refused" in ev.message
    assert ev.message.startswith("[WorkflowError]")

def test_event_adapter_run_error_event_handles_missing_content():
    """Defensive: if `RunErrorEvent.content` is None / empty (e.g.
    agno evolves the dataclass), the adapter must still emit an
    `ErrorEvent` with a placeholder message rather than dropping
    the event silently (which is the regression we're closing)."""
    class RunErrorEvent:
        def to_dict(self):
            return {"event": "RunError", "content": None, "error_type": "RunErrorEvent"}

    events = EventAdapter().adapt([RunErrorEvent()])
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    # The fallback message keeps the chat panel honest even when
    # agno's error payload is sparse.
    assert events[0].message == "[RunErrorEvent] agent run failed"
