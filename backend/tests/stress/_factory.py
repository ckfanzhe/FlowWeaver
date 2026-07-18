"""Programmatic workflow builders for stress tests.

Why not just use JSON fixtures (like `tests/fixtures/workflows/*.json`)?
Because stress tests need to construct LARGE / COMPLEX workflows where
manually writing JSON is error-prone. These helpers generate JSON-shaped
dicts (the same shape `workflow_io.parse` produces) so we can hand them
to the existing `executor.execute()` / `harness.run_fixture()` paths
without any new testing surface.

Each builder returns a `(nodes, edges)` tuple ready to feed into
`executor.execute(workflow_id, workflow_nodes, workflow_edges, input)`.

Position layout
---------------
Nodes are placed on a grid: column 100*n for node n, all at y=0 by
default. This matches the existing fixtures' convention so the IR
topology is the focus, not the position arithmetic.
"""
from __future__ import annotations

from typing import Any

# ─────────────────────────────────────────────────────────────────
# Atomic node builders — one per node type
# ─────────────────────────────────────────────────────────────────
def agent_node(
    node_id: str,
    instructions: str = "You are a helpful assistant.",
    *,
    tools_ref: list[str] | None = None,
    position: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "position": {"x": position[0], "y": position[1]},
        "data": {
            "label": node_id,
            "config": {
                "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": ""},
                "instructions": instructions,
                "toolsRef": tools_ref or [],
                "markdown": False,
            },
        },
    }

def human_input_node(
    node_id: str,
    prompt: str,
    *,
    input_type: str = "text",
    choices: list[str] | None = None,
    position: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "human_input",
        "position": {"x": position[0], "y": position[1]},
        "data": {
            "label": node_id,
            "config": {
                "prompt": prompt,
                "inputType": input_type,
                "choices": choices or [],
            },
        },
    }

def parallel_node(
    node_id: str,
    branches: list[str],
    *,
    position: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "flow",
        "position": {"x": position[0], "y": position[1]},
        "data": {
            "label": node_id,
            "config": {"mode": "parallel", "branches": branches},
        },
    }

def loop_node(
    node_id: str,
    body_target: str,
    *,
    max_iterations: int = 3,
    end_condition: str = "",
    forward_iteration_output: bool = False,
    position: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "loop",
        "position": {"x": position[0], "y": position[1]},
        "data": {
            "label": node_id,
            "config": {
                "maxIterations": max_iterations,
                "endCondition": end_condition,
                "forwardIterationOutput": forward_iteration_output,
                "bodyTarget": body_target,
            },
        },
    }

def condition_node(
    node_id: str,
    condition: str = "always",
    *,
    else_target: str = "",
    position: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "condition",
        "position": {"x": position[0], "y": position[1]},
        "data": {
            "label": node_id,
            "config": {
                "condition": condition,
                "elseTarget": else_target,
            },
        },
    }

def router_node(
    node_id: str,
    condition: str = "",
    branches: list[str] | None = None,
    *,
    position: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "router",
        "position": {"x": position[0], "y": position[1]},
        "data": {
            "label": node_id,
            "config": {
                "condition": condition,
                "branches": branches or [],
            },
        },
    }

# ─────────────────────────────────────────────────────────────────
# Edge builders
# ─────────────────────────────────────────────────────────────────
def edge(
    source: str,
    target: str,
    *,
    source_handle: str | None = None,
    target_handle: str | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": f"{source}->{target}",
        "source": source,
        "target": target,
    }
    if source_handle is not None:
        e["sourceHandle"] = source_handle
    if target_handle is not None:
        e["targetHandle"] = target_handle
    return e

# ─────────────────────────────────────────────────────────────────
# Composite builders — common stress-test shapes
# ─────────────────────────────────────────────────────────────────
def linear_chain(node_ids: list[str], *, with_agent_per_id: bool = True) -> tuple[list[dict], list[dict]]:
    """`A → B → C → ...` — simple sequential chain.

    Each node becomes either an agent (default) or a passthrough if
    `with_agent_per_id=False` (in which case the caller supplies custom
    nodes via `replace_nodes`). Most stress tests want agents.
    """
    nodes = [
        agent_node(nid, instructions=f"Step {nid}") for nid in node_ids
    ] if with_agent_per_id else []
    edges = [edge(node_ids[i], node_ids[i + 1]) for i in range(len(node_ids) - 1)]
    return nodes, edges

def workflow(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pass-through helper for symmetry: most callers want to write
    `nodes, edges = workflow(my_nodes, my_edges)`."""
    return nodes, edges