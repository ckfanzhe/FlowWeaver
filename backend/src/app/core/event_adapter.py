"""Translate agno's `WorkflowRunOutputEvent` stream into our `RuntimeEvent`s.

The visual builder's frontend (and the chat panel + trace panel) expect
`data: {RuntimeEvent...}\\n\\n` over SSE. The exact set of event types is
defined in `app.core.events` and mirrored in
`frontend/src/types/workflow.ts`. We MUST NOT change that wire format —
this module is the only place that knows about agno's event types.

Mapping table:

  WorkflowStartedEvent   -> (no-op — SSE consumer doesn't need it)
  StepStartedEvent       -> NodeStartEvent(nodeId, nodeType, label, t)
  StepOutputEvent        -> TextEvent (when content is str) OR ignored
                            when the same content already appeared as a
                            StepCompletedEvent payload
  StepCompletedEvent     -> NodeEndEvent(nodeId, status, durationMs, tokens, t)
                            + TextEvent(content) if non-empty
  WorkflowPausedEvent    -> ConfirmationEvent(kind, prompt, choices)
  WorkflowCompletedEvent -> CompletedEvent(output=content)
  WorkflowErrorEvent     -> ErrorEvent(message)
  WorkflowCancelledEvent -> ErrorEvent(message='workflow cancelled')
  ToolCallStartedEvent   -> ToolCallEvent (passed through)
  ToolCallCompletedEvent -> ToolResultEvent (passed through)
  RunContentEvent        -> TextEvent
  RunContentCompletedEvent -> (suppressed — content already in StepCompleted)

We accumulate a small per-step map of `step_id -> start_time` so we can
measure `durationMs` from the StepStarted→StepCompleted gap (or fall back
to zero when the events arrive out of order).
"""
from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

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
from app.runtime.session import RuntimeSession, session_store

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────
class EventAdapter:
    """Consume an agno event iterator and yield our `RuntimeEvent`s.

    Used by:
      - `app.api.runtime` SSE endpoint (streaming, one event at a time)
      - the JSON test harness (collect into a list and assert)
    """

    def __init__(self, *, session_id: Optional[str] = None) -> None:
        self.session_id = session_id
        self._step_starts: dict[str, float] = {}
        self._step_node_types: dict[str, str] = {}
        # step_id -> last text emitted by that step, so we don't double-emit
        self._step_emitted_text: set[str] = set()
        # step_id -> True if the step's only payload was a terminal
        # CompletedEvent (the workflow-level CompletedEvent will carry it).
        self._step_skip_text: set[str] = set()
        # step_id -> True when the step is the "no handler" sentinel
        # (we emit ONLY the ErrorEvent, not NodeStart/NodeEnd — matches
        # the legacy executor's behaviour so existing tests pass).
        self._step_is_synthetic_error: set[str] = set()
        # Tracks the step_id of the most-recently-started step, so the
        # pause handlers can record it on the session as `_pending_node`
        # (agno pauses a step with `requires_user_input=True` WITHOUT
        # calling its executor, so the legacy handler never gets a chance
        # to write `_pending_node` itself).
        self._last_started_step: Optional[str] = None
        # step_name -> step_id, captured from StepStartedEvent. Used to
        # reverse-map agno's StepCompletedEvent — in agno 2.8.7 the
        # completion event often arrives with `step_id` dropped (its
        # `to_dict()` filters None fields) and only the human-readable
        # `step_name` survives. Without this map the NodeEndEvent would
        # be emitted with `nodeId=<label>` instead of `<workflow-node-id>`,
        # which breaks the frontend's trace-store match-up and leaves
        # nodes stuck in "running" forever.
        self._step_name_to_id: dict[str, str] = {}

    def adapt(self, events: Iterator) -> list[RuntimeEvent]:
        """Consume an agno event iterator, return our translated events."""
        out: list[RuntimeEvent] = []
        # Reset stale resume bookkeeping on the SESSION so a single
        # Workflow instance can serve back-to-back legs without
        # leaking state. The bookkeeping now lives on RuntimeSession
        # itself ( multi-user refactor) — two concurrent
        # users can no longer race on a shared module-level dict.
        sess = self._resolve_session()
        if sess is not None:
            sess.set_last_run_id(None)
            sess.set_last_step_requirements(None)
        for ev in events:
            translated = self._translate_one(ev)
            if translated:
                out.extend(translated)
        return out

    def _capture_resume_state(self, ev) -> None:
        """Pull `run_id` + `step_requirements` off workflow-level events.

        agno 2.8.7 attaches `run_id` to every workflow event and
        `step_requirements` to `WorkflowPausedEvent` (only emitted when
        `stream_executor_events=False`). When the runtime requests
        `stream_executor_events=True` agno instead emits a
        `StepPausedEvent` per paused step — we synthesise a
        `StepRequirement` from its `user_input_schema` so the runtime
        service has something concrete to feed `Wf.continue_run(...)`.
        The runtime service reads these after the stream ends to build
        the next leg's `Wf.continue_run(...)` call.

        Writes land on `RuntimeSession` directly (per-user, per-
        session). No module-level globals, no cross-user contention.
        """
        sess = self._resolve_session()
        if sess is None:
            return
        run_id = getattr(ev, "run_id", None)
        if run_id:
            sess.set_last_run_id(run_id)
        cls_name = type(ev).__name__
        # 1) WorkflowPausedEvent carries a `step_requirements` list
        #    populated by the orchestrator. Keep only the latest capture.
        reqs = getattr(ev, "step_requirements", None)
        if reqs:
            sess.set_last_step_requirements(list(reqs))
            return
        # 2) StepPausedEvent carries the schema for ONE paused step.
        #    Reconstruct a StepRequirement so the resume leg can pass
        #    it to `Wf.continue_run(step_requirements=[...])`.
        if cls_name == "StepPausedEvent" and getattr(ev, "requires_user_input", False):
            schema_payload = getattr(ev, "user_input_schema", None) or []
            from agno.workflow.types import StepRequirement, UserInputField
            fields = []
            for f in schema_payload:
                if not isinstance(f, dict):
                    continue
                fields.append(UserInputField.from_dict(f))
            req = StepRequirement(
                step_name=getattr(ev, "step_name", None) or "",
                step_id=getattr(ev, "step_id", None),
                step_index=getattr(ev, "step_index", None),
                requires_user_input=True,
                user_input_message=getattr(ev, "user_input_message", None),
                user_input_schema=fields or None,
            )
            sess.set_last_step_requirements([req])

    # ─────────────────────────────────────────────────────────────────
    # Per-event translation
    # ─────────────────────────────────────────────────────────────────
    def _translate_one(self, ev) -> list[RuntimeEvent]:
        cls_name = type(ev).__name__
        # Pull the lightweight fields we need; `ev.to_dict()` always works.
        d = ev.to_dict() if hasattr(ev, "to_dict") else {}

        # Capture resume bookkeeping from workflow-level events. agno
        # attaches `run_id` to every workflow event and `step_requirements`
        # to pause events. The runtime service reads these after the
        # stream ends to build the next leg's `Wf.continue_run(...)` call.
        self._capture_resume_state(ev)

        if cls_name == "StepStartedEvent":
            return self._on_step_started(d)
        if cls_name == "StepCompletedEvent":
            return self._on_step_completed(d)
        if cls_name == "StepOutputEvent":
            return self._on_step_output(d)
        if cls_name == "WorkflowPausedEvent":
            return self._on_workflow_paused(d)
        if cls_name == "StepPausedEvent":
            return self._on_step_paused(d)
        if cls_name == "WorkflowCompletedEvent":
            return self._on_workflow_completed(d)
        if cls_name == "WorkflowErrorEvent":
            # Workflow-level error (catch-all in agno's
            # `_execute_stream`). Surface the error type + message
            # so the chat panel can show a real "what went wrong"
            # banner instead of an empty `completed` event.
            err_type = d.get("error_type") or "WorkflowError"
            err_msg = d.get("error") or "workflow error"
            return [ErrorEvent(
                message=f"[{err_type}] {err_msg}",
            )]
        if cls_name == "RunErrorEvent":
            # Agent-level error (e.g. LLM connection refused, model
            # not running, auth failure). agno's
            # `_handle_model_response_stream` raises
            # `ModelProviderError` after retries, which the agent
            # catches and re-emits as `RunErrorEvent` with the
            # error message on `content`. PRE-FIX: silently dropped
            # — the workflow then completed with empty `content`
            # and the user saw an empty `completed` event with no
            # indication of what failed (the user's report: "I
            # forgot to start vLLM, the system showed no error").
            err_type = d.get("error_type") or "RunError"
            err_msg = d.get("content") or d.get("error") or "agent run failed"
            return [ErrorEvent(
                message=f"[{err_type}] {err_msg}",
            )]
        if cls_name == "WorkflowCancelledEvent":
            return [ErrorEvent(message="workflow cancelled")]
        if cls_name == "RunContentEvent":
            # agno streams the agent's response token-by-token as
            # RunContentEvents. Each one would become its own TextEvent,
            # and the chat panel renders each TextEvent as its own bubble
            # — so a 40-token sentence shows as 40+ fragmented bubbles.
            # The full text is already carried by StepCompletedEvent (see
            # `_on_step_completed`), so we suppress mid-stream events to
            # keep the chat transcript coherent. The trace panel still
            # sees them via `traceEvent` so token-level telemetry is
            # preserved.
            return []
        if cls_name == "ToolCallStartedEvent":
            # agno's `ToolCallStartedEvent.tool` is a `ToolExecution`
            # dataclass whose `to_dict()` flattens into the parent
            # event dict under the `tool` key. Reaching for the
            # dataclass attribute directly is safer than
            # `d.get("tool")` because the dict shape can drift
            # (e.g. fields filtered out by None-strip, weird
            # approval_id sharing), and our `ToolCallEvent.tool`
            # field is typed as plain `str` — passing a dict
            # raises `type=string_type` and the SSE stream blows
            # up.
            tool_obj = getattr(ev, "tool", None)
            tool_name = getattr(tool_obj, "tool_name", None) or "tool"
            tool_args = getattr(tool_obj, "tool_args", None) or {}
            return [ToolCallEvent(tool=tool_name, args=tool_args)]
        if cls_name == "ToolCallCompletedEvent":
            tool_obj = getattr(ev, "tool", None)
            tool_name = getattr(tool_obj, "tool_name", None) or "tool"
            # In agno 2.8.7, `ToolCallCompletedEvent.content` is
            # the auto-generated timing string
            # `"<tool_name>(<args>) completed in <elapsed>s."` (set
            # at `agno/models/base.py:2970`), NOT the tool's actual
            # return value. The real return is in
            # `tool_executions[i].result` (str-wrapped). We prefer
            # `tool_obj.result` so the LLM gets an observable
            # success signal and the chat panel can render the real
            # output; we fall back to `ev.content` only when
            # `tool_obj.result` is missing. Matches
            # `chat_builder_service._stringify_content` and the
            # chat builder's `BuilderToolResultEvent.message`
            # contract.
            result = getattr(tool_obj, "result", None)
            if result is None or result == "":
                result = getattr(ev, "content", None)
            # agno's `tool_call_error` is set on the ToolExecution
            # dataclass: True if the tool raised an exception, False
            # otherwise. Forward it to the frontend so the chat
            # panel renders ✓ vs ✗ correctly (otherwise the
            # `data.ok ?? false` fallback in
            # `chatRuntimeAdapters.toolResultPart` always shows ✗).
            ok = not bool(getattr(tool_obj, "tool_call_error", None))
            return [ToolResultEvent(tool=tool_name, result=result, ok=ok)]
        # Unhandled event types are silently dropped — keeps us forward-
        # compatible with new agno events without crashing the SSE stream.
        return []

    def _on_step_started(self, d: dict) -> list[RuntimeEvent]:
        step_id = d.get("step_id")
        step_name = d.get("step_name") or step_id or ""
        # agno's StepStartedEvent sometimes arrives with empty step_id
        # (notably inside Router/Parallel executors when the executor is
        # the function form). Fall back to step_name so downstream
        # StepCompletedEvent matching still works.
        if not step_id:
            step_id = step_name
        if not step_id:
            return []
        node_type = self._lookup_node_type(step_id)
        now = time.monotonic()
        self._step_starts[step_id] = now
        self._step_node_types[step_id] = node_type
        self._last_started_step = step_id
        # Remember which workflow node id this human-readable label
        # belongs to so the matching StepCompletedEvent (which often
        # arrives with `step_id` stripped — see __init__) can be
        # reversed back to the same id.
        if step_name:
            self._step_name_to_id[step_name] = step_id
        t_ms = self._session_relative_ms()

        out: list[RuntimeEvent] = []
        # Drain any pending router-announce text. After the single-
        # engine refactor we no longer mirror `_router_announce` onto
        # the slim `RuntimeSession.context` — the announce is best-
        # effort and only survives in `additional_data["_router_announce"]`
        # on the step_input. agno 2.8.7 doesn't surface that through
        # StepStartedEvent, so we simply skip the announce emission
        # here. The trace panel still sees the branch steps firing in
        # order via their own NodeStart/NodeEnd events.
        out.append(NodeStartEvent(
            nodeId=step_id,
            nodeType=node_type or "step",
            label=step_name,
            t=t_ms,
        ))
        return out

    def _on_step_output(self, d: dict) -> list[RuntimeEvent]:
        # Mid-step text payload (e.g. an Agent streaming tokens). For
        # legacy handlers we emit the final content in StepCompletedEvent,
        # so we suppress duplicate text emissions here to avoid echoing
        # the same content twice on the frontend.
        step_output = d.get("step_output") or {}
        step_id = step_output.get("step_id") or d.get("step_name")
        content = step_output.get("content")
        if not content or not step_id:
            return []
        if step_id in self._step_emitted_text:
            return []
        # Only emit if this is a streaming-style chunk; for legacy
        # handlers the same content will arrive in StepCompletedEvent and
        # we emit it there instead.
        return []

    def _on_step_completed(self, d: dict) -> list[RuntimeEvent]:
        step_id = d.get("step_id")
        step_name = d.get("step_name") or step_id or ""
        if not step_id:
            # Same fallback as _on_step_started — match by name when agno
            # leaves step_id empty (notably inside Router/Parallel executors).
            step_id = step_name
        if not step_id:
            return []
        # agno 2.8.7 quirk: StepCompletedEvent.to_dict() strips None-valued
        # fields, so when agno forgets to populate `step_id` the wire
        # payload has only `step_name` (the user-facing label like
        # "ChatAgent"). Sometimes agno sends `step_id=<label>` directly,
        # and sometimes it sends nothing at all — in either case we may
        # end up with a value that's NOT a workflow node id. The label
        # is not unique enough to identify the workflow node (different
        # workflows can reuse "ChatAgent"), but the matching
        # StepStartedEvent recorded the mapping a moment earlier. If
        # the step_id we landed on isn't a known started step, look it
        # up by step_name and prefer that.
        if step_id not in self._step_starts and step_name in self._step_name_to_id:
            step_id = self._step_name_to_id[step_name]
        step_response = d.get("step_response") or {}
        content = d.get("content") or step_response.get("content")
        success = step_response.get("success", True)
        error_msg = step_response.get("error")
        executor_type = step_response.get("executor_type")
        metrics = step_response.get("metrics") or {}
        tokens = self._extract_tokens(metrics)
        duration_ms = self._compute_duration_ms(step_id)
        t_ms = self._session_relative_ms()

        # agent : after the single-engine refactor we
        # read errors straight from `step_response.error` — agno's
        # canonical channel. The legacy `[error] ` content-prefix sentinel
        # and the `sess.context["_error"]` mirror write are gone; both
        # were workarounds for a hand-rolled state machine that no longer
        # exists. Surface as a canonical ErrorEvent so the SSE stream
        # matches the v1 wire shape.
        out: list[RuntimeEvent] = []
        if error_msg:
            out.append(ErrorEvent(message=str(error_msg)))
        out.append(NodeEndEvent(
            nodeId=step_id,
            status="ok" if success else "error",
            durationMs=duration_ms,
            error=str(error_msg) if error_msg else None,
            tokens=tokens,
            t=t_ms,
        ))
        if error_msg:
            # Don't also emit the plain text payload — for error cases the
            # chat panel wants the ErrorEvent above, not a body of text.
            return out

        # Human-input steps surface their user reply as a `function`
        # executor's `content` (the echo executor returns the raw
        # `user_input` dict). That payload is NOT a chat-panel message
        # — it's the structured answer we already showed as the
        # confirmation response. Suppress the text echo so the SSE
        # stream doesn't emit `{"type":"text","content":"{}"}` after
        # every confirmation cycle.
        if executor_type == "function":
            return out

        # Plain text payload → emit a TextEvent the chat panel can render.
        if content and not isinstance(content, (list, dict)):
            text = str(content).strip()
            if text and step_id not in self._step_emitted_text:
                self._step_emitted_text.add(step_id)
                out.append(TextEvent(content=text))
        return out

    def _on_step_paused(self, d: dict) -> list[RuntimeEvent]:
        """A single step requested human input. Emit our `ConfirmationEvent`
        so the frontend knows to prompt the user.

        After the single-engine refactor  the prompt + choices
        ride on the agno event itself (`user_input_message` and
        `user_input_schema`). The legacy mirror writes
        (`sess.context["_pending_kind"]`, `_pending_node`,
        `_pending_human_choices`, `_pending_human_choices_map`) are gone
        — `RuntimeSession.pending_requirements` is populated by
        `_capture_resume_state` from the parent `WorkflowPausedEvent`
        instead.
        """
        prompt = d.get("user_input_message") or "Please provide input"
        choices = self._choices_from_payload(d.get("user_input_schema"))
        return [ConfirmationEvent(
            kind="ask",
            prompt=str(prompt),
            choices=choices,
        )]

    @staticmethod
    def _choices_from_payload(schema) -> Optional[list]:
        """Recover a flat `choices` list from an agno `user_input_schema`.

        agno's `requires_user_input=True` schema carries the choices
        under `[{name: ..., options: [...]}]`. For v1 we surface just
        the first field's options — multi-field schemas are out of
        scope for the chat-style confirmation flow.
        """
        if not schema or not isinstance(schema, list) or not schema:
            return None
        first = schema[0]
        if not isinstance(first, dict):
            return None
        opts = first.get("options")
        return list(opts) if opts else None

    def _on_workflow_paused(self, d: dict) -> list[RuntimeEvent]:
        # Build a ConfirmationEvent from the active StepRequirement(s).
        # For v1 we expect exactly one paused step at a time.
        step_requirements = d.get("step_requirements") or []
        if not step_requirements:
            # Some agno versions put requirements under different keys —
            # fall back to whatever we find.
            step_requirements = (
                d.get("requirements")
                or d.get("step_requirement")
                or []
            )
        if step_requirements:
            req = step_requirements[0]
            kind = "ask"
            prompt = (
                req.get("confirmation_message")
                or req.get("user_input_message")
                or req.get("user_input_schema", [{}])[0].get("description", "")
                if isinstance(req, dict)
                else "Awaiting input"
            )
            schema = req.get("user_input_schema") if isinstance(req, dict) else None
            choices = None
            if schema and isinstance(schema, list) and schema:
                first = schema[0]
                if isinstance(first, dict) and "options" in first:
                    choices = first["options"]
            return [ConfirmationEvent(
                kind=kind,
                prompt=str(prompt),
                choices=choices,
            )]
        # Fallback: a generic confirmation with the workflow pause reason.
        reason = d.get("reason") or d.get("message") or "workflow paused"
        return [ConfirmationEvent(
            kind="ask",
            prompt=str(reason),
        )]

    def _on_workflow_completed(self, d: dict) -> list[RuntimeEvent]:
        return [CompletedEvent(output=str(d.get("content") or ""))]

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────
    def _compute_duration_ms(self, step_id: str) -> int:
        start = self._step_starts.get(step_id)
        if start is None:
            return 0
        return int((time.monotonic() - start) * 1000)

    def _extract_tokens(self, metrics: dict) -> Optional[dict[str, int]]:
        """Re-shape agno's `metrics` dict into our `{input, output, total}`."""
        if not metrics:
            return None
        try:
            inp = int(metrics.get("input_tokens") or 0)
            outp = int(metrics.get("output_tokens") or 0)
            tot = int(metrics.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return None
        if inp == 0 and outp == 0 and tot == 0:
            return None
        if tot == 0:
            tot = inp + outp
        return {"input": inp, "output": outp, "total": tot}

    def _lookup_node_type(self, step_id: str) -> Optional[str]:
        """Recover the original node `type` from the active RuntimeSession.

        The slim RuntimeSession carries a `node_types: {step_id: type}`
        mapping that the orchestrator populates after compiling the
        graph. The legacy `sess.node_map` is gone — the workflow
        object owns the graph now.

        Returns None when no session is available (synthesise 'step').
        """
        sess = self._resolve_session()
        if sess is None:
            return None
        return sess.node_types.get(step_id)

    def _resolve_session(self) -> Optional[RuntimeSession]:
        if not self.session_id:
            return None
        return session_store().get(self.session_id)

    def _session_relative_ms(self) -> int:
        """Milliseconds since session start (used for the `t` field)."""
        sess = self._resolve_session()
        if sess is None or sess.started_at == 0.0:
            return 0
        return int((time.monotonic() - sess.started_at) * 1000)