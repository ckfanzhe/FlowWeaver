"""Unit + integration tests for `app.core.connection_rules`.

The validator is a pure function: it walks the nodes + edges and returns
a list of `ConnectionError`. These tests build minimal graphs directly
(no DB / no FastAPI) and assert the rule outcomes.

A few integration cases at the bottom exercise the FastAPI endpoints to
confirm the validator is wired in correctly (422 + structured
`detail.errors`).

NOTE: the workflow's input comes from `Workflow.run(input=...)` and the
output is the last Step's result. There are NO `input` or `output` node
types in the platform — neither maps to an agno primitive. The tests
below exercise only the 9 actual node types.
"""
from __future__ import annotations

import pytest

from app.core.connection_rules import (
    CONNECTION_RULES,
    EXECUTABLE_TYPES,
    NodeView,
    TOOL_SOURCE_TYPES,
    check_node_view,
    validate_connections,
    would_be_valid_connection,
)

# ─────────────────────────────────────────────────────────────────
# Small builders so each test reads as data, not boilerplate
# ─────────────────────────────────────────────────────────────────
def node(nid: str, ntype: str, **config) -> dict:
    """Build a node dict. Extra kwargs become `data.config`."""
    return {
        "id": nid,
        "type": ntype,
        "position": {"x": 0, "y": 0},
        "data": {"config": config} if config else {},
    }

def edge(eid: str, src: str, tgt: str) -> dict:
    return {"id": eid, "source": src, "target": tgt}

def error_codes(errors) -> list[str]:
    return [e.code for e in errors]

# ─────────────────────────────────────────────────────────────────
# 1. Pure-function sanity (no graph, no edges)
# ─────────────────────────────────────────────────────────────────
def test_empty_graph_is_valid():
    assert validate_connections([], []) == []

def test_minimal_valid_workflow():
    """A single agent with no edges is valid — the workflow's input comes
    from `Workflow.run(input=...)` and the output is the agent's result."""
    nodes = [node("a", "agent")]
    assert validate_connections(nodes, []) == []

# ─────────────────────────────────────────────────────────────────
# 2. Edge-level checks
# ─────────────────────────────────────────────────────────────────
def test_self_loop_is_rejected():
    nodes = [node("a", "agent")]
    edges = [edge("e1", "a", "a")]
    errs = validate_connections(nodes, edges)
    assert "selfLoop" in error_codes(errs)

def test_duplicate_edge_is_rejected():
    nodes = [node("a", "agent"), node("b", "agent")]
    edges = [
        edge("e1", "a", "b"),
        edge("e2", "a", "b"),  # duplicate
    ]
    errs = validate_connections(nodes, edges)
    assert "duplicateEdge" in error_codes(errs)

def test_edge_to_unknown_source_is_rejected():
    """Edge references a node id not in `nodes`."""
    nodes = [node("a", "agent"), node("b", "agent")]
    edges = [edge("e1", "ghost", "a"), edge("e2", "a", "b")]
    errs = validate_connections(nodes, edges)
    codes = error_codes(errs)
    # ghost source → incompatibleSource
    assert "incompatibleSource" in codes

def test_edge_to_unknown_target_is_rejected():
    nodes = [node("a", "agent")]
    edges = [edge("e1", "a", "ghost")]
    errs = validate_connections(nodes, edges)
    assert "incompatibleTarget" in error_codes(errs)

# ─────────────────────────────────────────────────────────────────
# 3. Tool-source isolation: tool (collapsed from tools/mcp/http) must NOT be wired by edge
# ─────────────────────────────────────────────────────────────────
# The three tool-source types (http/mcp/tools) collapsed into a
# single `tool` node. Parametrize over ['tool'] — the test still
# proves the invariant (no tool-source can sit on a flow edge).
@pytest.mark.parametrize("tool_type", ["tool"])
def test_tool_source_as_edge_source_is_rejected(tool_type):
    """A `tool` node must not have an outgoing edge — it's a tool
    source, configured via `cfg.toolsRef`, not via flow edges."""
    nodes = [
        node("t", tool_type),
        node("a", "agent"),
    ]
    edges = [
        edge("e1", "t", "a"),       # NOT ok — t cannot be an edge source
    ]
    errs = validate_connections(nodes, edges)
    codes = error_codes(errs)
    assert "incompatibleSource" in codes

# The three tool-source types (http/mcp/tools) collapsed into a
# single `tool` node. Parametrize over ['tool'] — the test still
# proves the invariant (no tool-source can sit on a flow edge).
@pytest.mark.parametrize("tool_type", ["tool"])
def test_tool_source_as_edge_target_is_rejected(tool_type):
    """Equally: nothing should connect TO a `tool` node."""
    nodes = [
        node("a", "agent"),
        node("t", tool_type),
    ]
    edges = [
        edge("e1", "a", "t"),       # NOT ok — t cannot be an edge target
    ]
    errs = validate_connections(nodes, edges)
    codes = error_codes(errs)
    assert "incompatibleTarget" in codes

def test_tool_source_in_isolation_is_valid():
    """A lone `tool` node attached to nothing is fine (it's just unused)."""
    nodes = [node("a", "agent"), node("t", "tool", source="function")]
    assert validate_connections(nodes, []) == []

# ─────────────────────────────────────────────────────────────────
# 4. agent / ask — max 1 outgoing (control_flow shares the gate shape)
# ─────────────────────────────────────────────────────────────────
def test_agent_max_one_outgoing():
    nodes = [
        node("a", "agent"),
        node("b", "agent"),
        node("c", "agent"),
    ]
    edges = [
        edge("e1", "a", "b"),
        edge("e2", "a", "c"),       # 2 outgoing from agent → tooManyOutgoing
    ]
    errs = validate_connections(nodes, edges)
    assert "tooManyOutgoing" in error_codes(errs)

def test_ask_max_one_outgoing():
    nodes = [
        node("h", "ask"),
        node("a", "agent"),
        node("b", "agent"),
    ]
    edges = [
        edge("e1", "h", "a"),
        edge("e2", "h", "b"),       # 2 outgoing from ask → tooManyOutgoing
    ]
    errs = validate_connections(nodes, edges)
    assert "tooManyOutgoing" in error_codes(errs)

# ─────────────────────────────────────────────────────────────────
# 5. router / parallel — unlimited outgoing (one per branch is OK)
# ─────────────────────────────────────────────────────────────────
def test_router_many_outgoing_ok():
    nodes = [
        node("r", "branch", mode="switch"),
        node("a", "agent"),
        node("b", "agent"),
        node("c", "agent"),
        node("d", "agent"),
    ]
    edges = [
        edge("e1", "r", "a"),
        edge("e2", "r", "b"),
        edge("e3", "r", "c"),
        edge("e4", "r", "d"),
    ]
    assert validate_connections(nodes, edges) == []

def test_parallel_many_outgoing_ok():
    """flow has `max_outgoing=None` — runtime walks `outgoing`."""
    nodes = [
        node("p", "flow"),
        node("a", "agent"),
        node("b", "agent"),
        node("c", "agent"),
    ]
    edges = [
        edge("e1", "p", "a"),
        edge("e2", "p", "b"),
        edge("e3", "p", "c"),
    ]
    assert validate_connections(nodes, edges) == []

# ─────────────────────────────────────────────────────────────────
# 6. condition — max 2 outgoing, min 1 (noThen)
# ─────────────────────────────────────────────────────────────────
# The prior `condition` connection-rule
# tests (max_outgoing=2, min_outgoing=1) were intentionally relaxed
# at the connection layer — both `branch` modes (`switch` and
# `if-else`) now share the lenient `router`-shaped rules
# (`max_outgoing=null, min_outgoing=0`). The if-else mode's strict
# outgoing bounds are enforced at the strategy / IR layer
# (`BranchStrategy._build_if_else` raises if no `then` target).
# See `shared/connection_rules.json::branch` for the design note.
#
# The equivalent strategy-layer tests live in
# `tests/test_strategies.py::TestBranchStrategy*`.

# ─────────────────────────────────────────────────────────────────
# 7. loop — max 1 outgoing; body via cfg.bodyTarget
# ─────────────────────────────────────────────────────────────────
def test_loop_max_one_outgoing():
    nodes = [
        node("l", "loop", bodyTarget="b"),
        node("b", "agent"),
        node("x", "agent"),
        node("y", "agent"),
    ]
    edges = [
        edge("e1", "l", "x"),
        edge("e2", "l", "y"),       # 2 outgoing from loop → tooManyOutgoing
    ]
    errs = validate_connections(nodes, edges)
    assert "tooManyOutgoing" in error_codes(errs)

def test_loop_body_via_edge_is_rejected():
    """If `cfg.bodyTarget` AND an outgoing edge point at the same node,
    the runtime would execute it twice (once at top level, once inside the
    loop). The validator must flag this so the UI prevents it."""
    nodes = [
        node("l", "loop", bodyTarget="b"),
        node("b", "agent"),
    ]
    edges = [
        edge("e1", "l", "b"),       # ← this is the problem: b is bodyTarget AND target of edge
    ]
    errs = validate_connections(nodes, edges)
    codes = error_codes(errs)
    assert "loopBodyViaEdge" in codes
    # Should also flag tooManyOutgoing? No — l has exactly 1 outgoing,
    # which is what the rule allows. The body-via-edge is the *extra*
    # problem.
    assert "tooManyOutgoing" not in codes

def test_loop_body_only_via_bodyTarget_is_valid():
    """The standard loop pattern: body wired via cfg.bodyTarget only.
    The loop has no outgoing edge — its body recursively runs the
    target, and after iteration the workflow ends with the body's
    last output (the loop's "post-loop continuation" is implicit)."""
    nodes = [
        node("l", "loop", bodyTarget="b"),
        node("b", "agent"),
    ]
    # Loop has no outgoing edge; body is owned by the loop via cfg.
    assert validate_connections(nodes, []) == []

def test_loop_with_no_bodyTarget_is_valid():
    """A loop without bodyTarget is technically still construct-able,
    even if it does nothing at runtime — that's the user's problem, not
    a connection-rule violation."""
    nodes = [
        node("l", "loop"),
    ]
    assert validate_connections(nodes, []) == []

# ─────────────────────────────────────────────────────────────────
# 8. Schema sanity — the rule table matches what tests assume
# ─────────────────────────────────────────────────────────────────
def test_rule_table_has_all_15_types():
    """If a new node type is added, the rule table MUST include it or
    every edge touching it will be flagged `incompatibleSource` /
    `incompatibleTarget`. This test exists to remind future maintainers.

    The legacy `steps` type joined the executable group, then
    `parallel` + `steps` collapsed into `flow`. The
    `router` + `condition` pair collapsed into `branch`, and
    http + mcp + tools collapsed into `tool`. The
    `human_input` rename became `ask`. The 5 presets
    (wikipedia / tavily_search /
    duckduckgo / calculator / arxiv_search) collapsed into the `tool`
    node's `preset` config discriminator — they no longer appear as
    separate rule table rows. 6 base types × edge rules remain.

    The set here mirrors `shared/connection_rules.json::groups.tool_source
    + groups.executable` — drift between this test and the JSON is
    caught by `scripts/check_connection_rules_consistency.py`.
    """
    expected = {
        "agent", "ask",
        "branch", "flow", "loop",
        # http + mcp + tools collapsed into `tool`. The
        # 5 presets collapsed into the
        # `tool` node's `preset` discriminator — no separate rules.
        "tool",
    }
    assert set(CONNECTION_RULES.keys()) == expected

def test_tool_source_types_match_executable_boundary():
    # The three tool-source types (http/mcp/tools) collapsed into a
    # single `tool` node. The 5 preset tool types collapsed
    # into the `tool` node's `preset` discriminator — they no
    # longer appear as separate `tool_source` entries.
    assert TOOL_SOURCE_TYPES == {"tool"}
    assert TOOL_SOURCE_TYPES.isdisjoint(EXECUTABLE_TYPES)

# ─────────────────────────────────────────────────────────────────
# 9. Multiple errors are all returned (not just the first)
# ─────────────────────────────────────────────────────────────────
def test_multiple_violations_are_all_reported():
    """Two tool-source edges — we should see both errors, not bail out
    at the first."""
    nodes = [
        node("t", "tool", source="function"),
        node("h", "tool", source="http"),
        node("a", "agent"),
    ]
    edges = [
        edge("e1", "t", "a"),       # tool as source
        edge("e2", "h", "a"),       # tool as source
    ]
    errs = validate_connections(nodes, edges)
    codes = error_codes(errs)
    assert codes.count("incompatibleSource") >= 2

# ─────────────────────────────────────────────────────────────────
# 10. Integration: where connection rules are enforced
# ─────────────────────────────────────────────────────────────────
#
# Saving a workflow is a DRAFT commit — connection rules (per-type
# constraints, tool-source isolation, degree bounds) must NOT block
# the save, otherwise a router mid-wiring (1 branch so far) or a
# freshly-added tool node would be un-saveable. The rules are
# enforced at runtime by `validate_workflow` and at code-export time
# by `generator.render_python`, where a malformed graph would
# actually break execution. These tests pin that contract: CRUD
# accepts the workflow; the export endpoint rejects it with a
# structured 422.

def _export_workflow(client, wf_id: str):
    """Helper: hit the export endpoint and return (status, body).

    The export endpoint returns plain-text Python on success, so
    `body` is just the raw bytes when status is 200. On 422 it's the
    JSON detail (string message)."""
    r = client.get(f"/api/v1/workflows/{wf_id}/export")
    return r.status_code, r.text

def test_create_with_tool_source_edge_succeeds_but_export_fails(client):
    """Saving a `tool → agent` edge must succeed (draft state), but
    the export must reject it because the runtime cannot execute it."""
    payload = {
        "name": "Bad",
        "nodes": [
            node("t", "tool", source="function"),
            node("a", "agent"),
        ],
        "edges": [
            edge("e1", "t", "a"),
        ],
    }
    r = client.post("/api/v1/workflows", json=payload)
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]

    status, body = _export_workflow(client, wf_id)
    assert status == 422, f"expected export to reject; got {status}: {body!r}"

def test_put_with_too_many_outgoing_succeeds_but_export_fails(client):
    """PUT replaces the workflow; too many outgoing edges is allowed
    at save time (drafts are drafty) but blocks code export."""
    create = client.post("/api/v1/workflows", json={"name": "V1"}).json()
    payload = {
        "name": "V2",
        "nodes": [
            node("a", "agent"),
            node("b", "agent"),
            node("c", "agent"),
        ],
        "edges": [
            edge("e1", "a", "b"),
            edge("e2", "a", "c"),       # too many outgoing from agent
        ],
    }
    r = client.put(f"/api/v1/workflows/{create['id']}", json=payload)
    assert r.status_code == 200, r.text

    status, body = _export_workflow(client, create["id"])
    assert status == 422, f"expected export to reject; got {status}: {body!r}"

def test_patch_with_dangling_edge_succeeds_but_export_fails(client):
    """PATCH that removes a node but leaves an edge pointing to it
    must save (autosave UX) but fail at export time."""
    create = client.post(
        "/api/v1/workflows",
        json={
            "name": "V1",
            "nodes": [node("a", "agent"), node("b", "agent")],
            "edges": [edge("e1", "a", "b")],
        },
    ).json()
    bad_nodes = [node("a", "agent")]
    r = client.patch(
        f"/api/v1/workflows/{create['id']}",
        json={"nodes": bad_nodes},
    )
    assert r.status_code == 200, r.text

    status, body = _export_workflow(client, create["id"])
    assert status == 422, f"expected export to reject; got {status}: {body!r}"

def test_import_json_with_tool_source_edge_returns_422(client):
    """POST /import-json with a violating workflow envelope → 422.

    The import path is the trusted boundary between an external JSON
    blob and our DB, so it stays strict — unlike the in-app CRUD
    endpoints where drafts need to save freely.
    """
    envelope = {
        "schemaVersion": "1.0",
        "kind": "agnobuilder.workflow",
        "exportedAt": "-13T00:00:00+00:00",
        "workflow": {
            "name": "imported bad",
            "nodes": [
                node("h", "tool", source="http"),
                node("a", "agent"),
            ],
            "edges": [
                edge("e1", "h", "a"),
            ],
        },
    }
    r = client.post(
        "/api/v1/workflows/import-json",
        json={"payload": envelope},
    )
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["detail"]["errors"]]
    assert "incompatibleSource" in codes

def test_self_loop_saves_but_export_fails(client):
    """A self-loop edge is a draft-time curiosity — the user might be
    dragging from a node back onto itself mid-edit. We save it, then
    reject at export when the runtime can't actually wire it."""
    payload = {
        "name": "Self-loop",
        "nodes": [node("a", "agent")],
        "edges": [edge("e1", "a", "a")],
    }
    r = client.post("/api/v1/workflows", json=payload)
    assert r.status_code == 201, r.text
    status, body = _export_workflow(client, r.json()["id"])
    assert status == 422, f"expected export to reject; got {status}: {body!r}"

def test_condition_with_three_outgoing_saves_but_export_fails(client):
    """A condition node with three outgoing branches exceeds its
    `max_outgoing=2` — save must succeed (drafts are drafty), but
    export rejects because agno's Condition only supports then/else."""
    payload = {
        "name": "Condition overflow",
        "nodes": [
            node("c", "branch", mode="if-else"),
            node("a", "agent"),
            node("b", "agent"),
            node("x", "agent"),
        ],
        "edges": [
            edge("e1", "c", "a"),
            edge("e2", "c", "b"),
            edge("e3", "c", "x"),
        ],
    }
    r = client.post("/api/v1/workflows", json=payload)
    assert r.status_code == 201, r.text
    status, body = _export_workflow(client, r.json()["id"])
    assert status == 422, f"expected export to reject; got {status}: {body!r}"

def test_valid_workflow_passes_create(client):
    """Sanity: a well-formed workflow still goes through."""
    payload = {
        "name": "OK",
        "nodes": [
            node("a", "agent"),
        ],
        "edges": [],
    }
    r = client.post("/api/v1/workflows", json=payload)
    assert r.status_code == 201

# ─────────────────────────────────────────────────────────────────
# JSON loader + group resolution
# ─────────────────────────────────────────────────────────────────
def test_json_rules_file_loads_successfully():
    """Connection-rules JSON exists and parses — the loader is the
    single source of truth for all connection rules."""
    import json
    from pathlib import Path

    from app.core.connection_rules import _RULES_PATH

    assert _RULES_PATH.exists(), f"missing {_RULES_PATH}"
    payload = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    assert "rules" in payload
    assert "groups" in payload

def test_all_6_node_types_have_rules():
    """Every node type the platform supports must have a JSON entry.

    There are 6 base types — agent + ask (renamed from
    human_input) + branch (collapsed from router+condition) +
    flow (collapsed from parallel+steps) + loop + tool (collapsed
    from http/mcp/tools plus the 5 preset tool types). Asserted
    as exact count so a future base-type addition is a deliberate
    test update rather than a silent drift. The 5 presets now
    route through the `tool` node's `preset` config discriminator
    and don't need their own rule table rows.
    """
    expected = {
        "agent", "ask",
        "branch", "flow", "loop",
        # http + mcp + tools collapsed into `tool`. The
        # 5 presets collapsed into the
        # `tool` node's `preset` discriminator — no separate rules.
        "tool",
    }
    assert set(CONNECTION_RULES.keys()) == expected

def test_group_references_resolve():
    """`@executable` and `@tool_source` expand to the expected sets."""
    from app.core.connection_rules import _load_groups

    groups = _load_groups()
    assert "executable" in groups
    assert "tool_source" in groups
    assert "agent" in groups["executable"]
    # `ask` now lives in `groups.control_flow`,
    # not `groups.executable`. Same dataflow semantics, separate group
    # so the rule tables can grow independently (control_flow is a
    # kind-level carve-out per the spec).
    assert "ask" in groups["control_flow"]
    assert "human_input" not in groups["executable"]
    # http + mcp + tools collapsed into `tool`. The 5 preset tool types collapsed
    # into the `tool` node's `preset` discriminator — they no
    # longer appear as separate `tool_source` group entries.
    assert groups["tool_source"] == frozenset({"tool"})

def test_executable_types_constant_matches_json():
    """EXECUTABLE_TYPES is derived from the JSON groups; it must
    agree with the rule table."""
    assert "agent" in EXECUTABLE_TYPES
    # The old `tools` (function-source tool) type is now
    # `tool` with `source='function'`.
    assert "tool" in TOOL_SOURCE_TYPES
    # EXECUTABLE and TOOL_SOURCE must be disjoint
    assert EXECUTABLE_TYPES.isdisjoint(TOOL_SOURCE_TYPES)

# ─────────────────────────────────────────────────────────────────
# Node-centric checker (NodeView)
# ─────────────────────────────────────────────────────────────────
def _view(type_, inputs=(), outputs=(), body_target=None) -> NodeView:
    return NodeView(
        type=type_,
        inputs=list(inputs),
        outputs=list(outputs),
        body_target=body_target,
    )

def test_node_view_minimal_valid():
    """A correct node view produces no errors."""
    views = {
        "a": _view("agent", outputs=["b"]),
        "b": _view("agent", inputs=["a"]),
    }
    assert check_node_view(views) == []

def test_node_view_self_loop_detected():
    views = {
        "a": _view("agent", outputs=["a"]),
    }
    assert "selfLoop" in error_codes(check_node_view(views))

def test_node_view_duplicate_edge_detected():
    """Same (src, tgt) appearing twice in `outputs` is a duplicate."""
    views = {
        "a": _view("agent", inputs=[], outputs=["b", "b"]),
        "b": _view("agent", inputs=["a", "a"]),
    }
    assert "duplicateEdge" in error_codes(check_node_view(views))

def test_node_view_degree_bounds_min_outgoing():
    """`branch` (lenient — matches prior `router` shape) requires 0
    outgoing. The strict if-else min_outgoing=1 constraint moved to
    `BranchStrategy._build_if_else`."""
    views = {
        "c": _view("branch"),
    }
    errs = check_node_view(views)
    # Branch is lenient — no min_outgoing at the connection layer.
    assert "noThen" not in error_codes(errs)
    assert error_codes(errs) == []

def test_node_view_degree_bounds_max_outgoing():
    """Agent has max 1 outgoing."""
    views = {
        "a": _view("agent", outputs=["o", "o2"]),
        "o": _view("agent"),
        "o2": _view("agent"),
    }
    assert "tooManyOutgoing" in error_codes(check_node_view(views))

def test_node_view_incompatible_target_type():
    """Tool-source as a target is rejected."""
    views = {
        "a": _view("agent", outputs=["t"]),
        "t": _view("tool"),
    }
    errs = check_node_view(views)
    assert "incompatibleTarget" in error_codes(errs)

def test_node_view_loop_body_via_edge():
    """If body_target is also in outputs, surface loopBodyViaEdge."""
    views = {
        "L": _view("loop", outputs=["body", "a"], body_target="body"),
        "body": _view("agent"),
        "a": _view("agent"),
    }
    assert "loopBodyViaEdge" in error_codes(check_node_view(views))

def test_node_view_matches_edge_view():
    """Node-view and edge-view checks must agree on the same graph.

    Construct a graph, then build both `(nodes, edges)` and
    `{id: NodeView}` representations and compare the resulting
    error codes. If they ever diverge, callers will be confused.
    """
    nodes = [
        node("a", "agent"),
        node("b", "agent"),
        node("c", "agent"),
    ]
    edges = [
        edge("e1", "a", "b"),
        edge("e2", "b", "c"),
    ]
    from_edge = set(error_codes(validate_connections(nodes, edges)))

    # Now build the NodeView from edges and check.
    views: dict[str, NodeView] = {}
    for n in nodes:
        views[n["id"]] = NodeView(type=n["type"], inputs=[], outputs=[])
    for e in edges:
        views[e["source"]].outputs.append(e["target"])
        views[e["target"]].inputs.append(e["source"])
    from_view = set(error_codes(check_node_view(views)))

    # Both views should accept the same valid graph.
    assert from_edge == set()
    assert from_view == set()

def test_node_view_unknown_type_is_rejected():
    """A node with an unknown type surfaces an error rather than crashing."""
    views = {
        "x": _view("mystery"),
    }
    errs = check_node_view(views)
    assert any("unknown type" in e.message for e in errs)

# ─────────────────────────────────────────────────────────────────
# Single-edge validator (drag-time / drop-time UX)
# ─────────────────────────────────────────────────────────────────
def test_wbvc_valid_candidate_returns_empty():
    """A legal candidate against an existing graph returns no errors."""
    nodes = [node("a", "agent"), node("b", "agent")]
    edges = []
    assert would_be_valid_connection("a", "b", nodes, edges) == []

def test_wbvc_valid_candidate_in_incomplete_graph():
    """The drag-time check must NOT surface workflow-level errors
    when the graph is incomplete (e.g. an agent has no outgoing yet
    because the user is mid-drag)."""
    nodes = [node("a", "agent")]
    edges = []
    candidate_tgt = node("a2", "agent")
    assert would_be_valid_connection("a", "a2", nodes + [candidate_tgt], edges) == []

def test_wbvc_does_not_surface_min_outgoing():
    """Drag-time check must NOT emit `missingOutgoing` for unrelated
    nodes (this was the original bug)."""
    nodes = [node("a", "agent")]
    edges = []
    candidate_tgt = node("b", "agent")
    errs = would_be_valid_connection("a", "b", nodes + [candidate_tgt], edges)
    assert "missingOutgoing" not in error_codes(errs)

def test_wbvc_self_loop_detected():
    nodes = [node("a", "agent")]
    edges = []
    assert "selfLoop" in error_codes(
        would_be_valid_connection("a", "a", nodes, edges)
    )

def test_wbvc_incompatible_target_type():
    """Drag FROM an agent TO a tool-source is rejected."""
    nodes = [node("a", "agent"), node("t", "tool", source="function")]
    edges = []
    assert "incompatibleTarget" in error_codes(
        would_be_valid_connection("a", "t", nodes, edges)
    )

def test_wbvc_incompatible_source_type():
    """Drag FROM a tool-source is rejected (tool-source has no outgoing)."""
    nodes = [node("t", "tool", source="function"), node("a", "agent")]
    edges = []
    assert "incompatibleSource" in error_codes(
        would_be_valid_connection("t", "a", nodes, edges)
    )

def test_wbvc_duplicate_edge_detected():
    nodes = [node("a", "agent"), node("b", "agent")]
    edges = [edge("e1", "a", "b")]
    assert "duplicateEdge" in error_codes(
        would_be_valid_connection("a", "b", nodes, edges)
    )

def test_wbvc_tooManyOutgoing_for_source():
    """Adding the candidate would push source over its max outgoing."""
    nodes = [
        node("a", "agent"),  # max_outgoing = 1
        node("b", "agent"),
        node("c", "agent"),
    ]
    edges = [
        edge("e1", "a", "b"),  # already has 1 outgoing
    ]
    errs = would_be_valid_connection("a", "c", nodes, edges)
    assert "tooManyOutgoing" in error_codes(errs)

def test_wbvc_tooManyIncoming_for_target():
    """Adding the candidate would push target over its max incoming.
    We use a tool-source node because it has max_incoming=0 (the only
    node type that does)."""
    nodes = [
        node("a", "agent"),
        node("t", "tool", source="function"),  # max_incoming = 0
    ]
    edges = []
    # But tools can't be a target at all — that fires first.
    # Verify the candidate is rejected with incompatibleTarget.
    errs = would_be_valid_connection("a", "t", nodes, edges)
    assert "incompatibleTarget" in error_codes(errs)

def test_wbvc_tooManyOutgoing_for_target():
    """branch (switch mode) has max_outgoing=None (unlimited), so we
    test max-outgoing on the source side instead via branch."""
    nodes = [
        node("r", "branch", mode="switch"),  # max_outgoing = None
        node("a", "agent"),
        node("a2", "agent"),
        node("a3", "agent"),
    ]
    edges = [
        edge("e1", "r", "a"),
        edge("e2", "r", "a2"),
    ]
    # branch has 2 outgoing already (max=None), so this should be fine.
    errs = would_be_valid_connection("r", "a3", nodes, edges)
    assert error_codes(errs) == []

def test_wbvc_unknown_node_id_is_rejected():
    """If source or target doesn't exist in the graph, the edge is invalid."""
    nodes = [node("a", "agent")]
    edges = []
    errs = would_be_valid_connection("a", "ghost", nodes, edges)
    codes = error_codes(errs)
    assert "incompatibleTarget" in codes or "incompatibleSource" in codes

def test_wbvc_empty_ids_are_no_op():
    """An empty source or target id returns no errors (no edge to check)."""
    nodes = [node("a", "agent")]
    assert would_be_valid_connection("", "a", nodes, []) == []
    assert would_be_valid_connection("a", "", nodes, []) == []

# ─────────────────────────────────────────────────────────────────
# 12. — `tool_attachment` edge kind 
#
# The `tool_attachment` kind lets tool-source nodes (`tools` / `http` /
# `mcp`) hand their definition to an agent. The legacy dataflow check
# (no kind → default "dataflow") MUST still reject `tools → agent`, and
# the new kind MUST accept it. Behaviour is kept tightly deterministic so
# the FE canvas can rely on it for drag-time dimming.
# ─────────────────────────────────────────────────────────────────
def te(eid: str, src: str, tgt: str, kind: str = "tool_attachment") -> dict:
    """`tool_attachment` edge helper — same shape as `edge()` but with
    an explicit kind. Saves each test from repeating the dict literal."""
    return {"id": eid, "source": src, "target": tgt, "kind": kind}

def test_tool_attachment_tools_to_agent_is_accepted():
    """A `tools → agent` edge with kind=tool_attachment is valid."""
    nodes = [node("t", "tool", source="function"), node("a", "agent")]
    edges = [te("e1", "t", "a")]
    assert validate_connections(nodes, edges) == []

def test_tool_attachment_http_to_agent_is_accepted():
    nodes = [node("h", "tool", source="http"), node("a", "agent")]
    edges = [te("e1", "h", "a")]
    assert validate_connections(nodes, edges) == []

def test_tool_attachment_mcp_to_agent_is_accepted():
    nodes = [node("m", "tool", source="mcp"), node("a", "agent")]
    edges = [te("e1", "m", "a")]
    assert validate_connections(nodes, edges) == []

def test_tool_attachment_agent_to_tools_is_rejected():
    """Agents can be a TARGET of tool_attachment but NOT a source."""
    nodes = [node("a", "agent"), node("t", "tool", source="function")]
    edges = [te("e1", "a", "t", "tool_attachment")]
    codes = error_codes(validate_connections(nodes, edges))
    assert "incompatibleSource" in codes

def test_tool_attachment_tools_to_tools_is_rejected():
    """Tool-source can't be the target of tool_attachment — only agents can."""
    nodes = [node("t1", "tool", source="function"), node("t2", "tool", source="function")]
    edges = [te("e1", "t1", "t2")]
    codes = error_codes(validate_connections(nodes, edges))
    # Tool→Tool: source `tools` allowed_target_types=["agent"], so target
    # `tools` is not in the allowed set.
    assert "incompatibleSource" in codes

def test_tool_attachment_one_tool_many_agents_is_accepted():
    """A single tools node fanning out to multiple agents (max_outgoing=null)."""
    nodes = [
        node("t", "tool", source="function"),
        node("a1", "agent"),
        node("a2", "agent"),
        node("a3", "agent"),
    ]
    edges = [
        te("e1", "t", "a1"),
        te("e2", "t", "a2"),
        te("e3", "t", "a3"),
    ]
    assert validate_connections(nodes, edges) == []

def test_legacy_tools_to_agent_default_still_rejected():
    """An edge WITHOUT a kind field defaults to dataflow, which still
    forbids tools → agent. Backward-compat anchor for phase callers."""
    nodes = [node("t", "tool", source="function"), node("a", "agent")]
    edges = [edge("e1", "t", "a")]
    codes = error_codes(validate_connections(nodes, edges))
    assert "incompatibleSource" in codes

def test_dataflow_and_tool_attachment_coexist_on_same_graph():
    """A workflow that mixes dataflow agent→agent edges with typed
    tool → agent edges validates both kinds independently. The two
    kinds share no degree counters."""
    nodes = [
        node("a1", "agent"),
        node("a2", "agent"),
        node("t", "tool", source="function"),
    ]
    edges = [
        edge("e_df1", "a1", "a2"),          # dataflow agent→agent
        te("e_ta1", "t", "a1"),             # tool_attachment tools→agent
        te("e_ta2", "t", "a2"),             # tool_attachment tools→agent
    ]
    assert validate_connections(nodes, edges) == []

def test_tool_attachment_wbvc_tools_to_agent_accepted():
    """Drag-time validator accepts a tool→agent candidate under
    kind='tool_attachment'."""
    nodes = [node("t", "tool", source="function"), node("a", "agent")]
    errs = would_be_valid_connection("t", "a", nodes, [], kind="tool_attachment")
    assert errs == []

def test_tool_attachment_wbvc_tools_to_agent_rejected_under_dataflow():
    """Same candidate under the legacy dataflow kind is still rejected."""
    nodes = [node("t", "tool", source="function"), node("a", "agent")]
    errs = would_be_valid_connection("t", "a", nodes, [], kind="dataflow")
    codes = error_codes(errs)
    assert "incompatibleSource" in codes