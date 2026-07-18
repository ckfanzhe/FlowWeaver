"""F4  — high-level pattern primitives for the chat builder.

Three L3-layer tools that compose common workflow topologies the
LLM would otherwise have to assemble via 5-10 imperative tool
calls. Each one builds a `WorkflowPlan` internally and routes it
through the same `_plan_workflow` snapshot / validate / commit
path — atomicity + structured Issue errors are inherited.

  * `create_react_agent` — agent + named tool sources wired in.
    The classic "agent with N tools" topology. Idempotent on the
    tool side (calling with the same tool list twice is a no-op).

  * `create_router_pattern` — router + N named branches, each a
    target node. Pads branch count down to ≤ `max_branches` (router
    has no hard cap, but the LLM shouldn't blow up the palette).

  * `create_retry_loop` — loop that wraps an agent, retrying up to
    `max_iterations` times. The agent's id becomes `loop.body_target`
    so the runtime knows which step to re-run.

Why three, not more. Each pattern pins a SPECIFIC topology the
LLM gets wrong most often in practice. Higher-level patterns
("RAG pipeline", "supervisor agent") are too project-specific —
their shape varies by user requirement. The three above are
broadly useful, well-defined, and don't impose a template the
user has to wrestle with.

The patterns share a single execution pipeline. They differ only
in how they generate the plan — each pattern has a `_build_plan`
helper that emits a `WorkflowPlan` (nodes + edges + delete lists).
The session-bound handler passes that plan to `_plan_workflow`
without modification, so every F4 tool inherits:
  * atomicity (no partial state on failure),
  * structured Issue errors (Pydantic + connection + graph rules),
  * config_echo for every added/updated node.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from app.core.node_types import NODE_TYPES as MANIFEST_NODE_TYPES
from app.services.chat_builder_plan import (
    Issue,
    IssueCode,
    PlanEdge,
    PlanNode,
    WorkflowPlan,
)

# ─────────────────────────────────────────────────────────────────
# Helpers — id generation, manifest lookup
# ─────────────────────────────────────────────────────────────────
def _new_node_id(prefix: str) -> str:
    """Generate a unique node id. Uses a short random suffix so
    repeated LLM calls produce stable-enough ids (collisions
    across a single chat turn are vanishingly unlikely; we still
    let `_atomic_stage` raise on the rare duplicate)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"

def _validate_node_type(node_type: str) -> None:
    """Defensive gate. Thin wrapper around `core.validate.known_node_type`
    (row L, ) — kept as a private alias so the rest of
    this module doesn't change.
    """
    from app.core.validate import known_node_type
    known_node_type(node_type)

# ─────────────────────────────────────────────────────────────────
# create_react_agent — agent + named tool sources
# ─────────────────────────────────────────────────────────────────
def build_react_agent_plan(
    *,
    instructions: str,
    tools: list[dict[str, Any]],
    id: str = "",
    label: str = "",
    max_iterations: Optional[int] = None,
) -> WorkflowPlan:
    """Build the `WorkflowPlan` for a "react agent with tool sources".

    `tools` is a list of `{type, config?}` dicts. Each entry
    becomes one tool-source node wired to the agent with a
    `kind='tool_attachment'` edge. The agent's id (and the tool
    ids) are generated if not provided.

    Args:
        instructions: the agent's `instructions` field.
        tools: list of `{type, id?, config?, label?}` dicts.
        id: optional explicit agent id; one is generated if empty.
        label: optional agent label; defaults to the id.
        max_iterations: when set, the agent config carries
            `toolCallLimit` so the runtime caps tool-call volume.
            None leaves the field at its default (unlimited).

    Returns:
        A `WorkflowPlan` ready to be fed to `_plan_workflow`.
        Does NOT mutate any session; that's the caller's job.
    """
    agent_id = id or _new_node_id("agent")
    plan_nodes: list[PlanNode] = []
    plan_edges: list[PlanEdge] = []
    agent_label = label or agent_id
    agent_config: dict[str, Any] = {"instructions": instructions}
    if max_iterations is not None:
        agent_config["toolCallLimit"] = max_iterations
    plan_nodes.append(PlanNode(
        id=agent_id,
        type="agent",
        data={"label": agent_label, "config": agent_config},
    ))
    for entry in tools:
        if not isinstance(entry, dict):
            raise ValueError(
                f"tools entries must be objects, got {type(entry).__name__}"
            )
        tool_type = entry.get("type")
        if not tool_type:
            raise ValueError(
                "tool entry must specify 'type' (one of "
                f"{sorted(MANIFEST_NODE_TYPES)})"
            )
        _validate_node_type(tool_type)
        tool_id = entry.get("id") or _new_node_id(tool_type)
        tool_label = entry.get("label") or tool_id
        tool_config = dict(entry.get("config") or {})
        plan_nodes.append(PlanNode(
            id=tool_id,
            type=tool_type,
            data={"label": tool_label, "config": tool_config},
        ))
        # Wire tool → agent. `kind='tool_attachment'` is the magic
        # that the connection-rule table accepts while rejecting
        # these as dataflow edges (the tool source has zero
        # dataflow degree).
        plan_edges.append(PlanEdge(
            source=tool_id, target=agent_id, kind="tool_attachment",
        ))
    return WorkflowPlan(nodes=plan_nodes, edges=plan_edges)

# ─────────────────────────────────────────────────────────────────
# create_router_pattern — router + N branches
# ─────────────────────────────────────────────────────────────────
def build_router_pattern_plan(
    *,
    branches: list[dict[str, Any]],
    selector_mode: str = "function",
    selector_expression: str = "",
    id: str = "",
    label: str = "",
    delete_existing_router: bool = False,
) -> WorkflowPlan:
    """Build the `WorkflowPlan` for a router with named branches.

    `branches` is a list of `{type, id?, config?, label?}` dicts;
    each becomes a downstream node. The router's `branches` config
    mirrors the target ids + labels, and edges wire the router to
    each branch (dataflow edges).

    Args:
        branches: list of `{type, id?, config?, label?}` dicts.
            Order matters — branch N's `BranchTarget.condition` is
            set to `""` for now; the runtime's selector drives the
            actual choice.
        selector_mode: `"function"` | `"cel"` | `"hitl"`. Maps to
            `RouterNodeConfig.selector.mode`. The matching
            `selector_expression` is the CEL or function source.
        selector_expression: when mode is `"cel"` or `"function"`,
            the source string. Empty when mode is `"hitl"`.
        id: optional explicit router id; one is generated if empty.
        label: optional router label; defaults to the id.
        delete_existing_router: when True, the plan deletes the
            previous router (if any) so this call replaces an
            existing router topology cleanly. Used by
            `update_router_pattern`.

    Returns:
        A `WorkflowPlan` ready to be fed to `_plan_workflow`.
    """
    router_id = id or _new_node_id("router")
    router_label = label or router_id
    branch_targets: list[dict[str, Any]] = []
    plan_nodes: list[PlanNode] = [
        PlanNode(
            id=router_id,
            # Emit the new `branch` type with `mode='switch'` (the
            # N-ary routing shape carried over from `router`).
            # `build_condition_pattern_plan` does the same with
            # `mode='if-else'`.
            type="branch",
            data={
                "label": router_label,
                "config": {
                    "mode": "switch",
                    "selector": {
                        "mode": selector_mode,
                        "expression": selector_expression,
                    },
                    "branches": branch_targets,
                },
            },
        ),
    ]
    plan_edges: list[PlanEdge] = []
    delete_nodes: list[str] = []
    if delete_existing_router:
        delete_nodes.append(router_id)
    for entry in branches:
        if not isinstance(entry, dict):
            raise ValueError(
                f"branch entries must be objects, got {type(entry).__name__}"
            )
        branch_type = entry.get("type")
        if not branch_type:
            raise ValueError(
                "branch entry must specify 'type' (one of "
                f"{sorted(MANIFEST_NODE_TYPES)})"
            )
        _validate_node_type(branch_type)
        branch_id = entry.get("id") or _new_node_id(branch_type)
        branch_label = entry.get("label") or branch_id
        branch_config = dict(entry.get("config") or {})
        plan_nodes.append(PlanNode(
            id=branch_id,
            type=branch_type,
            data={"label": branch_label, "config": branch_config},
        ))
        branch_targets.append({
            "label": branch_label,
            "target": branch_id,
            "condition": None,  # F7: default to None — field is unused at runtime
        })
        plan_edges.append(PlanEdge(
            source=router_id, target=branch_id, kind="dataflow",
        ))
    return WorkflowPlan(
        nodes=plan_nodes, edges=plan_edges,
        delete_nodes=delete_nodes,
    )

# ─────────────────────────────────────────────────────────────────
# create_retry_loop — loop wrapping an agent
# ─────────────────────────────────────────────────────────────────
def build_retry_loop_plan(
    *,
    instructions: str,
    max_iterations: int = 3,
    end_condition: str = "",
    agent_id: str = "",
    agent_label: str = "",
    loop_id: str = "",
    loop_label: str = "",
) -> WorkflowPlan:
    """Build the `WorkflowPlan` for an agent wrapped in a retry loop.

    The agent is the loop's body. The loop's `body_target` is set
    to the agent's id so the runtime knows which step to re-run.

    Args:
        instructions: the agent's `instructions`.
        max_iterations: bounded 1..1000 (Pydantic enforces; the
            caller doesn't have to validate).
        end_condition: optional substring-match early-exit. Empty
            means "always run max_iterations".
        agent_id: optional explicit agent id; one is generated if
            empty.
        agent_label: optional agent label.
        loop_id: optional explicit loop id; one is generated.
        loop_label: optional loop label.

    Returns:
        A `WorkflowPlan` with the loop + agent + the wiring
        between them.
    """
    agent_id = agent_id or _new_node_id("agent")
    loop_id = loop_id or _new_node_id("loop")
    agent_label = agent_label or agent_id
    loop_label = loop_label or loop_id
    plan_nodes = [
        PlanNode(
            id=loop_id,
            type="loop",
            data={
                "label": loop_label,
                "config": {
                    "maxIterations": max_iterations,
                    "endCondition": end_condition,
                    "bodyTarget": agent_id,
                },
            },
        ),
        PlanNode(
            id=agent_id,
            type="agent",
            data={
                "label": agent_label,
                "config": {"instructions": instructions},
            },
        ),
    ]
    # The agent doesn't connect to the loop via a bare dataflow
    # edge — the loop's `body_target` carries that signal. The
    # connection-rule table explicitly forbids `loopBodyViaEdge`
    # (we surface a structured `IssueCode` when an LLM tries the
    # old pattern). So no edges here.
    return WorkflowPlan(nodes=plan_nodes, edges=[])

# ─────────────────────────────────────────────────────────────────
# Tool wrappers — the LLM-facing handlers
# ─────────────────────────────────────────────────────────────────
def pattern_plan_to_dict(plan: WorkflowPlan) -> dict[str, Any]:
    """Convert a `WorkflowPlan` to the dict shape
    `_plan_workflow` expects."""
    return json.loads(plan.model_dump_json())

__all__ = [
    "build_react_agent_plan",
    "build_router_pattern_plan",
    "build_retry_loop_plan",
    "pattern_plan_to_dict",
]