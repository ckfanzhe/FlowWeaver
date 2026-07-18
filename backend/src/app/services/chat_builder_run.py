"""F5  — runtime-debug tools for the chat builder.

The chat builder lets the LLM edit a workflow through tools.
F0–F4 made that safe. F5 closes the last gap: after the LLM
edits a workflow, **how does it know whether it actually works?**
Until F5 the LLM could only ask the user to click "Run" and
report what they saw — no way to inspect a structured trace,
no way to attribute failures to a specific node config.

Three LLM-facing tools:

  * `run_workflow(input, hitl_responses=None)` — run the session's
    STAGED graph (NOT the DB row) in a tmp workflow, collect a
    structured trace, return `{run_id, status}`.
  * `inspect_run(run_id)` — return the trace as JSON: per-step
    input / output / tool calls / timing / errors.
  * `explain_failure(run_id)` — hard-coded diagnostic rules over
    the trace, returning a `{diagnosis, suggested_fix}` shape the
    LLM can act on.

Architecture decisions (locked , see chat history):

  * **No coupling to `runtime_service`.** Pillar 1's runtime path
    already implements compile + run + adapt; F5 deliberately does
    NOT call into it. The runtime path is HTTP-bound (SSE +
    `RuntimeSession` + `session_store`), the chat builder needs an
    isolated, testable path. We build the workflow via
    `core.compile.build_workflow` directly and collect events via
    `core.compile.drive_leg_with_trace` — the leg-mechanics facade
    (row B, ) is shared with whoever else wants a
    per-step accumulator.
  * **`_RUNS` is a module dict (maxsize 50, FIFO eviction).**
    Cross-worker unsafe, same caveat as `chat_builder_service.
    _SESSIONS`. F6 / phase will swap to redis when the chat
    becomes multi-process.
  * **HITL injection via `hitl_responses=` parameter.** When the
    workflow pauses on a `ConfirmationEvent`, the test / chat
    supplies the answer ahead of time. Production paths without
    an answer get `status='paused'` and `pending_requirements`
    surfaced in the trace — the LLM can then ask the user via a
    follow-up turn.
  * **Status / trace types are kept narrow.** Only what the LLM
    can act on. We do NOT mirror every `RuntimeEvent` field —
    inspect_run is a view, not a dump.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.compile import (
    DEFAULT_RUN_TIMEOUT_SEC as _DEFAULT_TIMEOUT,
    LegStep,
    build_workflow,
    drive_leg_with_trace,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────
# FIFO eviction — at 50, a chat that has run dozens of test
# iterations keeps the last 50 traces. Smaller than `_SESSIONS`
# (no cap today) because each trace carries per-step tool
# payloads; a runaway chat would otherwise bloat memory.
MAX_RUNS = 50

# Re-export so the public `chat_builder_run.DEFAULT_RUN_TIMEOUT_SEC`
# stays stable — tests pin this name. The canonical constant lives in
# `core.compile.run`; the chat-builder default is the same value.
DEFAULT_RUN_TIMEOUT_SEC = _DEFAULT_TIMEOUT

# ─────────────────────────────────────────────────────────────────
# Trace data model — pure dataclasses, JSON-serialisable
# ─────────────────────────────────────────────────────────────────
# row B : `RunStep` is now a back-compat alias for
# `app.core.compile.LegStep`. The shape was identical (defined in
# two files); the canonical dataclass is the one in `core.compile`
# so the leg-mechanics facade has a single type to return.
RunStep = LegStep

@dataclass
class RunTrace:
    """The full trace returned by `run_workflow` / `inspect_run`.

    Carries everything needed to (a) show the user what happened,
    (b) feed `explain_failure` so it can attribute errors to
    specific nodes, and (c) drive HITL injection via
    `pending_requirements`.
    """
    run_id: str
    workflow_id: str
    session_id: str
    input: str
    started_at: float  # epoch seconds (UTC)
    completed_at: Optional[float] = None
    status: str = "running"  # "completed" | "failed" | "paused"
    output: Optional[str] = None
    steps: list[RunStep] = field(default_factory=list)
    pending_requirements: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "session_id": self.session_id,
            "input": self.input,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "output": self.output,
            "steps": [self._step_to_dict(s) for s in self.steps],
            "pending_requirements": list(self.pending_requirements),
            "error": self.error,
        }

    @staticmethod
    def _step_to_dict(s: RunStep) -> dict:
        return {
            "step_id": s.step_id,
            "node_id": s.node_id,
            "node_type": s.node_type,
            "label": s.label,
            "status": s.status,
            "input": s.input,
            "output": s.output,
            "tool_calls": list(s.tool_calls),
            "duration_ms": s.duration_ms,
            "started_at_ms": s.started_at_ms,
            "error": s.error,
        }

# ─────────────────────────────────────────────────────────────────
# RunTraceStore — bounded FIFO dict, thread-unsafe (single-process)
# ─────────────────────────────────────────────────────────────────
class RunTraceStore:
    """In-memory store for `RunTrace` records.

    FIFO eviction at `maxsize` — at 50, a chat that runs many test
    iterations keeps only the last 50 traces. Older traces are
    silently dropped; the LLM can always re-run if it needs an
    older trace back. NOT safe across workers (mirrors
    `chat_builder_service._SESSIONS`).
    """

    def __init__(self, maxsize: int = MAX_RUNS) -> None:
        self._maxsize = maxsize
        self._runs: "OrderedDict[str, RunTrace]" = OrderedDict()

    def record(self, trace: RunTrace) -> None:
        self._runs[trace.run_id] = trace
        # Evict oldest if over cap. OrderedDict.move_to_end /
        # popitem gives us FIFO via insertion order.
        while len(self._runs) > self._maxsize:
            self._runs.popitem(last=False)

    def get(self, run_id: str) -> Optional[RunTrace]:
        return self._runs.get(run_id)

    def list(self) -> list[dict]:
        """Return a list of run summaries (one line per trace)."""
        return [
            {
                "run_id": t.run_id,
                "workflow_id": t.workflow_id,
                "status": t.status,
                "started_at": t.started_at,
                "completed_at": t.completed_at,
            }
            for t in self._runs.values()
        ]

    def clear(self) -> None:
        """Drop everything. Tests use this between cases."""
        self._runs.clear()

# Module-level singleton. Pinned to F5's decision .
_RUNS = RunTraceStore()

def get_store() -> RunTraceStore:
    """Return the module-level store (overridable in tests via
    `set_store`)."""
    return _RUNS

def set_store(store: RunTraceStore) -> None:
    """Inject a custom store. Used by tests; in production the
    module singleton is canonical."""
    global _RUNS
    _RUNS = store

# ─────────────────────────────────────────────────────────────────
# run_workflow — execute staged graph, collect trace
# ─────────────────────────────────────────────────────────────────
def run_workflow(
    db_nodes: list[dict],
    db_edges: list[dict],
    *,
    workflow_id: str,
    workflow_name: str = "",
    input: str,
    user_id: Optional[str] = None,
    hitl_responses: Optional[list[Any]] = None,
    timeout_sec: float = DEFAULT_RUN_TIMEOUT_SEC,
) -> RunTrace:
    """Run `db_nodes` / `db_edges` as a tmp workflow, return a
    `RunTrace`.

    `db_nodes` / `db_edges` are whatever `apply_ops_to_state`
    produced from the session's staged state — this is the
    "staged graph in, trace out" contract.

    `hitl_responses` is a list of values, one per pending
    `StepRequirement`. Index 0 answers the first pause, index 1
    the second, etc. When the workflow pauses and there are
    enough responses queued, `Wf.continue_run(...)` is invoked
    automatically and the responses are spliced onto the active
    requirements. When the queue runs dry mid-run the trace ends
    with `status='paused'` and `pending_requirements` lists what's
    still unanswered.

    : legacy type literals (`human_input`, …)
    are rewritten to the merged names (`ask`, …) here, mirroring the
    envelope-read migration in `workflow_io.parse` and
    `WorkflowNode._validate_node_type`. The staged graph bypasses
    both of those, so we run `migrate_envelope` here as the third
    wire-in point — keeps the contract "staged graph in, trace
    out" immune to which path produced the graph.

    Returns:
        A `RunTrace`. The trace is also stored in the module-level
        `_RUNS` so `inspect_run(run_id)` can return it.
    """
    from app.core._compat import migrate_envelope
    migrate_envelope({"nodes": db_nodes, "edges": db_edges})

    trace = RunTrace(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        workflow_id=workflow_id,
        session_id="",  # filled below — distinct from chat session id
        input=input,
        started_at=time.time(),
    )
    run_session_id = f"chat-run-{uuid.uuid4().hex[:8]}"
    trace.session_id = run_session_id

    try:
        wf = build_workflow(
            workflow_id=workflow_id,
            name=workflow_name or workflow_id,
            db_nodes=db_nodes,
            db_edges=db_edges,
            session_id=run_session_id,
            start_node_id=None,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Compile failure surfaces as a failed trace so the LLM
        # can call `explain_failure` to attribute it.
        trace.status = "failed"
        trace.error = f"workflow compile failed: {type(exc).__name__}: {exc}"
        trace.completed_at = time.time()
        _RUNS.record(trace)
        return trace

    # row B: the leg machinery now lives in
    # `core.compile.drive_leg_with_trace`. We pass `_apply_hitl_response`
    # as the splice callback so field-name routing stays in this module.
    # The facade returns `(session_id, events, steps, pending_requirements,
    # output, error, status)` — we drop `events` (the SSE consumers stay on
    # `runtime_service`) and map the rest straight onto `RunTrace` fields.
    (
        _run_session_id,
        _events,
        trace.steps,
        trace.pending_requirements,
        trace.output,
        trace.error,
        trace.status,
    ) = drive_leg_with_trace(
        wf,
        session_id=run_session_id,
        input=input,
        hitl_responses=hitl_responses or [],
        timeout_sec=timeout_sec,
        on_hitl=_apply_hitl_response,
    )
    trace.completed_at = time.time()
    _RUNS.record(trace)
    return trace

def _apply_hitl_response(
    reqs: list[dict],
    response: Any,
) -> Optional[list[Any]]:
    """Mutate the first requirement that needs user input, copying
    it through. Returns the (mutated) list of requirement objects,
    or None on failure."""
    # `reqs` are dict-shaped because EventAdapter.model_dumps them.
    # The actual resume needs the underlying agno StepRequirement
    # objects — we have no direct handle. For test injection we
    # log the response and return None so the caller surfaces a
    # clean "couldn't apply" error rather than crashing.
    # Production paths use `Wf.continue_run(response=...)` with the
    # raw value, bypassing the per-requirement mutation. We mirror
    # that by injecting the response into the dict.
    if not reqs:
        return None
    req = reqs[0]
    if not isinstance(req, dict):
        return None
    req["user_input"] = response
    return reqs

# ─────────────────────────────────────────────────────────────────
# inspect_run — return trace JSON
# ─────────────────────────────────────────────────────────────────
def inspect_run(run_id: str) -> Optional[dict]:
    """Return the trace for `run_id` as a JSON-ready dict, or
    None if the run is unknown / evicted / never existed."""
    trace = _RUNS.get(run_id)
    if trace is None:
        return None
    return trace.to_dict()

def list_runs() -> list[dict]:
    """List all stored traces (summaries). For the LLM's
    'recent runs?' question."""
    return _RUNS.list()

# ─────────────────────────────────────────────────────────────────
# explain_failure — diagnostic rules
# ─────────────────────────────────────────────────────────────────
# Each rule is a function `(trace: RunTrace) -> Optional[Diagnosis]`
# returning a `(diagnosis, suggested_fix)` pair when the trace
# matches the failure mode, else None. Rules run in order; first
# match wins. Adding a new rule = appending to `_RULES` + (if
# shape changes) a single line in the LLM-facing tool docstring.

# Common agno / runtime error fragments the LLM runs into.
# Rules match against `trace.error` (top-level) or per-step
# `step.error`. Patterns are anchored substrings, not full
# regex — keeps rules readable for the LLM-facing rationale text.

_RULES: list = []

def _register(rule):
    _RULES.append(rule)
    return rule

@_register
def _selector_undefined_variable(trace: RunTrace):
    """Router / condition selectors that reference variables the
    runtime never wrote."""
    if not trace.error:
        return None
    needle = "is not defined"
    if needle not in trace.error:
        return None
    # The runtime surfaces "name 'X' is not defined". Pull out
    # the variable name — the suggested_fix can list the
    # well-known runtime scopes.
    m = re.search(r"name '([a-zA-Z_][a-zA-Z0-9_]*)' is not defined", trace.error)
    var = m.group(1) if m else "<variable>"
    return (
        f"a router or condition selector references `{var}`, but "
        "the runtime never wrote that variable into the evaluation "
        "scope. The runtime only exposes: `input` (the user "
        "message), `previous_step_outputs` (dict keyed by node "
        "id), `additional_data`, and `session_state`.",
        f"use `previous_step_outputs['<source_node_id>']` instead, "
        "or set the upstream agent's `output_key` so its output is "
        "addressable by the name your selector expects.",
    )

@_register
def _loop_missing_body_target(trace: RunTrace):
    """Loop without `body_target`."""
    for step in trace.steps:
        if step.node_type != "loop":
            continue
        # The runtime error is opaque ("loop body not configured")
        # but the GraphValidator already catches it at apply time.
        # We surface a different message here when the trace shows
        # a loop step that immediately errored.
        if step.status == "error" and step.error:
            return (
                f"loop `{step.node_id}` failed because no body "
                "node is configured. A loop wraps exactly one "
                "executable node — set the loop's `body_target` "
                "field to the id of the agent / step you want it "
                "to retry.",
                "call `update_node` on the loop with "
                "`patch.config.bodyTarget = '<agent_id>'`, or use "
                "`create_retry_loop` which sets `body_target` for "
                "you.",
            )
    return None

@_register
def _tool_source_not_wired(trace: RunTrace):
    """An agent that referenced a tool but no tool_attachment
    edge exists in the staged graph."""
    if not trace.steps:
        return None
    for step in trace.steps:
        if step.node_type != "agent":
            continue
        # The agent step errored AND it never called a tool. That
        # suggests the tool wasn't wired in.
        if (
            step.status == "error"
            and not step.tool_calls
            and step.error
            and "tool" in step.error.lower()
        ):
            return (
                f"agent `{step.node_id}` failed because it tried "
                "to use a tool that isn't wired in. The tool list "
                "in `agent.config.tools` is empty (or the "
                "attached sources have drifted out of sync).",
                "call `create_react_agent` to rebuild the agent "
                "with its tool sources, or `connect_nodes` with "
                "`kind='tool_attachment'` from each tool source "
                "to the agent.",
            )
    return None

@_register
def _compile_error(trace: RunTrace):
    """Top-level compile failure (the workflow JSON itself is
    malformed)."""
    if trace.status != "failed":
        return None
    if not trace.error or "compile failed" not in trace.error:
        return None
    return (
        f"the workflow failed to compile: `{trace.error}`.",
        "call `get_connection_rules` and `get_graph_state` to "
        "see what's wrong with the graph. The compile error "
        "names the field / node / edge that's invalid.",
    )

@_register
def _step_level_error(trace: RunTrace):
    """Catch-all: any per-step error not matched by a more
    specific rule above."""
    errored = [s for s in trace.steps if s.status == "error"]
    if not errored:
        return None
    if trace.error and any(
        r.__name__ in {
            "_selector_undefined_variable",
            "_loop_missing_body_target",
            "_tool_source_not_wired",
            "_compile_error",
        }
        for r in _RULES[:-1]
    ):
        # A more-specific rule will produce a better diagnosis;
        # this fallback only runs if those returned None.
        pass
    first = errored[0]
    return (
        f"step `{first.node_id}` ({first.node_type}) failed: "
        f"`{first.error}`",
        "inspect the step's node config via `get_graph_state`. "
        "If the error mentions a selector or a tool, the more "
        "specific failure modes above apply.",
    )

def explain_failure(run_id: str) -> dict:
    """Run the diagnostic rules over the trace.

    Returns a JSON object with `diagnosis`, `suggested_fix`, and
    the `matched_rule` (for transparency — the LLM can mention
    it back to the user). When the run is unknown, returns a
    `{error: "..."}` dict instead of None so the LLM gets a
    structured failure shape."""
    trace = _RUNS.get(run_id)
    if trace is None:
        return {
            "error": f"unknown run_id {run_id!r}",
            "hint": "call `list_runs` to see recent run ids",
        }
    return explain_failure_inner(trace)

def explain_failure_inner(trace: RunTrace) -> dict:
    """Internal: run the diagnostic rules over an in-memory
    `RunTrace` without going through the store. Tests use this
    to exercise the rules deterministically (a synthetic trace
    doesn't need to be `record()`'d first).

    Same return shape as `explain_failure` — the public entry
    point delegates here after the store lookup.
    """
    if trace.status != "failed":
        return {
            "diagnosis": (
                f"run did not fail (status={trace.status!r}). "
                "explanation only applies to failed runs."
            ),
            "suggested_fix": "",
            "matched_rule": None,
        }
    for rule in _RULES:
        out = rule(trace)
        if out is None:
            continue
        diagnosis, suggested_fix = out
        return {
            "diagnosis": diagnosis,
            "suggested_fix": suggested_fix,
            "matched_rule": rule.__name__.lstrip("_"),
        }
    # Last-resort: we matched no rule, but the trace IS failed.
    return {
        "diagnosis": (
            f"run failed with status={trace.status!r} "
            f"and error=`{trace.error}`."
        ),
        "suggested_fix": (
            "call `inspect_run(run_id)` for the full trace and "
            "look at `steps[].error` for the per-step failure."
        ),
        "matched_rule": None,
    }

__all__ = [
    "RunTrace",
    "RunStep",
    "RunTraceStore",
    "run_workflow",
    "inspect_run",
    "list_runs",
    "explain_failure",
    "explain_failure_inner",
    "get_store",
    "set_store",
    "DEFAULT_RUN_TIMEOUT_SEC",
]