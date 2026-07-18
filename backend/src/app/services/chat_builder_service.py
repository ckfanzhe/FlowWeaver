"""ChatBuilderService — the LLM-driven workflow-creation chat.

The flow:
  1. The frontend sends `POST /api/v1/chat/builder` with
     `{workflow_id, messages}`.
  2. The service loads the current workflow JSON, builds an agno
     `Agent` with five workflow-edit tools
     (`add_node` / `update_node` / `remove_node` / `connect_nodes` /
     `disconnect` / `preview_workflow`), and runs the agent against
     the user's conversation history.
  3. The agent streams `RunOutputEvent`s. We translate them into
     `BuilderEvent`s (thinking / text / tool_call / tool_result /
     diff / completed / error) **in real time** — each event lands
     on the SSE channel as soon as the LLM emits it, so the user
     sees the tool calls land live instead of staring at a
     "thinking…" spinner for 5–10 s. Each tool call is validated
     against the **staged** workflow state (original + every
     previously accepted pending change in this session); valid
     changes are stored in `pending_changes`; invalid changes are
     returned to the agent as a tool error so it can self-correct.
  4. The diff is emitted EXACTLY ONCE per turn, after the stream
     loop ends (success path) or before the error event (failure
     path so the user can still apply the partial changes). The
     client renders a single diff card and shows Apply / Cancel.
     This is intentional: per-turn tool calls tend to bunch up
     because the LLM's tools are narrow (one node per `add_node`,
     one patch per `update_node`), so a single user instruction
     often produces 5–10 tool calls. Folding them into one final
     diff keeps the chat looking like one logical round of work
     — one apply click per turn.
  5. When the user clicks "Apply", the client sends
     `POST /api/v1/chat/builder/apply` with the session id; the
     service re-validates and writes the changes to the workflow
     row via `workflow_service.update_workflow`.

Sessions are kept in-process (`_SESSIONS` dict) for v1 — chat
sessions are short-lived (the user opens a chat, edits a few
nodes, applies or cancels). The apply endpoint invalidates the
session so the next chat starts fresh.
"""
from __future__ import annotations

import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Union

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.auth import CurrentUser

log = logging.getLogger(__name__)

from app.core.graph import validate_workflow
from app.core.llm_runner import (
    _resolve_default_preset_id,
    build_model,
)
from app.db.models import Workflow
from app.db.models import LlmPreset
from app.schemas.chat_builder import (
    BuilderCompletedEvent,
    BuilderDiffEvent,
    BuilderErrorEvent,
    BuilderEvent,
    BuilderRetryEvent,
    BuilderStartEvent,
    BuilderTextEvent,
    BuilderThinkingEvent,
    BuilderToolCallEvent,
    BuilderToolResultEvent,
)
from app.schemas.workflow import (
    WorkflowEdge,
    WorkflowNode,
    WorkflowUpdate,
)
from app.services import member_service, workflow_service
from app.core.node_types import NODE_TYPES as MANIFEST_NODE_TYPES
from app.services.chat_builder_plan import (
    Issue,
    IssueCode,
    PlanEdge,
    PlanNode,
    PlanResult,
    WorkflowPlan,
    apply_plan_to_snapshot,
    hint_for,
    validate_node_config_for_llm,
    validate_plan,
)
from app.services.chat_builder_schema import (
    get_node_types_tool,
    node_types_for_prompt,
)
from app.services.chat_builder_read import (
    get_connection_rules_tool,
    get_graph_state_tool,
    summarise_connection_rules,
)
from app.services.chat_builder_context import render_workflow_context
from app.services.chat_builder_patterns import (
    build_react_agent_plan,
    build_retry_loop_plan,
    build_router_pattern_plan,
    pattern_plan_to_dict,
)
from app.services.chat_builder_run import (
    explain_failure as _explain_failure,
    inspect_run as _inspect_run,
    list_runs as _list_runs,
    run_workflow as _run_workflow,
)

# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────
# Tunable limits — kept here for back-compat (callers + tests still
# read them as `cbs.MAX_TOOL_CALLS_PER_TURN` etc.). Single source of
# truth lives in `app.services.chat_builder_limits` (row I,
# ); new code should import from there directly.
# ─────────────────────────────────────────────────────────────────
from app.services.chat_builder_limits import (
    MAX_TOOL_CALLS_PER_TURN,
    MAX_PENDING_CHANGES_PER_SESSION,
    REJECTION_BUDGET_PER_TURN,
)  # noqa: F401 — re-exported for `cbs.MAX_*` references

# High-level tools that DON'T count against MAX_TOOL_CALLS_PER_TURN.
# Each one is a single call that internally batches many operations
# (e.g. `plan_workflow` adds N nodes + M edges in one shot), so
# charging it against the cap would be the wrong unit of accounting.
# The LLM is steered toward these in the system prompt — when it uses
# them the cap effectively doesn't exist; when it falls back to
# imperative add_node × N, the cap kicks in (the intended behaviour).
HIGH_LEVEL_TOOLS = frozenset({
    "plan_workflow",
    "replace_workflow",
    "create_react_agent",
    "create_router_pattern",
    "create_retry_loop",
    # Read-only / diagnostic tools are free too — the LLM should be
    # able to call get_node_types / get_connection_rules / preview
    # liberally without worrying about the cap.
    "get_node_types",
    "get_connection_rules",
    "get_graph_state",
    "preview_workflow",
    "inspect_run",
    "explain_failure",
    "list_runs",
    "run_workflow",
})

def _unwrap_json_decode_error(exc: BaseException) -> Optional[json.JSONDecodeError]:
    """If `exc` is (or wraps) a `json.JSONDecodeError`, return the
    underlying error; otherwise return None.

    Used in two places: `chat_turn_stream` (top-level retry — one
    retry on transient parse failures) and `_consume_stream` (mid-
    stream guard — when the SSE parser chokes on a partial chunk
    during iteration, we can't retry without replaying events the
    user already saw, so we surface a friendly error and preserve
    the partial diff instead).

    agno 2.8.7's `Claude._handle_api_error` wraps non-API exceptions
    (including `JSONDecodeError` from the Anthropic SDK's SSE
    parser) as `ModelProviderError(message=str(e)) from e`, so the
    original `JSONDecodeError` lives on `__cause__`. We unwrap it
    here so both layers see the original error and the caller's
    retry/fallback logic works uniformly.
    """
    if isinstance(exc, json.JSONDecodeError):
        return exc
    try:
        from agno.exceptions import ModelProviderError
    except ImportError:  # pragma: no cover — agno missing
        ModelProviderError = None  # type: ignore[assignment]
    if ModelProviderError is not None and isinstance(exc, ModelProviderError):
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, json.JSONDecodeError):
            return cause
    return None

# Common substrings of a `json.JSONDecodeError` repr / str. Used
# to detect SSE-parser failures that agno surfaces as
# `RunErrorEvent.content` rather than as Python exceptions (the
# path the existing try/except can't catch — see the bug from
#  where the user kept seeing "key must be a string at
# line 1 column 3984" in `last_error`).
_JSON_DECODE_NEEDLES = (
    "key must be a string at line",
    "Expecting value at line",
    "Expecting property name enclosed in double quotes at line",
    "Unterminated string at line",
    "Invalid \\escape at line",
    "Extra data at line",
    "JSONDecodeError",
)

def _is_json_decode_message(s: str) -> bool:
    """True if `s` looks like a `json.JSONDecodeError` repr.

    Cheap substring check — JSONDecodeError messages all start
    with a small fixed vocabulary (the parser-localised phrase
    above). We don't try to parse the line/col back out of every
    message; if we can't find a known needle we treat the error
    as opaque and let the caller surface it raw.
    """
    return any(needle in s for needle in _JSON_DECODE_NEEDLES)

def _extract_colno(s: str) -> Optional[int]:
    """Best-effort extraction of `column N` from a JSONDecodeError
    message. Used only for the warning log line; returns None
    when the format is unexpected.
    """
    import re
    m = re.search(r"column\s+(\d+)", s)
    return int(m.group(1)) if m else None

def _mid_stream_friendly_message() -> str:
    """The user-facing message emitted when a transient SSE-parser
    failure interrupts the LLM stream. Centralised so all three
    recovery paths (top-level retry-exhausted, mid-stream
    try/except, RunErrorEvent content) surface the same text.
    """
    return (
        "The LLM provider's stream was interrupted mid-turn "
        "(a malformed SSE chunk landed). The partial diff "
        "above can still be applied — please resend your "
        "request to continue editing."
    )

# ─────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────
@dataclass
class ChatSession:
    """In-memory state for one chat session.

    A session is created lazily on the first `chat` call and
    destroyed on Apply (or on a subsequent chat request that
    includes a `reset` flag, not yet implemented). The session
    owns the staged workflow state separately from the DB row so
    the LLM can hand back-and-forth without round-tripping.

    `original_nodes` / `original_edges` — the snapshot of the
    workflow when the session started. Used to compute the diff
    and to detect stale sessions (the user might have edited the
    workflow in another tab between starting the chat and
    clicking Apply).

    `pending_changes` — list of normalized operations, in order:
      * `{"op": "add_node", "node": {...}}`
      * `{"op": "update_node", "node_id": ..., "patch": {...}}`
      * `{"op": "remove_node", "node_id": ...}`
      * `{"op": "add_edge", "edge": {...}}`
      * `{"op": "remove_edge", "edge_id": ...}`

    `staged_nodes` / `staged_edges` — the current staged state
    (= original + applied pending changes). Each tool call
    validates against this — never against the original alone,
    so the LLM can make sequential edits in one turn.
    """
    session_id: str
    workflow_id: str
    user_id: str
    original_nodes: list[dict] = field(default_factory=list)
    original_edges: list[dict] = field(default_factory=list)
    pending_changes: list[dict[str, Any]] = field(default_factory=list)
    staged_nodes: list[dict] = field(default_factory=list)
    staged_edges: list[dict] = field(default_factory=list)
    # F6 : per-turn rejection counter. Incremented every
    # time a tool returns a rejection; reset on success and at
    # turn boundaries. Drives the budget-exhaustion hint — once the
    # budget is exceeded the rejection message tells the LLM to
    # STOP calling mutation tools and instead call a diagnostic
    # (`preview_workflow` / `get_graph_state` / `explain_failure`).
    # We deliberately do NOT cap at a hard limit — the user can
    # keep going if they want — but we surface the count so the
    # LLM can self-correct instead of looping.
    turn_rejection_count: int = 0
    # User-initiated cancel . Set by `cancel_session`
    # when the user clicks the Stop button; read by `_consume_stream`
    # between event yields so the LLM call on the server doesn't
    # keep running pointlessly after the client has cut the fetch.
    # The flag is per-session and is reset to False at the start
    # of every new turn (see `_load_or_create_session` lifecycle).
    cancel_requested: bool = False

# In-process session store. Bounded by `MAX_PENDING_CHANGES_PER_SESSION`
# per session; we never expect more than a handful of concurrent chats
# for a single user (the platform's chat is one-at-a-time per workflow).
# Kept in a module-level dict; cleared by Apply / Cancel.
_SESSIONS: dict[str, ChatSession] = {}

# ─────────────────────────────────────────────────────────────────
# Tool plumbing
# ─────────────────────────────────────────────────────────────────
class ToolCallRejected(Exception):
    """Raised by a tool handler when validation fails.

    The service catches this and surfaces a `tool_result` event
    with `ok=False` + the message. The LLM can then self-correct
    on the next turn. Distinguishing this from a programmer error
    lets the agent's loop carry on without unwinding the whole
    Agent run.

    F6  — `hint` carries a short engineering tip
    ("see get_node_types for the schema", etc.) so the LLM
    has a concrete next step instead of just an error message.
    The hint is folded into the message bubble at the chat
    surface so the LLM reads both at once. `code` is an
    optional `IssueCode`-style string the LLM can branch on
    (mirrors Plan DSL's `Issue.code` contract).
    """

    def __init__(
        self,
        tool: str,
        message: str,
        hint: str = "",
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.tool = tool
        self.message = message
        self.hint = hint
        self.code = code

    def formatted(self) -> str:
        """The LLM-facing tool result string.

        When a hint is present we append it as a separate line
        so the LLM can read the message + the next step at the
        same time. Empty hint → just the message (no extra
        blank line, no chatter).
        """
        if self.hint:
            return f"{self.message}\n\nHint: {self.hint}"
        return self.message

@dataclass
class _ToolCtx:
    """Bundled arguments passed to each tool handler."""
    session: ChatSession
    args: dict[str, Any]

# Each tool handler returns a JSON-serializable dict (the platform
# formats it onto the SSE stream as a `tool_result` event).
ToolHandler = Any  # we accept plain callables; type alias kept loose

def _check_same_workflow(session: ChatSession, workflow_id: str) -> None:
    """Defensive: every tool call must reference the workflow the
    session was opened against. Mismatches usually mean the LLM
    hallucinated a different id — reject cleanly so the LLM can
    self-correct."""
    if workflow_id != session.workflow_id:
        raise ToolCallRejected(
            "session",
            f"workflow_id mismatch: session is for {session.workflow_id!r}, "
            f"got {workflow_id!r}",
        )

# ─────────────────────────────────────────────────────────────────
# Tool handlers — validate against STAGED state, then stage
# ─────────────────────────────────────────────────────────────────
def _check_node_type(node_type: str) -> None:
    # F0.1 : the gate reads from the manifest-driven
    # registry (`app.core.node_types.NODE_TYPES`, all 15 entries incl.
    # preset tool sources). The actual membership check lives in
    # `core.validate.known_node_type`; we wrap the `ValueError` into
    # `ToolCallRejected` so the LLM still gets the structured
    # rejection envelope it relied on. The registry now contains
    # 6 base types (the 5 preset tool types collapsed into
    # `tool`+`preset`); the legacy preset names still resolve via
    # `_LEGACY_TOOL_PRESETS` so `attach_tool(tool_type='wikipedia', ...)`
    # keeps working — the translation to `type='tool' +
    # config.preset='wikipedia'` happens in `_attach_tool` itself.
    from app.core.validate import known_node_type
    try:
        known_node_type(node_type)
    except ValueError as exc:
        raise ToolCallRejected("add_node", str(exc)) from exc

# The 5 legacy preset tool type names (wikipedia / tavily_search /
# duckduckgo / calculator / arxiv_search) accepted by `attach_tool`
# for back-compat. They're translated into the unified `tool` type
# with the corresponding `preset` discriminator before the manifest
# gate runs. The full set lives in `_compat.LEGACY_NODE_ALIASES`;
# this subset is the one we surface to the LLM via `attach_tool`'s
# public contract.
_LEGACY_TOOL_PRESETS: frozenset[str] = frozenset({
    "wikipedia", "tavily_search", "duckduckgo", "calculator", "arxiv_search",
})

def _resolve_tool_type_to_node(tool_type: str, tool_config: dict | None) -> tuple[str, dict]:
    """Legacy preset tool type names (`wikipedia` / `tavily_search` /
    `duckduckgo` / `calculator` / `arxiv_search`) are translated into
    the unified `tool` type with the corresponding `preset` config
    discriminator. Returns `(node_type, merged_config)`. Non-preset
    tool types pass through unchanged with the original config."""
    cfg = dict(tool_config or {})
    if tool_type in _LEGACY_TOOL_PRESETS:
        cfg.setdefault("preset", tool_type)
        return "tool", cfg
    return tool_type, cfg

def _find_staged_node(session: ChatSession, node_id: str) -> dict:
    for n in session.staged_nodes:
        if n["id"] == node_id:
            return n
    raise ToolCallRejected(
        "update_node",
        f"node {node_id!r} does not exist in the current workflow",
    )

def _find_staged_edge(session: ChatSession, edge_id: str) -> dict:
    for e in session.staged_edges:
        if e["id"] == edge_id:
            return e
    raise ToolCallRejected(
        "disconnect",
        f"edge {edge_id!r} does not exist in the current workflow",
    )

def _normalize_position(position: Any) -> dict[str, float]:
    if not isinstance(position, dict):
        raise ToolCallRejected(
            "add_node",
            f"position must be an object like {{'x': 0, 'y': 0}}, got {position!r}",
        )
    try:
        x = float(position.get("x", 0))
        y = float(position.get("y", 0))
    except (TypeError, ValueError):
        raise ToolCallRejected(
            "add_node",
            f"position.x and position.y must be numbers, got {position!r}",
        )
    return {"x": x, "y": y}

def _validate_graph(nodes: list[dict], edges: list[dict]) -> None:
    """Run the same validation the import endpoint runs, on the
    given node/edge lists (NOT necessarily `session.staged_*` — this
    is also called on a temp snapshot in `_atomic_stage`).

    Pydantic per-node validation runs through `WorkflowNode`'s
    `_validate_config` model validator (per-type schema check +
    config coercion), then `validate_workflow` for the graph-
    level rules (known types, no orphans, no cycles, connection
    rules).
    """
    try:
        for n in nodes:
            WorkflowNode.model_validate(n)
        for e in edges:
            WorkflowEdge.model_validate(e)
        validate_workflow(nodes, edges)
    except Exception as exc:
        raise ToolCallRejected("graph", f"validation failed: {exc}") from exc

def _validate_staged_graph(session: ChatSession) -> None:
    """Convenience wrapper — validate the session's current staged
    state. Use this ONLY for read-only checks; mutating tools should
    use `_atomic_stage` so validation runs against a temp snapshot,
    not the committed staged state (see F0.2)."""
    _validate_graph(session.staged_nodes, session.staged_edges)

def _atomic_stage(
    session: ChatSession,
    change: dict[str, Any],
    mutate,
) -> None:
    """F0.2  — copy-on-write staging.

    Apply `mutate(target_nodes, target_edges)` to a SNAPSHOT of
    `session.staged_nodes` / `staged_edges`, validate, then commit on
    success or raise on failure.

    Contract: on validation failure, `session.staged_*` is
    UNTOUCHED and no `pending_change` is appended. The previous
    behaviour (mutate-then-validate-then-raise) corrupted the
    session: a rejected tool call left invalid staged state behind,
    after which every subsequent Apply failed with HTTP 422
    "workflow state changed incompatibly while chatting" — even
    though the user hadn't done anything wrong, just the LLM one
    turn earlier.

    Callers raise precondition errors (duplicate id, invalid type,
    malformed config) BEFORE calling `_atomic_stage` so the snapshot
    machinery only sees graph-internal validation. Anything raised
    inside `mutate` (rare — most preconditions are pre-checked) is
    also propagated without committing.

    `mutate` is a callable `(target_nodes, target_edges) -> None`.
    It mutates the lists in place — they are fresh copies of the
    session's staged state.
    """
    target_nodes = list(session.staged_nodes)
    target_edges = list(session.staged_edges)
    mutate(target_nodes, target_edges)
    # Run the full validator on the snapshot. Anything raised here
    # propagates without committing.
    _validate_graph(target_nodes, target_edges)
    # Cap check on the post-commit count — `pending_changes` only
    # grows on success, so we check the count BEFORE appending.
    if len(session.pending_changes) >= MAX_PENDING_CHANGES_PER_SESSION:
        raise ToolCallRejected(
            "session",
            f"session cap reached ({MAX_PENDING_CHANGES_PER_SESSION} "
            "pending changes); please apply or cancel before continuing",
        )
    # Commit. From here on, no validation can fail.
    session.staged_nodes = target_nodes
    session.staged_edges = target_edges
    session.pending_changes.append(change)

def _stage_change(session: ChatSession, change: dict[str, Any]) -> None:
    """Backwards-compat alias for direct append — only used by
    callers that already validated before appending (none today;
    see `_atomic_stage`). Kept for tests that inspect the
    `pending_changes` shape directly. New code MUST use
    `_atomic_stage`."""
    session.pending_changes.append(change)

def _diff_summary(session: ChatSession) -> dict[str, int]:
    """Compute the small summary form: {added, removed, updated}."""
    original_by_id = {n["id"]: n for n in session.original_nodes}
    staged_by_id = {n["id"]: n for n in session.staged_nodes}
    added_nodes = len([n for n in session.staged_nodes if n["id"] not in original_by_id])
    removed_nodes = len([n for n in session.original_nodes if n["id"] not in staged_by_id])
    updated_nodes = 0
    for nid, new_n in staged_by_id.items():
        if nid in original_by_id and new_n != original_by_id[nid]:
            updated_nodes += 1
    original_e_by_id = {e["id"]: e for e in session.original_edges}
    staged_e_by_id = {e["id"]: e for e in session.staged_edges}
    added_edges = len([e for e in session.staged_edges if e["id"] not in original_e_by_id])
    removed_edges = len([e for e in session.original_edges if e["id"] not in staged_e_by_id])
    return {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "updated_nodes": updated_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
    }

def _diff_full(session: ChatSession) -> tuple[list[dict], list[dict]]:
    """Compute the full diff for the expandable detail: a list of
    node change dicts and edge change dicts with `op` (`added` /
    `removed` / `updated`)."""
    original_by_id = {n["id"]: n for n in session.original_nodes}
    staged_by_id = {n["id"]: n for n in session.staged_nodes}
    nodes: list[dict] = []
    for nid, new_n in staged_by_id.items():
        if nid not in original_by_id:
            nodes.append({"op": "added", "node": new_n})
        elif new_n != original_by_id[nid]:
            nodes.append({"op": "updated", "before": original_by_id[nid], "after": new_n})
    for nid, old_n in original_by_id.items():
        if nid not in staged_by_id:
            nodes.append({"op": "removed", "node": old_n})

    original_e_by_id = {e["id"]: e for e in session.original_edges}
    staged_e_by_id = {e["id"]: e for e in session.staged_edges}
    edges: list[dict] = []
    for eid, new_e in staged_e_by_id.items():
        if eid not in original_e_by_id:
            edges.append({"op": "added", "edge": new_e})
        elif new_e != original_e_by_id[eid]:
            edges.append({"op": "updated", "before": original_e_by_id[eid], "after": new_e})
    for eid, old_e in original_e_by_id.items():
        if eid not in staged_e_by_id:
            edges.append({"op": "removed", "edge": old_e})
    return nodes, edges

# ─────────────────────────────────────────────────────────────────
# Tool handlers — pure functions that mutate the staged state.
# Each one returns a JSON string (the tool's return value, which
# the agent loop forwards to the LLM as the tool result).
# ─────────────────────────────────────────────────────────────────
def _add_node(session: ChatSession, workflow_id: str, type: str, id: str = "",
              position: Optional[dict] = None, label: str = "",
              config: Optional[dict] = None) -> str:
    """Add a new node to the workflow.

    Args:
        workflow_id: The workflow to edit (must match the session).
        type: Node type — see the manifest for the full list (agent,
              tool, branch, flow, loop, ask). :
              preset tool nodes (wikipedia / tavily_search / duckduckgo
              / calculator / arxiv_search) are NOT separate types — set
              `type='tool'` + `config.preset='<name>'` instead.
        id: Optional node id. If empty, the platform generates one. Must be unique.
        position: {'x': <number>, 'y': <number>} on the canvas.
        label: Display name shown on the canvas. Defaults to the id.
        config: Node-type-specific config object. The platform validates it against the type's schema.

    Returns:
        A JSON object: {"added": "<id>", "type": "<type>", "config": <as_stored>}
        The `config` field is the post-Pydantic-coercion view of
        `data.config` so the LLM can see what survived (see F0.5).
    """
    _check_same_workflow(session, workflow_id)
    _check_node_type(type)
    node_id = id or f"node-{uuid.uuid4().hex[:8]}"
    if any(n["id"] == node_id for n in session.staged_nodes):
        raise ToolCallRejected(
            "add_node",
            f"node id {node_id!r} already exists in this workflow",
        )
    pos = _normalize_position(position or {})
    cfg = config or {}
    if not isinstance(cfg, dict):
        raise ToolCallRejected(
            "add_node",
            f"config must be an object, got {type(cfg).__name__}",
        )
    node_label = label or node_id
    # Strict write-time pre-check. The lax
    # `WorkflowNode.model_validate` below ignores unknown fields;
    # the strict sibling rejects them so the LLM gets a typed
    # `INVALID_CONFIG` Issue with a "did you mean" hint instead of
    # a silently-empty staged node. Read-time loading (workflow
    # import + canvas save) still uses the lax path, so this is
    # `add_node`-only.
    strict_issues = validate_node_config_for_llm(type, cfg)
    if strict_issues:
        raise ToolCallRejected(
            "add_node",
            json.dumps(
                {
                    "ok": False,
                    "issues": [i.to_dict() for i in strict_issues],
                },
                ensure_ascii=False,
            ),
        )
    # Build the node through the same Pydantic path the API uses
    # so config coercion runs (= per-type schema validation +
    # default fill-in). This is a PRECONDITION check — fails before
    # we touch staged state, so no rollback needed.
    forced = {"label": node_label, "config": cfg}
    try:
        node_obj = WorkflowNode.model_validate(
            {"id": node_id, "type": type, "position": pos, "data": forced}
        )
    except Exception as exc:
        raise ToolCallRejected("add_node", f"invalid node config: {exc}") from exc
    node_dict = node_obj.model_dump()
    # F0.2: copy-on-write. The lambda runs against a SNAPSHOT —
    # if validation fails, session.staged_nodes is untouched.
    _atomic_stage(
        session,
        change={"op": "add_node", "node": node_dict},
        mutate=lambda nodes, _edges: nodes.append(node_dict),
    )
    # F0.5 : echo the stored `data.config` back so the
    # LLM can see what survived Pydantic coercion + `extra="ignore"`.
    # Without this, an LLM that wrote a deprecated field (e.g.
    # `router.condition` instead of `router.selector`) would see
    # `{"added": id}` and assume success — the runtime would then
    # receive a no-op router.
    return json.dumps(
        {
            "added": node_id,
            "type": type,
            "config": node_dict.get("data", {}).get("config", {}),
        },
        ensure_ascii=False,
    )

def _update_node(session: ChatSession, workflow_id: str, node_id: str,
                 patch: Optional[dict] = None) -> str:
    """Update an existing node's label, config, or position.

    Args:
        workflow_id: The workflow to edit (must match the session).
        node_id: The id of the node to update.
        patch: Object with any of: label (str), config (object), position ({x, y}). Missing keys are left alone.

    Returns:
        A JSON object: {"updated": "<node_id>", "config": <as_stored>}
        The `config` field is the post-Pydantic-coercion view of
        `data.config` so the LLM can see what survived (see F0.5).
    """
    _check_same_workflow(session, workflow_id)
    if not node_id:
        raise ToolCallRejected("update_node", "node_id is required")
    existing = _find_staged_node(session, node_id)
    patch = patch or {}
    if not isinstance(patch, dict):
        raise ToolCallRejected(
            "update_node",
            f"patch must be an object, got {type(patch).__name__}",
        )
    new_data = dict(existing.get("data") or {})
    if "label" in patch:
        new_data["label"] = patch["label"]
    if "config" in patch:
        if not isinstance(patch["config"], dict):
            raise ToolCallRejected(
                "update_node",
                f"patch.config must be an object, got {type(patch['config']).__name__}",
            )
        new_data["config"] = patch["config"]
    new_position = dict(existing.get("position") or {"x": 0.0, "y": 0.0})
    if "position" in patch:
        new_position = _normalize_position(patch["position"])
    new_node = {
        "id": existing["id"],
        "type": existing["type"],
        "position": new_position,
        "data": new_data,
    }
    # Strict write-time pre-check on the merged config. The
    # patch only carries `config` when the LLM asked to change it,
    # so we run the strict check on the would-be `new_data.config`.
    # If the existing config has drifted fields (legacy workflow
    # state), this still passes because we only re-check the part
    # the LLM is actively writing — the merged result gets re-validated
    # by the lax `WorkflowNode.model_validate` below as defence in depth.
    merged_cfg = new_data.get("config")
    if isinstance(merged_cfg, dict):
        strict_issues = validate_node_config_for_llm(
            existing.get("type", ""), merged_cfg
        )
        if strict_issues:
            raise ToolCallRejected(
                "update_node",
                json.dumps(
                    {
                        "ok": False,
                        "issues": [i.to_dict() for i in strict_issues],
                    },
                    ensure_ascii=False,
                ),
            )
    try:
        node_obj = WorkflowNode.model_validate(new_node)
    except Exception as exc:
        raise ToolCallRejected("update_node", f"invalid updated node: {exc}") from exc
    new_node = node_obj.model_dump()
    # F0.2: copy-on-write. The lambda replaces the matching node in
    # the snapshot; if the resulting graph fails validation,
    # session.staged_nodes is unchanged.
    def _replace(nodes, _edges):
        for i, n in enumerate(nodes):
            if n["id"] == node_id:
                nodes[i] = new_node
                break
    _atomic_stage(
        session,
        change={
            "op": "update_node",
            "node_id": node_id,
            "before": existing,
            "after": new_node,
        },
        mutate=_replace,
    )
    # F0.5 : see the add_node counterpart — echo the
    # stored config so the LLM learns what the validator accepted.
    return json.dumps(
        {
            "updated": node_id,
            "config": new_node.get("data", {}).get("config", {}),
        },
        ensure_ascii=False,
    )

def _remove_node(session: ChatSession, workflow_id: str, node_id: str) -> str:
    """Remove a node from the workflow. Any edges touching the node are also removed.

    Args:
        workflow_id: The workflow to edit (must match the session).
        node_id: The id of the node to remove.

    Returns:
        A JSON object: {"removed": "<node_id>", "cascaded_edges": ["<id>", ...]}
    """
    _check_same_workflow(session, workflow_id)
    if not node_id:
        raise ToolCallRejected("remove_node", "node_id is required")
    _find_staged_node(session, node_id)
    # F0.2: build the cascaded-edge list against the SNAPSHOT, not
    # the live staged state. We compute removed_edge_ids in the
    # mutate callback below so the snapshot is the single source of
    # truth — a graph-rule violation later rolls back both the node
    # and the cascaded edges.
    def _remove(nodes, edges):
        # Compute the cascaded-edge ids BEFORE mutating either list,
        # because the change record we emit later needs them. They're
        # determined by node_id alone (which is closed over from
        # the outer scope) so the order of edge/node removal is moot.
        nonlocal removed_edge_ids
        removed_edge_ids = [
            e["id"] for e in edges
            if e.get("source") == node_id or e.get("target") == node_id
        ]
        edges[:] = [e for e in edges if e["id"] not in removed_edge_ids]
        nodes[:] = [n for n in nodes if n["id"] != node_id]
    # `nonlocal` requires an existing binding; declare up front.
    removed_edge_ids: list[str] = []
    _atomic_stage(
        session,
        change={
            "op": "remove_node",
            "node_id": node_id,
            "removed_edge_ids": removed_edge_ids,
        },
        mutate=_remove,
    )
    return json.dumps(
        {"removed": node_id, "cascaded_edges": removed_edge_ids},
        ensure_ascii=False,
    )

def _connect_nodes(session: ChatSession, workflow_id: str, source: str,
                   target: str, kind: str = "dataflow",
                   source_handle: Optional[str] = None) -> str:
    """Connect two nodes with an edge.

    Args:
        workflow_id: The workflow to edit (must match the session).
        source: The source node's id.
        target: The target node's id.
        kind: "dataflow" (default) for control flow, or "tool_attachment" to wire a tool source to an agent.
        source_handle: branch label on a router node (e.g. `"confirm"`,
            `"cancel"`). `None` for the router's default branch. Mirrors
            `plan_workflow`'s edge model so the LLM can express router
            branches via either tool without losing the label.

    Returns:
        A JSON object: {"added": "<edge_id>", "from": "<source>", "to": "<target>", "source_handle": "<label or null>"}
    """
    _check_same_workflow(session, workflow_id)
    if not source or not target:
        raise ToolCallRejected("connect_nodes", "source and target are required")
    # `connect_nodes` previously rejected `sourceHandle` with a
    # Pydantic validation error (`Unexpected keyword argument`),
    # forcing the LLM to fall back to `plan_workflow` for every
    # edge that needed a non-default handle. The plan/replace edge
    # model already persists the handle and `core.graph` /
    # `schemas.workflow` carry the field end-to-end, so accepting
    # it here keeps the two tools consistent. The render path
    # matches because we write the label into the edge dict
    # verbatim — same shape `plan_workflow` produces.
    _find_staged_node(session, source)
    _find_staged_node(session, target)
    if kind not in ("dataflow", "tool_attachment"):
        raise ToolCallRejected(
            "connect_nodes",
            f"kind must be 'dataflow' or 'tool_attachment', got {kind!r}",
        )
    edge_id = f"edge-{uuid.uuid4().hex[:8]}"
    edge_dict = {
        "id": edge_id,
        "source": source,
        "target": target,
        "sourceHandle": source_handle,
        "targetHandle": None,
        "kind": kind,
    }
    # F0.2: copy-on-write. Dedup check + append run against the
    # snapshot; if validation rejects (e.g. duplicate edge,
    # incompatible source/target per the rule table), the snapshot
    # is discarded and session.staged_edges stays exactly as it was.
    def _add_edge(_nodes, edges):
        for e in edges:
            # Two edges are the same iff they share source, target,
            # AND source_handle (different handles = different router
            # branches on the same source/target pair — legitimately
            # distinct edges). Match plan_workflow's dedup rule
            # (`chat_builder_plan._validate_plan`).
            if (
                e["source"] == source
                and e["target"] == target
                and e.get("sourceHandle") == source_handle
                and e.get("targetHandle") is None
            ):
                raise ToolCallRejected(
                    "connect_nodes",
                    f"edge {source} -> {target} (handle={source_handle!r}) already exists",
                )
        edges.append(edge_dict)
    _atomic_stage(
        session,
        change={"op": "add_edge", "edge": edge_dict},
        mutate=_add_edge,
    )
    return json.dumps(
        {"added": edge_id, "from": source, "to": target,
         "source_handle": source_handle},
        ensure_ascii=False,
    )

def _disconnect(session: ChatSession, workflow_id: str, edge_id: str) -> str:
    """Remove an edge from the workflow.

    Args:
        workflow_id: The workflow to edit (must match the session).
        edge_id: The id of the edge to remove.

    Returns:
        A JSON object: {"removed": "<edge_id>"}
    """
    _check_same_workflow(session, workflow_id)
    if not edge_id:
        raise ToolCallRejected("disconnect", "edge_id is required")
    _find_staged_edge(session, edge_id)
    # F0.2: copy-on-write.
    _atomic_stage(
        session,
        change={"op": "remove_edge", "edge_id": edge_id},
        mutate=lambda _nodes, edges: edges.__setitem__(
            slice(None, None),
            [e for e in edges if e["id"] != edge_id],
        ),
    )
    return json.dumps({"removed": edge_id}, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────
# F7  — `attach_tool` / `detach_tool` imperative tools.
#
# `create_react_agent(tools=[...])` only attaches the listed tools
# to THAT agent. There's no imperative path to (a) add a second tool
# to an existing agent or (b) wire a tool to a *different* downstream
# agent — until now. These helpers close that gap without renaming
# `create_react_agent` (3 existing tests pin the name; the LLM has
# already learned it).
#
# Both tools route through `_plan_workflow` so they inherit the same
# atomicity + structured-Issue guarantees as the rest of the
# imperative surface. `attach_tool` builds a minimal
# {nodes:[<tool>], edges:[<tool_attachment>]} plan; `detach_tool`
# uses {delete_edges:[edge_id]}.
# ─────────────────────────────────────────────────────────────────
def _attach_tool(
    session: ChatSession,
    workflow_id: str,
    *,
    agent_id: str,
    tool_type: str,
    tool_id: str = "",
    tool_config: Any = None,
    tool_label: str = "",
) -> str:
    """Build a 1-node + 1-edge plan and route it through
    `_plan_workflow`. Pre-validates that `agent_id` exists, is type
    'agent', and that `tool_type` is in the manifest."""
    _check_same_workflow(session, workflow_id)
    if not agent_id:
        raise ToolCallRejected("attach_tool", "agent_id is required")
    if not tool_type:
        raise ToolCallRejected("attach_tool", "tool_type is required")
    # Validate the target agent exists AND is an 'agent' node. The
    # `create_react_agent` helper attaches tools to the new agent
    # only; attach_tool must enforce the same constraint explicitly.
    target = _find_staged_node(session, agent_id)
    if target.get("type") != "agent":
        raise ToolCallRejected(
            "attach_tool",
            f"target node {agent_id!r} must be of type 'agent', "
            f"got {target.get('type')!r}. attach_tool only wires "
            f"tools to agent nodes — use plan_workflow directly for "
            f"other shapes.",
        )
    # Translate legacy preset tool type names (`wikipedia` /
    # `tavily_search` / `duckduckgo` / `calculator` / `arxiv_search`)
    # into the unified `tool` type with the corresponding `preset`
    # config discriminator. Non-preset tool types (`http` / `mcp` /
    # `function`) pass through unchanged.
    resolved_type, resolved_cfg = _resolve_tool_type_to_node(
        tool_type, tool_config,
    )
    # Validate the resolved type against the manifest (same gate
    # as add_node). Legacy preset names now resolve to `tool`,
    # which is in the manifest — the translation happens BEFORE
    # the gate runs so callers can keep using the legacy names.
    _check_node_type(resolved_type)
    cfg = resolved_cfg
    if not isinstance(cfg, dict):
        raise ToolCallRejected(
            "attach_tool",
            f"tool_config must be an object, got {type(cfg).__name__}",
        )
    # Build the minimal plan: 1 tool node + 1 tool_attachment edge.
    plan = {
        "nodes": [
            {
                "id": tool_id or f"{resolved_type}-{uuid.uuid4().hex[:8]}",
                "type": resolved_type,
                "data": {
                    "label": tool_label or tool_type,
                    "config": cfg,
                },
            }
        ],
        "edges": [
            {
                "source": tool_id or "__RESOLVED__",  # placeholder; replaced below
                "target": agent_id,
                "kind": "tool_attachment",
            }
        ],
    }
    # Resolve the placeholder source to the actual generated id.
    new_tool_id = plan["nodes"][0]["id"]
    plan["edges"][0]["source"] = new_tool_id
    return _plan_workflow(session, workflow_id, plan=plan)

def _detach_tool(
    session: ChatSession,
    workflow_id: str,
    *,
    edge_id: str,
) -> str:
    """Remove a single tool_attachment edge by id. Source and target
    nodes remain; only the edge is gone. Routes through
    `_plan_workflow` so atomicity + Issue errors are inherited."""
    _check_same_workflow(session, workflow_id)
    if not edge_id:
        raise ToolCallRejected("detach_tool", "edge_id is required")
    # Pre-flight: confirm the edge exists AND is a tool_attachment.
    # Refusing to detach a dataflow edge prevents accidental
    # detachment of control flow.
    edge = _find_staged_edge(session, edge_id)
    if edge.get("kind") != "tool_attachment":
        raise ToolCallRejected(
            "detach_tool",
            f"edge {edge_id!r} is not a tool_attachment edge "
            f"(kind={edge.get('kind')!r}). Use `disconnect` to remove "
            f"dataflow edges.",
        )
    return _plan_workflow(
        session, workflow_id,
        plan={"delete_edges": [edge_id]},
    )

# ─────────────────────────────────────────────────────────────────
# F1  — Plan DSL handlers
#
# `plan_workflow` and `replace_workflow` are the declarative
# successors to the imperative tools above. The LLM describes the
# TARGET STATE in one call; the backend applies + validates
# atomically and returns every issue at once (instead of failing
# on the first one and forcing the LLM into a long retry loop).
#
# Both handlers follow the same copy-on-write discipline as the
# imperative tools: validate against a SNAPSHOT, commit on
# success, raise on failure with `staged_*` UNTOUCHED.
# ─────────────────────────────────────────────────────────────────
# Shape validation . Pydantic's default `WorkflowPlan`
# validation surfaces errors as opaque blobs like
# "1 validation error for WorkflowPlan / nodes.2.type / Field required".
# The LLM gets nothing actionable — it has to guess which field on
# which node is wrong, and often re-queries `get_node_types` instead
# of fixing the actual payload. This helper turns each Pydantic
# error into a structured `Issue` with:
#   - precise `path` (the same JSONPath the LLM would write),
#   - a `message` that names the node's index + (if available) its
#     `data.label` so the LLM can find it in its own plan,
#   - a `hint` that points at the exact missing/wrong field.
def _plan_shape_issues(plan: dict, exc: Exception) -> list[Issue]:
    """Translate a Pydantic ValidationError from `WorkflowPlan.model_validate`
    into a list of per-error Issues.

    Adds a structural pre-check that catches missing-required-field
    problems (e.g. node with no `type`) with field-specific hints,
    BEFORE relying on Pydantic's bulk error list — Pydantic's
    message format is opaque ("Field required") and gives the LLM
    no actionable next step without re-querying the schema.
    """
    issues: list[Issue] = []

    # 1. Structural pre-check — walk `plan["nodes"]` and emit a
    #    targeted Issue for each top-level field that is missing
    #    or malformed. Pydantic also catches these, but the
    #    pre-check gives us a chance to write a node-specific
    #    message + hint that names the field AND the index AND
    #    the node's `data.label` (when present).
    nodes = plan.get("nodes") or []
    if not isinstance(nodes, list):
        issues.append(Issue(
            path="nodes",
            code=IssueCode.INVALID_CONFIG,
            message=f"`nodes` must be a list, got {type(nodes).__name__}",
            hint="plan shape: {nodes: [{id, type, position, data: {label, config}}], edges: [...]}",
        ))
        return issues
    for idx, n in enumerate(nodes):
        if not isinstance(n, dict):
            issues.append(Issue(
                path=f"nodes[{idx}]",
                code=IssueCode.INVALID_CONFIG,
                message=f"nodes[{idx}] must be a dict, got {type(n).__name__}",
                hint="Each plan node is a dict: {id, type, position, data: {label, config}}",
            ))
            continue
        label_hint = (n.get("data") or {}).get("label") or n.get("label") or "?"
        # `type` is the only REQUIRED top-level field on PlanNode.
        # Catch it explicitly so the hint names the field instead of
        # Pydantic's generic "Field required" message.
        if "type" not in n or n.get("type") in (None, ""):
            issues.append(Issue(
                path=f"nodes[{idx}].type",
                code=IssueCode.MISSING_REQUIRED_FIELD,
                message=(
                    f"nodes[{idx}] (label={label_hint!r}) is missing the "
                    f"required `type` field"
                ),
                hint=(
                    "Add `type` to the node — one of: agent / branch / "
                    "flow / loop / ask / tool. Call get_node_types for "
                    "the full per-type config schema."
                ),
            ))
        # `id` is OPTIONAL on PlanNode (the backend auto-generates
        # `node-<uuid>` if absent) — BUT edges reference node ids,
        # so an auto-generated id can't be reached from any edge.
        # Surface this so the LLM doesn't silently lose wiring.
        if "id" not in n or not n.get("id"):
            issues.append(Issue(
                path=f"nodes[{idx}].id",
                code=IssueCode.MISSING_REQUIRED_FIELD,
                message=(
                    f"nodes[{idx}] (label={label_hint!r}) is missing the "
                    f"`id` field — without it, edges cannot reference this node"
                ),
                hint=(
                    "Add a stable `id` (e.g. 'agent_main', 'tool_query'). "
                    "Edges use {source: <id>, target: <id>}; both must "
                    "match an existing node id."
                ),
            ))
        # `position` defaults to {x: 0, y: 0} — only flag if the LLM
        # explicitly set it wrong (non-dict, NaN, etc.). Most nodes
        # should omit it and let the canvas pick a spot.

    # 2. Pass through Pydantic's structural errors for fields we
    #    DIDN'T pre-check (config shape, edge shape, etc.). Pydantic
    #    returns the actual `loc` tuple per error — translate to a
    #    JSONPath the LLM can write back, and surface msg + type.
    from pydantic import ValidationError as _PydValidationError
    if isinstance(exc, _PydValidationError):
        for err in exc.errors():
            err_loc = err.get("loc") or ()
            err_type = err.get("type", "")
            err_msg = err.get("msg", "")
            # Path: tuple → dot-separated. For top-level PlanNode
            # fields (loc starts with 'nodes'), preserve the index;
            # for nested config errors, prepend nothing — the LLM
            # already knows the per-type shape.
            path = ".".join(str(p) for p in err_loc) or "(plan)"
            # Specialise the message for the common "missing
            # required" case so the LLM gets a hint it can act on.
            hint = ""
            if err_type == "missing":
                missing_field = err_loc[-1] if err_loc else "?"
                hint = (
                    f"Add the missing `{missing_field}` field to {path}. "
                    f"Call get_node_types for the full per-type config shape."
                )
            elif err_type == "extra_forbidden":
                bad = err_loc[-1] if err_loc else "?"
                hint = (
                    f"Remove the unknown `{bad}` field from {path} — "
                    f"check the field name + nesting against get_node_types."
                )
            else:
                hint = (
                    f"Check the value at {path} — see get_node_types "
                    f"for the expected shape."
                )
            # Friendly message — include the path the LLM wrote
            # AND the field name Pydantic flagged.
            issues.append(Issue(
                path=path,
                code=IssueCode.INVALID_CONFIG,
                message=(
                    f"{path}: {err_msg} "
                    f"(type={err_type!r})"
                )[:300],
                hint=hint,
            ))

    # 3. Edge shape — same treatment (PlanEdge requires source +
    #    target). Pre-check so we can name the field.
    edges = plan.get("edges") or []
    if not isinstance(edges, list):
        issues.append(Issue(
            path="edges",
            code=IssueCode.INVALID_CONFIG,
            message=f"`edges` must be a list, got {type(edges).__name__}",
            hint="plan shape: edges: [{id?, source, target, kind?: dataflow|tool_attachment}]",
        ))
    else:
        for idx, e in enumerate(edges):
            if not isinstance(e, dict):
                issues.append(Issue(
                    path=f"edges[{idx}]",
                    code=IssueCode.INVALID_CONFIG,
                    message=f"edges[{idx}] must be a dict",
                    hint="Each edge is a dict: {source: <node_id>, target: <node_id>, kind?: 'dataflow'|'tool_attachment'}",
                ))
                continue
            for required_field in ("source", "target"):
                if required_field not in e or not e.get(required_field):
                    issues.append(Issue(
                        path=f"edges[{idx}].{required_field}",
                        code=IssueCode.MISSING_REQUIRED_FIELD,
                        message=(
                            f"edges[{idx}] is missing the "
                            f"required `{required_field}` field"
                        ),
                        hint=(
                            f"Add `{required_field}: <node_id>` — the id "
                            f"must match an existing node's `id` field."
                        ),
                    ))

    if not issues:
        # Fallback — should not happen if Pydantic raised, but if
        # something else fired (e.g. JSON decode) we still want the
        # LLM to know SOMETHING was wrong.
        issues.append(Issue(
            path="(plan)",
            code=IssueCode.INVALID_CONFIG,
            message=f"plan shape invalid: {str(exc)[:300]}",
            hint="See get_node_types for the node shape; "
                 "edges use {source, target, kind?}",
        ))
    return issues

def _plan_workflow(session: ChatSession, workflow_id: str, plan: dict) -> str:
    """Apply a `WorkflowPlan` (add/update/delete in one call).

    The plan is validated as a WHOLE before any mutation. On
    failure the staged state is untouched and `issues` lists every
    validation error (not just the first). On success the staged
    state is the result of applying the plan to the previous
    staged state.

    Args:
        workflow_id: The workflow to edit (must match the session).
        plan: A `WorkflowPlan` dict with keys `nodes`, `edges`,
              `delete_nodes`, `delete_edges` — all optional.

    Returns:
        A JSON object:
            ok=true  → {applied: {added_nodes, ...},
                        config_echo: {node_id: {post_coercion_config}}}
            ok=false → {issues: [{path, code, message, hint}, ...],
                        state_unchanged: true}
    """
    _check_same_workflow(session, workflow_id)
    # Tolerate JSON-encoded strings for `plan` — some LLM providers
    # serialize complex args this way (e.g. Anthropic tool-use
    # sometimes leaks the JSON-string shape when the LLM copies a
    # template verbatim). Falls through with a clear ToolCallRejected
    # if the string isn't valid JSON for an object.
    if not isinstance(plan, dict):
        try:
            plan = _coerce_dict_arg(plan)
        except ToolCallRejected:
            raise ToolCallRejected(
                "plan_workflow",
                f"plan must be an object, got {type(plan).__name__}",
            )
    # Parse the plan via Pydantic first — this catches schema
    # errors (unknown type, wrong field shape) BEFORE we run any
    # other validator. The errors come out as Pydantic v2
    # ValidationError; we surface them as a structured list of
    # Issues so the LLM can fix each one on the next call without
    # re-querying the schema.
    try:
        parsed = WorkflowPlan.model_validate(plan)
    except Exception as exc:
        issues = _plan_shape_issues(plan, exc)
        session.turn_rejection_count += 1
        budget_msg = _budget_exhausted_message(session)
        return json.dumps(
            PlanResult(
                ok=False, issues=issues, next_step=budget_msg,
            ).to_dict(),
            ensure_ascii=False,
        )

    # Apply on a SNAPSHOT. `_atomic_stage`'s contract: clone
    # staged_* → mutate the clones → validate the clones → commit
    # on success. We implement that directly here so the plan
    # validator's issue list is preserved verbatim (the generic
    # `_atomic_stage` only knows how to translate GraphError to a
    # flat message, which would lose the per-issue path / code).
    target_nodes = list(session.staged_nodes)
    target_edges = list(session.staged_edges)
    new_nodes, new_edges = apply_plan_to_snapshot(
        target_nodes, target_edges, parsed,
    )
    issues = validate_plan(new_nodes, new_edges)
    if issues:
        # Roll back: target_nodes / target_edges are local lists,
        # so session.staged_* is automatically untouched. We still
        # need to record the failure so the LLM's transcript shows
        # the structured errors instead of a generic rejection.
        session.turn_rejection_count += 1
        result = PlanResult(
            ok=False,
            issues=issues,
            state_unchanged=True,
            next_step=_budget_exhausted_message(session),
        )
        return json.dumps(result.to_dict(), ensure_ascii=False)

    # Pending-changes cap. The plan DSL subsumes multiple
    # imperative ops into one — count the implied operations
    # against the cap so a single chat turn can't blow past it.
    implied_ops = (
        len(parsed.delete_nodes)
        + len(parsed.delete_edges)
        + len(parsed.nodes)
        + len(parsed.edges)
    )
    if (
        len(session.pending_changes) + implied_ops
        > MAX_PENDING_CHANGES_PER_SESSION
    ):
        session.turn_rejection_count += 1
        result = PlanResult(
            ok=False,
            issues=[Issue(
                path="(plan)",
                code=IssueCode.PLAN_ATOMIC_REJECTED,
                message=(
                    f"plan would exceed session cap "
                    f"({MAX_PENDING_CHANGES_PER_SESSION} pending changes); "
                    f"please apply or cancel before continuing"
                ),
                hint=hint_for(
                    IssueCode.PLAN_ATOMIC_REJECTED,
                    cap=MAX_PENDING_CHANGES_PER_SESSION,
                ),
            )],
            state_unchanged=True,
            next_step=_budget_exhausted_message(session),
        )
        return json.dumps(result.to_dict(), ensure_ascii=False)

    # Success: record one combined pending_change so the apply
    # path replays it as a single atomic op. We store the
    # post-apply state directly so re-application on top of a
    # fresh DB row (race against another tab) is correct.
    session.staged_nodes = new_nodes
    session.staged_edges = new_edges
    session.pending_changes.append({
        "op": "plan",
        "nodes": [dict(n) for n in new_nodes],
        "edges": [dict(e) for e in new_edges],
    })

    # Build the applied summary. Compare pre/post staged state to
    # compute added/removed/updated counts — same shape as
    # `_diff_summary` but local to the plan so we don't recompute
    # the entire turn's diff.
    original_by_id = {n["id"]: n for n in session.original_nodes}
    applied = {
        "added_nodes": sum(
            1 for n in new_nodes if n["id"] not in original_by_id
        ),
        "removed_nodes": sum(
            1 for n in session.original_nodes
            if n["id"] not in {nn["id"] for nn in new_nodes}
        ),
        "updated_nodes": sum(
            1 for n in new_nodes
            if n["id"] in original_by_id
            and original_by_id[n["id"]] != n
        ),
        "added_edges": len(new_edges)
            - sum(1 for e in session.original_edges
                  if e["id"] in {ee["id"] for ee in new_edges}),
        "removed_edges": 0,  # computed below
    }
    new_edge_ids = {e["id"] for e in new_edges}
    applied["removed_edges"] = sum(
        1 for e in session.original_edges if e["id"] not in new_edge_ids
    )

    # Echo back the post-Pydantic config for every node the LLM
    # touched (added or updated). This is the F0.5 contract — the
    # LLM can see exactly what the validator kept.
    touched_ids = {
        pn.id for pn in parsed.nodes if pn.id is not None
    }
    config_echo: dict[str, dict] = {}
    for n in new_nodes:
        if n["id"] in touched_ids:
            config_echo[n["id"]] = n.get("data", {}).get("config", {})

    result = PlanResult(
        ok=True,
        applied=applied,
        config_echo=config_echo,
        state_unchanged=False,
    )
    return json.dumps(result.to_dict(), ensure_ascii=False)

def _replace_workflow(
    session: ChatSession,
    workflow_id: str,
    nodes: list[dict],
    edges: list[dict],
) -> str:
    """Replace the entire staged graph with the given nodes/edges.

    Equivalent to `plan_workflow` with `delete_nodes` covering
    every existing node and every existing edge. Use this when the
    LLM wants to throw away the current state and rebuild from
    scratch — typically a "create a brand new workflow" request.

    Args:
        workflow_id: The workflow to edit (must match the session).
        nodes: New nodes (replaces everything; the old `a1` is
               dropped, the new nodes take its place).
        edges: New edges (same semantics).

    Returns:
        Same shape as `plan_workflow`. `applied` reports what was
        replaced; `state_unchanged=False` on success.
    """
    _check_same_workflow(session, workflow_id)
    # Tolerate JSON-encoded strings for nodes/edges — same
    # reasoning as `_plan_workflow` above. See comment there.
    if not isinstance(nodes, list):
        try:
            nodes = _coerce_list_dict_arg(nodes)
        except ToolCallRejected:
            raise ToolCallRejected(
                "replace_workflow",
                f"nodes must be a list, got {type(nodes).__name__}",
            )
    if not isinstance(edges, list):
        try:
            edges = _coerce_list_dict_arg(edges)
        except ToolCallRejected:
            raise ToolCallRejected(
                "replace_workflow",
                f"edges must be a list, got {type(edges).__name__}",
            )
    # Build a synthetic plan that deletes every existing node id
    # and adds the new ones. This reuses `plan_workflow`'s
    # validation + commit path so the two tools are guaranteed to
    # have identical semantics — `_replace_workflow` is purely a
    # convenience over `_plan_workflow`.
    plan = {
        "delete_nodes": [n["id"] for n in session.staged_nodes],
        "delete_edges": [e["id"] for e in session.staged_edges],
        "nodes": nodes,
        "edges": edges,
    }
    return _plan_workflow(session, workflow_id, plan=plan)

def _preview_workflow(session: ChatSession, workflow_id: str) -> str:
    """Read-only: return the current staged state of the workflow.

    Use this when you're unsure of the current node ids or layout.

    Args:
        workflow_id: The workflow to read (must match the session).

    Returns:
        A JSON object: {"nodes": [...], "edges": [...], "pending_changes": <int>}
    """
    _check_same_workflow(session, workflow_id)
    return json.dumps(
        {
            "nodes": copy.deepcopy(session.staged_nodes),
            "edges": copy.deepcopy(session.staged_edges),
            "pending_changes": len(session.pending_changes),
        },
        ensure_ascii=False,
    )

# ─────────────────────────────────────────────────────────────────
# Session loader
# ─────────────────────────────────────────────────────────────────
def _load_or_create_session(
    db: Session,
    workflow_id: str,
    user: CurrentUser,
) -> ChatSession:
    """Return the existing session for (workflow_id, user) or
    create a fresh one snapshotting the current DB row.

    The session key is `(workflow_id, user.id)` — one chat per
    user per workflow. If a chat is already running, the new
    request takes over (we replace the staged node/edge state but
    keep the session id stable so the client can keep streaming).
    """
    # Look up an existing session for this user + workflow.
    existing = None
    for s in _SESSIONS.values():
        if s.workflow_id == workflow_id and s.user_id == user.id:
            existing = s
            break
    if existing is not None:
        return existing
    # Fresh session: load the current workflow row.
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(404, "Workflow not found")
    member_service.require_role(db, workflow_id, user, "editor")
    original_nodes = copy.deepcopy(row.nodes or [])
    original_edges = copy.deepcopy(row.edges or [])
    session = ChatSession(
        session_id=f"chat-{uuid.uuid4().hex[:8]}",
        workflow_id=workflow_id,
        user_id=user.id,
        original_nodes=original_nodes,
        original_edges=original_edges,
        staged_nodes=copy.deepcopy(original_nodes),
        staged_edges=copy.deepcopy(original_edges),
    )
    _SESSIONS[session.session_id] = session
    return session

def get_session(session_id: str) -> Optional[ChatSession]:
    return _SESSIONS.get(session_id)

def discard_session(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)

# ─────────────────────────────────────────────────────────────────
# LLM call — the agent run
# ─────────────────────────────────────────────────────────────────
# System prompt for the builder. Tells the LLM exactly what tools
# are available, what the workflow JSON looks like, and how to
# behave (always call preview_workflow first if unsure; never
# skip validation; produce a small diff).
#
# F2 : the per-type list is GENERATED from
# `app.core.node_types.NODE_TYPES` via `node_types_for_prompt()`,
# so adding a new preset to the manifest automatically shows up
# here on the next chat turn. The legacy hardcoded list was the
# source of the  drift — preset tool names (`wikipedia` /
# `tavily_search` / `duckduckgo` / `calculator` / `arxiv_search`)
# were advertised as available types but the legacy 10-type tuple
# silently rejected them in `add_node`. Generating from the manifest
# closes that loop. : those 5 presets now route
# through the unified `tool` type via `config.preset` — they no
# longer appear in the generated type list.
_BUILDER_SYSTEM_PROMPT_HEADER = """You are a workflow editor. The user describes changes in plain English; you call tools to edit the workflow JSON.

Available node types (generated from the manifest — call `get_node_types` for the per-type config schema):
$node_list

Workflow rules:
- Each node has: id (string, unique), type (one of the above), position {{x, y}}, data {{label, config}}.
- config is per-type: agent needs {{instructions, ...}}; router needs {{branches, ...}}; etc. Pass config as a JSON object — the platform validates it against the type's schema. If you're unsure of a field name, call `get_node_types` first.
- **EXACT node JSON shape (this is what `label` and `config` MUST look like — they live under `data`, never at the top level):**
  ```json
  {{
    "id": "agent_extract",
    "type": "agent",
    "position": {{"x": 300, "y": 0}},
    "data": {{
      "label": "",
      "config": {{"instructions": "Extract entities from the user message"}}
    }}
  }}
  ```
  Putting `label` / `config` at the TOP level (instead of under `data`) silently DROPS them — the node is staged with empty `data` and cannot be rendered. The backend has a tolerance layer for this but you should still nest them correctly.
- Edges connect nodes: edge {{id, source, target, sourceHandle?, targetHandle?, kind?}}.
- kind defaults to "dataflow". Use "tool_attachment" only when connecting a tools/mcp/http source to an agent.
- Calling remove_node auto-removes any edges touching the node.

Workflow structure — when to split into multiple agents (READ THIS — the most common bad pattern):

DEFAULT: ONE agent with multiple tools attached. Most "do X with tool A, then do Y with tool B" workflows are a single agent whose instructions say "do X then do Y, calling the right tool for each". The agent makes multiple tool calls within ONE LLM turn — no opaque text → text → text information transfer between agents, no JSON-string parsing between nodes, no N near-identical "sorry the tool failed" responses.

Add a SECOND agent ONLY when one of these is genuinely true:
  (a) HITL gate between them — `ask` or `branch` (mode='switch') pauses for user input before continuing.
  (b) Parallel composition — `flow` (mode='parallel') fans out to N agents doing genuinely different work concurrently, with a sibling aggregator collecting the results.
  (c) Distinct, non-overlapping tool sets — agent A owns HTTP tool X, agent B owns function tool Y, neither needs the other's. (Even then, consider one agent with both tools — multi-tool is normal.)
  (d) Distinct model — e.g. a cheap classifier + an expensive reasoner, where context isolation matters.
  (e) Loop body — an agent referenced by a `loop.bodyTarget` is a separate iteration step (the loop re-runs it).

DO NOT split for any of these — they're all info-passing with no value:
  * "I want a welcome / greeting step first" → ONE agent. Put the greeting in its instructions.
  * "I want to extract entities, then call a tool with those entities" → ONE agent with the tool attached. The agent does entity extraction AND tool calling in one LLM turn.
  * "I want to query A, then based on the result call B, then format the output" → ONE agent with both tools attached. The agent chains tool calls internally based on results.
  * "I want a final formatter / summarizer step" → ONE agent. Formatting is part of its response.
  * "I want each agent to do one thing cleanly" → "one thing" usually means "one tool call" or "one prompt completion" — that's a SINGLE agent's job, not a chain.

Anti-pattern example (DO NOT generate — was a real user report, ): a 5-agent chain welcome → entity_extract → query_agent → dispatch_agent → result, each handling one narrow task. Produced 5 separate LLM calls, 5 separate user-visible messages (the welcome and result agents are pure boilerplate), and JSON-string-parsing brittleness at every hop. When the HTTP tool failed at step 3, all 3 subsequent agents each emitted a near-identical "the tool can't connect to the server" message — the user saw the same apology three times.

Good pattern for the same intent: ONE agent with both HTTP tools attached. Instructions: "You are a transformer-inspection assistant. The user gives you a city, district, and equipment type. Call query_substations to look up matching substations; if any match, call dispatch_task with confirmed=true to schedule an inspection; report the result back to the user." One LLM turn, one user-visible response, structured tool calls, error handled within the agent.

Rule of thumb: if you can describe the workflow as "A does X, then B does Y based on A's output" with no HITL gate between, it's ONE agent with multiple tools. If you can describe it as "the user must confirm Y before X continues", that IS multiple agents with an `ask` in between.

Recommended workflow:
1. If the user asks for a change and you're unsure of the current state, call `preview_workflow` first.
2. **Reach for `plan_workflow(plan={{nodes: [...], edges: [...]}})` for ANY non-trivial edit** — adding ≥1 new node, adding ≥1 new edge, or any multi-node change. ONE `plan_workflow` call adds the whole batch atomically and validates everything up front. The imperative `add_node` / `connect_nodes` tools each count against your per-turn budget — building a 7-node workflow with ~10 edges via them takes ~17 calls and you can hit the cap mid-stream. **Reserve the imperative tools for surgical single-step fixes** (rename, reposition, patch one config field, add exactly one node in isolation).
3. For small single-node tweaks (rename, reposition, patch one config field), `update_node` is fine.
4. Each tool call returns either a JSON object on success, or an error message on failure. If you see an error, fix the arguments and try again — do NOT pretend the change succeeded.
5. After all changes, briefly describe what you did (1-2 sentences). Do NOT output the JSON directly — the platform renders the diff.

Tool-call budget (read this before reaching for `add_node` repeatedly):
- You have a hard cap of $tool_call_cap imperative tool calls per turn (`add_node` / `update_node` / `remove_node` / `connect_nodes`). High-level tools (`plan_workflow`, `replace_workflow`, `create_*`, `preview_*`, `get_*`, `inspect_*`) don't count — they're batch operations or read-only diagnostics. Hitting the cap surfaces a "tool-call cap reached" error and your partial diff is applied as-is — the user must start a new turn to keep editing.
- A typical workflow (10 nodes + 9 edges) takes ONE `plan_workflow` call. The same workflow via `add_node` + `connect_nodes` would take ~19 calls and exhaust the budget on call 8.
- **Rule of thumb**: as soon as you can see your edit needs ≥2 imperative calls, STOP and rewrite as a single `plan_workflow` / `replace_workflow` call. If it needs `create_react_agent` / `create_router_pattern` / `create_retry_loop`, prefer those even more — they collapse 5+ imperative calls into one.
- For a "build from scratch" workflow (no existing nodes to preserve), use `replace_workflow(nodes=[...], edges=[...])` — same batched-validation contract as `plan_workflow`.

Self-correction — when a tool call is rejected (you get a tool result that starts with an error message or has `ok:false`):
  a. Read the `hint` line — it tells you the concrete next step (which field to fix, which tool to call, which schema to consult).
  b. If the error message includes an `Issue` object (path / code / message / hint), match on `code` (a stable enum value) rather than `message` (which can be reworded).
  c. Diagnostic tools available:
       - `preview_workflow` — see current staged ids + topology (use when you don't know what's there).
       - `get_node_types` — return the per-type config schema (use when a `INVALID_CONFIG` / `MISSING_REQUIRED_FIELD` error mentions a field you don't recognize).
       - `get_connection_rules` — return the allowed edge source/target table (use when a `INCOMPATIBLE_SOURCE` / `INCOMPATIBLE_TARGET` error means you wired the wrong kind).
       - `get_graph_state` — return cycle / dangling / orphan diagnoses for the current graph (use when a `cycle` or `danglingInput`/`danglingOutput` error fires).
       - `inspect_run(run_id)` / `explain_failure(run_id)` — diagnose a recent run; the run_id comes from a `run_workflow` tool result.
  d. Do NOT loop calling the same mutating tool with slight variations. If two consecutive retries fail with the same `code`, switch to a diagnostic.

HITL — choosing between `ask` and `branch` (READ THIS — common mistake):
- `ask` is a VALUE COLLECTOR. It pauses the pipeline so the user can type / confirm / pick a choice; the answer is then fed downstream as the step's output text. It does NOT decide which branch to take — the workflow always continues into whatever is wired downstream.
- `branch` (mode='switch' with `selector_mode='hitl'`) is a BRANCH DECIDER. The runtime asks the user "which branch?" and the user's selection chooses the downstream path. With `selector_mode='function'` / `'cel'` the runtime picks itself.
- DECISION RULE:
    * "Ask the user for X and keep going down the same path"  →  `ask`.
    * "Ask the user YES/NO and route to different nodes depending on the answer"  →  `branch` (mode='switch') with `selector_mode='hitl'` (use `create_router_pattern(selector_mode='hitl', branches=[...])`).
    * "Ask the user to pick from 2+ options and route to a different agent per option"  →  `branch` (mode='switch') with `selector_mode='hitl'`.
- COMMON MISTAKE: putting a yes/no question on an `ask` and then connecting multiple downstream paths to it. The `ask` will always continue downstream — the user's "" becomes a text payload, not a branch decision. If you need branch routing, the `ask` MUST be replaced by a `branch` (mode='switch') with `selector_mode='hitl'`.
- `flow` (mode='sequential') with `requiresConfirmation: true` is a block-level gate (one yes/no before the whole batch runs) — that's a separate concern from per-step branch routing.

The user's prompt describes the AGENT'S JOB, not the workflow's steps (READ THIS — recurring LLM-builder mistake):

Workflow primitives (`ask` / `branch` / `flow` / `loop`) exist for CONTROL FLOW. They don't exist to express what an agent does in its LLM turn. When you find yourself putting prose like "greet the user", "extract entities", "format the response", "ask the user to confirm", "call the tool with X then call it again with Y" into a workflow node's config — that's not a workflow step, that's part of ONE agent's job. The agent does all of that in one LLM turn (with multiple tool calls, structured tool results, its own internal reasoning).

Worked example — same intent, two opposite drafts:
```
# BAD (the recurring LLM-builder mistake)
nodes:
  ask(prompt="XX", inputType="text")    # "greeting" as workflow primitive
  agent(instructions="XX...")               # does the actual work
edges: [ask → agent]
# Result: user types the real request → workflow pauses for the greeting
# → user types "" to dismiss → agent sees a literal "yes" and asks for
# details the user already provided. Workflow is functionally broken.

# GOOD
nodes:
  agent(instructions="XX... ... ...")
edges: []
# Result: user types the real request once → agent greets, waits,
# extracts, calls tools, asks confirmation, dispatches — all in one
# LLM turn. No pause, no lost input, no broken flow.
```

Decision rules — when does each workflow primitive earn its place:
  * User says "user must confirm X before Y runs" → `ask` between X and Y (value collector — the workflow genuinely needs to pause for user input that downstream steps depend on).
  * User says "do A and B in parallel, then combine" → `flow(mode=parallel)` + sibling aggregator.
  * User says "iterate X until Y" → `loop`.
  * User says "agent greets, extracts, calls tools, asks confirmation, dispatches" → ONE agent with multiple tools. None of those verbs map to a workflow node.
  * User says "based on the user's intent, route to specialist A or specialist B" → `branch(mode=switch)`.

Self-check before submitting `plan_workflow`: read each non-compound node's `config.instructions` (for agents) or `prompt` (for `ask`). If two of them together read like "agent A greets → agent B extracts → agent C calls tool → agent D formats", that's the bug — merge them into ONE agent. If a single `ask`'s `prompt` is a greeting / welcome / "what can I help you" message (i.e. something that could just as well be the first sentence of an agent's `instructions`), delete the `ask` and put the greeting at the top of the downstream agent's `instructions`.

__ROUTER_SEMANTICS_SECTION__

__HTTP_CONFIG_SECTION__

__ATTACH_TOOL_SECTION__

You may NOT make system-level changes (workflow name, description, deletion, creation). This chat only edits the nodes/edges of the workflow named below."""

_BUILDER_SYSTEM_PROMPT_ROUTER_SECTION = """\
Router selector semantics (READ THIS — runtime will reject if mismatched):

- `selector.expression` (function mode) must RETURN a BRANCH STEP OBJECT, NOT a label string. The runtime wraps it as `return (<your expression>)` and matches the return value against the branch step references. Returning the label "yes" matches nothing at runtime — the workflow silently skips the router and falls through.
- Each branch node is exposed in the selector scope as `<branch_id>_step` (e.g. `yes_agent_step`). So if you build a router via `create_router_pattern(branches=[{id: yes_agent, ...}, {id: no_agent, ...}])`, the selector MUST be `yes_agent_step if previous_step_content == 'yes' else no_agent_step`.
- Edges from the router → branch carry `sourceHandle=<branch label>` (cosmetic — drives the canvas visual, not runtime matching).

Worked example (function-mode router with 2 branches):
```json
{
  "nodes": [
    {"id":"router1","type":"router","data":{"label":"yes/no","config":{
      "selector":{"mode":"function","expression":"yes_agent_step if previous_step_content == 'yes' else no_agent_step"},
      "branches":[
        {"label":"yes","target":"yes_agent"},
        {"label":"no","target":"no_agent"}
      ]
    }}},
    {"id":"yes_agent","type":"agent","data":{"label":"Yes","config":{"instructions":"..."}}},
    {"id":"no_agent","type":"agent","data":{"label":"No","config":{"instructions":"..."}}}
  ],
  "edges":[
    {"source":"router1","target":"yes_agent","kind":"dataflow","sourceHandle":"yes"},
    {"source":"router1","target":"no_agent","kind":"dataflow","sourceHandle":"no"}
  ]
}
```

The 5 locals available in `selector.expression` (function mode): `previous_step_content` (str | None), `previous_step_outputs` (mapping), `input` (str | None), `additional_data` (mapping), `session_state` (mapping).

"""

_BUILDER_SYSTEM_PROMPT_HTTP_SECTION = """\
HTTP node — full config shape (under-emitting silently produces an untitled tool):

When `type='http'` is used, pass ALL of these fields in `data.config` — missing ones
default to empty / None and render as a generic untitled tool at runtime. Call
`get_node_types` first if you forget a field name.

Worked example (a complete HTTP node in `plan_workflow`):
```json
{
  "type": "http",
  "id": "http_weather",
  "label": "Weather API",
  "config": {
    "toolName": "get_weather",
    "toolDescription": "Look up current weather for a city. Returns JSON with temp_c, condition, humidity.",
    "method": "GET",
    "baseUrl": "https://api.weather.example.com",
    "path": "/v1/current",
    "headers": {"X-API-Key": "<your key>"},
    "queryParams": {"units": "metric"},
    "bodySchema": "",
    "authToken": "<bearer token>"
  }
}
```

Rules:

- `toolName` and `toolDescription` MUST be set — the LLM sees those when invoking the tool, not the node label.
- `baseUrl` MUST be non-empty (Pydantic enforces; missing + POST → 422).
- `method` is `"GET"` or `"POST"`. For POST, populate `bodySchema` with the JSON shape the API expects (a string of the JSON schema).
- `headers` and `queryParams` are dicts; `authToken` is a single bearer token string (use either `headers` or `authToken`, not both).
- For `create_react_agent(tools=[{type:'http', config:{...}}])`, the same config shape applies — the platform creates the HTTP node and attaches it to the agent in one call.

"""

_BUILDER_SYSTEM_PROMPT_ATTACH_TOOL_SECTION = """\
Attaching tools to an existing agent (READ THIS — common mistake):

- `create_react_agent(tools=[...])` only attaches the listed tools to THAT agent — the one it creates in the same call. It will NOT wire tools to an agent that already exists in the staged graph, and it will NOT wire a tool to a different downstream agent.
- To add a tool to an agent created by an earlier call, OR to wire a tool to a different downstream agent, use `attach_tool(agent_id=<existing_agent_id>, tool_type='http', tool_config={...})` — OR set `tool_config={'preset': '<name>'}` for one of the 5 presets (`wikipedia` / `tavily_search` / `duckduckgo` / `calculator` / `arxiv_search`).  collapsed those presets into the unified `tool` node via the `preset` discriminator. It creates the tool source node + a `kind='tool_attachment'` edge in one call.
- To undo, use `detach_tool(edge_id=<the tool_attachment edge id>)`. This removes only the edge — the tool source node and the agent node are kept.

Both `attach_tool` and `detach_tool` count against the per-turn tool-call cap. For ≥2 attachments in one shot, prefer `plan_workflow(plan={nodes:[<tools>], edges:[<tool_attachments>]})`.

"""

def BUILDER_SYSTEM_PROMPT() -> str:
    """Build the system prompt with the live node-type table.

    Generated at chat-start time (not module-load time) so unit
    tests that mutate `NODE_TYPES` between tests can see fresh
    data. Cheap — `node_types_for_prompt` is an `lru_cache`d
    function so the manifest walk only happens once per process.

    `tool_call_cap` is interpolated from `MAX_TOOL_CALLS_PER_TURN`
    so if the cap ever changes the prompt stays in sync — the LLM
    always sees the actual ceiling, not a stale literal.
    """
    header = (
        _BUILDER_SYSTEM_PROMPT_HEADER
        .replace("__ROUTER_SEMANTICS_SECTION__", _BUILDER_SYSTEM_PROMPT_ROUTER_SECTION)
        .replace("__HTTP_CONFIG_SECTION__", _BUILDER_SYSTEM_PROMPT_HTTP_SECTION)
        .replace("__ATTACH_TOOL_SECTION__", _BUILDER_SYSTEM_PROMPT_ATTACH_TOOL_SECTION)
    )
    # Use string.Template instead of str.format so the JSON example's
    # `{...}` literals don't trip .format()'s placeholder parser.
    from string import Template
    return Template(header).safe_substitute(
        node_list=node_types_for_prompt(),
        tool_call_cap=MAX_TOOL_CALLS_PER_TURN,
    )

def _build_llm_model(
    db: Session,
    user: CurrentUser,
    preset_id_override: Optional[str] = None,
):
    """Resolve the LLM the builder chat should use and build an agno Model.

    `preset_id_override` — when set, prefer this preset over the user's
    default. Used by the chat UI to swap to a stronger model for a
    complex build ("") without changing the user's system default.
    The override is validated: it must reference an existing preset
    that belongs to the caller OR is system-shared (user_id IS NULL).
    An invalid override silently falls back to the default rather than
    erroring — the chat UI shouldn't break just because a stale id is
    in localStorage.

    Falls back to the user's default preset when no override is set.
    Raises HTTP 400 with a helpful instruction if no preset is
    configured at all (neither an override nor a default).
    """
    preset_id: Optional[str] = preset_id_override
    if preset_id:
        # Validate the override exists and is accessible to this user.
        # Owner check: either the preset's user_id matches the caller
        # OR the preset is system-shared (user_id IS NULL).
        row = (
            db.query(LlmPreset)
            .filter(LlmPreset.id == preset_id)
            .filter(or_(LlmPreset.user_id == user.id, LlmPreset.user_id.is_(None)))
            .first()
        )
        if row is None:
            # Silently fall back — see the docstring above.
            preset_id = None
    if not preset_id:
        preset_id = _resolve_default_preset_id(db=db, user_id=user.id)
    if not preset_id:
        raise HTTPException(
            400,
            "No default LLM preset configured. Set one in Settings → LLM Models "
            "before using the workflow builder chat.",
        )
    model = build_model({"presetId": preset_id}, user_id=user.id)
    if model is None:
        raise HTTPException(
            400,
            "Selected LLM preset is malformed (missing apiKey or unknown provider). "
            "Pick another one in the chat header, or fix the preset in Settings → LLM Models.",
        )
    return model

def _build_tools_for_session(session: ChatSession) -> list:
    """Wrap the tool handlers in agno Function objects whose
    docstring + signature the agent uses to plan tool calls.

    Each Function is bound to the session via closure — the agent
    loop just calls `function(**kwargs)` with the LLM's parsed
    args, and our handler validates + mutates the staged state.
    agno's `Function.from_callable` introspects the function's
    type hints and docstring to build the JSON schema for the
    LLM's tool call."""

    from agno.tools.function import Function

    # Each wrapper exposes the LLM-visible signature + docstring
    # (Function.from_callable reads both). The body just calls
    # the session-bound handler.
    #
    # `workflow_id` is OPTIONAL with a default that falls back to
    # the session's workflow_id. The LLM shouldn't have to pass it
    # (the agent picks the workflow up from the chat session), but
    # some models (notably Sonnet with tool calls) sometimes omit
    # it because the system prompt doesn't surface it as a field
    # the LLM has to fill in. Making it optional here avoids a
    # silent `_check_same_workflow` rejection that left the user
    # staring at a "No changes to apply" diff card.
    def add_node(type: str, id: str = "",
                 position: Any = None, label: str = "",
                 config: Any = None,
                 workflow_id: str = "") -> str:
        """Add ONE node to the workflow. Each call counts against the per-turn tool-call cap — building a workflow via repeated `add_node` will exhaust the budget long before it's done.

        **For ≥2 new nodes, a whole new workflow, or any multi-node edit, prefer `plan_workflow(plan={nodes:[...],edges:[...]})` in a single atomic call instead** — it bypasses the per-turn cap and validates the whole batch up front. This imperative tool is best for single-step tweaks (adding exactly one node in isolation, e.g. a post-hoc inspector or an extra branch endpoint). See the system prompt's "Recommended workflow" rule #2.

        The platform validates the config against the node type's schema before staging the change. See tool schemas for parameter details. `position` and `config` may be passed as JSON objects OR as JSON-encoded strings (decoded automatically).

        Type choice — pick the right primitive for the job:
          * `ask` — pauses the pipeline to COLLECT a value from the user (text / yes-no confirmation / single-choice pick). The answer flows downstream as the step's output; the workflow always continues. NOT for branching. : renamed from `human_input`.
          * `branch` (mode='switch' with `selector_mode='hitl'`) — pauses to let the user PICK which downstream branch to take. Use this whenever the user's answer should route to different downstream nodes. Prefer `create_router_pattern(selector_mode='hitl', branches=[...])` over hand-rolling the branch + edges.
          * `steps` with `requiresConfirmation: true` — one-shot yes/no BEFORE running the whole steps block (block-level gate, not per-step).
          * `condition` — deterministic if/else on an expression (no user prompt).
          * `agent` / `parallel` / `loop` / tool-source types — see the per-type usage hint at the top of the system prompt for a one-liner.
        """
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            position = _coerce_dict_arg(position, allow_none=True)
            config = _coerce_dict_arg(config, allow_none=True)
            return _add_node(session, workflow_id, type=type, id=id,
                             position=position, label=label, config=config)
        except ToolCallRejected as exc:
            # F6 : surface the rejection as a tool
            # result string so the LLM sees both the message AND
            # the structured hint. Without this, AGNO turns the
            # exception into a generic error string and the LLM
            # loses the next-step guidance.
            return _rejection_result(exc, session)

    def update_node(node_id: str, patch: Any = None,
                    workflow_id: str = "") -> str:
        """Update an existing node's label, config, or position. Pass a patch object with only the keys you want to change. Re-validates the resulting graph. `patch` may be a JSON object or a JSON-encoded string."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            patch = _coerce_dict_arg(patch, allow_none=True)
            return _update_node(session, workflow_id, node_id=node_id, patch=patch)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def remove_node(node_id: str, workflow_id: str = "") -> str:
        """Remove a node. Any edges touching the node are also removed (cascade). Re-validates the resulting graph."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            return _remove_node(session, workflow_id, node_id)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def connect_nodes(source: str, target: str,
                      kind: str = "dataflow",
                      source_handle: str = "",
                      workflow_id: str = "") -> str:
        """Connect TWO existing nodes with ONE edge. Each call counts against the per-turn tool-call cap — wiring a workflow's edges one-at-a-time burns the budget fast.

        **For ≥2 edges or wiring up a whole workflow, prefer `plan_workflow(plan={nodes:[...],edges:[...]})` in a single atomic call instead** — it bypasses the per-turn cap and validates the whole batch up front. Reserve this imperative tool for single-edge fixes (e.g. patching one missing wire between two already-present nodes).

        `kind='dataflow'` for control flow, `'tool_attachment'` for tool source → agent wiring. Pass `source_handle=<branch_label>` for router branches — without it, the edge is the router's DEFAULT branch only. Re-validates the resulting graph."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            return _connect_nodes(session, workflow_id, source=source, target=target,
                                  kind=kind, source_handle=source_handle or None)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def attach_tool(agent_id: str, tool_type: str,
                    tool_id: str = "", tool_config: Any = None,
                    tool_label: str = "",
                    workflow_id: str = "") -> str:
        """Attach ONE tool source to an EXISTING agent. Creates the tool node (type from the manifest tool-source list — http / mcp / function; for preset tools set `tool_config={'preset': '<name>'}` for one of wikipedia / tavily_search / duckduckgo / calculator / arxiv_search) and wires it to `agent_id` with a `kind='tool_attachment'` edge in one call.

        Use this when: (a) you want to add a tool to an agent created by an earlier `create_react_agent` or `add_node` call; (b) you want to attach a tool to a mid-flow agent that `create_react_agent` wouldn't touch (it only wires tools to the new agent it creates). Use `detach_tool(edge_id=...)` to undo.

        Each call counts against the per-turn tool-call cap. For attaching ≥2 tools in one shot, prefer `plan_workflow(plan={nodes:[<tool>], edges:[<tool_attachment>]})` instead.
        """
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            return _attach_tool(
                session, workflow_id,
                agent_id=agent_id, tool_type=tool_type,
                tool_id=tool_id, tool_config=tool_config,
                tool_label=tool_label,
            )
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def detach_tool(edge_id: str, workflow_id: str = "") -> str:
        """Remove ONE tool_attachment edge by id. The source tool node AND the target agent node are kept — only the edge is removed. Use this to undo an `attach_tool` call without losing the tool source."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            return _detach_tool(session, workflow_id, edge_id=edge_id)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def disconnect(edge_id: str, workflow_id: str = "") -> str:
        """Remove an edge. Re-validates the resulting graph."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            return _disconnect(session, workflow_id, edge_id)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def preview_workflow(workflow_id: str = "") -> str:
        """Return the current staged state of the workflow as JSON. Use this when you're unsure of the current node/edge ids."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            return _preview_workflow(session, workflow_id)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    # F1  — Plan DSL tools. These are the recommended
    # path for any non-trivial edit: one call describes the full
    # target state, the backend validates the whole batch
    # atomically, and every failure is returned as a structured
    # `Issue` with a path the LLM can use to pinpoint the bad
    # entry. The imperative tools above remain available as a
    # fallback for small tweaks but the chat prompt steers the
    # LLM toward `plan_workflow` first.
    def plan_workflow(plan: Any, workflow_id: str = "") -> str:
        """Submit a declarative WorkflowPlan describing the target state. The backend validates the WHOLE plan (every node, edge, connection rule, graph rule) BEFORE mutating anything. On failure the staged state is untouched and the response carries a list of {path, code, message, hint} issues; on success it commits the new state atomically and echoes the post-validation config of every touched node.

        The plan object has these keys, all optional:
            nodes:           list of {id?, type, position?, data?}  -- upsert (id match → replace)
            edges:           list of {id?, source, target, sourceHandle?, targetHandle?, kind?} -- upsert
            delete_nodes:    list of node ids to remove (cascades edges)
            delete_edges:    list of edge ids to remove

        See `get_node_types` for the per-type config schema. Always pass `type` as one of the manifest types; pass `config` as the type-specific object the runtime expects. `plan` may be passed as a JSON object or a JSON-encoded string (decoded automatically)."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            plan = _coerce_dict_arg(plan)
            return _plan_workflow(session, workflow_id, plan=plan)
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def replace_workflow(
        nodes: Any,
        edges: Any,
        workflow_id: str = "",
    ) -> str:
        """Replace the entire staged graph with the given nodes and edges. Equivalent to calling plan_workflow with `delete_nodes` covering every existing node — convenience wrapper for the "build from scratch" case. Same atomicity + structured-error guarantees as plan_workflow. `nodes` and `edges` may be JSON arrays or JSON-encoded strings."""
        if not workflow_id:
            workflow_id = session.workflow_id
        try:
            nodes = _coerce_list_dict_arg(nodes)
            edges = _coerce_list_dict_arg(edges)
            return _replace_workflow(
                session, workflow_id, nodes=nodes, edges=edges,
            )
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    # F2  — schema introspection. The LLM can call this
    # on demand to read the per-type config schema: which fields a
    # given node type accepts, what the alias keys are, what the
    # default config produces. Generated from the manifest + the
    # Pydantic per-type config schemas, so it stays in sync with
    # what the runtime actually accepts.
    def get_node_types() -> str:
        """Return the per-type config schema documentation as JSON. Call this BEFORE issuing a plan_workflow when you're unsure of a field name, when the workflow has types you haven't used before, or when you want to know what defaults the platform applies when a field is omitted.

        Returns a JSON object `{"node_types": [...]}` — each entry carries `type`, `display_name`, `kind`, `default_config`, and `fields[]`. Each field has `name` (python name), `alias` (camelCase key the platform accepts on input), `required`, and `description`."""
        return get_node_types_tool()

    # F3  — read-only inspection tools. The LLM can call
    # these to read the staged graph's current state and the
    # connection-rule table before issuing a plan. Both are
    # non-mutating and side-effect-free.
    def get_graph_state(workflow_id: str = "") -> str:
        """Return a structured summary of the current staged graph as JSON. Includes counts (nodes / edges / by-type breakdown), entry points (no incoming edges), terminal nodes (no outgoing edges), orphans (no edges in either direction), and a per-node view of outgoing/incoming degree. Read-only — does not modify state.

        Call this before issuing plan_workflow to verify your mental model of the graph (which nodes are wired, which are orphans). Useful for "is this graph done?" questions."""
        if not workflow_id:
            workflow_id = session.workflow_id
        _check_same_workflow(session, workflow_id)
        return get_graph_state_tool(
            session.staged_nodes, session.staged_edges,
        )

    def get_connection_rules() -> str:
        """Return the connection rule table as JSON. For every node type, lists which other node types may appear as source or target of an outgoing dataflow edge, plus degree bounds (`max_outgoing`, `min_outgoing`, `max_incoming`, `min_incoming`). Aliases (`@executable` / `@tool_source`) are expanded to concrete node-type lists.

        Call this BEFORE issuing plan_workflow to verify every edge's source/target types are allowed. Read-only, no state changes."""
        return get_connection_rules_tool()

    # F4  — high-level pattern primitives. Each one
    # builds a WorkflowPlan internally and routes it through
    # `_plan_workflow` so atomicity + structured Issue errors
    # are inherited for free.
    def create_react_agent(
        instructions: str,
        tools: Any,
        agent_id: str = "",
        agent_label: str = "",
        max_tool_calls: Optional[int] = None,
        workflow_id: str = "",
    ) -> str:
        """Build a "react agent with N tools" topology in one call. Internally generates a plan that adds an Agent node plus one node per tool source (preset or generic), wired with `kind='tool_attachment'` edges.

        Args:
            instructions: the agent's `instructions` field (passed to AgentNodeConfig).
            tools: list of `{type, id?, config?, label?}` dicts OR a JSON-encoded string of the same. `type` is one of the manifest tool-source types (http / mcp / function). For a preset tool (wikipedia / tavily_search / duckduckgo / calculator / arxiv_search) set `type='tool'` and put `{'preset': '<name>'}` in `config` —  collapsed the 5 preset tool types into the unified `tool` node via the `preset` discriminator.
            agent_id: optional explicit agent id; one is generated if empty.
            agent_label: optional display label; defaults to the id.
            max_tool_calls: optional `toolCallLimit` cap on tool calls per agent invocation. None leaves the runtime default.

        Use this when the user asks for "an agent that can search the web and do math" or similar — the pattern produces a clean, fully-wired graph in one shot instead of 5+ imperative calls.

        ⚠️ The tools in this call attach ONLY to the new agent this function creates. To add tools to an existing agent (or to a different downstream agent), use `attach_tool(agent_id=<existing>, tool_type=..., tool_config={...})` — it creates the tool source + a tool_attachment edge in one call.
        """
        if not workflow_id:
            workflow_id = session.workflow_id
        _check_same_workflow(session, workflow_id)
        try:
            tools = _coerce_list_dict_arg(tools)
            plan = build_react_agent_plan(
                instructions=instructions,
                tools=tools,
                id=agent_id,
                label=agent_label,
                max_iterations=max_tool_calls,
            )
        except (ValueError, TypeError) as exc:
            return json.dumps({
                "ok": False,
                "issues": [Issue(
                    path="(pattern)",
                    code=IssueCode.INVALID_CONFIG,
                    message=str(exc),
                    hint="",
                ).to_dict()],
                "state_unchanged": True,
            }, ensure_ascii=False)
        try:
            return _plan_workflow(
                session, workflow_id,
                plan=pattern_plan_to_dict(plan),
            )
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def create_router_pattern(
        branches: Any,
        selector_mode: str = "function",
        selector_expression: str = "",
        router_id: str = "",
        router_label: str = "",
        replace_existing: bool = False,
        workflow_id: str = "",
    ) -> str:
        """Build a "router with N branches" topology in one call. Adds a Router node + one downstream node per branch + dataflow edges.

        Args:
            branches: list of `{type, id?, config?, label?}` dicts OR a JSON-encoded string of the same. Order is preserved as the branch order in the router config.
            selector_mode: `"function"` | `"cel"` | `"hitl"`. Determines how the runtime picks the branch. Default `"function"`.
                - `"hitl"`: the runtime prompts the user to PICK which branch to take — this is the correct primitive when the user (not an LLM judge or a CEL expression) needs to make the routing decision.
                - `"function"`: the runtime evaluates `selector_expression` as a Python expression (wrapped as `return (<your expression>)`). Must RETURN a branch step object — e.g. `yes_agent_step if previous_step_content == 'yes' else no_agent_step`. Returning a label string ("yes") matches nothing at runtime.
                - `"cel"`: the runtime evaluates `selector_expression` as a CEL expression over the prior step's output.
            selector_expression: when mode is `"cel"` or `"function"`, the source string. Empty for `"hitl"`.
                - **Function mode**: the expression is wrapped as `return (<your expression>)` and evaluated against `step_input`. The 5 locals are `previous_step_content`, `previous_step_outputs`, `input`, `additional_data`, `session_state`. The return value MUST be one of `<branch_id>_step` (each branch node is exposed in scope as `<id>_step`). Example: `yes_agent_step if 'urgent' in previous_step_content else no_agent_step`.
                - **CEL mode**: passed verbatim to agno's CEL evaluator; the result is matched against branch ids (NOT `_step` suffix).
            router_id: optional explicit router id.
            router_label: optional display label.
            replace_existing: when True, deletes the existing router (if any) before adding the new one. Default False (the new router is added alongside).

        Use this for "route to one of these agents based on…" requests, OR for "ask the user YES/NO (or pick from a list) and route to different downstream nodes" — the latter is `selector_mode='hitl'`. ⚠️ Do NOT use an `ask` node for branch routing — `ask` collects a value but does not choose a branch (the workflow always continues downstream). See the HITL section in the system prompt for the full decision rule.
        """
        if not workflow_id:
            workflow_id = session.workflow_id
        _check_same_workflow(session, workflow_id)
        try:
            branches = _coerce_list_dict_arg(branches)
            plan = build_router_pattern_plan(
                branches=branches,
                selector_mode=selector_mode,
                selector_expression=selector_expression,
                id=router_id,
                label=router_label,
                delete_existing_router=replace_existing,
            )
        except (ValueError, TypeError) as exc:
            return json.dumps({
                "ok": False,
                "issues": [Issue(
                    path="(pattern)",
                    code=IssueCode.INVALID_CONFIG,
                    message=str(exc),
                    hint="",
                ).to_dict()],
                "state_unchanged": True,
            }, ensure_ascii=False)
        try:
            return _plan_workflow(
                session, workflow_id,
                plan=pattern_plan_to_dict(plan),
            )
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    def create_retry_loop(
        instructions: str,
        max_iterations: int = 3,
        end_condition: str = "",
        agent_id: str = "",
        agent_label: str = "",
        loop_id: str = "",
        loop_label: str = "",
        workflow_id: str = "",
    ) -> str:
        """Build an "agent wrapped in a retry loop" topology in one call. Adds a Loop node whose `body_target` is a new Agent, plus the implicit body wiring (no dataflow edge — the loop's `body_target` carries that signal).

        Args:
            instructions: the agent's `instructions`.
            max_iterations: bounded 1..1000 (Pydantic enforces).
            end_condition: optional substring-match early-exit. Empty means "always run max_iterations".
            agent_id: optional explicit agent id.
            agent_label: optional agent label.
            loop_id: optional explicit loop id.
            loop_label: optional loop label.

        Use this for "retry until X" requests where the body is a single agent step.
        """
        if not workflow_id:
            workflow_id = session.workflow_id
        _check_same_workflow(session, workflow_id)
        try:
            plan = build_retry_loop_plan(
                instructions=instructions,
                max_iterations=max_iterations,
                end_condition=end_condition,
                agent_id=agent_id,
                agent_label=agent_label,
                loop_id=loop_id,
                loop_label=loop_label,
            )
        except (ValueError, TypeError) as exc:
            return json.dumps({
                "ok": False,
                "issues": [Issue(
                    path="(pattern)",
                    code=IssueCode.INVALID_CONFIG,
                    message=str(exc),
                    hint="",
                ).to_dict()],
                "state_unchanged": True,
            }, ensure_ascii=False)
        try:
            return _plan_workflow(
                session, workflow_id,
                plan=pattern_plan_to_dict(plan),
            )
        except ToolCallRejected as exc:
            return _rejection_result(exc, session)

    # F5  — runtime debug tools. The LLM calls these
    # to verify a freshly-edited workflow actually runs. `run_workflow`
    # builds a tmp workflow off the SESSION's STAGED graph (NOT the
    # DB row — apply hasn't happened yet) and returns a trace;
    # `inspect_run` reads the trace back; `explain_failure` runs
    # diagnostic rules over a failed trace to attribute the error.
    #
    # All three route through `app.services.chat_builder_run` which
    # holds the RunTraceStore — they don't import `runtime_service`
    # (Pillar 1), keeping the architectural isolation agreed on
    # . The store is a module-level dict (maxsize 50,
    # FIFO eviction); same cross-worker caveat as `_SESSIONS`.
    def run_workflow(
        input: str,
        hitl_responses: Optional[list] = None,
        workflow_id: str = "",
    ) -> str:
        """Run the session's STAGED graph (not the persisted DB row) as a tmp workflow, return a trace summary.

        Builds the workflow off the session's staged state and runs it once. The trace is stored in the chat builder's `RunTraceStore` and the `run_id` is returned so you can call `inspect_run(run_id)` to read the full trace or `explain_failure(run_id)` to attribute a failure.

        Args:
            input: the user message to feed the workflow's start node.
            hitl_responses: optional list of values to splice onto confirmation events. Index 0 answers the first pause, index 1 the second. When the queue runs dry mid-run the trace ends with `status='paused'` and lists unanswered requirements.

        Returns:
            A JSON object: `{run_id, status, output, error}` — short summary. Call `inspect_run(run_id)` for the full per-step trace.
        """
        if not workflow_id:
            workflow_id = session.workflow_id
        _check_same_workflow(session, workflow_id)
        staged = get_staged_for_run(session.session_id)
        if staged is None:
            return json.dumps({
                "ok": False,
                "issues": [Issue(
                    path="(run)",
                    code=IssueCode.INVALID_CONFIG,
                    message="session missing or expired",
                    hint="start a chat turn to create a fresh session",
                ).to_dict()],
            }, ensure_ascii=False)
        staged_nodes, staged_edges = staged
        # user_id threaded for LLM-preset / MCP scoping inside
        # the agent emitter (mirrors runtime_service._run_leg).
        from app.auth import CurrentUser as _CU  # noqa: F401
        trace = _run_workflow(
            staged_nodes,
            staged_edges,
            workflow_id=workflow_id,
            workflow_name=workflow_id,
            input=input,
            user_id=session.user_id,
            hitl_responses=hitl_responses,
        )
        return json.dumps({
            "run_id": trace.run_id,
            "status": trace.status,
            "output": trace.output,
            "error": trace.error,
        }, ensure_ascii=False)

    def inspect_run(run_id: str) -> str:
        """Return the structured trace for `run_id` as JSON. Includes per-step input / output / tool calls / durationMs / status, plus `pending_requirements` for paused runs.

        Returns `{"error": "unknown run_id ..."}` if the trace has been evicted or never existed. Traces are evicted FIFO at maxsize=50 — if you ran many test iterations, the older ones may be gone.
        """
        trace = _inspect_run(run_id)
        if trace is None:
            return json.dumps({
                "error": f"unknown run_id {run_id!r}",
                "hint": "call list_runs to see recent run ids",
            }, ensure_ascii=False)
        return json.dumps(trace, ensure_ascii=False)

    def explain_failure(run_id: str) -> str:
        """Attribute a failed run to a specific node / config. Runs diagnostic rules over the trace and returns `{diagnosis, suggested_fix, matched_rule}`. The first matching rule wins; rules cover the most common failure modes (selector undefined variable, loop missing body_target, tool source not wired, compile error).

        Use this AFTER `inspect_run(run_id)` — the diagnosis points at one node id; you can then call `get_graph_state` / `preview_workflow` to see the relevant config and propose a fix.
        """
        return json.dumps(_explain_failure(run_id), ensure_ascii=False)

    def list_runs() -> str:
        """List recent run summaries (run_id, workflow_id, status, started_at). Use this to find a run_id after a chat has called `run_workflow` several times."""
        return json.dumps(_list_runs(), ensure_ascii=False)

    return [
        Function.from_callable(add_node),
        Function.from_callable(update_node),
        Function.from_callable(remove_node),
        Function.from_callable(connect_nodes),
        Function.from_callable(disconnect),
        Function.from_callable(attach_tool),
        Function.from_callable(detach_tool),
        Function.from_callable(preview_workflow),
        Function.from_callable(plan_workflow),
        Function.from_callable(replace_workflow),
        Function.from_callable(get_node_types),
        Function.from_callable(get_graph_state),
        Function.from_callable(get_connection_rules),
        Function.from_callable(create_react_agent),
        Function.from_callable(create_router_pattern),
        Function.from_callable(create_retry_loop),
        Function.from_callable(run_workflow),
        Function.from_callable(inspect_run),
        Function.from_callable(explain_failure),
        Function.from_callable(list_runs),
    ]

# ─────────────────────────────────────────────────────────────────
# F4 — direct test shims (NOT for the LLM)
#
# `create_react_agent` / `create_router_pattern` / `create_retry_loop`
# above are closures over `session` — they're meant to run inside
# `_build_tools_for_session`. For tests that drive the pattern
# pipeline without going through the LLM tool surface, we expose
# thin wrappers that take `session` explicitly. The behaviour is
# identical: each one builds a plan and routes it through
# `_plan_workflow` so atomicity + structured Issue errors are
# preserved.
# ─────────────────────────────────────────────────────────────────
def _create_react_agent_via_wrapper(
    session: ChatSession,
    workflow_id: str,
    *,
    instructions: str,
    tools: list[dict],
    agent_id: str = "",
    agent_label: str = "",
    max_tool_calls: Optional[int] = None,
) -> str:
    try:
        plan = build_react_agent_plan(
            instructions=instructions,
            tools=tools,
            id=agent_id,
            label=agent_label,
            max_iterations=max_tool_calls,
        )
    except (ValueError, TypeError) as exc:
        return json.dumps({
            "ok": False,
            "issues": [Issue(
                path="(pattern)",
                code=IssueCode.INVALID_CONFIG,
                message=str(exc),
                hint="",
            ).to_dict()],
            "state_unchanged": True,
        }, ensure_ascii=False)
    return _plan_workflow(
        session, workflow_id,
        plan=pattern_plan_to_dict(plan),
    )

def _create_router_pattern_via_wrapper(
    session: ChatSession,
    workflow_id: str,
    *,
    branches: list[dict],
    selector_mode: str = "function",
    selector_expression: str = "",
    router_id: str = "",
    router_label: str = "",
    replace_existing: bool = False,
) -> str:
    try:
        plan = build_router_pattern_plan(
            branches=branches,
            selector_mode=selector_mode,
            selector_expression=selector_expression,
            id=router_id,
            label=router_label,
            delete_existing_router=replace_existing,
        )
    except (ValueError, TypeError) as exc:
        return json.dumps({
            "ok": False,
            "issues": [Issue(
                path="(pattern)",
                code=IssueCode.INVALID_CONFIG,
                message=str(exc),
                hint="",
            ).to_dict()],
            "state_unchanged": True,
        }, ensure_ascii=False)
    return _plan_workflow(
        session, workflow_id,
        plan=pattern_plan_to_dict(plan),
    )

def _create_retry_loop_via_wrapper(
    session: ChatSession,
    workflow_id: str,
    *,
    instructions: str,
    max_iterations: int = 3,
    end_condition: str = "",
    agent_id: str = "",
    agent_label: str = "",
    loop_id: str = "",
    loop_label: str = "",
) -> str:
    try:
        plan = build_retry_loop_plan(
            instructions=instructions,
            max_iterations=max_iterations,
            end_condition=end_condition,
            agent_id=agent_id,
            agent_label=agent_label,
            loop_id=loop_id,
            loop_label=loop_label,
        )
    except (ValueError, TypeError) as exc:
        return json.dumps({
            "ok": False,
            "issues": [Issue(
                path="(pattern)",
                code=IssueCode.INVALID_CONFIG,
                message=str(exc),
                hint="",
            ).to_dict()],
            "state_unchanged": True,
        }, ensure_ascii=False)
    return _plan_workflow(
        session, workflow_id,
        plan=pattern_plan_to_dict(plan),
    )

# ─────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────
def run_chat_turn(
    db: Session,
    workflow_id: str,
    messages: list[dict[str, Any]],
    user: CurrentUser,
    preset_id: Optional[str] = None,
) -> Iterator[BuilderEvent]:
    """Run one chat turn against the workflow.

    Yields `BuilderEvent` objects **in real time** as the LLM emits
    them — the SSE layer iterates the generator and ships each event
    to the client without buffering the whole response. The first
    event is always `BuilderStartEvent` with the session id; the
    last is either `BuilderCompletedEvent` or `BuilderErrorEvent`.

    Contract: the service ALWAYS yields a `start` event first so
    the client can correlate the session, even on the error path.

    Streaming contract. We call `agent.run(stream=True,
    stream_events=True)` and translate each `RunOutputEvent` into
    a `BuilderEvent`:
      * `ToolCallStartedEvent`  → `tool_call`
      * `ToolCallCompletedEvent`→ `tool_result` (no diff emission —
                                  the consolidated diff is yielded
                                  once at the end of the turn)
      * `ToolCallErrorEvent`    → `tool_result` (ok=False)
      * `RunContentEvent` (str) → `text(delta=true)` — shipped
                                  immediately per token so the chat
                                  shows the LLM "typing"
      * `Reasoning*Event`       → ignored (already covered by the
                                  upfront `thinking` event)
      * `RunCompletedEvent`     → loop ends; ONE consolidated
                                  `diff` (if pending_changes grew)
                                  + `completed`
      * `RunErrorEvent`         → ONE consolidated `diff` (so the
                                  user can apply partial changes)
                                  + `error`

    Backward compatibility. Tests stub `Agent.run` to return a
    `RunOutput` (non-iterable). When `agent.run` returns something
    that isn't iterable, we fall back to walking `out.messages` —
    keeping the existing test surface green while real agno runs
    benefit from the streaming path.

    `preset_id` — optional override forwarded by the chat UI when
    the user picked a non-default LLM in the header. When set,
    `_build_llm_model` validates it (must exist AND be accessible
    to the caller) and prefers it over the user's default preset.
    Invalid overrides silently fall back to the default — see
    `_build_llm_model` for the rationale.
    """
    session = _load_or_create_session(db, workflow_id, user)
    yield BuilderStartEvent(session_id=session.session_id)

    # F6 : reset the per-turn rejection budget at the
    # start of every turn. The session persists across turns (the
    # user can keep chatting), but the budget is per-turn — a
    # rejected call last turn shouldn't count against this turn.
    # The session field is intentionally on the dataclass (not
    # local to this generator) so diagnostic tools like
    # `explain_failure` can read it; we just zero it here.
    session.turn_rejection_count = 0
    # Reset the user-cancel flag too. Defensive — a fresh session
    # created via `_load_or_create_session` already defaults to False,
    # but if the session was reused (existing session in `_SESSIONS`
    # for this (workflow, user)), the flag from a previous cancel that
    # didn't reach this turn's `_consume_stream` would still be set.
    # Zero it now so the current turn can proceed without an
    # immediate break.
    session.cancel_requested = False

    try:
        model = _build_llm_model(db, user, preset_id_override=preset_id)
    except HTTPException as exc:
        yield BuilderErrorEvent(message=str(exc.detail))
        return

    user_messages = [m for m in messages if m.get("role") in ("user", "assistant")]
    latest_user = next(
        (m["content"] for m in reversed(user_messages) if m.get("role") == "user"),
        "",
    )
    if not latest_user:
        yield BuilderErrorEvent(message="No user message in the request")
        return

    # Build the LLM context. The agent sees the workflow's CURRENT
    # nodes/edges (the original snapshot — the API of the chat is
    # "what you see is what you edit"). The history is passed as
    # the canonical agno message list so the LLM remembers the
    # flow.
    #
    # F6 : the first lines surface the per-turn
    # rejection budget (initial 0, budget cap = REJECTION_BUDGET_PER_TURN)
    # plus the staged-payload size (imperative ops + plan ops
    # already accumulated). The LLM can plan against a concrete
    # ceiling instead of guessing how many calls it has left.
    pending_ops = len(session.pending_changes)
    staged_nodes = len(session.staged_nodes)
    staged_edges = len(session.staged_edges)
    workflow_context = render_workflow_context(
        session.original_nodes, session.original_edges,
    )
    context = (
        f"Budget: rejection_count=0/{REJECTION_BUDGET_PER_TURN}; "
        f"pending_ops={pending_ops}/{MAX_PENDING_CHANGES_PER_SESSION}; "
        f"staged={staged_nodes} nodes / {staged_edges} edges.\n"
        f"Workflow id: {workflow_id}\n"
        f"{workflow_context}\n"
        f"---\n"
        f"User's latest request: {latest_user}"
    )

    # Build the agent. We don't stream intermediate text — the
    # LLM tends to produce a single assistant turn per turn, so we
    # discard content and rely on the tool events for the diff.
    # If a turn the LLM produces text without any tool call, we
    # emit it as a `text` event so the user sees the explanation.
    yield BuilderThinkingEvent()
    try:
        out = _start_streaming_run(model, session, context)
    except json.JSONDecodeError as exc:
        # Anthropic SDK / agno sometimes raises JSONDecodeError when
        # the SSE buffer holds a partial JSON chunk — typical error
        # is "key must be a string at line 1 column 1439". These are
        # 95% transient (provider-side fragmentation, network hiccup)
        # so retry once before surfacing an error.
        log.warning(
            "chat builder: streaming run JSONDecodeError on first attempt "
            "(col %s) — retrying once: %s", exc.colno, exc,
        )
        try:
            out = _start_streaming_run(model, session, context)
        except json.JSONDecodeError as exc2:
            log.warning("chat builder: streaming retry also failed: %s", exc2)
            yield BuilderErrorEvent(message=_mid_stream_friendly_message())
            return
    except Exception as exc:
        yield BuilderErrorEvent(message=f"LLM call failed: {exc}")
        return

    # Two paths:
    #   1. agno returned an iterator (real streaming run) → drive
    #      `_consume_stream` and yield as we go.
    #   2. agno returned a `RunOutput` (test stub / non-streaming
    #      fallback) → walk `out.messages` and synthesize the same
    #      event sequence for parity.
    #
    # Mid-stream retry : both _consume_stream and
    # _consume_batch can hit a JSONDecodeError raised by the
    # Anthropic SDK's SSE parser mid-flight (typical: "key must be
    # a string at line 1 column N"). Unlike the top-level retry in
    # _start_streaming_run (which fires when `agent.run()` itself
    # raises), this one is for errors raised inside the for-loop
    # AFTER we've already yielded some events to the SSE response.
    # We retry the whole run once: the user sees the partial events
    # we already sent, a `BuilderRetryEvent` notice, then the new
    # attempt's events below. Duplicate `tool_call_id`s are deduped
    # by id (each id is fresh per LLM run, so collisions only happen
    # when the LLM legitimately replays the same call). The partial
    # diff (if any) is already on the wire — the user can apply it
    # even if the retry also fails.
    for attempt in range(2):
        try:
            if hasattr(out, "__iter__"):
                yield from _consume_stream(out, session, workflow_id)
            else:
                yield from _consume_batch(out, session, workflow_id)
            break  # success — done
        except json.JSONDecodeError as exc:
            if attempt == 1:
                # Both attempts failed — surface the friendly error.
                # The partial diff (if any) already went out from the
                # first attempt's _consume_stream handler, so the
                # user can still apply mid-turn work.
                log.warning(
                    "chat builder: mid-stream JSONDecodeError "
                    "survived retry (col %s): %s",
                    exc.colno, exc,
                )
                yield BuilderErrorEvent(message=_mid_stream_friendly_message())
                return
            log.warning(
                "chat builder: mid-stream JSONDecodeError (col %s) "
                "— retrying whole stream: %s",
                exc.colno, exc,
            )
            yield BuilderRetryEvent(reason=str(exc))
            try:
                out = _start_streaming_run(model, session, context)
            except json.JSONDecodeError as exc2:
                # The retry's own initial call also failed — bail
                # with the friendly message.
                log.warning(
                    "chat builder: retry initial call also "
                    "JSONDecodeError'd (col %s): %s",
                    exc2.colno, exc2,
                )
                yield BuilderErrorEvent(message=_mid_stream_friendly_message())
                return

# ─────────────────────────────────────────────────────────────────
# Streaming translation — agno RunOutputEvent → BuilderEvent
# ─────────────────────────────────────────────────────────────────
def _start_streaming_run(model, session, context: str):
    """Build an agno Agent for the builder chat and call
    `agent.run(stream=True, stream_events=True)`.

    Centralised so the chat turn can retry once on transient
    parser failures without rebuilding the agent. The Anthropic
    SDK occasionally fails to parse a partial SSE chunk
    (typically "key must be a string at line 1 column N"); agno
    wraps that as `ModelProviderError(...) from JSONDecodeError`,
    so the wrapper raises `ModelProviderError` to the caller —
    we unwrap it here and re-raise the original `JSONDecodeError`
    so the caller's `except json.JSONDecodeError` retry fires.
    See `chat_turn_stream` for the retry logic.

    The agno `Agent` import is deferred to here (not module top)
    because importing agno is non-trivial and the test suite stubs
    `Agent.run`, so we want the import surface as small as possible
    when this helper is patched out.
    """
    from agno.agent import Agent
    agent = Agent(
        model=model,
        instructions=BUILDER_SYSTEM_PROMPT(),
        markdown=False,
        tools=_build_tools_for_session(session),
    )
    try:
        return agent.run(context, stream=True, stream_events=True)
    except Exception as exc:
        # agno's `Claude._handle_api_error` wraps any non-API
        # exception (incl. JSONDecodeError from the SDK's response
        # parser) as `ModelProviderError(message=str(e)) from e`,
        # so the original `JSONDecodeError` lives on `__cause__`.
        # Unwrap so the caller's `except json.JSONDecodeError`
        # retry block fires.
        unwrapped = _unwrap_json_decode_error(exc)
        if unwrapped is not None:
            log.warning(
                "chat builder: unwrapping ModelProviderError → "
                "JSONDecodeError (col %s) for retry: %s",
                unwrapped.colno, unwrapped,
            )
            raise unwrapped from None
        raise

def _consume_stream(
    stream: Union[Iterator[Any], Any],
    session: ChatSession,
    workflow_id: str,
) -> Iterator[BuilderEvent]:
    """Drive a real `agent.run(stream=True)` iterator and emit
    `BuilderEvent`s as each agno event arrives.

    `ToolCallStartedEvent` lands first (carries the call id + args),
    followed by `ToolCallCompletedEvent` (carries the result). We
    pair them by `tool_call_id` and emit a `tool_call` /
    `tool_result` pair.

    The diff event is emitted EXACTLY ONCE at the end of the turn,
    not after every tool call. Per-turn tool calls tend to bunch up
    (the LLM's tools are narrow — one node per `add_node`, one
    patch per `update_node` — so a single user instruction often
    produces 5–10 tool calls). Emitting a `diff` after every call
    made the UI flicker and signalled to the user "you need to
    apply now" between every pair of calls. Folding them into one
    final diff keeps the chat looking like one logical round of
    work: tool calls → LLM's verbal summary → one diff card →
    one apply click. The session's `pending_changes` are still
    accumulated server-side throughout the turn, so a mid-turn
    apply (if the user clicks Apply while the LLM is still
    speaking) still commits everything collected so far.

    `RunContentEvent` is streamed as `text(delta=true)` per token
    so the chat shows the LLM "typing" — same ChatGPT-like feel.
    """
    # Track started calls so the completed handler can correlate
    # without trusting tool names alone (different model servers
    # may report the tool name differently on the two events).
    pending_calls: dict[str, dict[str, Any]] = {}
    errored_out = False
    # F0.4 : per-turn tool-call cap. Previously this
    # constant was only enforced in the `_consume_batch` fallback
    # path (which runs in tests), so the production streaming path
    # was uncapped — the LLM could `add_node` 30 times in a single
    # turn and the user would see 30 additions in one diff card.
    # We now bail out with a `BuilderErrorEvent` after the cap is
    # hit; we still yield any pending events so the user can apply
    # the partial work.
    calls_this_turn = 0

    # Local-import the agno event classes for isinstance checks.
    # Importing at module scope would force agno as a hard
    # dependency for tooling that imports this module only for
    # the apply/cancel endpoints.
    try:
        from agno.run.agent import (
            RunCompletedEvent,
            RunContentEvent,
            RunErrorEvent,
            ToolCallCompletedEvent,
            ToolCallErrorEvent,
            ToolCallStartedEvent,
        )
    except ImportError:  # pragma: no cover — agno missing
        yield BuilderErrorEvent(message="agno runtime not available")
        return

    # Mid-stream guard (P1, ): the Anthropic SDK's SSE
    # parser occasionally chokes on a partial chunk during iteration
    # (typical message: "key must be a string at line 1 column N").
    # The top-level unwrap in `_start_streaming_run` only catches
    # errors from the INITIAL `agent.run()` call — once we're inside
    # the for-loop, errors from `stream.__next__()` are NOT routed
    # through that try/except. We can't retry here without replaying
    # events the user has already seen, so we surface a friendly
    # error AND emit the partial diff (if any staged changes
    # landed before the parse failure) so the user can still apply
    # the LLM's half-finished work.
    try:
        for event in stream:
            # User-initiated cancel — the Stop button on the chat
            # composer aborts the client fetch AND sets
            # `session.cancel_requested` via `cancel_session`. We
            # check it on every event-yield so the LLM call on the
            # server stops within one event of the user clicking Stop
            # instead of running to natural completion. The partial
            # diff (if any) was already emitted by earlier tool
            # events, so the user can still apply whatever staged
            # before the cancel — same shape as the tool-call-cap
            # exit below.
            if session.cancel_requested:
                log.info(
                    "chat builder: cancel_requested — breaking out of stream "
                    "(session %s, workflow %s)",
                    session.session_id, workflow_id,
                )
                if session.pending_changes:
                    yield BuilderDiffEvent(
                        summary=_diff_summary(session),
                        nodes=_diff_full(session)[0],
                        edges=_diff_full(session)[1],
                    )
                yield BuilderCompletedEvent(output="(cancelled by user)")
                return
            if isinstance(event, ToolCallStartedEvent):
                t = getattr(event, "tool", None)
                if t is None:
                    continue
                tc_id = t.tool_call_id or f"call-{uuid.uuid4().hex[:8]}"
                args = _coerce_args(t.tool_args)
                if not args.get("workflow_id"):
                    args["workflow_id"] = workflow_id
                pending_calls[tc_id] = {
                    "tool": t.tool_name or "",
                    "args": args,
                }
                # F0.4: count IMPERATIVE tool calls toward the per-turn
                # cap. High-level tools (plan_workflow / replace_workflow
                # / create_* / read-only diagnostics) are exempt — each
                # one internally batches many operations, so charging it
                # against the cap would be the wrong unit. See
                # `HIGH_LEVEL_TOOLS` for the rationale.
                tool_name = t.tool_name or ""
                is_imperative = tool_name not in HIGH_LEVEL_TOOLS

                if is_imperative and calls_this_turn >= MAX_TOOL_CALLS_PER_TURN:
                    # Cap tripped: emit the partial diff + a friendly
                    # error, then BREAK out of the stream loop. The LLM
                    # must stop emitting tool calls; otherwise the
                    # mutations keep landing silently and the user sees
                    # an unexpected swarm of nodes in the apply card.
                    # (`continue` here would silently drain the rest of
                    # the iterator — that was the  production
                    # bug.)
                    if session.pending_changes:
                        yield BuilderDiffEvent(
                            summary=_diff_summary(session),
                            nodes=_diff_full(session)[0],
                            edges=_diff_full(session)[1],
                        )
                    yield BuilderErrorEvent(
                        message=(
                            f"tool-call cap reached ({MAX_TOOL_CALLS_PER_TURN} "
                            f"per turn); partial diff applied. NEXT TURN: use "
                            f"`plan_workflow(plan={{nodes: [...], edges: [...]}})` "
                            f"for batch edits — one call adds the whole "
                            f"remaining batch atomically. `add_node` + "
                            f"`connect_nodes` one-at-a-time will hit the same cap."
                        )
                    )
                    errored_out = True
                    break
                if is_imperative:
                    calls_this_turn += 1
                yield BuilderToolCallEvent(
                    tool_call_id=tc_id,
                    tool=t.tool_name or "",
                    args=args,
                )
            elif isinstance(event, ToolCallCompletedEvent):
                t = getattr(event, "tool", None)
                if t is None:
                    continue
                tc_id = t.tool_call_id or ""
                call = pending_calls.pop(tc_id, {})
                ok = not bool(getattr(t, "tool_call_error", None))
                # agno's Model wrapper yields a `ModelResponse` whose
                # `content` is the auto-generated timing string
                # `"<tool_name>(<args>) completed in <elapsed>s."`,
                # NOT our tool's actual return value. The real return
                # goes into `tool_executions[i].result` (carried on
                # this event as `event.tool.result`). For every chat
                # builder tool we want the LLM to see the actual
                # payload — `plan_workflow`'s `applied` counts, the
                # `add_node` config echo, etc — so prefer
                # `t.result` and fall back to `event.content` only
                # when it's missing.
                #
                # Prefer `t.result` over `event.content` so the
                # LLM sees an observable success signal. When
                # every tool result it observes is a timing
                # string, it falls back from `plan_workflow` to
                # `add_node` / `connect_nodes` because it can't
                # tell whether the previous tool call worked.
                message = _stringify_content(getattr(t, "result", None))
                if not message:
                    message = _stringify_content(event.content)
                yield BuilderToolResultEvent(
                    tool_call_id=tc_id,
                    tool=call.get("tool") or t.tool_name or "",
                    ok=ok,
                    message=message or ("ok" if ok else "error"),
                )
                # No per-tool-call diff emission: see `_consume_stream`
                # docstring. The consolidated diff is yielded once
                # after the stream loop ends.
            elif isinstance(event, ToolCallErrorEvent):
                t = getattr(event, "tool", None)
                tc_id = (t.tool_call_id if t else None) or f"call-{uuid.uuid4().hex[:8]}"
                call = pending_calls.pop(tc_id, {})
                err = getattr(event, "error", None) or "tool error"
                yield BuilderToolResultEvent(
                    tool_call_id=tc_id,
                    tool=call.get("tool") or (t.tool_name if t else "") or "",
                    ok=False,
                    message=str(err),
                )
            elif isinstance(event, RunContentEvent):
                # Stream text deltas in real time. Previously we
                # buffered all deltas and emitted one final text
                # event at run completion — that gave a 5–10 s UX
                # black hole for long responses (quicksort code,
                # prose paragraphs, etc.) where the user saw
                # nothing until the LLM had finished generating.
                # Now each delta is shipped immediately with
                # `delta=True` so the frontend can grow the text
                # bubble character by character — same feel as
                # ChatGPT's typing indicator.
                content = getattr(event, "content", None)
                if content and getattr(event, "content_type", "str") == "str":
                    yield BuilderTextEvent(content=str(content), delta=True)
            elif isinstance(event, RunErrorEvent):
                err = getattr(event, "content", None) or "LLM call failed"
                # Detect transient SSE-parser failures — agno catches
                # the JSONDecodeError from the Anthropic SDK and
                # surfaces it as RunErrorEvent.content (NOT a Python
                # exception that our mid-stream try/except could
                # catch). Without this branch, the user sees the
                # raw "key must be a string at line 1 column N"
                # message — confusing and non-actionable.
                #
                # Behaviour : preserve the partial diff
                # and raise a synthetic JSONDecodeError so the caller
                # (run_chat_turn) can retry the whole stream once.
                # Same code path as the mid-stream try/except — the
                # two error surfaces now converge on the same retry
                # policy.
                if _is_json_decode_message(str(err)):
                    log.warning(
                        "chat builder: RunErrorEvent carries "
                        "JSONDecodeError-shaped content "
                        "(col %s) — raising for retry: %s",
                        _extract_colno(str(err)), err,
                    )
                    if session.pending_changes:
                        yield BuilderDiffEvent(
                            summary=_diff_summary(session),
                            nodes=_diff_full(session)[0],
                            edges=_diff_full(session)[1],
                        )
                    # Synthetic JSONDecodeError so the caller's
                    # `except json.JSONDecodeError` retry block fires.
                    # `msg` carries the SDK's original error text so
                    # the retry path can log col / line for debugging;
                    # `doc=''` is fine — we never re-parse it.
                    raise json.JSONDecodeError(
                        str(err), "", 0
                    ) from None
                # Emit the consolidated diff BEFORE the error so the
                # user can still apply the partial changes that landed
                # before the failure. If we skipped it on the error
                # path, the diff card would vanish (the user would
                # think nothing was staged) and any chance to commit
                # the LLM's half-finished work would be lost.
                if session.pending_changes:
                    yield BuilderDiffEvent(
                        summary=_diff_summary(session),
                        nodes=_diff_full(session)[0],
                        edges=_diff_full(session)[1],
                    )
                yield BuilderErrorEvent(message=str(err))
                errored_out = True
                break
            # Reasoning*Event / RunStartedEvent / pre/post hooks —
            # intentionally ignored. The upfront `thinking` event
            # already covers reasoning; we don't need pre/post hooks
            # for the builder UX.
    except Exception as exc:
        # Mid-stream guard fires here when the SSE parser chokes on
        # a partial chunk (e.g. "key must be a string at line 1
        # column 364"). The top-level retry in `chat_turn_stream`
        # doesn't apply — by the time we reach this for-loop the
        # iterator is already mid-flight.
        #
        # Behaviour : preserve the partial diff so the
        # user can still apply mid-turn work, then RAISE the
        # unwrapped JSONDecodeError so `run_chat_turn` can retry the
        # whole stream once. The retry is silent from the user's POV
        # (the partial diff event already went out; a `BuilderRetryEvent`
        # tells the frontend to render a tiny "stream interrupted,
        # retrying…" notice). If the second attempt also dies, we
        # fall through to the friendly error message — but the user
        # typically doesn't see it because the first retry succeeds.
        unwrapped = _unwrap_json_decode_error(exc)
        if unwrapped is None:
            # Not a transient parse failure — propagate so the
            # outer code logs it as a real bug.
            raise
        log.warning(
            "chat builder: mid-stream JSONDecodeError (col %s) "
            "— preserving partial diff + raising for retry: %s",
            unwrapped.colno, unwrapped,
        )
        if session.pending_changes:
            yield BuilderDiffEvent(
                summary=_diff_summary(session),
                nodes=_diff_full(session)[0],
                edges=_diff_full(session)[1],
            )
        # Bubble up to run_chat_turn — it owns the retry policy.
        raise unwrapped from None

    if errored_out:
        return

    # Consolidated diff: emit ONE event for the entire turn so the
    # user sees a single diff card and clicks Apply once. Emitted
    # only when the LLM actually staged changes — empty turns (the
    # LLM just answered a question) skip it cleanly.
    if session.pending_changes:
        yield BuilderDiffEvent(
            summary=_diff_summary(session),
            nodes=_diff_full(session)[0],
            edges=_diff_full(session)[1],
        )

    yield BuilderCompletedEvent(output="")

def _consume_batch(
    out: Any,
    session: ChatSession,
    workflow_id: str,
) -> Iterator[BuilderEvent]:
    """Backward-compatible path: walk `RunOutput.messages` and
    synthesize the same event sequence a real streaming run would
    produce. Used when tests stub `Agent.run` to return a
    `RunOutput` (no `__iter__`) — keeps the existing test surface
    green without an event-driven rewrite.

    The order matches the streaming path:
        tool_call → tool_result → diff (per call)
        …
        text (final assistant turn)
        diff (if anything changed and not yet emitted)
        completed
    """
    messages = getattr(out, "messages", None) or []

    new_text = ""
    new_tool_calls: list[dict[str, Any]] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role != "assistant":
            continue
        content = getattr(m, "content", None)
        if content:
            if isinstance(content, str):
                new_text = content
            elif isinstance(content, list):
                text_parts = [
                    str(p.get("text", "")) for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                if text_parts:
                    new_text = "".join(text_parts)
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                fn = getattr(tc, "function", None) or tc
                name = (
                    getattr(fn, "name", None)
                    if not isinstance(tc, dict)
                    else tc.get("function", {}).get("name")
                )
                args_raw = (
                    getattr(fn, "arguments", None)
                    if not isinstance(tc, dict)
                    else tc.get("function", {}).get("arguments")
                )
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        args = {"_raw": args_raw}
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    args = {}
                new_tool_calls.append({
                    "tool_call_id": (
                        getattr(tc, "id", None) if not isinstance(tc, dict)
                        else tc.get("id") or f"call-{uuid.uuid4().hex[:8]}"
                    ) or f"call-{uuid.uuid4().hex[:8]}",
                    "tool": name,
                    "args": args,
                })

    #  fallback: surface the agent's top-level content
    # when the message walk produced nothing. Some model servers
    # (notably vLLM without `--enable-auto-tool-choice`) return
    # an error string in `out.content` instead of a real
    # assistant turn — without this fallback the user just sees
    # "thinking… Done." and has no idea the LLM call failed.
    if not new_text and not new_tool_calls:
        out_content = getattr(out, "content", None)
        if isinstance(out_content, str) and out_content.strip():
            new_text = out_content

    # Apply the same per-turn cap as the streaming path, but only
    # count IMPERATIVE tool calls (high-level tools are exempt —
    # see HIGH_LEVEL_TOOLS for the rationale). We keep all high-
    # level calls + the first N imperative calls; if the cap
    # tripped, drop the rest of the imperative calls.
    imperative_count = 0
    trimmed: list[dict[str, Any]] = []
    cap_dropped_imperative = False
    for tc in new_tool_calls:
        if tc["tool"] in HIGH_LEVEL_TOOLS:
            trimmed.append(tc)
        else:
            if imperative_count >= MAX_TOOL_CALLS_PER_TURN:
                cap_dropped_imperative = True
                continue
            trimmed.append(tc)
            imperative_count += 1
    new_tool_calls = trimmed
    cap_hit = cap_dropped_imperative

    # Emit text FIRST (before tool calls), matching the
    # historical batched behavior so existing tests don't have
    # to change. Streaming path emits text last; batched path
    # keeps the legacy order.
    if new_text:
        yield BuilderTextEvent(content=new_text)

    for tc in new_tool_calls:
        tool_name = tc["tool"]
        tool_args = tc["args"] or {}
        if not tool_args.get("workflow_id"):
            tool_args["workflow_id"] = workflow_id
        yield BuilderToolCallEvent(
            tool_call_id=tc["tool_call_id"],
            tool=tool_name,
            args=tool_args,
        )

        # Pair with the tool result message; respect
        # `tool_call_error` flag.
        ok = True
        result_message = ""
        for m in messages:
            if (
                getattr(m, "role", None) == "tool"
                and getattr(m, "tool_call_id", None) == tc["tool_call_id"]
            ):
                if getattr(m, "tool_call_error", None):
                    ok = False
                content = getattr(m, "content", None)
                if isinstance(content, str):
                    result_message = content
                elif isinstance(content, list):
                    text_parts = [
                        str(p.get("text", "")) for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    if text_parts:
                        result_message = "".join(text_parts)
                break

        yield BuilderToolResultEvent(
            tool_call_id=tc["tool_call_id"],
            tool=tool_name,
            ok=ok,
            message=result_message or ("ok" if ok else "error"),
        )

    if session.pending_changes:
        yield BuilderDiffEvent(
            summary=_diff_summary(session),
            nodes=_diff_full(session)[0],
            edges=_diff_full(session)[1],
        )

    if cap_hit:
        # Mirror the streaming path: cap-tripped turns end with
        # `error`, not `completed`. The user sees the partial diff
        # + the friendly hint and can apply or start a new turn.
        yield BuilderErrorEvent(
            message=(
                f"tool-call cap reached ({MAX_TOOL_CALLS_PER_TURN} "
                f"per turn); partial diff applied. NEXT TURN: use "
                f"`plan_workflow(plan={{nodes: [...], edges: [...]}})` "
                f"for batch edits — one call adds the whole "
                f"remaining batch atomically. `add_node` + "
                f"`connect_nodes` one-at-a-time will hit the same cap."
            )
        )
        return

    yield BuilderCompletedEvent(output="")

def _rejection_result(
    exc: "ToolCallRejected",
    session: Optional[ChatSession] = None,
) -> str:
    """F6  — format a `ToolCallRejected` for the
    LLM-facing tool result string.

    The LLM needs both the WHAT (message) and the NEXT STEP
    (hint). Without this helper, AGNO turns the exception into
    a single-line `str(exc)` and the structured hint is lost.
    `formatted()` already appends the hint line; this function
    is the bridge from the wrapped tool wrapper to AGNO's
    return-string contract.

    When `session` is provided and the per-turn rejection count
    has crossed the budget threshold, an extra "STOP — call a
    diagnostic" line is appended. The session path is optional
    so this helper remains a pure formatter for tests that
    don't have a session.
    """
    msg = exc.formatted()
    if session is not None:
        session.turn_rejection_count += 1
        budget_msg = _budget_exhausted_message(session)
        if budget_msg:
            msg = f"{msg}\n\n{budget_msg}"
    return msg

def _budget_exhausted_message(session: ChatSession) -> str:
    """Return the escalation hint when the per-turn rejection
    count crosses the budget, or `""` if still under budget.

    The hint points at three diagnostic tools:
      * `preview_workflow` — see current staged ids + topology
      * `explain_failure` — diagnose a recent run error
      * `get_node_types` — confirm a node-type's config schema
    """
    if session.turn_rejection_count <= REJECTION_BUDGET_PER_TURN:
        return ""
    return (
        f"You have hit {session.turn_rejection_count} consecutive "
        f"rejections this turn (budget is {REJECTION_BUDGET_PER_TURN}). "
        "STOP calling mutation tools and call one of these instead: "
        "`preview_workflow` (see current staged ids), `get_node_types` "
        "(confirm a node-type's schema), or `explain_failure` (if a "
        "run error is involved). Describe your goal in plain text to "
        "the user instead."
    )

def _coerce_args(raw: Any) -> dict[str, Any]:
    """Normalize tool_args from the streaming event into a dict.

    agno's `ToolExecution.tool_args` is normally a dict, but some
    model providers serialize it as a JSON string. Handle both
    shapes so the chat UI gets a consistent `args` payload.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"_raw": raw}
        except Exception:
            return {"_raw": raw}

# ─────────────────────────────────────────────────────────────────
# Arg-shape normalizers — LLM sometimes passes a JSON-encoded string
# where a typed dict/list is expected (e.g. plan_workflow(plan='{...}')
# instead of plan_workflow(plan={...})). agno introspects the function
# signature to build the JSON schema; we keep the typed annotations
# (`dict` / `list[dict]`) so the schema stays informative, but the
# per-handler body calls these helpers to accept either shape.
#
# Failure mode this prevents: Pydantic `validate_call` rejects the
# string with `Input should be a valid dictionary` BEFORE our code
# runs, so we never get a chance to surface a friendly error to the
# LLM — the tool just errors out and the user sees a raw SDK trace.
# ─────────────────────────────────────────────────────────────────
def _coerce_dict_arg(raw: Any, *, allow_none: bool = False) -> dict[str, Any]:
    """Accept `dict` (pass-through) or JSON `str` (decoded)."""
    if raw is None:
        if allow_none:
            return None  # type: ignore[return-value]
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    raise ToolCallRejected(
        tool="arg_coercion",
        message=f"Expected a JSON object, got {type(raw).__name__}: {raw!r}",
        code="INVALID_ARG_TYPE",
        hint=(
            "Pass the value as a JSON object literal, not a string. "
            "e.g. plan_workflow(plan={'nodes': [...], 'edges': [...]}), "
            "not plan_workflow(plan='{...}' as a string)."
        ),
    )

def _coerce_list_dict_arg(raw: Any) -> list[dict[str, Any]]:
    """Accept `list` (pass-through) or JSON `str` (decoded)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [dict(item) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [dict(item) for item in parsed]
        except Exception:
            pass
    raise ToolCallRejected(
        tool="arg_coercion",
        message=f"Expected a JSON array, got {type(raw).__name__}: {raw!r}",
        code="INVALID_ARG_TYPE",
        hint=(
            "Pass the value as a JSON array literal, not a string. "
            "e.g. replace_workflow(nodes=[{...}, {...}]), "
            "not replace_workflow(nodes='[...]' as a string)."
        ),
    )
    return {}

def _stringify_content(content: Any) -> str:
    """Convert a tool-result payload to a string for `tool_result.message`.

    agno's tool result content can be a plain string, a list of
    typed chunks, or a JSON-ish object. Render consistently so
    the chat thread doesn't show `{}` or `[]` for valid results.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        if parts:
            return "".join(parts)
    return str(content)

# ─────────────────────────────────────────────────────────────────
# Apply
# ─────────────────────────────────────────────────────────────────
def apply_pending_changes(
    db: Session,
    workflow_id: str,
    session_id: str,
    user: CurrentUser,
) -> Workflow:
    """Apply the session's pending changes to the DB row.

    Reads the LATEST workflow row (race-safe against edits from
    another tab), then re-applies every pending change in order
    against the freshly loaded state. Throws away the session on
    success.

    The pending changes are a *validated* list (each one was
    already checked when the LLM emitted it). We re-validate the
    final graph one more time as a defence-in-depth measure —
    Pydantic validation is the source of truth, not the LLM.
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Chat session not found or expired")
    if session.workflow_id != workflow_id or session.user_id != user.id:
        raise HTTPException(404, "Chat session not found or expired")
    member_service.require_role(db, workflow_id, user, "editor")

    # Re-load the workflow row to race-safely merge with latest
    # edits from another tab. Drop the session's stale staged
    # state and rewalk the change list against the current DB.
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(404, "Workflow not found")
    current_nodes = copy.deepcopy(row.nodes or [])
    current_edges = copy.deepcopy(row.edges or [])
    _apply_changes_to_state(current_nodes, current_edges, session.pending_changes)

    # Final validation. We DON'T raise 422 here — the chat
    # already validated each change incrementally; this is a
    # safety net. If it fails it usually means the workflow
    # changed externally in a way that conflicts with the
    # pending changes; surface a clear error so the user can
    # retry.
    try:
        for n in current_nodes:
            WorkflowNode.model_validate(n)
        for e in current_edges:
            WorkflowEdge.model_validate(e)
        validate_workflow(current_nodes, current_edges)
    except Exception as exc:
        raise HTTPException(
            422,
            f"workflow state changed incompatibly while chatting: {exc}",
        ) from exc

    # Commit. The `update_workflow` runs the same Pydantic +
    # connection-rule validation pipeline so we keep one
    # canonical write path.
    workflow_service.update_workflow(
        db,
        workflow_id,
        WorkflowUpdate(nodes=current_nodes, edges=current_edges),
        user=user,
    )
    discard_session(session_id)
    return row

def cancel_session(session_id: str, user: CurrentUser) -> None:
    """Discard a session without applying. Idempotent.

    Also flips the session's `cancel_requested` flag so any
    in-flight `_consume_stream` generator for this session breaks
    out on the next event-yield iteration. Without this, the LLM
    call on the server keeps running until the turn completes
    naturally even after the client has cut the fetch via the Stop
    button — wasted tokens, wasted latency.
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        return
    if session.user_id != user.id:
        return
    # Flip BEFORE discard so any concurrent _consume_stream iteration
    # on this session sees the flag immediately. `_consume_stream`
    # reads `session.cancel_requested` (not the dict), so the read
    # path stays consistent.
    session.cancel_requested = True
    discard_session(session_id)

def get_staged_for_run(session_id: str) -> Optional[tuple[list[dict], list[dict]]]:
    """Return `(staged_nodes, staged_edges)` for the chat session,
    or `None` if the session is missing.

    F5 : the chat builder's `run_workflow` tool needs
    to read "the staged graph" without going through the DB. This
    helper is the single source of truth — the run path injects
    this function rather than reaching into `_SESSIONS` directly,
    so unit tests can supply a synthetic session and F6's
    future redis-backed store can swap implementations cleanly.

    Returned lists are `copy.deepcopy`'d so the caller can mutate
    without disturbing the session's staged state — `run_workflow`
    builds a tmp workflow off these, never the session itself.
    """
    session = _SESSIONS.get(session_id)
    if session is None:
        return None
    return (
        copy.deepcopy(session.staged_nodes),
        copy.deepcopy(session.staged_edges),
    )

def apply_ops_to_state(
    nodes: list[dict],
    edges: list[dict],
    changes: list[dict[str, Any]],
) -> tuple[list[dict], list[dict]]:
    """F5  — pure functional version of
    `_apply_changes_to_state`.

    Returns NEW `(nodes, edges)` lists instead of mutating in
    place, so callers (`run_workflow`'s tmp-workflow construction)
    can build their snapshot without touching the source state.
    The original `_apply_changes_to_state` stays for the existing
    Apply path, which mutates `current_nodes` / `current_edges` to
    stay race-safe against a stale DB row.

    The op vocabulary is identical to `_apply_changes_to_state` —
    we share the docstring + the same defensive `else: continue`
    for unknown op kinds.
    """
    out_nodes = list(nodes)
    out_edges = list(edges)
    for change in changes:
        op = change.get("op")
        if op == "add_node":
            out_nodes.append(change["node"])
        elif op == "update_node":
            new_node = change["after"]
            for i, n in enumerate(out_nodes):
                if n["id"] == new_node["id"]:
                    out_nodes[i] = new_node
                    break
        elif op == "remove_node":
            node_id = change["node_id"]
            removed_edge_ids = set(change.get("removed_edge_ids") or [])
            out_edges = [e for e in out_edges if e["id"] not in removed_edge_ids]
            out_nodes = [n for n in out_nodes if n["id"] != node_id]
        elif op == "add_edge":
            out_edges.append(change["edge"])
        elif op == "remove_edge":
            out_edges = [e for e in out_edges if e["id"] != change["edge_id"]]
        elif op == "plan":
            # Plan is atomic by construction — replay as a full
            # overwrite so idempotence is preserved.
            out_nodes = [dict(n) for n in change.get("nodes") or []]
            out_edges = [dict(e) for e in change.get("edges") or []]
        else:
            # Defensive: skip unknown op kinds silently.
            continue
    return out_nodes, out_edges

def _apply_changes_to_state(
    nodes: list[dict],
    edges: list[dict],
    changes: list[dict[str, Any]],
) -> None:
    """Replay the pending change list against the given state.

    Mutates `nodes` and `edges` in place. Each change is one of
    the ops defined in `ChatSession.pending_changes`. The order
    is the order the LLM emitted them; for the imperative ops
    order matters only when the LLM is patching the same node
    twice (the second patch wins). For `op == "plan"` the change
    is the FULL POST-APPLY state — it replaces the in-progress
    nodes/edges entirely.

    F1 : `op == "plan"` was added for the Plan DSL
    path. A plan change is a single atomic "the result is THIS"
    record; replaying it just overwrites the cumulative in-progress
    state with the post-plan snapshot. If a turn contains both
    plan changes and imperative ones (the LLM can mix), the plan
    is replayed as a base reset and the imperative ops run after
    — exactly the same effect as if the LLM had issued them after
    the plan, because the plan was validated atomically.
    """
    for change in changes:
        op = change.get("op")
        if op == "add_node":
            nodes.append(change["node"])
        elif op == "update_node":
            new_node = change["after"]
            for i, n in enumerate(nodes):
                if n["id"] == new_node["id"]:
                    nodes[i] = new_node
                    break
        elif op == "remove_node":
            node_id = change["node_id"]
            # Cascade-remove edges as the LLM did; the LLM's
            # snapshot was validated at chat time, but the DB
            # may have moved on.
            removed_edge_ids = set(change.get("removed_edge_ids") or [])
            edges[:] = [e for e in edges if e["id"] not in removed_edge_ids]
            nodes[:] = [n for n in nodes if n["id"] != node_id]
        elif op == "add_edge":
            edges.append(change["edge"])
        elif op == "remove_edge":
            edges[:] = [e for e in edges if e["id"] != change["edge_id"]]
        elif op == "plan":
            # Replace the in-progress state with the plan's
            # post-apply snapshot. A plan is atomic by
            # construction — replaying it as a full overwrite
            # gives us idempotence (applying the same plan twice
            # yields the same final state) and the same race-
            # safety the imperative ops get: if the DB row
            # moved on between chat and apply, we start from
            # the latest row and replay the post-plan state
            # exactly once.
            nodes[:] = [dict(n) for n in change.get("nodes") or []]
            edges[:] = [dict(e) for e in change.get("edges") or []]
        else:
            # Defensive: unknown op kinds (e.g. legacy ops from
            # an old session that survived a deploy) are skipped
            # silently rather than crashing Apply. The chat
            # session is in-memory and short-lived, so an
            # unknown op means the LLM saw a stale tool schema
            # — recoverable on the next chat.
            continue
