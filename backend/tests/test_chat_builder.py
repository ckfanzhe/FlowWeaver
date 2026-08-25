"""Tests for the LLM-driven workflow-creation chat
(`app.services.chat_builder_service`).

Two layers of tests:

1. **Service-layer (tool handler) tests** — drive the tool handlers
   directly. No LLM in the loop, so we don't need a stubbed
   `Claude.invoke`. These pin the contract that the LLM relies on:
     * `add_node` / `update_node` / `remove_node` / `connect_nodes` /
       `disconnect` / `preview_workflow` mutate the staged state.
     * Pydantic per-node validation runs on every mutation.
     * Graph validation (no cycle, no orphan, connection rules) runs
       after every mutation.
     * Invalid mutations raise `ToolCallRejected` with a clear message
       and DO NOT mutate the staged state.
     * For `remove_node`, edges touching the node cascade.

2. **HTTP / SSE tests** — drive the API endpoint with a
   deterministic `agent.run` stub. The stub returns a fake
   `RunOutput` with a single tool call; the test asserts the SSE
   stream carries the expected `BuilderEvent` sequence and the
   apply endpoint commits the diff to the DB row.

The HTTP tests rely on `seeded_default_preset` (see `conftest.py`)
for the LLM preset, but they don't need a real LLM — they
monkeypatch `Agent.run` to return a canned tool-call payload.
"""
from __future__ import annotations

import copy
import json
import uuid

import pytest

from app.auth import CurrentUser
from app.db.models import User, Workflow
from app.services import chat_builder_service as cbs

USER_ID = "alice@example.com"

# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def user(db) -> CurrentUser:
    """Stand in for the FastAPI dependency — materials a real
    `users` row so `member_service.bootstrap_owner` keeps working."""
    db.add(User(id=USER_ID, tenant_id="tenant-default"))
    db.commit()
    return CurrentUser(id=USER_ID, tenant_id="tenant-default")

@pytest.fixture()
def empty_workflow(db, user) -> Workflow:
    """A workflow with a single agent node. The chat builder adds
    the `alice` member so editor checks pass."""
    wid = f"wf-{uuid.uuid4().hex[:8]}"
    db.add(Workflow(
        id=wid,
        name="seed",
        description="seed",
        nodes=[{
            "id": "a1",
            "type": "agent",
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": "A1", "config": {"instructions": ""}},
        }],
        edges=[],
        created_by=USER_ID,
    ))
    db.commit()
    from app.services import member_service
    member_service.bootstrap_owner(db, wid, USER_ID)
    db.commit()
    return db.query(Workflow).filter_by(id=wid).one()

# ─────────────────────────────────────────────────────────────────
# Tool-handler tests (no LLM)
# ─────────────────────────────────────────────────────────────────
def test_add_node_stages_and_validates(db, user, empty_workflow):
    """Adding a valid agent node mutates the staged state and
    the running Pydantic + graph validation passes."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    msg = cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100.0, "y": 200.0},
        label="Second",
        config={"instructions": "Help me."},
    )
    out = json.loads(msg)
    assert out["added"] == "a2"
    assert out["type"] == "agent"
    # Staged state has 2 nodes; the original still has 1 (untouched).
    assert len(session.staged_nodes) == 2
    assert session.staged_nodes[-1]["id"] == "a2"
    assert session.original_nodes[0]["id"] == "a1"
    # The pending change is recorded.
    assert len(session.pending_changes) == 1
    assert session.pending_changes[0]["op"] == "add_node"

def test_add_node_rejects_unknown_type(db, user, empty_workflow):
    """The platform rejects unknown node types so the LLM gets
    a clear error and can self-correct."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._add_node(
            session,
            workflow_id=empty_workflow.id,
            type="unicorn",
            id="x",
            position={"x": 0, "y": 0},
            config={},
        )
    assert "unknown node type" in ei.value.message
    # Staged state is unchanged on rejection.
    assert len(session.staged_nodes) == 1
    assert session.pending_changes == []

def test_add_node_rejects_duplicate_id(db, user, empty_workflow):
    """Adding a node with an id that already exists is rejected."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._add_node(
            session,
            workflow_id=empty_workflow.id,
            type="agent",
            id="a1",  # already exists
            position={"x": 0, "y": 0},
            config={},
        )
    assert "already exists" in ei.value.message
    assert len(session.staged_nodes) == 1

# ─────────────────────────────────────────────────────────────────
# F0.2 — copy-on-write contract 
#
# Every rejected tool call MUST leave the session's staged state
# exactly as it found it AND must NOT append a `pending_change`.
# Previously the tools mutated-then-validated; a rejected call left
# invalid staged state behind, which made every subsequent Apply
# fail with HTTP 422 "workflow state changed incompatibly while
# chatting" — even though the user hadn't done anything wrong.
# ─────────────────────────────────────────────────────────────────
def test_add_node_rollback_leaves_staged_untouched(db, user, empty_workflow):
    """A rejected `add_node` (here: invalid config) must NOT mutate
    the staged state and must NOT append a pending_change. This
    is the F0.2 contract: the next valid tool call sees the same
    state the LLM was working from."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    before_nodes = copy.deepcopy(session.staged_nodes)
    before_changes = list(session.pending_changes)
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._add_node(
            session,
            workflow_id=empty_workflow.id,
            type="agent",
            id="bad",
            position={"x": 0, "y": 0},
            # `instructions` is required for an agent — leaving it
            # out and passing a non-dict triggers Pydantic coercion.
            config={"instructions": 12345},  # wrong type
        )
    assert "invalid" in ei.value.message.lower()
    assert session.staged_nodes == before_nodes
    assert session.pending_changes == before_changes

def test_connect_nodes_duplicate_edge_rollback(db, user, empty_workflow):
    """A second `connect_nodes` for the same pair must NOT
    mutate staged state. Pre-F0.2 the second call appended the
    edge BEFORE running the duplicate check, leaving the staged
    graph with two identical edges."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(session, workflow_id=empty_workflow.id, source="a1", target="a2")
    before_edges = copy.deepcopy(session.staged_edges)
    before_changes = list(session.pending_changes)
    with pytest.raises(cbs.ToolCallRejected):
        cbs._connect_nodes(session, workflow_id=empty_workflow.id, source="a1", target="a2")
    assert session.staged_edges == before_edges
    assert session.pending_changes == before_changes

def test_connect_nodes_graph_rule_violation_rollback(db, user, empty_workflow):
    """An agent may have at most one outgoing dataflow edge — adding
    a second one must NOT mutate staged state. This is the F0.2
    guarantee: a graph-rule violation doesn't leave a partial graph
    behind."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a3",
        position={"x": 200, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(session, workflow_id=empty_workflow.id, source="a1", target="a2")
    before_edges = copy.deepcopy(session.staged_edges)
    before_changes = list(session.pending_changes)
    # The second outgoing edge from a1 violates the connection-rule
    # table (max_outgoing=1 for `agent`).
    with pytest.raises(cbs.ToolCallRejected):
        cbs._connect_nodes(session, workflow_id=empty_workflow.id, source="a1", target="a3")
    assert session.staged_edges == before_edges
    assert session.pending_changes == before_changes

def test_update_node_invalid_config_rollback(db, user, empty_workflow):
    """An `update_node` whose new config fails Pydantic must leave
    the staged node unchanged."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    before_node = copy.deepcopy(next(n for n in session.staged_nodes if n["id"] == "a1"))
    with pytest.raises(cbs.ToolCallRejected):
        cbs._update_node(
            session,
            workflow_id=empty_workflow.id,
            node_id="a1",
            patch={"config": {"instructions": 12345}},  # wrong type
        )
    after_node = next(n for n in session.staged_nodes if n["id"] == "a1")
    assert after_node == before_node
    # pending_changes should have just the original session snapshot
    # (no entries from a rejected call).
    assert session.pending_changes == []

# ─────────────────────────────────────────────────────────────────
# F0.5 — `config_echo` in add_node / update_node returns
# 
#
# The tool result must include the post-Pydantic-coercion view of
# `data.config` so the LLM can see what survived. Without this,
# an LLM that wrote a deprecated field (e.g. `router.condition`
# instead of `router.selector`) would see `{"added": id}` and
# assume success — the runtime would then receive a no-op router.
# ─────────────────────────────────────────────────────────────────
def test_add_node_returns_config_echo(db, user, empty_workflow):
    """`add_node` returns the stored config so the LLM sees what
    Pydantic coercion actually kept."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    msg = cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 0, "y": 0},
        config={"instructions": "Help me.", "markdown": True},
    )
    out = json.loads(msg)
    assert out["added"] == "a2"
    assert "config" in out
    assert out["config"]["instructions"] == "Help me."
    assert out["config"]["markdown"] is True
    # `toolsRef` is a default-filled field on AgentNodeConfig; even
    # when the LLM omits it, the echo shows the default so the
    # LLM learns what its omission resolved to.
    assert "toolsRef" in out["config"] or "tools_ref" in out["config"]

def test_add_node_rejects_deprecated_field_with_structured_issue(
    db, user, empty_workflow
):
    """The write-time strict validator now REJECTS unknown fields
    instead of silently dropping them. The
    LLM previously had to learn about deprecated fields via the
    post-coercion `config_echo`; the new contract surfaces the
    mistake on the same turn with a "did you mean" hint.

    Read-time (workflow load / canvas save) is still lax — see
    `test_extra_fields_silently_ignored` in
    `test_node_config_schemas.py` for that invariant.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    with pytest.raises(cbs.ToolCallRejected) as excinfo:
        cbs._add_node(
            session,
            workflow_id=empty_workflow.id,
            type="router",
            id="r1",
            position={"x": 0, "y": 0},
            # `condition` is now a known migration path on
            # `BranchNodeConfig` (`_migrate_legacy_condition`),
            # so we use a truly unknown field instead.
            config={"unknownField": "stale", "branches": []},
        )
    # The rejection envelope carries structured issues.
    payload = json.loads(str(excinfo.value))
    assert payload["ok"] is False
    codes = [i["code"] for i in payload["issues"]]
    assert "invalidConfig" in codes
    # The offending path points at `data.config.unknownField`.
    paths = [i["path"] for i in payload["issues"]]
    assert any(p.endswith("unknownField") for p in paths)
    # Hint names the schema-lookup escape hatch.
    assert any(
        "get_node_types" in (i.get("hint") or "")
        for i in payload["issues"]
    )

def test_update_node_returns_config_echo(db, user, empty_workflow):
    """`update_node` returns the post-coercion config so the LLM
    learns whether its patch landed correctly."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    msg = cbs._update_node(
        session,
        workflow_id=empty_workflow.id,
        node_id="a1",
        patch={"config": {"instructions": "Be helpful.", "markdown": True}},
    )
    out = json.loads(msg)
    assert out["updated"] == "a1"
    assert out["config"]["instructions"] == "Be helpful."
    assert out["config"]["markdown"] is True

# ─────────────────────────────────────────────────────────────────
# F0.1 — gate now reads from the manifest-driven registry
# 
#
# `add_node(type=…)` accepts every type declared in
# `shared/nodes.manifest.json` (15 entries incl. preset tool
# sources). Pre-F0.1 the gate was the legacy 10-tuple, so preset
# types were rejected even though the runtime + canvas accepted
# them — the LLM could never wire an Agent to wikipedia /
# duckduckgo / calculator / arxiv_search / tavily_search.
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "preset_type",
    ["wikipedia", "duckduckgo", "calculator", "arxiv_search", "tavily_search"],
)
def test_add_node_accepts_preset_tool_sources(db, user, empty_workflow, preset_type):
    """Every preset declared in the manifest must be accepted by
    the chat's `add_node` gate. Pre-F0.1 these were silently
    rejected by the legacy 10-tuple."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    msg = cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type=preset_type,
        id=f"preset_{preset_type}",
        position={"x": 0, "y": 0},
        config={},
    )
    out = json.loads(msg)
    assert out["added"] == f"preset_{preset_type}"
    assert out["type"] == preset_type
    assert any(n["id"] == f"preset_{preset_type}" for n in session.staged_nodes)

def test_update_node_patches_label_and_config(db, user, empty_workflow):
    """Update merges label + config + position into the existing
    node; original snapshot is untouched."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    msg = cbs._update_node(
        session,
        workflow_id=empty_workflow.id,
        node_id="a1",
        patch={"label": "Renamed", "config": {"instructions": "New"}},
    )
    out = json.loads(msg)
    assert out["updated"] == "a1"
    staged = next(n for n in session.staged_nodes if n["id"] == "a1")
    assert staged["data"]["label"] == "Renamed"
    assert staged["data"]["config"]["instructions"] == "New"
    # Original untouched.
    assert session.original_nodes[0]["data"]["label"] == "A1"

def test_update_node_rejects_missing_node(db, user, empty_workflow):
    """Updating a node id that doesn't exist throws a clear
    error and doesn't mutate."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._update_node(
            session,
            workflow_id=empty_workflow.id,
            node_id="ghost",
            patch={"label": "x"},
        )
    assert "does not exist" in ei.value.message
    assert session.pending_changes == []

def test_remove_node_cascades_edges(db, user, empty_workflow):
    """Removing a node drops any edges touching it."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    # Add a second node and an edge.
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="a1",
        target="a2",
    )
    assert len(session.staged_edges) == 1
    # Remove a1 — edge should cascade.
    msg = cbs._remove_node(
        session,
        workflow_id=empty_workflow.id,
        node_id="a1",
    )
    out = json.loads(msg)
    assert out["removed"] == "a1"
    assert len(out["cascaded_edges"]) == 1
    assert session.staged_nodes == [n for n in session.staged_nodes if n["id"] != "a1"]
    assert session.staged_edges == []

def test_connect_nodes_rejects_duplicate_edge(db, user, empty_workflow):
    """An existing edge can't be re-added in the same shape."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="a1",
        target="a2",
    )
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._connect_nodes(
            session,
            workflow_id=empty_workflow.id,
            source="a1",
            target="a2",
        )
    assert "already exists" in ei.value.message

def test_connect_nodes_rejects_invalid_kind(db, user, empty_workflow):
    """Edge `kind` must be one of the two supported values."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._connect_nodes(
            session,
            workflow_id=empty_workflow.id,
            source="a1",
            target="a2",
            kind="shortcut",
        )
    assert "kind" in ei.value.message

# ─────────────────────────────────────────────────────────────────
# Handle parameters (F0.3, )
#
# The chat LLM used to be able to pass `source_handle` /
# `target_handle` to `connect_nodes` — and reliably invented values
# (`'default'`, `'br1'`, `'branch-news'`, `'input'`) that don't
# exist on BaseNode. The platform's defence was `_normalize_chat_handle`,
# which silently collapsed every non-empty value to None so the
# edge could land on the single unnamed default handle.
#
# That defence had two problems:
#   1. It advertised parameters to the LLM it couldn't actually use,
#      inviting the LLM to keep inventing handle ids we then had to
#      silently drop.
#   2. It wasn't a contract — if BaseNode ever grows a second source
#      handle (router branches, parallel lanes), the silent collapse
#      would mask the change.
#
# F0.3 removes the parameters entirely. The chat tool surface no
# longer has `source_handle` / `target_handle`, so the LLM can't
# invent values for fields it doesn't know exist. Edges always
# stage with NULL handles.
# ─────────────────────────────────────────────────────────────────
def test_connect_nodes_stages_null_handles(db, user, empty_workflow):
    """The chat path always stages edges with NULL sourceHandle /
    targetHandle. This is the contract: BaseNode has one unnamed
    source and one unnamed target handle, so there's nothing for
    a non-NULL value to resolve to.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="a1",
        target="a2",
    )
    [edge] = session.staged_edges
    assert edge["source"] == "a1"
    assert edge["target"] == "a2"
    assert edge["sourceHandle"] is None
    assert edge["targetHandle"] is None

def test_connect_nodes_accepts_source_handle_param(db, user, empty_workflow):
    """Regression: when the LLM emits
    `connect_nodes(source=router, target=task,
    sourceHandle='confirm', ...)`, the tool must accept
    `sourceHandle` rather than rejecting it. Previously the
    handler hid `source_handle` because `BaseNode` exposed one
    unnamed handle, so any value the LLM invented would never
    match React Flow's handle lookup and the edge would render
    invisibly. The same conversation replay also showed the LLM
    falling back to `plan_workflow` solely because `connect_nodes`
    rejected `sourceHandle` with a Pydantic
    `Unexpected keyword argument` error — a worse outcome than the
    F0.3 invisible-edge class.

    Re-introduced : `connect_nodes` accepts `source_handle`
    as an optional string, defaulting to `None`. The dedup key is
    `(source, target, source_handle)` so different router branches
    on the same (source, target) pair are distinct edges. The
    handle is persisted on the staged edge and round-trips through
    `core.graph` / `schemas.workflow`.
    """
    funcs = cbs._build_tools_for_session(
        cbs._load_or_create_session(db, empty_workflow.id, user)
    )
    by_name = {f.name: f for f in funcs}
    assert "connect_nodes" in by_name, "connect_nodes must be exposed to the LLM"
    cn = by_name["connect_nodes"]
    params = cn.parameters or {}
    props = params.get("properties") or {}
    # `source_handle` is back as an optional string (router branch label).
    assert "source_handle" in props, (
        "connect_nodes must expose source_handle so the LLM can wire "
        "router branches without falling back to plan_workflow"
    )
    assert props["source_handle"].get("type") == "string"
    # `target_handle` is still absent — routers have one unnamed
    # source per branch but only one unnamed target per downstream
    # node, so the LLM never needs it.
    assert "target_handle" not in props, (
        "connect_nodes must NOT expose target_handle — only one "
        "unnamed target handle per node, no need for the LLM to set it"
    )
    # The legacy contract still exposes source / target / kind.
    assert "source" in props
    assert "target" in props
    assert "kind" in props

def test_connect_nodes_dedup_two_calls(db, user, empty_workflow):
    """Two calls with the same (source, target, source_handle) dedup as
    one edge. Different `source_handle` values on the same
    source/target pair are LEGITIMATELY distinct (different router
    branches) — only collisions on the full 3-tuple are rejected.

    Pre- dedup key was just `(source, target)` — the F0.3
    contract that source_handle was hidden. Re-introducing the
    parameter required widening the dedup key.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="a1",
        target="a2",
    )
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._connect_nodes(
            session,
            workflow_id=empty_workflow.id,
            source="a1",
            target="a2",
        )
    assert "already exists" in ei.value.message

def test_connect_nodes_router_branches_are_distinct(db, user, empty_workflow):
    """Two `connect_nodes` calls with the same source/target but
    different `source_handle` must NOT collide — they represent
    different router branches (e.g. `confirm` vs `cancel`). A
    router with `branches: [confirm, cancel]` specifically
    triggered this case: it needs TWO distinct edges with the
    same source/target.

    Uses a branch source (switch mode — allows multiple outgoing
    edges) and two distinct downstream agents (the dedup key test).
    `empty_workflow` already has `a1`, so use `r1` / `yes_a` / `no_a`.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    # Branch source (switch mode) — agents only allow 1 outgoing,
    # so use branch (— `router` legacy alias still
    # accepted, gets migrated to `branch` + `mode='switch'`).
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="router",
        id="r1",
        position={"x": 0, "y": 0},
        config={
            "selector": {"mode": "function", "expression": "yes_a_step"},
            "branches": [{"label": "yes", "target": "yes_a"},
                         {"label": "no", "target": "no_a"}],
        },
    )
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="yes_a",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="no_a",
        position={"x": 100, "y": 100},
        config={"instructions": ""},
    )
    # Two router branches, distinct source_handle — must both succeed.
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="r1",
        target="yes_a",
        source_handle="yes",
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="r1",
        target="no_a",
        source_handle="no",
    )
    # The two edges are present in staged state with distinct ids and handles.
    handles = sorted(
        e.get("sourceHandle") for e in session.staged_edges
    )
    assert handles == ["no", "yes"]
    # Same source/target + SAME handle = duplicate (regression for the
    # widening of the dedup key).
    with pytest.raises(cbs.ToolCallRejected) as ei:
        cbs._connect_nodes(
            session,
            workflow_id=empty_workflow.id,
            source="r1",
            target="yes_a",
            source_handle="yes",
        )
    assert "already exists" in ei.value.message

def test_disconnect_removes_existing_edge(db, user, empty_workflow):
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="a1",
        target="a2",
    )
    edge_id = session.staged_edges[0]["id"]
    msg = cbs._disconnect(
        session,
        workflow_id=empty_workflow.id,
        edge_id=edge_id,
    )
    out = json.loads(msg)
    assert out["removed"] == edge_id
    assert session.staged_edges == []

def test_session_keyed_by_user_and_workflow(db, user, empty_workflow):
    """Two back-to-back calls for the same workflow return the
    same session (so the LLM's accumulating changes survive)."""
    s1 = cbs._load_or_create_session(db, empty_workflow.id, user)
    s2 = cbs._load_or_create_session(db, empty_workflow.id, user)
    assert s1.session_id == s2.session_id

def test_diff_summary_counts_added_removed_updated(db, user, empty_workflow):
    """`_diff_summary` returns the chip counts the UI renders."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._update_node(
        session,
        workflow_id=empty_workflow.id,
        node_id="a1",
        patch={"label": "Renamed"},
    )
    summary = cbs._diff_summary(session)
    assert summary["added_nodes"] == 1
    assert summary["updated_nodes"] == 1
    assert summary["removed_nodes"] == 0
    assert summary["added_edges"] == 0
    assert summary["removed_edges"] == 0

def test_apply_persists_pending_changes_to_db(db, user, empty_workflow):
    """Apply replays the staged changes to the DB row and
    discards the session."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(
        session,
        workflow_id=empty_workflow.id,
        type="agent",
        id="a2",
        position={"x": 100, "y": 0},
        config={"instructions": ""},
    )
    cbs._connect_nodes(
        session,
        workflow_id=empty_workflow.id,
        source="a1",
        target="a2",
    )
    sid = session.session_id

    # Apply.
    cbs.apply_pending_changes(
        db,
        workflow_id=empty_workflow.id,
        session_id=sid,
        user=user,
    )

    # DB row now has 2 nodes + 1 edge.
    db.refresh(empty_workflow)
    assert len(empty_workflow.nodes) == 2
    assert len(empty_workflow.edges) == 1
    # Session is gone.
    assert cbs.get_session(sid) is None

def test_apply_rejects_foreign_session(db, user, empty_workflow):
    """A user can't apply someone else's session."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    # Forge a second-user CurrentUser.
    mallory = CurrentUser(id="mallory@example.com", tenant_id="tenant-default")
    with pytest.raises(Exception) as ei:
        cbs.apply_pending_changes(
            db,
            workflow_id=empty_workflow.id,
            session_id=session.session_id,
            user=mallory,
        )
    # 404 — we don't leak existence to non-owners.
    from fastapi import HTTPException
    assert isinstance(ei.value, HTTPException)
    assert ei.value.status_code == 404

def test_apply_missing_session_is_404(db, user, empty_workflow):
    """An unknown session id returns 404, not 500."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        cbs.apply_pending_changes(
            db,
            workflow_id=empty_workflow.id,
            session_id="chat-does-not-exist",
            user=user,
        )
    assert ei.value.status_code == 404

# ─────────────────────────────────────────────────────────────────
# HTTP / SSE tests — drive the endpoint with a stubbed LLM
# ─────────────────────────────────────────────────────────────────
def _stub_agent_run(tool_calls: list[dict], text: str = ""):
    """Build a fake `RunOutput` whose `.messages` makes the
    chat_builder_service emit the given tool calls.

    The service walks the agent's `messages` list looking for
    `assistant` role messages with `tool_calls`. We construct
    the minimal duck-typed objects the service reads.
    """
    from app.services.chat_builder_service import ToolCallRejected

    handler_map = {
        "add_node": cbs._add_node,
        "update_node": cbs._update_node,
        "remove_node": cbs._remove_node,
        "connect_nodes": cbs._connect_nodes,
        "disconnect": cbs._disconnect,
        "preview_workflow": cbs._preview_workflow,
    }

    class _Fn:
        def __init__(self, name, args):
            self.name = name
            self.arguments = json.dumps(args)

    class _ToolCall:
        def __init__(self, tc_id, name, args):
            self.id = tc_id
            self.function = _Fn(name, args)

    calls = []
    for tc in tool_calls:
        try:
            result = handler_map[tc["tool"]](tc["session"], **tc["args"])
        except ToolCallRejected as exc:
            result = f"error: {exc.message}"
        calls.append(_ToolCall(tc["id"], tc["tool"], tc["args"]))
        # Stash the result for the tool message we synthesize below.
        tc["_result"] = result

    class _AssistantMsg:
        role = "assistant"
        content = text
        tool_calls = calls

    class _ToolMsg:
        role = "tool"
        tool_call_id = None  # filled per-call below

        def __init__(self, tc_id, content):
            self.tool_call_id = tc_id
            self.content = content

    tool_result_msgs = [
        _ToolMsg(tc["id"], tc.get("_result") or "ok")
        for tc in tool_calls
    ]

    class _RunOutput:
        def __init__(self):
            self.messages = [_AssistantMsg()] + tool_result_msgs

    return _RunOutput()

def _stub_agent_run_real_shape(tool_calls: list[dict], text: str = ""):
    """Build a fake `RunOutput` whose `.messages` matches the
    shape agno 2.8.7 actually returns.

    agno's `Message` (agno/models/message.py) is a Pydantic model
    with `tool_calls: Optional[List[Dict[str, Any]]]` — each tool
    call is a dict shaped like OpenAI's tool call payload:
    `{"id": ..., "type": "function", "function": {"name": ...,
    "arguments": "<json string>"}}`. The service's parser walks
    `out.messages` looking for assistant-role messages; if the
    shape doesn't match what the parser expects (e.g. attribute
    access on a dict, or the wrong nesting), the tool call gets
    silently dropped — which is exactly what we observed in
    production. This stub asserts the parser handles the real
    shape correctly.
    """
    from app.services.chat_builder_service import ToolCallRejected

    handler_map = {
        "add_node": cbs._add_node,
        "update_node": cbs._update_node,
        "remove_node": cbs._remove_node,
        "connect_nodes": cbs._connect_nodes,
        "disconnect": cbs._disconnect,
        "preview_workflow": cbs._preview_workflow,
    }

    class _ToolMsg:
        def __init__(self, tc_id, content):
            self.role = "tool"
            self.tool_call_id = tc_id
            self.content = content

    class _AssistantMsg:
        # Duck-typed like agno.models.message.Message: a plain
        # object whose .tool_calls attribute is a list of DICTS
        # (the exact shape OpenAI returns, dumped via pydantic).
        def __init__(self, tc_dicts, text):
            self.role = "assistant"
            self.content = text
            self.tool_calls = tc_dicts

    tc_dicts = []
    tool_msgs = []
    for tc in tool_calls:
        try:
            result = handler_map[tc["tool"]](tc["session"], **tc["args"])
        except ToolCallRejected as exc:
            result = f"error: {exc.message}"
        tc_dicts.append({
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["tool"],
                "arguments": json.dumps(tc["args"]),
            },
        })
        tool_msgs.append(_ToolMsg(tc["id"], result))

    class _RunOutput:
        def __init__(self):
            self.messages = [_AssistantMsg(tc_dicts, text)] + tool_msgs

    return _RunOutput()

def test_chat_endpoint_parses_agno_real_message_shape(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Regression: when the agent's `messages` list carries agno
    2.8.7's real shape (assistant message has `tool_calls` as a
    list of OpenAI-shaped dicts), the chat endpoint must still
    emit tool_call / tool_result / diff events.

    Previous stub used attribute-style `tc.function.name`; this
    one matches what `t.model_dump()` actually produces from
    `openai.types.chat.chat_completion_message_tool_call`.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc_id = f"call-{uuid.uuid4().hex[:8]}"
    fake_output = _stub_agent_run_real_shape(
        tool_calls=[{
            "id": tc_id,
            "tool": "add_node",
            "args": {
                "workflow_id": empty_workflow.id,
                "type": "agent",
                "id": "a2",
                "position": {"x": 100, "y": 0},
                "label": "Second",
                "config": {"instructions": "Hi"},
            },
            "session": session,
        }],
        text="Sure, I'll add a second agent.",
    )

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: fake_output)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a second agent"}],
        },
    )
    assert resp.status_code == 200, resp.text
    events = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            events.append(payload)
    types = [e["type"] for e in events]
    assert "tool_call" in types, f"parser dropped the tool call — events were {types}"
    assert "diff" in types, f"parser emitted no diff — events were {types}"
    assert types[-1] == "completed"
    diff = next(e for e in events if e["type"] == "diff")
    assert diff["summary"]["added_nodes"] == 1
    assert any(n["node"]["id"] == "a2" for n in diff["nodes"])

def test_chat_endpoint_streams_diff_and_completed(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """`POST /api/v1/chat/builder` runs the agent, emits
    start → thinking → tool_call → tool_result → diff → completed,
    and the diff summary matches the pending change."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc_id = f"call-{uuid.uuid4().hex[:8]}"
    fake_output = _stub_agent_run(
        tool_calls=[{
            "id": tc_id,
            "tool": "add_node",
            "args": {
                "workflow_id": empty_workflow.id,
                "type": "agent",
                "id": "a2",
                "position": {"x": 100, "y": 0},
                "label": "Second",
                "config": {"instructions": "Hi"},
            },
            "session": session,
        }],
        text="Sure, I'll add a second agent.",
    )

    # Stub Agent.run so the test doesn't need a real LLM.
    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: fake_output)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a second agent"}],
        },
    )
    assert resp.status_code == 200, resp.text
    events = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            events.append(payload)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "thinking" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "diff" in types
    assert types[-1] == "completed"
    # The diff carries the new node.
    diff = next(e for e in events if e["type"] == "diff")
    assert diff["summary"]["added_nodes"] == 1
    assert any(n["node"]["id"] == "a2" for n in diff["nodes"])

def test_chat_endpoint_surfaces_out_content_when_no_assistant_turn(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Regression: when the model returns only an error string in
    `RunOutput.content` (no assistant message in `messages`), the
    chat endpoint must surface that string as a `text` event so
    the user can see why the LLM call failed — instead of the
    silent "thinking… Done." we used to ship.

    Triggered in production by vLLM servers started without
    `--enable-auto-tool-choice` and `--tool-call-parser`, which
    return `"auto" tool choice requires ...` as content when the
    agent tries to invoke tools.
    """
    class _EmptyRunOutput:
        # Mirrors what agno 2.8.7 produces when the model call
        # fails server-side: messages list contains only the
        # system + user turns (no assistant), and `content`
        # carries the error string from the model server.
        def __init__(self):
            self.messages = [
                type("_Sys", (), {"role": "system", "content": "sys prompt", "tool_calls": None})(),
                type("_User", (), {"role": "user", "content": "add router", "tool_calls": None})(),
            ]
            self.content = '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
            self.formatted_content = None

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: _EmptyRunOutput())

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add router"}],
        },
    )
    assert resp.status_code == 200, resp.text
    events = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            events.append(payload)
    text_events = [e for e in events if e["type"] == "text"]
    assert text_events, f"no text event emitted — events were {[e['type'] for e in events]}"
    assert "--enable-auto-tool-choice" in text_events[-1]["content"]

def test_apply_endpoint_writes_diff_to_db(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """`/apply` commits the staged diff and returns the new
    workflow state."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc_id = f"call-{uuid.uuid4().hex[:8]}"
    fake_output = _stub_agent_run(
        tool_calls=[{
            "id": tc_id,
            "tool": "add_node",
            "args": {
                "workflow_id": empty_workflow.id,
                "type": "agent",
                "id": "a2",
                "position": {"x": 100, "y": 0},
                "config": {"instructions": "Hi"},
            },
            "session": session,
        }],
    )

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: fake_output)

    chat_resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a2"}],
        },
    )
    assert chat_resp.status_code == 200, chat_resp.text

    # Pull the session id from the start event.
    start = None
    for chunk in chat_resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            if payload.get("type") == "start":
                start = payload["session_id"]
    assert start is not None

    apply_resp = client.post(
        "/api/v1/chat/builder/apply",
        headers={"X-User-Id": USER_ID},
        json={"workflow_id": empty_workflow.id, "session_id": start, "pending": []},
    )
    assert apply_resp.status_code == 200, apply_resp.text
    body = apply_resp.json()
    assert len(body["nodes"]) == 2
    assert any(n["id"] == "a2" for n in body["nodes"])

def test_chat_endpoint_surfaces_tool_call_error_in_result(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Regression: when the agent's tool call raises (e.g. our
    `ToolCallRejected` from validation), the SSE `tool_result`
    event MUST carry `ok=False` so the chat renders ✗ instead of
    a misleading ✓.

    Previously the backend hardcoded `ok = True` after every run
    — the user saw a stack of ✓ ticks followed by a "No changes
    to apply" diff card with no clue why the staged state was
    empty. We now read agno's `tool_call_error` flag on the tool
    message and reflect it.
    """
    from app.services.chat_builder_service import ToolCallRejected

    class _ToolMsg:
        def __init__(self, tc_id, content, error):
            self.role = "tool"
            self.tool_call_id = tc_id
            self.content = content
            self.tool_call_error = error  # <-- the new field

    class _AssistantMsg:
        def __init__(self, tc_dicts):
            self.role = "assistant"
            self.content = ""
            self.tool_calls = tc_dicts

    tc_id = f"call-{uuid.uuid4().hex[:8]}"
    tc_dict = {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": "add_node",
            "arguments": json.dumps({
                # Note: NO workflow_id — the LLM omitted it, the
                # tool wrapper falls back to the session's id.
                "type": "agent",
                "id": "ghost",
                "position": {"x": 0, "y": 0},
                "config": {},
            }),
        },
    }

    failed_msg = _ToolMsg(
        tc_id,
        "error: cannot add a node with the empty id",
        error=True,
    )

    class _RunOutput:
        def __init__(self):
            self.messages = [_AssistantMsg([tc_dict]), failed_msg]

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: _RunOutput())

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a node"}],
        },
    )
    assert resp.status_code == 200, resp.text
    events = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            events.append(payload)
    results = [e for e in events if e["type"] == "tool_result"]
    assert results, "no tool_result event emitted — events were " + str([e['type'] for e in events])
    assert results[0]["ok"] is False, (
        f"tool_result should report ok=False when the tool raised, "
        f"got {results[0]!r}"
    )

def test_chat_endpoint_returns_error_when_no_user_preset(
    client, db, user, empty_workflow, monkeypatch
):
    """If the user has no default LLM preset, the chat endpoint
    surfaces a clear error instead of crashing."""
    from app.core import llm_runner as lr
    monkeypatch.setattr(lr, "_resolve_default_preset_id", lambda db=None, user_id=None: None)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200  # SSE always 200, error is in the stream
    events = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            events.append(payload)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "error"
    assert "default LLM preset" in events[-1]["message"]

# ─────────────────────────────────────────────────────────────────
# preset_id override — the chat UI lets the user pick a non-default
# model (e.g. a stronger one for a complex build). The request
# carries `preset_id` and the service must (a) honour it when valid,
# (b) silently fall back to the user's default when the id is
# missing / unknown / owned by another user.
#
# We test the resolution layer (`_build_llm_model`) directly. Driving
# the full `run_chat_turn` would require stubbing agno's `Agent` and
# `RunOutput`, which is orthogonal to what we're verifying here.
#
# Note on test isolation: `_build_llm_model` opens its own session
# via `_resolve_default_preset_id` when `db=None`. That breaks test
# isolation because the test in-memory DB isn't reachable from a
# freshly-opened `session_scope()`. We avoid this by patching the
# resolver on the `cbs` module — which is where `_build_llm_model`
# looks up `_resolve_default_preset_id` (a name in the importing
# module's namespace, NOT the `lr` module's). The `cbs` patch takes
# precedence over the real resolver for the duration of the test.
# ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def two_presets(db, user):
    """Two presets owned by the test user: a default Claude + an
    alternative GPT-4. Returns `(default_id, alt_id)`."""
    import uuid
    from app.db.models import LlmPreset

    default_id = f"preset-default-{uuid.uuid4().hex[:8]}"
    alt_id = f"preset-alt-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=default_id,
        name="Default Claude",
        provider="anthropic",
        model_id="claude-sonnet-4-5",
        api_key="sk-test",
        is_default=True,
        user_id=USER_ID,
        thinking=False,
    ))
    db.add(LlmPreset(
        id=alt_id,
        name="Strong GPT-4",
        provider="openai",
        model_id="gpt-4-turbo",
        api_key="sk-test",
        is_default=False,
        user_id=USER_ID,
        thinking=False,
    ))
    db.commit()
    return default_id, alt_id

@pytest.fixture()
def captured_build_model(monkeypatch):
    """Stub `build_model` so we can inspect what config the chat
    service passed. Returns a list that receives every call's
    first argument (the `ModelConfig` dict)."""
    captured: list[dict] = []
    import app.services.chat_builder_service as cbs_mod
    monkeypatch.setattr(
        cbs_mod, "build_model",
        lambda cfg, user_id=None: captured.append(dict(cfg)) or "stub-model",
    )
    return captured

class _StubModel:
    """Marker object — only used so the service can hand it to
    `Agent(model=...)` without crashing. We never run it."""
    pass

def test_build_llm_model_honours_preset_id_override(
    db, user, two_presets, captured_build_model
):
    """When the caller supplies a valid `preset_id_override`, the
    service resolves to that preset (not the user's default)."""
    _default_id, alt_id = two_presets

    model = cbs._build_llm_model(db, user, preset_id_override=alt_id)

    assert model == "stub-model"  # our stub marker
    assert captured_build_model, "build_model was not called"
    assert captured_build_model[0]["presetId"] == alt_id

def test_build_llm_model_falls_back_to_default_when_override_is_none(
    db, user, two_presets, captured_build_model, monkeypatch
):
    """When no override is set, the service uses the user's
    default preset — the original behaviour."""
    default_id, _alt_id = two_presets

    # Patch the resolver on the `cbs` module (where
    # `_build_llm_model` actually looks it up). See the note on
    # test isolation at the top of this section.
    import app.services.chat_builder_service as cbs_mod
    monkeypatch.setattr(
        cbs_mod, "_resolve_default_preset_id",
        lambda db=None, user_id=None: default_id,
    )

    cbs._build_llm_model(db, user, preset_id_override=None)

    assert captured_build_model[0]["presetId"] == default_id

def test_build_llm_model_silently_falls_back_when_override_is_unknown(
    db, user, two_presets, captured_build_model, monkeypatch
):
    """An override id that doesn't match any preset must NOT
    crash — the service silently falls back to the default. The
    UI shouldn't lose its chat just because localStorage held a
    stale id."""
    default_id, _alt_id = two_presets

    import app.services.chat_builder_service as cbs_mod
    monkeypatch.setattr(
        cbs_mod, "_resolve_default_preset_id",
        lambda db=None, user_id=None: default_id,
    )

    cbs._build_llm_model(db, user, preset_id_override="preset-does-not-exist")

    assert captured_build_model[0]["presetId"] == default_id

def test_build_llm_model_rejects_preset_owned_by_another_user(
    db, user, two_presets, captured_build_model, monkeypatch
):
    """A `preset_id_override` that exists but belongs to a
    DIFFERENT user must NOT be honoured — that would let a chat
    leak model credentials across users. Falls back to the
    caller's default."""
    import uuid
    from app.db.models import LlmPreset, User

    # `llm_presets.user_id` is FK to `users.id`. Seed the other-user
    # so the subsequent INSERT doesn't violate the constraint
    # (test predates the FK, would silently pass on SQLite).
    db.merge(User(id="mallory@example.com", email="mallory@example.com"))
    db.commit()

    other_user_preset = f"preset-other-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=other_user_preset,
        name="Mallory's Claude",
        provider="anthropic",
        model_id="claude-sonnet-4-5",
        api_key="sk-mallory",
        is_default=False,
        user_id="mallory@example.com",  # NOT the test user
        thinking=False,
    ))
    db.commit()

    default_id, _alt_id = two_presets

    import app.services.chat_builder_service as cbs_mod
    monkeypatch.setattr(
        cbs_mod, "_resolve_default_preset_id",
        lambda db=None, user_id=None: default_id,
    )

    cbs._build_llm_model(db, user, preset_id_override=other_user_preset)

    # The override was rejected; we fell back to the caller's default.
    assert captured_build_model[0]["presetId"] == default_id

def test_build_llm_model_accepts_system_shared_preset(
    db, user, two_presets, captured_build_model, monkeypatch
):
    """A system-shared preset (user_id IS NULL) must be usable
    by any caller — even when the caller has their own default."""
    import uuid
    from app.db.models import LlmPreset

    shared_id = f"preset-shared-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=shared_id,
        name="System Shared Claude",
        provider="anthropic",
        model_id="claude-sonnet-4-5",
        api_key="sk-shared",
        is_default=False,
        user_id=None,  # system-shared — visible to everyone
        thinking=False,
    ))
    db.commit()

    import app.services.chat_builder_service as cbs_mod
    monkeypatch.setattr(
        cbs_mod, "_resolve_default_preset_id",
        lambda db=None, user_id=None: shared_id,
    )

    cbs._build_llm_model(db, user, preset_id_override=shared_id)

    assert captured_build_model[0]["presetId"] == shared_id

def test_build_llm_model_raises_when_user_has_no_default(
    db, user, captured_build_model, monkeypatch
):
    """If the user has NO presets at all, the service raises a
    clear 400 — the chat UI surfaces this as a helpful 'set a
    default in Settings' error."""
    import app.services.chat_builder_service as cbs_mod
    monkeypatch.setattr(
        cbs_mod, "_resolve_default_preset_id",
        lambda db=None, user_id=None: None,
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        cbs._build_llm_model(db, user, preset_id_override=None)
    assert "default LLM preset" in str(ei.value.detail)
    assert captured_build_model == []  # never reached

def test_chat_builder_request_schema_accepts_preset_id():
    """The Pydantic schema for the chat builder request must
    accept an optional `preset_id` field. This pins the API
    contract that the frontend types depend on."""
    from app.schemas.chat_builder import ChatBuilderRequest
    req = ChatBuilderRequest(
        workflow_id="wf-1",
        messages=[{"role": "user", "content": "hi"}],
        preset_id="preset-x",
    )
    assert req.preset_id == "preset-x"

    # Also accepts absent preset_id (omitted entirely).
    req2 = ChatBuilderRequest(
        workflow_id="wf-1",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert req2.preset_id is None

# ─────────────────────────────────────────────────────────────────
# Streaming path — `agent.run(stream=True, stream_events=True)`
#
# The batched tests above stub `Agent.run` to return a `RunOutput`
# (no `__iter__`) and exercise the legacy walk-messages path. The
# tests below stub it to return an *iterator* of agno
# `RunOutputEvent` subclasses, exercising the new streaming path
# where events land on the wire as the LLM produces them.
# ─────────────────────────────────────────────────────────────────
def _make_streaming_events(events: list) -> list:
    """Wrap plain dicts as the matching agno event dataclasses so
    the service's `isinstance` checks fire. Only the event types
    the service actually inspects are materialised; everything
    else is a no-op marker the consumer ignores.
    """
    from agno.run.agent import (
        RunCompletedEvent,
        RunContentEvent,
        RunErrorEvent,
        ToolCallCompletedEvent,
        ToolCallErrorEvent,
        ToolCallStartedEvent,
    )
    from agno.models.response import ToolExecution

    out = []
    for ev in events:
        kind = ev["kind"]
        if kind == "tool_call_started":
            out.append(ToolCallStartedEvent(tool=ToolExecution(
                tool_call_id=ev["tool_call_id"],
                tool_name=ev["tool_name"],
                tool_args=ev["tool_args"],
            )))
        elif kind == "tool_call_completed":
            # `result` is the ToolExecution's actual payload (what
            # our tool returned). `content` is the ModelResponse
            # wrapper — agno's Model layer overrides this with
            # `f"{tool}(args) completed in {elapsed}s"` for every
            # tool call, so the LLM only sees the timing string
            # unless we reach into `tool_executions[0].result`.
            # Tests that simulate the real agno behaviour pass BOTH:
            # the timing-string `content` (the production shape)
            # AND the actual JSON in `result` (the production
            # shape that lives on ToolExecution.result).
            te = ToolExecution(
                tool_call_id=ev["tool_call_id"],
                tool_name=ev.get("tool_name", ""),
                tool_args=ev.get("tool_args", {}),
                tool_call_error=ev.get("tool_call_error"),
                result=ev.get("result"),
            )
            out.append(ToolCallCompletedEvent(tool=te, content=ev.get("content", "ok")))
        elif kind == "tool_call_error":
            out.append(ToolCallErrorEvent(tool=ToolExecution(
                tool_call_id=ev["tool_call_id"],
                tool_name=ev.get("tool_name", ""),
                tool_args=ev.get("tool_args", {}),
            ), error=ev.get("error", "tool error")))
        elif kind == "run_content":
            out.append(RunContentEvent(content=ev["content"]))
        elif kind == "run_completed":
            out.append(RunCompletedEvent())
        elif kind == "run_error":
            out.append(RunErrorEvent(content=ev.get("error", "LLM call failed")))
    return out

def test_chat_endpoint_streams_events_incrementally(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """When `Agent.run` returns an iterator (the real agno
    streaming contract), the SSE response carries one chunk per
    event as the iterator yields. The client no longer waits for
    the whole turn to finish before seeing any tool call.

    Order pinned by this test:
        start → thinking → tool_call → tool_result
             → tool_call → tool_result
             → text (streamed per token, delta=true)
             → diff (CONSOLIDATED — once per turn, at the end)
             → completed

    Note: in a real agno run, the agent loop invokes the tool
    handlers (which mutate `session.pending_changes`) BEFORE
    yielding the `tool_call_completed` event. To simulate that
    in a stub, we run the handlers ourselves right before each
    completed event.

    The diff emission is consolidated to ONCE per turn (not once
    per tool call) because the LLM's tools are narrow — a single
    user instruction often produces 5–10 tool calls, and emitting
    a diff after every one flickered the UI and signalled to the
    user "apply now" between every pair of calls. The user wants
    one logical round of work → one diff card → one apply.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc1_id = f"call-{uuid.uuid4().hex[:8]}"
    tc2_id = f"call-{uuid.uuid4().hex[:8]}"

    # Replay what agno would have done: invoke each tool handler
    # to mutate the staged state before yielding the completed
    # event. This is the same code path the streaming consumer
    # reads via `session.pending_changes`.
    cbs._add_node(session, workflow_id=empty_workflow.id, type="agent",
                  id="a2", position={"x": 100, "y": 0},
                  config={"instructions": "Hi"})
    cbs._update_node(session, workflow_id=empty_workflow.id, node_id="a1",
                     patch={"label": "Renamed"})

    stream = _make_streaming_events([
        {"kind": "tool_call_started", "tool_call_id": tc1_id,
         "tool_name": "add_node", "tool_args": {
             "workflow_id": empty_workflow.id, "type": "agent",
             "id": "a2", "position": {"x": 100, "y": 0},
             "config": {"instructions": "Hi"},
         }},
        {"kind": "tool_call_completed", "tool_call_id": tc1_id,
         "tool_name": "add_node", "tool_args": {}, "content": "ok"},
        {"kind": "tool_call_started", "tool_call_id": tc2_id,
         "tool_name": "update_node", "tool_args": {
             "workflow_id": empty_workflow.id, "node_id": "a1",
             "patch": {"label": "Renamed"},
         }},
        {"kind": "tool_call_completed", "tool_call_id": tc2_id,
         "tool_name": "update_node", "tool_args": {}, "content": "ok"},
        {"kind": "run_content", "content": "Done — added a2 and renamed a1."},
        {"kind": "run_completed"},
    ])

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: iter(stream))

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a2 and rename a1"}],
        },
    )
    assert resp.status_code == 200, resp.text

    # Parse the SSE stream and pull out the event types in order.
    types: list[str] = []
    payloads: list[dict] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            types.append(payload["type"])
            payloads.append(payload)

    # Tool calls stream in incrementally (no diff between them),
    # text streams per token, then ONE consolidated diff at the
    # very end before `completed`.
    assert types == [
        "start", "thinking",
        "tool_call", "tool_result",
        "tool_call", "tool_result",
        "text",
        "diff",
        "completed",
    ], f"unexpected event order: {types}"

    # Exactly ONE diff event — covering BOTH tool calls. The
    # consolidated diff carries the cumulative state at turn end:
    # 1 node added (a2) + 1 node updated (a1 → renamed).
    diffs = [p for p in payloads if p["type"] == "diff"]
    assert len(diffs) == 1, (
        f"expected one consolidated diff per turn, got {len(diffs)}: {diffs}"
    )
    assert diffs[0]["summary"]["added_nodes"] == 1
    assert diffs[0]["summary"]["updated_nodes"] == 1

    # Text was streamed per token with delta=True.
    texts = [p for p in payloads if p["type"] == "text"]
    assert len(texts) == 1
    assert texts[0].get("delta") is True
    assert "Done — added a2" in texts[0]["content"]

def test_streaming_path_emits_diff_only_after_successful_tool_call(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """When the LLM's tool call fails (agno's `ToolCallErrorEvent`),
    the chat must surface `tool_result(ok=False)` and MUST NOT emit
    a `diff` event — there are no pending changes to show."""
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc_id = f"call-{uuid.uuid4().hex[:8]}"

    stream = _make_streaming_events([
        {"kind": "tool_call_started", "tool_call_id": tc_id,
         "tool_name": "add_node", "tool_args": {
             # Missing `id` — the tool's Pydantic schema will
             # raise `ToolCallRejected` in production; here we
             # simulate the failure via `ToolCallErrorEvent`.
             "workflow_id": empty_workflow.id, "type": "agent",
             "position": {"x": 0, "y": 0}, "config": {},
         }},
        {"kind": "tool_call_error", "tool_call_id": tc_id,
         "tool_name": "add_node", "error": "id is required"},
        {"kind": "run_completed"},
    ])

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: iter(stream))

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add ghost"}],
        },
    )
    assert resp.status_code == 200, resp.text

    types: list[str] = []
    payloads: list[dict] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            types.append(payload["type"])
            payloads.append(payload)

    assert "diff" not in types, (
        f"streaming path leaked a diff event on a failed tool call: {types}"
    )
    results = [p for p in payloads if p["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "id is required" in results[0]["message"]
    assert types[-1] == "completed"

def test_tool_result_message_uses_tool_execution_result_not_agno_timing_string(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Regression: agno's `Model._handle_function_call_execution`
    (in `agno/models/base.py:2289`) yields a `ModelResponse` whose
    `content` is the auto-generated string
    `f"{tool}({args}) completed in {elapsed}s"` for EVERY tool
    call. Our tool's actual return value (the JSON
    `{"applied": {...}}` from `plan_workflow`, the config echo
    from `add_node`, etc.) goes into `ModelResponse
    .tool_executions[0].result` — NOT into `content`.

    Pre- our `_consume_stream` read `event.content`
    FIRST, so the LLM only ever saw the timing string. The
    conversation replay showed the LLM falling back from
    `plan_workflow` to `add_node`+`connect_nodes` because every
    tool result it saw was `"plan_workflow(plan=...) completed
    in 0.0006s."` — no observable success signal.

    Locked behaviour: prefer `tool.result` (the actual payload)
    over `content` (the timing wrapper).
    """
    tc_id = f"call-{uuid.uuid4().hex[:8]}"
    # Simulate exactly what agno does in production:
    #   - `content` = the timing wrapper (what `event.content` is)
    #   - `result`  = the tool's actual JSON return (what
    #     `event.tool_executions[0].result` carries)
    plan_result_json = json.dumps({
        "ok": True,
        "applied": {
            "added_nodes": 3, "added_edges": 3,
            "removed_nodes": 0, "removed_edges": 0,
            "updated_nodes": 0,
        },
        "config_echo": {"welcome_agent": {"instructions": "hi"}},
    })
    stream = _make_streaming_events([
        {"kind": "tool_call_started", "tool_call_id": tc_id,
         "tool_name": "plan_workflow", "tool_args": {
             "workflow_id": empty_workflow.id,
             "plan": {"nodes": [], "edges": []},
         }},
        {"kind": "tool_call_completed", "tool_call_id": tc_id,
         "tool_name": "plan_workflow",
         "tool_args": {"workflow_id": empty_workflow.id},
         # The agno timing wrapper — this is what `event.content` is.
         "content": "plan_workflow(plan=..., workflow_id=wf-...) completed in 0.0006s. ",
         # The actual payload — `event.tool_executions[0].result`.
         "result": plan_result_json},
        {"kind": "run_completed"},
    ])

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: iter(stream))

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "plan it"}],
        },
    )
    assert resp.status_code == 200, resp.text

    payloads: list[dict] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    results = [p for p in payloads if p["type"] == "tool_result"]
    assert len(results) == 1
    msg = results[0]["message"]
    # The LLM-visible message MUST contain the actual JSON payload,
    # not the agno timing wrapper. This is the regression lock.
    assert "applied" in msg and "added_nodes" in msg, (
        f"tool_result.message must surface the actual tool return "
        f"(plan_workflow's `applied` dict). Got: {msg!r}"
    )
    assert "completed in" not in msg, (
        f"tool_result.message must NOT be the agno auto-generated "
        f"timing wrapper. Got: {msg!r}"
    )
    assert "added_nodes\": 3" in msg or '"added_nodes": 3' in msg

def test_streaming_path_surfaces_run_error(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """If agno's `Agent.run` emits a `RunErrorEvent`, the chat
    ends with `BuilderErrorEvent` — not `completed`. The client
    must see what went wrong instead of a silent Done."""
    stream = _make_streaming_events([
        {"kind": "run_error", "error": "vLLM 502 — upstream timeout"},
    ])

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: iter(stream))

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert resp.status_code == 200, resp.text

    types: list[str] = []
    payloads: list[dict] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            types.append(payload["type"])
            payloads.append(payload)

    assert types[0] == "start"
    assert types[-1] == "error"
    assert "502" in payloads[-1]["message"]

def test_streaming_path_emits_consolidated_diff_on_error_with_partial_changes(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """When the LLM run errors out AFTER staging some changes,
    the service still emits ONE consolidated `diff` BEFORE the
    `error` event. This lets the user apply the partial work
    even though the LLM hiccuped on a follow-up tool call —
    otherwise the diff card would vanish on the error path and
    the user would lose the half-finished changes.

    Order pinned by this test:
        start → thinking → tool_call → tool_result
             → run_error
        And the consolidated diff lands immediately before
        the error (NOT after — the stream ends on error).
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc_id = f"call-{uuid.uuid4().hex[:8]}"

    # Replay what agno would have done: stage one valid add_node
    # so the LLM run has pending changes at error time. The
    # second tool call (below) is the one that triggers the
    # upstream failure.
    cbs._add_node(session, workflow_id=empty_workflow.id, type="agent",
                  id="a2", position={"x": 0, "y": 0},
                  config={"instructions": "Hi"})

    stream = _make_streaming_events([
        {"kind": "tool_call_started", "tool_call_id": tc_id,
         "tool_name": "add_node", "tool_args": {
             "workflow_id": empty_workflow.id, "type": "agent",
             "id": "a2", "position": {"x": 0, "y": 0},
             "config": {"instructions": "Hi"},
         }},
        {"kind": "tool_call_completed", "tool_call_id": tc_id,
         "tool_name": "add_node", "tool_args": {}, "content": "ok"},
        {"kind": "run_error", "error": "vLLM connection lost mid-turn"},
    ])

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: iter(stream))

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a2"}],
        },
    )
    assert resp.status_code == 200, resp.text

    types: list[str] = []
    payloads: list[dict] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            types.append(payload["type"])
            payloads.append(payload)

    # The diff lands BEFORE the error so the UI can show the
    # partial Apply/Cancel buttons alongside the error toast.
    # The user's pending work is salvageable.
    assert types == [
        "start", "thinking",
        "tool_call", "tool_result",
        "diff",
        "error",
    ], f"unexpected event order: {types}"

    # The diff carries the partial pending_changes (1 node added).
    diffs = [p for p in payloads if p["type"] == "diff"]
    assert len(diffs) == 1
    assert diffs[0]["summary"]["added_nodes"] == 1

    # Error event carries the upstream failure message.
    err = next(p for p in payloads if p["type"] == "error")
    assert "vLLM connection lost" in err["message"]

def test_streaming_path_emits_text_deltas_in_realtime(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Text deltas from `RunContentEvent` are streamed in real
    time as `text` events with `delta=True` — the chat shows
    the LLM "typing" character by character instead of
    buffering for several seconds and then dumping all text
    at once.

    Regression: previously the service buffered every text
    delta and emitted one final `text` event at run
    completion. For long outputs (quicksort code, prose
    paragraphs, anything >1 KB) this left the user staring
    at the thinking spinner for 5–10 s while the LLM had
    already produced the answer — UX black hole.
    """
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    tc_id = f"call-{uuid.uuid4().hex[:8]}"

    stream = _make_streaming_events([
        {"kind": "tool_call_started", "tool_call_id": tc_id,
         "tool_name": "add_node", "tool_args": {
             "workflow_id": empty_workflow.id, "type": "agent",
             "id": "a2", "position": {"x": 0, "y": 0},
             "config": {"instructions": "Hi"},
         }},
        {"kind": "tool_call_completed", "tool_call_id": tc_id,
         "tool_name": "add_node", "tool_args": {}, "content": "ok"},
        {"kind": "run_content", "content": "First chunk. "},
        {"kind": "run_content", "content": "Second chunk. "},
        {"kind": "run_content", "content": "Third chunk."},
        {"kind": "run_completed"},
    ])

    from agno.agent import Agent
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: iter(stream))

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add a2"}],
        },
    )
    assert resp.status_code == 200, resp.text

    types: list[str] = []
    payloads: list[dict] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            types.append(payload["type"])
            payloads.append(payload)

    # THREE text events — one per `RunContentEvent` delta.
    # Each carries `delta=True` so the frontend appends to the
    # same streaming bubble instead of opening new ones.
    text_events = [p for p in payloads if p["type"] == "text"]
    assert len(text_events) == 3, (
        f"expected one text event per delta, got {len(text_events)}: "
        f"{[e['content'] for e in text_events]}"
    )
    assert all(e["delta"] is True for e in text_events), (
        f"text events must carry delta=True for streaming, "
        f"got {[e.get('delta') for e in text_events]}"
    )
    assert [e["content"] for e in text_events] == [
        "First chunk. ", "Second chunk. ", "Third chunk.",
    ]

    # Deltas land AFTER the last tool result (the LLM first
    # produces tool calls, then the verbal summary) and
    # BEFORE `completed`.
    text_indices = [i for i, t in enumerate(types) if t == "text"]
    completed_idx = types.index("completed")
    last_tool_result_idx = max(
        i for i, t in enumerate(types) if t == "tool_result"
    )
    assert min(text_indices) > last_tool_result_idx
    assert max(text_indices) < completed_idx

def test_streaming_path_skips_reasoning_and_run_started_events(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """agno emits `Reasoning*Event` and `RunStartedEvent` markers
    around reasoning content. The chat must ignore these — the
    upfront `BuilderThinkingEvent` already covers the "thinking"
    UI state, and exposing reasoning chunks to the user would
    leak the LLM's internal monologue into the chat thread."""
    from agno.run.agent import (
        ReasoningStartedEvent,
        ReasoningCompletedEvent,
        RunStartedEvent,
    )
    from agno.agent import Agent

    tc_id = f"call-{uuid.uuid4().hex[:8]}"
    stream = iter(_make_streaming_events([
        {"kind": "tool_call_started", "tool_call_id": tc_id,
         "tool_name": "preview_workflow", "tool_args": {
             "workflow_id": empty_workflow.id,
         }},
        {"kind": "tool_call_completed", "tool_call_id": tc_id,
         "tool_name": "preview_workflow", "tool_args": {}, "content": "{}"},
        {"kind": "run_completed"},
    ]))
    # Prepend reasoning + run_started markers the service should
    # swallow silently.
    wrapped = iter([
        RunStartedEvent(),
        ReasoningStartedEvent(),
        ReasoningCompletedEvent(),
        *stream,
    ])
    monkeypatch.setattr(Agent, "run", lambda self, *a, **kw: wrapped)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "preview"}],
        },
    )
    assert resp.status_code == 200, resp.text

    types: list[str] = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payload = json.loads(chunk[len("data:"):].strip())
            types.append(payload["type"])

    # No reasoning events leak through. Exactly one thinking
    # event — the upfront one — plus the tool call pair.
    assert types.count("thinking") == 1
    assert "reasoning" not in " ".join(types)
    assert types[-1] == "completed"

def test_streaming_path_yields_events_lazy(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """The service is a true generator — events land on the SSE
    response before the LLM has finished emitting. If the
    service were buffering, a slow LLM would block the whole
    response; with streaming, each chunk ships as soon as the
    generator yields.

    We assert this by tracking when `Agent.run`'s iterator was
    *fully consumed* relative to when the streaming response
    was *received*. With the batched path the response can't be
    received until the iterator is exhausted; with the streaming
    path it can be received mid-iteration.

    Concretely: we patch the generator to yield nothing until
    the test pulls at least one event from the SSE response.
    """
    from agno.agent import Agent

    tc_id = f"call-{uuid.uuid4().hex[:8]}"

    # Build an iterator that BLOCKS on the second pull. If the
    # service buffers the whole generator, the response will
    # never return. If it streams, the first event lands before
    # the second pull.
    pull_state = {"n": 0}

    def streaming_run(self, *a, **kw):
        from agno.models.response import ToolExecution
        from agno.run.agent import (
            RunCompletedEvent,
            ToolCallCompletedEvent,
            ToolCallStartedEvent,
        )
        # First pull: emit tool_call_started immediately.
        # Second pull: block until the test signals go.
        yield ToolCallStartedEvent(tool=ToolExecution(
            tool_call_id=tc_id,
            tool_name="preview_workflow",
            tool_args={"workflow_id": empty_workflow.id},
        ))
        # Yield control to let the streaming response drain.
        import time
        time.sleep(0.01)
        pull_state["n"] += 1
        # Second pull: emit completed.
        yield ToolCallCompletedEvent(tool=ToolExecution(
            tool_call_id=tc_id,
            tool_name="preview_workflow",
            tool_args={"workflow_id": empty_workflow.id},
            tool_call_error=None,
        ), content="ok")
        yield RunCompletedEvent()

    monkeypatch.setattr(Agent, "run", streaming_run)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "preview"}],
        },
    )
    assert resp.status_code == 200, resp.text
    # If we got here, the service didn't buffer — the iterator
    # could have blocked forever, and FastAPI still responded.
    assert "tool_call" in resp.text

def test_streaming_path_preserves_event_timing_on_wire(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Wire-level timing: when the LLM emits events with gaps
    (simulated by `time.sleep` between yields), the SSE chunks
    must arrive with the SAME gaps.

    Why this test bypasses both TestClient and httpx's
    ASGITransport: both buffer the response body internally
    (ASGITransport collects all `http.response.body` messages
    before yielding), which would mask a real server-side
    bug. Instead we drive the `StreamingResponse` body
    iterator directly — that's what uvicorn consumes in
    production, and what `aiter_lines` in real HTTP clients
    (browsers, curl, httpx over a real socket) sees.

    The result: each chunk is yielded by the body iterator
    at the exact moment the service generator produces it.
    """
    import time

    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    cbs._add_node(session, workflow_id=empty_workflow.id, type="agent",
                  id="a2", position={"x": 0, "y": 0},
                  config={"instructions": "Hi"})

    from agno.agent import Agent
    from agno.models.response import ToolExecution
    from agno.run.agent import (
        RunCompletedEvent,
        ToolCallCompletedEvent,
        ToolCallStartedEvent,
    )

    tc_id = "call-timing-1"

    def streaming_run(self, *a, **kw):
        time.sleep(0.050)  # 50ms before first event
        yield ToolCallStartedEvent(tool=ToolExecution(
            tool_call_id=tc_id, tool_name="add_node",
            tool_args={"workflow_id": empty_workflow.id},
        ))
        time.sleep(0.200)  # 200ms before completed event
        yield ToolCallCompletedEvent(tool=ToolExecution(
            tool_call_id=tc_id, tool_name="add_node",
            tool_args={"workflow_id": empty_workflow.id},
            tool_call_error=None,
        ), content="ok")
        yield RunCompletedEvent()

    monkeypatch.setattr(Agent, "run", streaming_run)

    # Drive the service generator + the StreamingResponse body
    # iterator manually. Starlette's `iterate_in_threadpool`
    # wraps the sync iterator in an async one, which is what
    # uvicorn consumes in production.
    from app.api.chat_builder import _stream_response
    events_iter = cbs.run_chat_turn(
        db,
        workflow_id=empty_workflow.id,
        messages=[{"role": "user", "content": "add a2"}],
        user=user,
        preset_id=None,
    )
    streaming_resp = _stream_response(events_iter)

    # Consume `body_iterator` like uvicorn does. It's an async
    # iterator (sync iterators are wrapped via
    # `iterate_in_threadpool` inside `StreamingResponse`).
    body_iter = streaming_resp.body_iterator

    async def _consume():
        chunks = []
        async for chunk in body_iter:
            chunks.append((time.monotonic(), chunk))
        return chunks

    import asyncio
    t_start = time.monotonic()
    chunks = asyncio.run(_consume())

    # Each chunk is a serialized SSE event. Walk and parse.
    arrivals: list[tuple[float, str]] = []
    for ts, chunk in chunks:
        if not chunk:
            continue
        for line in chunk.decode("utf-8", errors="replace").split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
                arrivals.append((ts - t_start, ev.get("type", "?")))
            except Exception:
                pass

    types_only = [t for _, t in arrivals]

    # Sanity: events we expect.
    assert "thinking" in types_only
    assert "tool_call" in types_only
    assert "tool_result" in types_only
    assert "completed" in types_only

    t_thinking = arrivals[next(
        i for i, (ts, t) in enumerate(arrivals) if t == "thinking"
    )][0]
    t_tool_call = arrivals[next(
        i for i, (ts, t) in enumerate(arrivals) if t == "tool_call"
    )][0]
    t_tool_result = arrivals[next(
        i for i, (ts, t) in enumerate(arrivals) if t == "tool_result"
    )][0]

    gap_thinking_to_call = t_tool_call - t_thinking
    gap_call_to_result = t_tool_result - t_tool_call

    # If the service is NOT buffering, gap_thinking_to_call
    # should be ~50ms (the injected sleep) and gap_call_to_result
    # should be ~200ms. If buffering collapsed them, both
    # would be <10ms.
    assert gap_thinking_to_call >= 0.030, (
        f"server buffered the stream — gap thinking→tool_call "
        f"is {gap_thinking_to_call*1000:.0f}ms (expected ≥30ms)"
    )
    assert gap_call_to_result >= 0.150, (
        f"server buffered the stream — gap tool_call→tool_result "
        f"is {gap_call_to_result*1000:.0f}ms (expected ≥150ms)"
    )

# ─────────────────────────────────────────────────────────────────
# Streaming-run resilience — JSONDecodeError retry
#
# When the Anthropic SDK's SSE parser hits a fragmented tool-call
# chunk, it raises `json.JSONDecodeError("key must be a string ..."
# at column N)` instead of buffering. This surfaces in the chat
# service's `agent.run(stream=True, stream_events=True)` call.
#
# The chat turn must (a) retry the run once (these failures are
# ~95% transient — provider-side fragmentation or a network hiccup),
# and (b) when the retry also fails, surface a FRIENDLY message to
# the user instead of leaking `LLM call failed: json.JSONDecodeError
# at line 1 column 1439` into the chat bubble.
#
# We patch `cbs._start_streaming_run` (the new helper introduced
# alongside the retry logic) instead of `Agent.run` — same
# indirection point tests already use, but now the retry happens
# at the helper boundary where it actually lives.
# ─────────────────────────────────────────────────────────────────
def test_chat_endpoint_retries_streaming_run_on_first_attempt_json_error(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Simulates the SDK raising `JSONDecodeError` on the first
    `agent.run` call. The chat turn must retry once; the retry
    returns a normal non-iterable RunOutput (the simpler test
    surface) so the turn completes cleanly with no error event.
    """
    from app.services import chat_builder_service as cbs

    fake_json_err = json.JSONDecodeError(
        "key must be a string", '{"foo":"bar"', 7,
    )
    calls = {"n": 0}

    class _EmptyRunOutput:
        """Minimal stub: no messages → service emits `completed`."""
        messages: list = []

    def fake_start_streaming_run(model, session, context):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fake_json_err
        return _EmptyRunOutput()  # success on retry

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start_streaming_run)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    types = [p["type"] for p in payloads]
    assert types[0] == "start", types
    assert types[-1] == "completed", (
        f"expected the retry to succeed → completed; got {types!r}"
    )
    # No error event leaked to the user.
    assert "error" not in types, f"retry succeeded but error event fired: {types!r}"
    # And the helper was actually called twice (1 fail + 1 retry).
    assert calls["n"] == 2, f"expected one retry, got {calls['n']} calls"

def test_chat_endpoint_unwraps_model_provider_error_with_json_cause(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Bug C regression: when agno wraps the Anthropic SDK's
    `JSONDecodeError` in `ModelProviderError(message=str(e)) from e`,
    `_start_streaming_run` must unwrap and re-raise the original
    `JSONDecodeError` so the caller's `except JSONDecodeError` retry
    fires.

    Before this fix the wrapped error escaped uncaught — the user
    saw the raw SDK trace ("Unexpected error calling Claude API:
    key must be a string at line 1 column 1800") and the chat
    turn failed without a retry.

    To exercise the wrapper INSIDE `_start_streaming_run`, this
    test patches `Agent.run` (not `_start_streaming_run`) so the
    wrapper's try/except actually runs.
    """
    from app.services import chat_builder_service as cbs
    from agno.agent import Agent
    try:
        from agno.exceptions import ModelProviderError
    except ImportError:  # pragma: no cover — agno missing
        pytest.skip("agno not installed in test env")

    fake_json_err = json.JSONDecodeError(
        "key must be a string at line 1 column 1800", "x" * 2000, 1800,
    )

    # Build the wrapped error with __cause__ = JSONDecodeError,
    # mirroring what `agno.models.anthropic.claude._handle_api_error`
    # does in production:
    #   raise ModelProviderError(message=str(e), ...) from e
    def _make_wrapped():
        try:
            raise fake_json_err
        except json.JSONDecodeError as e:
            wrapped = ModelProviderError(
                message=str(e),
                model_name="claude-test",
                model_id="claude-test",
            )
            raise wrapped from e

    agent_run_calls = {"n": 0}

    def fake_agent_run(self, *args, **kwargs):
        agent_run_calls["n"] += 1
        if agent_run_calls["n"] == 1:
            # Raise the wrapped error from inside the helper's
            # try/except so unwrap kicks in.
            try:
                raise fake_json_err
            except json.JSONDecodeError as e:
                wrapped = ModelProviderError(
                    message=str(e),
                    model_name="claude-test",
                    model_id="claude-test",
                )
                # Mirror production: `raise ... from e`.
                raise wrapped from e
        # Second call: succeed with an empty RunOutput.
        class _EmptyRunOutput:
            messages: list = []
        return _EmptyRunOutput()

    monkeypatch.setattr(Agent, "run", fake_agent_run)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    types = [p["type"] for p in payloads]
    assert types[0] == "start", types
    # The retry should have unwrapped + succeeded → completed.
    assert types[-1] == "completed", (
        f"expected unwrapped retry → completed; got {types!r}"
    )
    assert "error" not in types, (
        f"wrapped ModelProviderError leaked to the user: "
        f"{[p for p in payloads if p['type']=='error']!r}"
    )
    assert agent_run_calls["n"] == 2, (
        f"expected unwrap → retry once; got {agent_run_calls['n']} "
        f"agent.run calls"
    )

def test_chat_endpoint_surfaces_friendly_error_when_retry_also_fails(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Both attempts raise `JSONDecodeError` — the user must see
    the friendly retry-exhausted message, NOT the raw SDK trace.
    """
    from app.services import chat_builder_service as cbs

    fake_json_err = json.JSONDecodeError(
        "key must be a string at line 1 column 1439", "x" * 1500, 1439,
    )
    calls = {"n": 0}

    def fake_start_streaming_run(model, session, context):
        calls["n"] += 1
        raise fake_json_err

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start_streaming_run)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    types = [p["type"] for p in payloads]
    assert types[0] == "start"
    assert types[-1] == "error", types
    msg = payloads[-1]["message"]
    # Friendly: no raw SDK exception text leaks to the user.
    assert "JSONDecodeError" not in msg, (
        f"raw SDK exception leaked to user: {msg!r}"
    )
    assert "key must be a string" not in msg, (
        f"raw SDK error text leaked to user: {msg!r}"
    )
    assert "1439" not in msg, f"raw SDK column leaked to user: {msg!r}"
    # And it tells the user what to do (retry).
    assert "resend" in msg.lower() or "retry" in msg.lower(), (
        f"friendly message should suggest retrying, got: {msg!r}"
    )
    # Helper was called twice (initial + 1 retry).
    assert calls["n"] == 2

def test_chat_endpoint_preserves_partial_diff_on_mid_stream_json_error(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """P1 : the top-level `agent.run()` unwrap only
    catches errors from the initial request — once the iterator is
    mid-flight, errors from `stream.__next__()` are NOT routed
    through it. Repro the production failure (col 364) by yielding
    a few events, then raising `JSONDecodeError` mid-iteration.

    With the fix the user sees:
      * a friendly BuilderErrorEvent — NOT the raw SDK exception
        text leaking to the chat,
      * the partial-diff emission runs first (when staged changes
        are present; we exercise this via direct `_consume_stream`
        invocation below so we don't depend on the agent mock
        actually executing tool wrappers).

    Without the fix the exception bubbles up to FastAPI's 500
    handler and either kills the SSE stream silently or surfaces a
    raw SDK error to the user.
    """
    from app.services import chat_builder_service as cbs

    fake_json_err = json.JSONDecodeError(
        "key must be a string at line 1 column 364", "x" * 400, 364,
    )

    def fake_start(model, session, context):
        def gen():
            yield from ()  # no events — error is the very first pull
            raise fake_json_err
        return gen()

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add an agent"}],
        },
    )
    assert resp.status_code == 200, resp.text
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    # Friendly error closes the stream — no raw SDK text leaks.
    error_events = [p for p in payloads if p["type"] == "error"]
    assert len(error_events) == 1, (
        f"expected exactly one friendly error, got {error_events}"
    )
    msg = error_events[0]["message"]
    assert "JSONDecodeError" not in msg, (
        f"raw SDK exception leaked to user: {msg!r}"
    )
    assert "key must be a string" not in msg, (
        f"raw SDK error text leaked to user: {msg!r}"
    )
    assert "364" not in msg, f"raw SDK column leaked to user: {msg!r}"
    # And it tells the user what to do (resend / apply).
    assert "resend" in msg.lower() or "apply" in msg.lower(), (
        f"friendly message should suggest resending or applying, "
        f"got: {msg!r}"
    )
    # The error is the LAST event — no further events leak after it.
    assert [p["type"] for p in payloads][-1] == "error"

def test_chat_endpoint_unwraps_model_provider_error_mid_stream(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """P1 : agno 2.8.7 wraps mid-stream `JSONDecodeError`
    the same way it wraps top-level ones — `ModelProviderError
    (message=str(e)) from e`. The mid-stream guard must unwrap
    that too, not just bare `JSONDecodeError`."""
    from app.services import chat_builder_service as cbs

    inner = json.JSONDecodeError(
        "key must be a string at line 1 column 1800", "x" * 1900, 1800,
    )

    def fake_start(model, session, context):
        def gen():
            yield from ()
            try:
                from agno.exceptions import ModelProviderError
            except ImportError:
                ModelProviderError = None  # type: ignore
            if ModelProviderError is None:
                raise inner
            wrapped = ModelProviderError(message=str(inner))
            raise wrapped from inner

        return gen()

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add another agent"}],
        },
    )
    assert resp.status_code == 200, resp.text
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    # Friendly error fires; no raw text leaks.
    error_events = [p for p in payloads if p["type"] == "error"]
    assert len(error_events) == 1
    msg = error_events[0]["message"]
    assert "JSONDecodeError" not in msg
    assert "ModelProviderError" not in msg
    assert "key must be a string" not in msg
    assert "1800" not in msg

def test_chat_endpoint_propagates_non_json_mid_stream_errors(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """P1 : the mid-stream guard only catches
    `JSONDecodeError` (or `ModelProviderError` wrapping it).
    Other exceptions (real bugs, network failures not wrapped by
    agno, etc.) must propagate so they're logged as actual errors
    rather than masked as transient parse failures.

    Invariant: a non-JSONDecodeError raised mid-iteration
    propagates out of the SSE generator (the TestClient re-raises
    it). The only wrong outcome is if the mid-stream guard caught
    what it shouldn't have and surfaced a friendly
    'stream was interrupted' message — that would mask a real
    bug as a transient parse failure.
    """
    import pytest
    from app.services import chat_builder_service as cbs

    def fake_start(model, session, context):
        def gen():
            yield from ()
            raise RuntimeError("unexpected non-JSON failure")
        return gen()

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start)

    # The exception must propagate. FastAPI's TestClient re-raises
    # any exception from inside the StreamingResponse generator,
    # so `pytest.raises` is the right shape for the assertion —
    # NOT a 200 + body inspection.
    with pytest.raises(RuntimeError, match="unexpected non-JSON failure"):
        client.post(
            "/api/v1/chat/builder",
            headers={"X-User-Id": USER_ID},
            json={
                "workflow_id": empty_workflow.id,
                "messages": [{"role": "user", "content": "add another"}],
            },
        )

def test_consume_stream_partial_diff_preserved_on_mid_stream_error(
    db, seeded_default_preset, user, empty_workflow
):
    """Direct unit test for the partial-diff-preservation branch
    of `_consume_stream`'s mid-stream guard. Stages a change via
    `_add_node` directly (no agent mock), then feeds an iterator
    that yields nothing followed by a JSONDecodeError. Verifies:
      * a BuilderDiffEvent is emitted with the staged change
      * a JSONDecodeError is RAISED so the caller (`run_chat_turn`)
        can retry the whole stream once.

    Pre- this test asserted a friendly error event was
    emitted directly — that's now `run_chat_turn`'s job, after the
    second retry attempt also fails. This unit test isolates the
    partial-diff + raise contract; the retry-exhausted error path
    is covered by the chat-endpoint tests above.
    """
    from app.auth import CurrentUser
    from app.services.chat_builder_service import (
        _add_node,
        _consume_stream,
        _load_or_create_session,
    )
    from app.services import member_service

    # Bootstrap the workflow + owner so _load_or_create_session works.
    member_service.bootstrap_owner(
        db, empty_workflow.id, "alice@example.com",
    )
    db.commit()
    session = _load_or_create_session(
        db, empty_workflow.id,
        CurrentUser(id="alice@example.com", tenant_id="tenant-default"),
    )
    # Stage a real change so `pending_changes` is non-empty when
    # the mid-stream guard fires.
    _add_node(
        session, empty_workflow.id,
        type="agent", id="agent-1",
        position={"x": 0, "y": 0},
    )
    assert session.pending_changes, "test setup: staged change must land"

    # Build an iterator that yields nothing then raises — the
    # guard fires on the very first pull.
    fake_json_err = json.JSONDecodeError(
        "key must be a string at line 1 column 364", "x" * 400, 364,
    )
    def bad_iter():
        yield from ()
        raise fake_json_err

    # The partial diff is emitted BEFORE the raise — that's the
    # invariant that lets the user apply mid-turn work even when
    # the retry fails.
    events = []
    with pytest.raises(json.JSONDecodeError):
        events.extend(_consume_stream(bad_iter(), session, empty_workflow.id))

    types = [e.type for e in events]
    assert "diff" in types, (
        f"partial diff must be preserved before raise; got {types}"
    )
    assert types[-1] == "diff", (
        f"diff must be the last emitted event before the raise so "
        f"the user can apply mid-turn work; got {types}"
    )

def test_chat_endpoint_retries_mid_stream_json_error_and_succeeds(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """: when the Anthropic SDK's SSE parser chokes on a
    partial chunk mid-iteration (typical: 'key must be a string at
    line 1 column N'), the chat endpoint should RETRY the whole
    streaming run once before surfacing the friendly error. The
    user shouldn't have to manually click "resend" for what is
    almost always a transient provider-side fragmentation issue.

    This test stubs `_start_streaming_run` to return a bad iterator
    on the first call (raises JSONDecodeError mid-flight) and a
    good iterator on the second call (yields a text delta then a
    run_completed event). The endpoint should:
      * yield a `retry` event between the two attempts
      * NOT yield a friendly error (the retry succeeded)
      * end with `completed`
    """
    from app.services import chat_builder_service as cbs
    from app.schemas.chat_builder import BuilderRetryEvent

    fake_json_err = json.JSONDecodeError(
        "key must be a string at line 1 column 1800", "x" * 1900, 1800,
    )

    call_count = {"n": 0}

    def fake_start(model, session, context):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First attempt: yields a real ToolCallStartedEvent,
            # then raises JSONDecodeError on the next __next__()
            # call. _consume_stream's try/except unwraps it to a
            # JSONDecodeError and re-raises; run_chat_turn's retry
            # loop then calls _start_streaming_run a second time.
            from agno.run.agent import ToolCallStartedEvent
            from agno.models.response import ToolExecution
            first_event = ToolCallStartedEvent(tool=ToolExecution(
                tool_call_id="call-bad",
                tool_name="add_node",
                tool_args={},
            ))

            def gen_then_raise():
                yield first_event
                raise fake_json_err

            return iter(gen_then_raise())
        # Second attempt: emits a clean RunContentEvent then a
        # RunCompletedEvent. _make_streaming_events wraps it.
        return _make_streaming_events([
            {"kind": "run_content", "content": "Recovered."},
            {"kind": "run_completed"},
        ])

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "add an agent"}],
        },
    )
    assert resp.status_code == 200, resp.text
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    types = [p["type"] for p in payloads]
    # Sanity: we called _start_streaming_run twice.
    assert call_count["n"] == 2, (
        f"retry should fire exactly once, got {call_count['n']} calls"
    )
    # The retry event was emitted.
    retry_events = [p for p in payloads if p["type"] == "retry"]
    assert len(retry_events) == 1, (
        f"expected one retry event, got {retry_events}"
    )
    # The retry's reason carries the SDK error text (useful for
    # debugging — the frontend can show it as a tooltip).
    assert "key must be a string" in retry_events[0].get("reason", "")
    # The retry happened BETWEEN the first attempt's events and
    # the second attempt's events — order matters: retry sits
    # before the recovered run_content.
    retry_idx = types.index("retry")
    assert "retry" in types, types
    assert retry_idx < types.index("text"), (
        f"retry must precede recovered events; got order {types}"
    )
    # Stream ends with completed — no friendly error (retry won).
    assert types[-1] == "completed", (
        f"stream should end cleanly on retry success; got {types}"
    )
    errs = [p for p in payloads if p["type"] == "error"]
    assert len(errs) == 0, (
        f"no friendly error on successful retry, got {errs}"
    )

def test_chat_endpoint_does_not_retry_on_non_json_exception(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """The retry only applies to `JSONDecodeError` — a generic
    `RuntimeError` (e.g. connection error, auth failure) must
    surface the original error path with the SDK trace, since
    retrying those won't help.
    """
    from app.services import chat_builder_service as cbs

    calls = {"n": 0}

    def fake_start_streaming_run(model, session, context):
        calls["n"] += 1
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start_streaming_run)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))

    types = [p["type"] for p in payloads]
    assert types[-1] == "error"
    assert "network unreachable" in payloads[-1]["message"]
    # No retry — generic exceptions fall straight through.
    assert calls["n"] == 1, (
        f"expected no retry on non-JSON error, got {calls['n']} calls"
    )

# ─────────────────────────────────────────────────────────────────
# JSON-string arg tolerance — some LLM providers serialize complex
# tool args as JSON-encoded strings instead of inline JSON objects
# (e.g. plan_workflow(plan='{"edges": [...]}') instead of
# plan_workflow(plan={"edges": [...]})). Previously this raised
# `Input should be a valid dictionary` from Pydantic validate_call
# BEFORE our handler ran, so the LLM got a raw SDK error with no
# way to recover.
#
# The fix: tool handlers now accept either shape. `plan` may be
# a dict OR a JSON string. Same for `nodes`/`edges` (lists) and
# `add_node`/`update_node` sub-args. Tested below by exercising
# the helper + the public `_plan_workflow` / `_replace_workflow`
# directly (the closure-bound LLM-facing handlers route through
# these via `_coerce_dict_arg` / `_coerce_list_dict_arg`).
# ─────────────────────────────────────────────────────────────────
def test_plan_workflow_accepts_json_string_for_plan_arg(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Regression: `plan_workflow(plan='{"nodes": [...]}')` must
    be accepted (decoded to a dict) instead of raising
    `Input should be a valid dictionary`.

    The LLM in the user's bug report emitted the plan as a
    JSON-encoded string for `plan_workflow(plan=...)` and the
    same for `replace_workflow(nodes=..., edges=...)`. Pydantic
    validate_call rejected both before our handler ran.
    """
    from app.services import chat_builder_service as cbs

    # Drive the tool handler directly via the session-bound
    # closure. The handler signature exposed to the LLM lives
    # inside `_build_tools_for_session` — but since it captures
    # the session in a closure, we test the helper functions
    # `_coerce_dict_arg` / `_coerce_list_dict_arg` + the public
    # `_plan_workflow` / `_replace_workflow` directly.
    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    plan_str = json.dumps({"nodes": [], "edges": []})

    # 1. Helper normalizes string → dict.
    assert cbs._coerce_dict_arg(plan_str) == {"nodes": [], "edges": []}

    # 2. The public `_plan_workflow` accepts either shape.
    out = cbs._plan_workflow(session, empty_workflow.id, plan=plan_str)
    parsed = json.loads(out)
    assert parsed["ok"] is True, parsed
    # Empty plan → no nodes added; `applied` is a count dict.
    assert parsed["applied"].get("added_nodes", 0) == 0

def test_replace_workflow_accepts_json_string_for_list_args(
    client, db, seeded_default_preset, user, empty_workflow
):
    """Regression: `replace_workflow(nodes='[...as string...]',
    edges='[...as string...]')` must be accepted.
    """
    from app.services import chat_builder_service as cbs

    session = cbs._load_or_create_session(db, empty_workflow.id, user)
    nodes_str = json.dumps([
        {
            "id": "n1",
            "type": "agent",
            "position": {"x": 0, "y": 0},
            "data": {"label": "n1", "config": {"instructions": "hi"}},
        }
    ])
    edges_str = json.dumps([])

    out = cbs._replace_workflow(
        session, empty_workflow.id, nodes=nodes_str, edges=edges_str,
    )
    parsed = json.loads(out)
    assert parsed["ok"] is True, parsed
    assert len(parsed.get("config_echo", {})) == 1

def test_coerce_dict_arg_rejects_garbage_with_friendly_error():
    """When the LLM passes something that isn't dict-shaped even
    after JSON decoding (e.g. `plan='oops'`), the helper raises
    a structured ToolCallRejected (NOT a raw TypeError) so the
    LLM gets a recoverable hint.
    """
    from app.services import chat_builder_service as cbs

    # Not a dict, not parseable as a dict
    with pytest.raises(cbs.ToolCallRejected) as exc_info:
        cbs._coerce_dict_arg("oops this is not JSON")
    assert exc_info.value.code == "INVALID_ARG_TYPE"
    assert "JSON object" in exc_info.value.hint

    # Parseable JSON but not an object (it's a list)
    with pytest.raises(cbs.ToolCallRejected) as exc_info:
        cbs._coerce_dict_arg("[1, 2, 3]")
    assert exc_info.value.code == "INVALID_ARG_TYPE"

def test_coerce_list_dict_arg_rejects_garbage_with_friendly_error():
    """Same friendly-error contract for list-typed args."""
    from app.services import chat_builder_service as cbs

    with pytest.raises(cbs.ToolCallRejected) as exc_info:
        cbs._coerce_list_dict_arg("not valid json")
    assert exc_info.value.code == "INVALID_ARG_TYPE"
    assert "JSON array" in exc_info.value.hint

# ─────────────────────────────────────────────────────────────────
# Per-turn tool-call cap — break-out + high-level exemption.
#
# Two bugs fixed at once :
#
#   A) When the cap tripped, the streaming loop used `continue`
#      which silently drained the iterator. The LLM's subsequent
#      tool calls still EXECUTED (mutations landed) but the UI
#      never saw them — user clicked Apply and was surprised by a
#      swarm of nodes that the chat log didn't mention. Fix: break.
#
#   B) High-level tools (plan_workflow / replace_workflow /
#      create_react_agent / create_router_pattern /
#      create_retry_loop) and read-only diagnostics
#      (get_node_types / get_connection_rules / etc.) were counted
#      against the cap. Since each high-level call internally
#      batches many operations, charging per-call is the wrong
#      unit. Fix: exempt them.
# ─────────────────────────────────────────────────────────────────
def _streaming_events_for_calls(calls: list[dict]) -> list:
    """Build a list of agno streaming events for a sequence of
    tool calls. Each `calls[i]` is `{"tool_name": str, "args": dict}`
    and gets a started + completed event pair.
    """
    events = []
    for i, call in enumerate(calls):
        events.append({
            "kind": "tool_call_started",
            "tool_call_id": f"call-{i}",
            "tool_name": call["tool_name"],
            "tool_args": call.get("args", {}),
        })
        events.append({
            "kind": "tool_call_completed",
            "tool_call_id": f"call-{i}",
            "tool_name": call["tool_name"],
            "tool_args": call.get("args", {}),
            "content": "ok",
        })
    events.append({"kind": "run_completed"})
    return _make_streaming_events(events)

def _collect_sse_events(client, db, user, empty_workflow, stream_events, monkeypatch):
    """Run the chat endpoint with a stubbed streaming run that
    yields the given events; return the parsed SSE event list.
    """
    from app.services import chat_builder_service as cbs

    def fake_start(model, session, context):
        return iter(stream_events)

    monkeypatch.setattr(cbs, "_start_streaming_run", fake_start)

    resp = client.post(
        "/api/v1/chat/builder",
        headers={"X-User-Id": USER_ID},
        json={
            "workflow_id": empty_workflow.id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    payloads = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk == "data: [DONE]":
            continue
        if chunk.startswith("data:"):
            payloads.append(json.loads(chunk[len("data:"):].strip()))
    return payloads

def test_streaming_cap_breaks_out_and_drops_subsequent_calls(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Bug A regression: emit 2 more `add_node` calls than the
    cap. The cap (currently `cbs.MAX_TOOL_CALLS_PER_TURN`) should
    fire on the cap-th call. The two overflow calls must NOT
    produce UI events — the loop must break, not silently drain.

    Before the fix the overflow calls were silently executed
    (mutations landed) but the user never saw the tool_call
    events for them — apply showed a swarm of nodes the chat
    log didn't mention.

    : cap was bumped 8 → 20 so the LLM can build out a
    6–7 node workflow imperatively. The test stays in sync by
    reading the constant instead of hardcoding the literal.
    """
    cap = cbs.MAX_TOOL_CALLS_PER_TURN
    total = cap + 2  # 2 over the cap so we still verify overflow drops
    calls = [
        {"tool_name": "add_node", "args": {"type": "agent", "id": f"n{i}"}}
        for i in range(total)
    ]
    events = _streaming_events_for_calls(calls)
    payloads = _collect_sse_events(
        client, db, user, empty_workflow, events, monkeypatch,
    )

    tool_call_events = [
        p for p in payloads
        if p["type"] == "tool_call" and p["tool"] == "add_node"
    ]
    error_events = [p for p in payloads if p["type"] == "error"]

    # Exactly `cap` add_node tool_call events get through.
    assert len(tool_call_events) == cap, (
        f"cap should limit to {cap}, got {len(tool_call_events)} "
        f"tool_call events"
    )
    # The overflow calls never appear as UI events — loop broke.
    emitted_ids = {p["args"].get("id") for p in tool_call_events}
    overflow_first = f"n{cap}"
    overflow_last = f"n{total - 1}"
    assert overflow_first not in emitted_ids, (
        f"overflow call ({overflow_first}) leaked past the cap — "
        f"A bug regressed"
    )
    assert overflow_last not in emitted_ids, (
        f"last overflow call ({overflow_last}) leaked past the cap — "
        f"A bug regressed"
    )
    # A single friendly error event is emitted.
    assert len(error_events) == 1, (
        f"expected 1 error event, got {len(error_events)}"
    )
    assert "tool-call cap reached" in error_events[0]["message"]
    assert "plan_workflow" in error_events[0]["message"]

def test_streaming_high_level_tools_exempt_from_cap(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Bug B regression: high-level tools (plan_workflow +
    replace_workflow + create_react_agent + ...) do NOT count
    against the per-turn cap. A turn can have 3 plan_workflow
    calls + `MAX_TOOL_CALLS_PER_TURN` add_node calls and the cap
    fires on the cap-th add_node, NOT on the 3rd plan_workflow.

    : the test reads the cap from the constant instead
    of hardcoding 8 — the cap was bumped 8 → 20 so a 6–7 node
    workflow can be built imperatively without tripping it.
    """
    cap = cbs.MAX_TOOL_CALLS_PER_TURN
    calls = [
        {"tool_name": "plan_workflow", "args": {"plan": {"nodes": [], "edges": []}}},
        {"tool_name": "plan_workflow", "args": {"plan": {"nodes": [], "edges": []}}},
        {"tool_name": "plan_workflow", "args": {"plan": {"nodes": [], "edges": []}}},
        {"tool_name": "create_react_agent", "args": {"instructions": "x"}},
        {"tool_name": "create_router_pattern", "args": {"branches": []}},
        # `cap` imperative add_node calls
        *[{"tool_name": "add_node", "args": {"type": "agent", "id": f"imp{i}"}}
          for i in range(cap)],
    ]
    events = _streaming_events_for_calls(calls)
    payloads = _collect_sse_events(
        client, db, user, empty_workflow, events, monkeypatch,
    )

    tool_calls_by_tool = {}
    for p in payloads:
        if p["type"] == "tool_call":
            tool_calls_by_tool.setdefault(p["tool"], 0)
            tool_calls_by_tool[p["tool"]] += 1
    # All 3 plan_workflow + both create_* calls get through —
    # they're exempt from the cap.
    assert tool_calls_by_tool.get("plan_workflow") == 3, tool_calls_by_tool
    assert tool_calls_by_tool.get("create_react_agent") == 1, tool_calls_by_tool
    assert tool_calls_by_tool.get("create_router_pattern") == 1, tool_calls_by_tool
    # Exactly `cap` add_node calls (the cap fired on the cap-th).
    assert tool_calls_by_tool.get("add_node") == cap, tool_calls_by_tool

def test_streaming_read_only_tools_exempt_from_cap(
    client, db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Read-only diagnostic tools (get_node_types /
    get_connection_rules / preview_workflow / get_graph_state)
    are also exempt — the LLM should be able to call them
    liberally without worrying about the budget.
    """
    calls = [
        {"tool_name": "get_node_types", "args": {}},
        {"tool_name": "get_connection_rules", "args": {}},
        {"tool_name": "preview_workflow", "args": {}},
        {"tool_name": "get_graph_state", "args": {}},
        {"tool_name": "get_node_types", "args": {}},
        {"tool_name": "get_connection_rules", "args": {}},
        {"tool_name": "preview_workflow", "args": {}},
        {"tool_name": "get_graph_state", "args": {}},
        {"tool_name": "get_node_types", "args": {}},
        {"tool_name": "get_connection_rules", "args": {}},
        # 10 read-only calls — none of these should hit the cap.
    ]
    events = _streaming_events_for_calls(calls)
    payloads = _collect_sse_events(
        client, db, user, empty_workflow, events, monkeypatch,
    )

    tool_call_events = [p for p in payloads if p["type"] == "tool_call"]
    error_events = [p for p in payloads if p["type"] == "error"]
    # All 10 tool_call events got through — no cap fired.
    assert len(tool_call_events) == 10, (
        f"read-only tools should be exempt; got {len(tool_call_events)} "
        f"events"
    )
    assert len(error_events) == 0, (
        f"no error should fire on read-only traffic, got {error_events}"
    )

def test_consume_stream_routes_run_error_event_with_json_decode_to_friendly_message(
    db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """P2  regression: the LLM stream produced an error
    whose `content` was the raw JSONDecodeError string ("key must be
    a string at line 1 column 3984"). agno surfaces this as a
    `RunErrorEvent` rather than a Python exception, so the existing
    mid-stream try/except guard (which catches `Exception` raised by
    iteration) doesn't fire — the raw error gets passed straight
    to `BuilderErrorEvent.message` and lands in the chat's
    `last_error` field, which is unreadable.

    Lock in the third recovery path: detect JSON-decode-shaped
    `RunErrorEvent.content` and surface the same friendly
    mid-stream message + preserve the partial diff so the user can
    still apply mid-turn work.
    """
    from app.schemas.chat_builder import BuilderErrorEvent
    from app.services.chat_builder_service import (
        _consume_stream,
        _load_or_create_session,
    )
    from app.services import member_service
    from app.auth import CurrentUser

    member_service.bootstrap_owner(db, empty_workflow.id, "alice@example.com")
    db.commit()
    session = _load_or_create_session(
        db, empty_workflow.id,
        CurrentUser(id="alice@example.com", tenant_id="tenant-default"),
    )

    # agno's RunErrorEvent shape: `content` carries the error
    # description. Build a stub matching that attribute surface,
    # then monkeypatch `agno.run.agent.RunErrorEvent` — `_consume_stream`
    # re-imports it lazily each call, so the source-module binding
    # is what the `isinstance(event, RunErrorEvent)` check sees.
    raw_msg = "key must be a string at line 1 column 3984"

    class _StubRunErrorEvent:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(
        "agno.run.agent.RunErrorEvent",
        _StubRunErrorEvent,
        raising=False,
    )

    fake_event = _StubRunErrorEvent(content=raw_msg)
    with pytest.raises(json.JSONDecodeError):
        # `_consume_stream` is responsible for DETECTING the
        # JSON-decode-shaped RunErrorEvent and bubbling it up as
        # a JSONDecodeError (after yielding any partial diff).
        # `run_chat_turn` owns the retry policy + the friendly
        # error message — see the chat-endpoint integration tests
        # below for that path.
        list(_consume_stream(iter([fake_event]), session, empty_workflow.id))

def test_consume_stream_passes_through_non_json_decode_run_error(
    db, seeded_default_preset, user, empty_workflow, monkeypatch
):
    """Counter-test: a `RunErrorEvent` whose content is NOT
    JSON-decode-shaped (e.g. a real LLM-side validation error)
    should still surface its message verbatim — we don't want to
    swallow legitimate error text.
    """
    from app.schemas.chat_builder import BuilderErrorEvent
    from app.services.chat_builder_service import (
        _consume_stream,
        _load_or_create_session,
    )
    from app.services import member_service
    from app.auth import CurrentUser

    member_service.bootstrap_owner(db, empty_workflow.id, "alice@example.com")
    db.commit()
    session = _load_or_create_session(
        db, empty_workflow.id,
        CurrentUser(id="alice@example.com", tenant_id="tenant-default"),
    )

    real_msg = "tools.0.custom.input_schema: invalid type (expected object)"

    class _StubRunErrorEvent:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(
        "agno.run.agent.RunErrorEvent",
        _StubRunErrorEvent,
        raising=False,
    )

    fake_event = _StubRunErrorEvent(content=real_msg)
    events = list(_consume_stream(iter([fake_event]), session, empty_workflow.id))
    errs = [e for e in events if isinstance(e, BuilderErrorEvent)]
    assert len(errs) == 1
    assert errs[0].message == real_msg

def test_is_json_decode_message_matches_common_shapes():
    """Unit-level — the helper's needle set covers the common
    JSONDecodeError surfaces from the Anthropic SDK SSE parser.
    If a new SDK release adds a new message shape, this test
    catches it so we update the helper."""
    from app.services.chat_builder_service import _is_json_decode_message

    # Positive cases
    for msg in [
        "key must be a string at line 1 column 3984",
        "Expecting value at line 5 column 12 (char 87)",
        "Expecting property name enclosed in double quotes at line 1 column 2",
        "Unterminated string at line 3 column 0",
        "Invalid \\escape at line 1 column 7",
        "Extra data at line 1 column 142 (char 142)",
        "foo.JSONDecodeError: bar",
    ]:
        assert _is_json_decode_message(msg), msg

    # Negative cases
    for msg in [
        "tools.0.custom.input_schema: invalid type",
        "rate limit exceeded",
        "401 Unauthorized",
    ]:
        assert not _is_json_decode_message(msg), msg

def test_extract_colno_pulls_column_from_standard_format():
    from app.services.chat_builder_service import _extract_colno

    assert _extract_colno("key must be a string at line 1 column 3984") == 3984
    assert _extract_colno("Expecting value at line 5 column 12") == 12
    assert _extract_colno("no column here") is None

# ─────────────────────────────────────────────────────────────────
# Strict write-time validation in add_node / update_node /
# plan_workflow, plus read-time lax contract.
# ─────────────────────────────────────────────────────────────────
class TestStrictWriteTime:
    """Write-time strict validation. The lax read-time path is
    tested separately in `test_node_config_schemas.py::
    TestAgentConfig::test_extra_fields_silently_ignored` — that
    invariant is preserved."""

    def _seed(self, db, user, empty_workflow):
        return cbs._load_or_create_session(db, empty_workflow.id, user)

    def test_add_node_rejects_unknown_field_with_hint(
        self, db, user, empty_workflow,
    ):
        """`add_node` with a flat `selector_expression` on a router
        (LLM's classic  mistake) must raise
        `ToolCallRejected` whose JSON envelope carries
        `INVALID_CONFIG` issues with a `selector.expression` hint."""
        session = self._seed(db, user, empty_workflow)
        with pytest.raises(cbs.ToolCallRejected) as excinfo:
            cbs._add_node(
                session,
                workflow_id=empty_workflow.id,
                type="router",
                id="r1",
                position={"x": 0, "y": 0},
                config={"selector_expression": "x"},
            )
        payload = json.loads(str(excinfo.value))
        assert payload["ok"] is False
        codes = [i["code"] for i in payload["issues"]]
        assert "invalidConfig" in codes
        # Hint points at the nested field the LLM was reaching for.
        hints = [i.get("hint", "") for i in payload["issues"]]
        assert any("selector.expression" in h for h in hints), (
            f"hint should name selector.expression; got: {hints}"
        )
        # Path uses the JSONPath-style convention so the LLM can
        # localise the failure.
        paths = [i["path"] for i in payload["issues"]]
        assert any("selector_expression" in p for p in paths)

    def test_update_node_rejects_unknown_field_with_hint(
        self, db, user, empty_workflow,
    ):
        """Same contract on `update_node`: the LLM patches an
        existing agent with an invented `instrctions` field →
        strict pre-check raises with a hint naming the actual field."""
        session = self._seed(db, user, empty_workflow)
        with pytest.raises(cbs.ToolCallRejected) as excinfo:
            cbs._update_node(
                session,
                workflow_id=empty_workflow.id,
                node_id="a1",
                patch={"config": {"instrctions": "be brief"}},
            )
        payload = json.loads(str(excinfo.value))
        assert payload["ok"] is False
        assert any(
            i["code"] == "invalidConfig" for i in payload["issues"]
        )
        assert any(
            "instructions" in (i.get("hint") or "")
            for i in payload["issues"]
        ), (
            f"hint should name instructions; got: "
            f"{[i.get('hint') for i in payload['issues']]}"
        )

    def test_plan_workflow_rejects_unknown_node_field_with_hint(
        self, db, user, empty_workflow,
    ):
        """The same strict gate runs inside `validate_plan` — a
        `plan_workflow` call that ships a node with an unknown
        field surfaces the issue with `nodes[N].` prefixed path
        and the standard hint. This locks in the read-through-the-
        DSL invariant."""
        from app.services.chat_builder_plan import (
            PlanNode,
            WorkflowPlan,
            validate_plan,
        )
        nodes = [
            {"id": "r1", "type": "router", "position": {"x": 0, "y": 0},
             "data": {"label": "R1",
                      "config": {"selector_expression": "x"}}},
        ]
        issues = validate_plan(nodes, [])
        assert any(i.code.name == "INVALID_CONFIG" for i in issues)
        # Path uses nodes[N].data.config.<field> convention.
        paths = [i.path for i in issues]
        assert any(
            p.startswith("nodes[0].data.config.selector_expression")
            for p in paths
        )
        # Hint points at selector.expression (nested form).
        assert any(
            "selector.expression" in (i.hint or "") for i in issues
        )

    def test_legacy_workflow_with_drifted_field_still_loads(
        self, db, user, empty_workflow,
    ):
        """Read-time MUST stay lax. A persisted workflow whose
        config has drifted fields (the very reason we want strict
        at write-time) still loads cleanly when re-opened. This
        is the F7 invariant that protects every saved workflow
        from the strict sibling upgrade."""
        from app.schemas.workflow import WorkflowNode
        legacy_node = {
            "id": "r1", "type": "router",
            "position": {"x": 0.0, "y": 0.0},
            "data": {"label": "R1",
                       # `condition` was a legacy field (now None);
                       # `selector_expression` is the  LLM
                       # mistake. Both should load via the lax path.
                       "config": {"condition": "is it urgent?",
                                  "selector_expression": "x",
                                  "selector": {"mode": "function",
                                                "expression": "yes_step"},
                                  "branches": []}},
        }
        # The lax WorkflowNode.model_validate must succeed even
        # though the strict sibling would reject this exact shape.
        validated = WorkflowNode.model_validate(legacy_node)
        # The drifted fields are silently dropped; the valid fields
        # survive and survive coercion.
        cfg = validated.data.get("config") or {}
        assert cfg.get("selector", {}).get("expression") == "yes_step"
        # `condition` and `selector_expression` are NOT in the lax
        # schema, so they're gone from the post-coercion view.
        assert "condition" not in cfg
        assert "selector_expression" not in cfg

    def test_create_react_agent_validates_strict_before_planning(
        self, db, user, empty_workflow,
    ):
        """`create_react_agent` is a high-level pattern that
        builds a `WorkflowPlan` internally. The strict pre-check
        must fire on the tool-source configs before the plan
        commits — otherwise the LLM's typo would slip through the
        pattern abstraction layer.

        We assert this by checking that the tool's docstring
        steers the LLM away from the mistake AND that the strict
        sibling would reject the same shape (the tool's plan path
        goes through `validate_plan`, which now runs strict-first).
        """
        from app.services.chat_builder_plan import (
            PlanNode,
            WorkflowPlan,
            validate_plan,
        )
        # Build a plan as if from create_react_agent with a bad
        # tool config (selector_expression typo).
        plan = WorkflowPlan(nodes=[
            # The legacy `http` type is now `tool` with
            # `source='http'`. Plan nodes typed as the legacy
            # `http` get migrated to `tool`+`source='http'`
            # on apply.
            PlanNode(
                id="http1", type="tool",
                position={"x": 0.0, "y": 0.0},
                data={"label": "HTTP1",
                       "config": {"source": "http",
                                  "method": "GET",
                                  "baseUrl": "https://x",
                                  "path": "/",
                                  "toolName": "x",
                                  "selector_expression": "wrong"}},
            ),
        ])
        from app.services.chat_builder_plan import (
            apply_plan_to_snapshot,
        )
        new_nodes, new_edges = apply_plan_to_snapshot(
            base_nodes=[], base_edges=[], plan=plan,
        )
        issues = validate_plan(new_nodes, new_edges)
        assert any(i.code.name == "INVALID_CONFIG" for i in issues), (
            f"strict-first pre-check should have caught "
            f"selector_expression typo; got: "
            f"{[(i.path, i.code, i.hint) for i in issues]}"
        )
        # Hint names the invalid field by its real (nested) name.
        assert any(
            "selector.expression" in (i.hint or "")
            or "Unknown field" in (i.hint or "")
            for i in issues
        )

    # ───────────────────────────────────────────────────────────────
    # `plan_workflow` shape-error regression. Previously
    # the LLM got a single opaque Issue with the raw Pydantic error
    # string ("1 validation error for WorkflowPlan / nodes.2.type /
    # Field required …"), no field name, no node identifier, no
    # actionable hint. Real-world impact: the LLM couldn't self-fix
    # and re-queried `get_node_types` instead of patching the
    # payload. `_plan_shape_issues` now produces a per-error Issue
    # with a precise `path`, a `message` that names the node's
    # index + (when available) its `data.label`, and a `hint`
    # that points at the exact field to fix.
    # ───────────────────────────────────────────────────────────────
    def test_plan_workflow_missing_type_surfaces_targeted_issue(
        self, db, user, empty_workflow,
    ):
        """Pydantic's bulk error for `nodes[N].type Field required`
        becomes a single `MISSING_REQUIRED_FIELD` Issue at the
        exact path, with a hint naming the valid type names."""
        sid = cbs._load_or_create_session(
            db, empty_workflow.id, user,
        ).session_id
        session = cbs._SESSIONS[sid]
        # Node 1 has `type`; node 2 is missing `type` (real bug
        # observed on ). `id` is auto-generated so it's
        # not required, but we include it so the test reproduces the
        # LLM's typical structure.
        plan = {
            "nodes": [
                {"id": "agent1", "type": "agent",
                 "position": {"x": 0, "y": 0},
                 "data": {"label": "A1",
                          "config": {"instructions": "x"}}},
                # MISSING `type` — this is the bug we want to flag.
                {"id": "tool_query",
                 "position": {"x": 0, "y": 0},
                 "data": {"label": "Query Tool",
                          "config": {"source": "http",
                                     "baseUrl": "http://x",
                                     "path": "/", "method": "GET",
                                     "toolName": "q"}}},
            ],
            "edges": [],
        }
        result = cbs._plan_workflow(session, empty_workflow.id, plan)
        import json as _json
        payload = _json.loads(result)
        assert payload["ok"] is False, (
            f"missing-type plan should be rejected; got: {payload}"
        )
        # At least one Issue must point at `nodes[1].type` with
        # the missing-required-field code + a hint that names the
        # valid types.
        bad = [
            i for i in payload["issues"]
            if i.get("path") == "nodes[1].type"
            and i.get("code") == "missingRequiredField"
        ]
        assert bad, (
            f"expected a missingRequiredField issue at "
            f"nodes[1].type; got: "
            f"{[(i.get('path'), i.get('code')) for i in payload['issues']]}"
        )
        # Hint names the fix — LLM can self-correct without
        # re-querying get_node_types.
        assert any(
            "agent" in (i.get("hint") or "")
            and "tool" in (i.get("hint") or "")
            for i in bad
        ), (
            f"hint should name the valid type set; got: "
            f"{[i.get('hint') for i in bad]}"
        )
        # Message names the node's label so the LLM can find it
        # in its own plan payload.
        assert any(
            "Query Tool" in (i.get("message") or "")
            for i in bad
        ), (
            f"message should name the offending node's label; got: "
            f"{[i.get('message') for i in bad]}"
        )

    def test_plan_workflow_missing_id_warns_edge_wiring(
        self, db, user, empty_workflow,
    ):
        """PlanNode.id is OPTIONAL (backend auto-generates), but
        edges reference ids — without `id`, the LLM silently loses
        wiring. The new pre-check warns so the LLM can add the id
        explicitly instead of relying on the auto-generated uuid."""
        sid = cbs._load_or_create_session(
            db, empty_workflow.id, user,
        ).session_id
        session = cbs._SESSIONS[sid]
        plan = {
            "nodes": [
                {"id": "agent1", "type": "agent",
                 "position": {"x": 0, "y": 0},
                 "data": {"label": "A1",
                          "config": {"instructions": "x"}}},
                # MISSING `id` AND `type` — both surface as
                # targeted issues.
                {"position": {"x": 0, "y": 0},
                 "data": {"label": "Mystery Tool",
                          "config": {}}},
            ],
            # Edge to the missing-id node — proves the id is
            # genuinely required for wiring.
            "edges": [
                {"source": "agent1",
                 "target": "Mystery Tool" if False else "tool_query",
                 "kind": "dataflow"},
            ],
        }
        result = cbs._plan_workflow(session, empty_workflow.id, plan)
        import json as _json
        payload = _json.loads(result)
        assert payload["ok"] is False
        paths = {i.get("path") for i in payload["issues"]}
        # Both `id` AND `type` Issues for node index 1.
        assert "nodes[1].type" in paths, (
            f"missing-type issue expected at nodes[1].type; "
            f"got paths: {paths}"
        )
        assert "nodes[1].id" in paths, (
            f"missing-id issue expected at nodes[1].id; "
            f"got paths: {paths}"
        )
        # The id-missing hint must explain WHY (edge wiring).
        id_issues = [
            i for i in payload["issues"]
            if i.get("path") == "nodes[1].id"
        ]
        assert any(
            "edges" in (i.get("hint") or "").lower()
            for i in id_issues
        ), (
            f"id-missing hint should mention edges; got: "
            f"{[i.get('hint') for i in id_issues]}"
        )

    def test_plan_workflow_missing_edge_source_or_target(
        self, db, user, empty_workflow,
    ):
        """Edges must have `source` and `target` — both required.
        The new pre-check emits a targeted Issue per missing field
        so the LLM can fix one edge at a time."""
        sid = cbs._load_or_create_session(
            db, empty_workflow.id, user,
        ).session_id
        session = cbs._SESSIONS[sid]
        plan = {
            "nodes": [
                {"id": "agent1", "type": "agent",
                 "position": {"x": 0, "y": 0},
                 "data": {"label": "A1",
                          "config": {"instructions": "x"}}},
            ],
            "edges": [
                # MISSING `target` — should surface targeted issue.
                {"source": "agent1"},
                # MISSING `source` — should surface targeted issue.
                {"target": "agent1"},
            ],
        }
        result = cbs._plan_workflow(session, empty_workflow.id, plan)
        import json as _json
        payload = _json.loads(result)
        assert payload["ok"] is False
        paths = {i.get("path") for i in payload["issues"]}
        assert "edges[0].target" in paths, (
            f"missing-target issue expected at edges[0].target; "
            f"got paths: {paths}"
        )
        assert "edges[1].source" in paths, (
            f"missing-source issue expected at edges[1].source; "
            f"got paths: {paths}"
        )
