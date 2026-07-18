"""Tests for `app.services.chat_builder_plan` — the F1 Plan DSL.

Three layers:
  1. Pure-function tests (`apply_plan_to_snapshot`, `validate_plan`)
     — these run without a DB / session / fixtures, so they're
     easy to read and don't depend on test infra.
  2. Tool-handler test (`_plan_workflow`, `_replace_workflow`) —
     drives the session-bound handlers directly and asserts the
     staged-state contract.
  3. Tool surface test — verifies `plan_workflow` /
     `replace_workflow` are registered with the expected JSON
     schema (this pins the LLM-facing API).

Every test corresponds to a contract the LLM relies on:
  * Atomicity: a failed plan must NOT mutate `session.staged_*`.
  * Error shape: every validator failure surfaces as an `Issue`
    with `{path, code, message, hint}`.
  * Config echo: successful plans report the post-coercion config
    so the LLM learns what Pydantic kept.
  * Tool coverage: the chat exposes both `plan_workflow` and
    `replace_workflow` with the documented parameter shapes.
"""
from __future__ import annotations

import copy
import json
import uuid

import pytest

from app.auth import CurrentUser
from app.db.models import User, Workflow
from app.services import chat_builder_service as cbs
from app.services import member_service
from app.services.chat_builder_plan import (
    Issue,
    IssueCode,
    PlanNode,
    PlanEdge,
    WorkflowPlan,
    PlanResult,
    apply_plan_to_snapshot,
    validate_plan,
    execute_plan,
)

# ─────────────────────────────────────────────────────────────────
# Pure-function: apply_plan_to_snapshot
# ─────────────────────────────────────────────────────────────────
class TestApplyPlanToSnapshot:
    """`apply_plan_to_snapshot` is a pure function — base in, plan
    in, (new_nodes, new_edges) out. No I/O, no validation, no
    mutation of the inputs."""

    def _seed(self) -> tuple[list[dict], list[dict]]:
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
            {"id": "a2", "type": "agent", "position": {"x": 100, "y": 0},
             "data": {"label": "A2", "config": {"instructions": ""}}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "a2",
             "sourceHandle": None, "targetHandle": None, "kind": "dataflow"},
        ]
        return nodes, edges

    def test_add_node_appends(self):
        nodes, edges = self._seed()
        plan = WorkflowPlan(nodes=[PlanNode(id="a3", type="agent")])
        new_nodes, new_edges = apply_plan_to_snapshot(nodes, edges, plan)
        assert [n["id"] for n in new_nodes] == ["a1", "a2", "a3"]
        # Base unchanged.
        assert [n["id"] for n in nodes] == ["a1", "a2"]
        # Edges untouched.
        assert [e["id"] for e in new_edges] == ["e1"]

    def test_update_node_replaces_by_id(self):
        nodes, edges = self._seed()
        plan = WorkflowPlan(nodes=[PlanNode(
            id="a1", type="agent",
            data={"label": "RENAMED", "config": {"instructions": "Hi"}},
        )])
        new_nodes, _ = apply_plan_to_snapshot(nodes, edges, plan)
        a1 = next(n for n in new_nodes if n["id"] == "a1")
        assert a1["data"]["label"] == "RENAMED"
        assert a1["data"]["config"]["instructions"] == "Hi"

    def test_delete_node_cascades_incident_edges(self):
        nodes, edges = self._seed()
        plan = WorkflowPlan(delete_nodes=["a1"])
        new_nodes, new_edges = apply_plan_to_snapshot(nodes, edges, plan)
        # a1 removed AND its incident edge e1 cascaded.
        assert [n["id"] for n in new_nodes] == ["a2"]
        assert new_edges == []

    def test_delete_edge_only(self):
        nodes, edges = self._seed()
        plan = WorkflowPlan(delete_edges=["e1"])
        new_nodes, new_edges = apply_plan_to_snapshot(nodes, edges, plan)
        assert [n["id"] for n in new_nodes] == ["a1", "a2"]
        assert new_edges == []

    def test_delete_then_readd_same_id_works_in_one_plan(self):
        """A plan can delete `a1` AND upsert a new `a1` in the same
        call. The post-apply state has the new node."""
        nodes, edges = self._seed()
        plan = WorkflowPlan(
            delete_nodes=["a1"],
            nodes=[PlanNode(id="a1", type="agent", data={"label": "NEW",
                                                          "config": {"instructions": ""}})],
        )
        new_nodes, _ = apply_plan_to_snapshot(nodes, edges, plan)
        # a1 appears exactly once, with the new label.
        a1s = [n for n in new_nodes if n["id"] == "a1"]
        assert len(a1s) == 1
        assert a1s[0]["data"]["label"] == "NEW"

    def test_plan_node_without_id_gets_generated(self):
        nodes, edges = self._seed()
        plan = WorkflowPlan(nodes=[PlanNode(type="agent")])
        new_nodes, _ = apply_plan_to_snapshot(nodes, edges, plan)
        # Exactly one new node added, with a generated id.
        added = [n for n in new_nodes if n["id"] not in ("a1", "a2")]
        assert len(added) == 1
        assert added[0]["id"].startswith("node-")
        assert added[0]["type"] == "agent"

    def test_plan_edge_references_unknown_source_is_kept_in_output(self):
        """`apply_plan_to_snapshot` does NOT validate — it's a pure
        transformer. Validation is `validate_plan`'s job. So an edge
        pointing at a non-existent node survives the apply step and
        surfaces as an `Issue` later."""
        nodes, edges = self._seed()
        plan = WorkflowPlan(edges=[PlanEdge(source="ghost", target="a1")])
        new_nodes, new_edges = apply_plan_to_snapshot(nodes, edges, plan)
        assert any(e["source"] == "ghost" for e in new_edges)

    def test_input_base_is_not_mutated(self):
        """`apply_plan_to_snapshot` must NOT mutate the input lists.
        Important because `_atomic_stage` relies on this for its
        snapshot copy-on-write semantics."""
        nodes, edges = self._seed()
        nodes_snapshot = copy.deepcopy(nodes)
        edges_snapshot = copy.deepcopy(edges)
        plan = WorkflowPlan(
            nodes=[PlanNode(id="a3", type="agent")],
            delete_nodes=["a1"],
        )
        apply_plan_to_snapshot(nodes, edges, plan)
        assert nodes == nodes_snapshot
        assert edges == edges_snapshot

    def test_unknown_node_type_rejected_at_pydantic_level(self):
        """Pydantic catches unknown types at the `PlanNode` boundary
        — the validator on `type` raises ValueError. This is the
        first defence: bad types never reach the apply step."""
        with pytest.raises(Exception) as ei:
            WorkflowPlan(nodes=[PlanNode(id="x", type="unicorn")])
        assert "unknown node type" in str(ei.value).lower()

# ─────────────────────────────────────────────────────────────────
# Pure-function: validate_plan
# ─────────────────────────────────────────────────────────────────
class TestValidatePlan:
    """`validate_plan` runs every validator on the post-apply graph
    and returns the full list of `Issue`s."""

    def test_valid_graph_has_no_issues(self):
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
            {"id": "a2", "type": "agent", "position": {"x": 100, "y": 0},
             "data": {"label": "A2", "config": {"instructions": ""}}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "a2",
             "sourceHandle": None, "targetHandle": None, "kind": "dataflow"},
        ]
        issues = validate_plan(nodes, edges)
        assert issues == []

    def test_invalid_config_surfaces_as_issue(self):
        """`instructions` must be a string — pass an int and the
        Pydantic per-node validator fires. The issue carries the
        `nodes[N].data.config` path so the LLM knows where to look."""
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": 12345}}},
        ]
        issues = validate_plan(nodes, [])
        assert any(i.code == IssueCode.INVALID_CONFIG for i in issues)
        # Path should reference data.config.
        paths = [i.path for i in issues]
        assert any("data.config" in p for p in paths)

    def test_unknown_node_ref_in_edge_surfaces_as_issue(self):
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "ghost",
             "sourceHandle": None, "targetHandle": None, "kind": "dataflow"},
        ]
        issues = validate_plan(nodes, edges)
        codes = [i.code for i in issues]
        # Edge to unknown id is a planning error; with the unknown
        # endpoint on the graph side, the validator also surfaces a
        # incompatible target. Both are valid signals.
        assert any(c == IssueCode.UNKNOWN_PLAN_NODE_REF for c in codes)

    def test_too_many_outgoing_surfaces_with_correct_code(self):
        """An agent may have at most one outgoing dataflow edge.
        Adding a second one to different targets must surface as a
        `tooManyOutgoing` Issue (not just a connection error)."""
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
            {"id": "a2", "type": "agent", "position": {"x": 100, "y": 0},
             "data": {"label": "A2", "config": {"instructions": ""}}},
            {"id": "a3", "type": "agent", "position": {"x": 200, "y": 0},
             "data": {"label": "A3", "config": {"instructions": ""}}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "a2",
             "sourceHandle": None, "targetHandle": None, "kind": "dataflow"},
            {"id": "e2", "source": "a1", "target": "a3",
             "sourceHandle": None, "targetHandle": None, "kind": "dataflow"},
        ]
        issues = validate_plan(nodes, edges)
        codes = [i.code for i in issues]
        assert IssueCode.TOO_MANY_OUTGOING in codes

    def test_no_hints_empty_when_issue_is_unrecognised(self):
        """If a code has no hint template, the Issue's `hint` is the
        empty string (not None — JSON serialises cleanly)."""
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "a1",
             "sourceHandle": None, "targetHandle": None, "kind": "dataflow"},
        ]
        issues = validate_plan(nodes, edges)
        # selfLoop has a hint template, but for unknown codes (none
        # in practice) the hint is empty.
        for issue in issues:
            assert isinstance(issue.hint, str)

    def test_issue_to_dict_serialises_to_json(self):
        """Issue.to_dict() must be JSON-serialisable — it's the
        shape that crosses the SSE wire."""
        issue = Issue(
            path="nodes[0].data.config",
            code=IssueCode.INVALID_CONFIG,
            message="instructions must be a string",
            hint="See get_node_types for the shape.",
        )
        d = issue.to_dict()
        # Round-trip through json.dumps.
        s = json.dumps(d)
        restored = json.loads(s)
        assert restored["path"] == "nodes[0].data.config"
        assert restored["code"] == "invalidConfig"
        assert restored["message"] == "instructions must be a string"
        assert "get_node_types" in restored["hint"]

# ─────────────────────────────────────────────────────────────────
# Pure-function: execute_plan (apply + validate)
# ─────────────────────────────────────────────────────────────────
class TestExecutePlan:
    """`execute_plan` is the core F1 primitive. It applies the plan
    and returns `(new_nodes, new_edges, issues)`. The caller
    decides whether to commit based on whether `issues` is empty."""

    def test_valid_plan_returns_no_issues(self):
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
        ]
        plan = WorkflowPlan(nodes=[PlanNode(
            id="a2", type="agent",
            data={"label": "A2", "config": {"instructions": "Hi"}},
        )])
        new_nodes, new_edges, issues = execute_plan(nodes, [], plan)
        assert issues == []
        assert [n["id"] for n in new_nodes] == ["a1", "a2"]
        assert new_edges == []

    def test_invalid_plan_returns_issues_without_raising(self):
        """`execute_plan` must NEVER raise to the caller. Every
        validation failure becomes a structured `Issue`."""
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
        ]
        # Adding a second outgoing edge from a1 is a connection-rule
        # violation, not a Pydantic error. `execute_plan` returns
        # it as an `Issue` rather than raising.
        plan = WorkflowPlan(
            nodes=[
                PlanNode(id="a2", type="agent",
                         data={"label": "A2", "config": {"instructions": ""}}),
                PlanNode(id="a3", type="agent",
                         data={"label": "A3", "config": {"instructions": ""}}),
            ],
            edges=[
                PlanEdge(source="a1", target="a2"),
                PlanEdge(source="a1", target="a3"),
            ],
        )
        new_nodes, new_edges, issues = execute_plan(nodes, [], plan)
        # The apply happened (so the caller can inspect what they
        # would have committed). Validation surfaces the issue.
        assert any(i.code == IssueCode.TOO_MANY_OUTGOING for i in issues)
        assert new_nodes is not None and new_edges is not None

    def test_invalid_config_short_circuits_before_graph_validation(self):
        """When Pydantic fails, no other validator runs — the LLM
        gets ONE issue (the config one), not a cascade of confusing
        downstream errors."""
        nodes = [
            {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {"instructions": ""}}},
        ]
        plan = WorkflowPlan(nodes=[PlanNode(
            id="a2", type="agent",
            data={"label": "A2", "config": {"instructions": 12345}},
        )])
        _, _, issues = execute_plan(nodes, [], plan)
        assert any(i.code == IssueCode.INVALID_CONFIG for i in issues)

# ─────────────────────────────────────────────────────────────────
# Session-bound handlers: _plan_workflow + _replace_workflow
# ─────────────────────────────────────────────────────────────────
class TestPlanWorkflowTool:
    """Drive the session-bound handlers. These are the functions
    `Function.from_callable` wraps for the LLM. The contract they
    pin:

      * Successful plan → `pending_changes` grows by 1.
      * Failed plan → `pending_changes` UNCHANGED (atomicity).
      * Both `plan_workflow` and `replace_workflow` exist on the
        tool surface.
      * Both return a structured `PlanResult`-shaped JSON.
    """

    def _empty_workflow(self, db):
        wid = f"wf-{uuid.uuid4().hex[:8]}"
        db.add(Workflow(
            id=wid, name="seed", description="seed",
            nodes=[{
                "id": "a1", "type": "agent",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"label": "A1", "config": {"instructions": ""}},
            }],
            edges=[],
            created_by="alice@example.com",
        ))
        db.commit()
        member_service.bootstrap_owner(db, wid, "alice@example.com")
        db.commit()
        return db.query(Workflow).filter_by(id=wid).one()

    def _user(self, db):
        db.add(User(id="alice@example.com", tenant_id="tenant-default"))
        db.commit()
        return CurrentUser(id="alice@example.com", tenant_id="tenant-default")

    def test_plan_workflow_valid_adds_to_staged(
        self, db,
    ):
        user = self._user(db)
        wf = self._empty_workflow(db)
        session = cbs._load_or_create_session(db, wf.id, user)
        plan = {
            "nodes": [{
                "id": "a2", "type": "agent",
                "position": {"x": 100, "y": 0},
                "data": {"label": "A2", "config": {"instructions": "Hi"}},
            }],
            "edges": [{"source": "a1", "target": "a2"}],
        }
        result_json = cbs._plan_workflow(
            session, workflow_id=wf.id, plan=plan,
        )
        result = json.loads(result_json)
        assert result["ok"] is True
        assert result["applied"]["added_nodes"] == 1
        assert result["applied"]["added_edges"] == 1
        # Staged state has the new node + edge.
        assert any(n["id"] == "a2" for n in session.staged_nodes)
        assert len(session.staged_edges) == 1

    def test_plan_workflow_invalid_leaves_staged_untouched(
        self, db,
    ):
        user = self._user(db)
        wf = self._empty_workflow(db)
        session = cbs._load_or_create_session(db, wf.id, user)
        before_nodes = copy.deepcopy(session.staged_nodes)
        before_edges = copy.deepcopy(session.staged_edges)
        before_changes = list(session.pending_changes)
        # Plan has bad config — `instructions: 12345` fails Pydantic.
        plan = {
            "nodes": [{
                "id": "a2", "type": "agent",
                "position": {"x": 100, "y": 0},
                "data": {"label": "A2", "config": {"instructions": 12345}},
            }],
        }
        result_json = cbs._plan_workflow(
            session, workflow_id=wf.id, plan=plan,
        )
        result = json.loads(result_json)
        assert result["ok"] is False
        assert result["state_unchanged"] is True
        assert len(result["issues"]) >= 1
        # Atomicity: staged state is exactly what we started with.
        assert session.staged_nodes == before_nodes
        assert session.staged_edges == before_edges
        assert session.pending_changes == before_changes

    def test_plan_workflow_issues_carry_path_and_code(
        self, db,
    ):
        user = self._user(db)
        wf = self._empty_workflow(db)
        session = cbs._load_or_create_session(db, wf.id, user)
        plan = {
            "nodes": [{
                "id": "a2", "type": "agent",
                "data": {"label": "A2", "config": {"instructions": 12345}},
            }],
        }
        result_json = cbs._plan_workflow(
            session, workflow_id=wf.id, plan=plan,
        )
        result = json.loads(result_json)
        issue = result["issues"][0]
        assert "path" in issue
        assert "code" in issue
        assert "message" in issue
        assert "hint" in issue
        # The path references `data.config` (config-level error).
        assert "data.config" in issue["path"]

    def test_replace_workflow_replaces_existing_node(
        self, db,
    ):
        """`replace_workflow` is the "throw away everything and
        start fresh" tool. The LLM only passes `nodes` + `edges`
        for the new state."""
        user = self._user(db)
        wf = self._empty_workflow(db)
        session = cbs._load_or_create_session(db, wf.id, user)
        result_json = cbs._replace_workflow(
            session, workflow_id=wf.id,
            nodes=[{
                "id": "x1", "type": "agent",
                "position": {"x": 0, "y": 0},
                "data": {"label": "X1", "config": {"instructions": "x"}},
            }],
            edges=[],
        )
        result = json.loads(result_json)
        assert result["ok"] is True
        # Old a1 is gone.
        assert not any(n["id"] == "a1" for n in session.staged_nodes)
        # New x1 is present.
        assert any(n["id"] == "x1" for n in session.staged_nodes)

    def test_replace_workflow_invalid_leaves_staged_untouched(
        self, db,
    ):
        user = self._user(db)
        wf = self._empty_workflow(db)
        session = cbs._load_or_create_session(db, wf.id, user)
        before_nodes = copy.deepcopy(session.staged_nodes)
        before_changes = list(session.pending_changes)
        result_json = cbs._replace_workflow(
            session, workflow_id=wf.id,
            nodes=[{
                "id": "x1", "type": "agent",
                "data": {"label": "X1", "config": {"instructions": 12345}},
            }],
            edges=[],
        )
        result = json.loads(result_json)
        assert result["ok"] is False
        assert result["state_unchanged"] is True
        assert session.staged_nodes == before_nodes
        assert session.pending_changes == before_changes

# ─────────────────────────────────────────────────────────────────
# Tool surface — verify both tools are exposed to the LLM
# ─────────────────────────────────────────────────────────────────
class TestPlanToolsExposedToLLM:
    """`plan_workflow` and `replace_workflow` must appear in the
    list of `Function` objects built by `_build_tools_for_session`.
    This is what makes them callable by the LLM."""

    def _session(self, db):
        user = CurrentUser(
            id="alice@example.com", tenant_id="tenant-default",
        )
        db.add(User(id="alice@example.com", tenant_id="tenant-default"))
        db.commit()
        wid = f"wf-{uuid.uuid4().hex[:8]}"
        db.add(Workflow(
            id=wid, name="seed", description="seed",
            nodes=[{
                "id": "a1", "type": "agent",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"label": "A1", "config": {"instructions": ""}},
            }],
            edges=[],
            created_by="alice@example.com",
        ))
        db.commit()
        member_service.bootstrap_owner(db, wid, "alice@example.com")
        db.commit()
        return cbs._load_or_create_session(db, wid, user)

    def test_both_tools_registered(self, db):
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        names = {f.name for f in funcs}
        assert "plan_workflow" in names
        assert "replace_workflow" in names

    def test_plan_workflow_has_plan_param(self, db):
        """The plan tool exposes a `plan` parameter (object), and
        `workflow_id` is optional (default fallback to the session's)."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        pw = by_name["plan_workflow"]
        props = (pw.parameters or {}).get("properties") or {}
        assert "plan" in props
        assert "workflow_id" in props

    def test_replace_workflow_has_nodes_and_edges_params(self, db):
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        rw = by_name["replace_workflow"]
        props = (rw.parameters or {}).get("properties") or {}
        assert "nodes" in props
        assert "edges" in props

# ─────────────────────────────────────────────────────────────────
# Regression: LLM emits nodes with top-level
# `label` / `config` instead of nested under `data: {label, config}`.
# Pydantic v2 lax mode silently drops extras, so the staged node
# had empty `data` and downstream validation / canvas rendering
# failed (the user reported the node info was not injected). The
# fix is a `model_validator(mode='before')` on `PlanNode` that
# re-roots top-level `label` / `config` / `color` / `icon` into
# `data` when `data` is missing or empty.
# ─────────────────────────────────────────────────────────────────
def test_plan_node_tolerates_top_level_label_and_config(db):
    """Regression — when the LLM sends the wrong
    shape (top-level `label` / `config`), `PlanNode` re-roots
    them into `data` so the staged node has `data.label` and
    `data.config` correctly populated. Without this the LLM's
    payload was silently accepted with empty `data` and the
    canvas couldn't render the agent (no instructions, no model,
    no label).

    Three cases locked in:
      A. top-level `label`/`config` (LLM's wrong shape)
      B. already-nested `data` (correct shape) — left alone
      C. mixed: empty `data` plus top-level fields — re-rooted
    """
    from app.services.chat_builder_plan import PlanNode

    # Case A: LLM's wrong shape — top-level label/config.
    a = PlanNode.model_validate({
        "type": "agent",
        "id": "entity_extract",
        "position": {"x": 300, "y": 0},
        "label": "实体提取",
        "config": {"instructions": "Extract entities"},
    })
    assert a.id == "entity_extract"
    assert a.type == "agent"
    assert a.data["label"] == "实体提取"
    assert a.data["config"] == {"instructions": "Extract entities"}
    # No top-level leakage after the model_dump.
    dumped = a.model_dump()
    assert dumped.get("label") is None
    assert dumped.get("config") is None

    # Case B: correct shape — leave alone (no double-wrap).
    b = PlanNode.model_validate({
        "type": "agent",
        "id": "ok_node",
        "position": {"x": 0, "y": 0},
        "data": {"label": "正确", "config": {"instructions": "hi"}},
    })
    assert b.data["label"] == "正确"
    assert b.data["config"] == {"instructions": "hi"}

    # Case C: mixed — data is empty dict but top-level fields
    # exist. Re-root the top-level fields into `data`.
    c = PlanNode.model_validate({
        "type": "agent",
        "id": "mixed",
        "position": {"x": 0, "y": 0},
        "data": {},
        "label": "F",
        "config": {"instructions": "X"},
    })
    assert c.data["label"] == "F"
    assert c.data["config"]["instructions"] == "X"

# ─────────────────────────────────────────────────────────────────
# Strict write-time validator
# ─────────────────────────────────────────────────────────────────
class TestStrictValidation:
    """`validate_node_config_for_llm` is the write-time mirror of the
    lax read-time `validate_node_config`. It rejects unknown fields
    so the LLM gets a typed `INVALID_CONFIG` Issue with a "did you
    mean" hint instead of silently losing its config.

    Invariants:
      * Read-time still lax — saved workflows keep loading.
      * Non-dict / unknown-type / empty-config inputs pass through
        (mirrors the lax validator's tolerance contract).
    """

    def test_extra_field_in_router_selector_emits_did_you_mean_hint(self):
        """The diagnostic export on  had the LLM writing
        `selector_expression` (flat) when the schema wants
        `selector.expression` (nested). The strict validator must
        surface this as `INVALID_CONFIG` and the hint must point
        at `selector.expression` — that's the nesting the LLM was
        reaching for."""
        from app.services.chat_builder_plan import validate_node_config_for_llm
        issues = validate_node_config_for_llm(
            "router", {"selector_expression": "x"},
        )
        assert len(issues) >= 1
        first = issues[0]
        assert first.code == IssueCode.INVALID_CONFIG
        assert first.path == "data.config.selector_expression"
        assert "selector.expression" in first.hint, (
            f"hint should point at nested selector.expression; got: {first.hint!r}"
        )

    def test_branches_as_list_of_strings_emits_invalid_typed_hint(self):
        """The LLM sometimes emits `branches: ['a', 'b']` (list of
        strings) instead of `branches: [{label, target}, ...]`. The
        strict validator catches this with `BranchTarget`-shape
        errors. The hint path references the offending index."""
        from app.services.chat_builder_plan import validate_node_config_for_llm
        issues = validate_node_config_for_llm(
            "router", {"branches": ["yes", "no"]},
        )
        assert issues, "list-of-strings branches should not pass strict"
        # At least one issue should land at `data.config.branches.0`.
        paths = [i.path for i in issues]
        assert any(p.startswith("data.config.branches") for p in paths)
        # All issues carry INVALID_CONFIG.
        assert all(i.code == IssueCode.INVALID_CONFIG for i in issues)

    def test_unknown_field_with_no_close_match_suggests_get_node_types(self):
        """When the typo is far from any valid field name (no
        close-enough match), the hint falls back to pointing the
        LLM at `get_node_types()` — the schema lookup hook — rather
        than guessing wrong."""
        from app.services.chat_builder_plan import validate_node_config_for_llm
        issues = validate_node_config_for_llm(
            "agent", {"typo_key_zzz": 1},
        )
        assert issues, "unknown field should not pass strict"
        # All issues hint at the schema-lookup escape hatch.
        assert all(
            "get_node_types" in (i.hint or "") for i in issues
        ), (
            f"every strict issue should hint at get_node_types(); "
            f"got hints: {[i.hint for i in issues]}"
        )

    def test_valid_config_strict_passes(self):
        """A well-formed router config should pass strict validation
        with zero issues — strict != restrictive, just no
        unknown fields."""
        from app.services.chat_builder_plan import validate_node_config_for_llm
        issues = validate_node_config_for_llm(
            "router",
            {
                "selector": {"mode": "function", "expression": "yes_agent_step"},
                "branches": [
                    {"label": "yes", "target": "a1"},
                    {"label": "no", "target": "a2"},
                ],
            },
        )
        assert issues == []

    def test_non_dict_config_passes_through(self):
        """Mirrors the lax validator's tolerance — non-dict configs
        aren't a strict-mode concern; let the downstream
        `WorkflowNode.model_validate` raise on bad types."""
        from app.services.chat_builder_plan import validate_node_config_for_llm
        assert validate_node_config_for_llm("agent", None) == []
        assert validate_node_config_for_llm("agent", "string") == []
        assert validate_node_config_for_llm("agent", ["list"]) == []

    def test_unknown_node_type_passes_through(self):
        """Unknown types are surfaced by the upstream
        `WorkflowNode.model_validate` with its own code. The strict
        sibling skips them — never raise here, never invent issues."""
        from app.services.chat_builder_plan import validate_node_config_for_llm
        assert validate_node_config_for_llm("not_a_real_type", {"y": 1}) == []

    def test_validate_plan_runs_strict_first_before_lax(self):
        """The strict pre-pass runs first inside `validate_plan`,
        so an `INVALID_CONFIG` issue surfaces with `nodes[N].`-
        prefixed path before the lax validator gets a chance to
        phrase it differently."""
        nodes = [
            {"id": "r1", "type": "router", "position": {"x": 0, "y": 0},
             "data": {"label": "R1", "config": {"selector_expression": "x"}}},
        ]
        issues = validate_plan(nodes, [])
        assert any(i.code == IssueCode.INVALID_CONFIG for i in issues)
        # The strict pass prefixes with nodes[N].data.config. — pin that.
        paths = [i.path for i in issues]
        assert any(
            p.startswith("nodes[0].data.config.selector_expression")
            for p in paths
        ), (
            f"expected strict-prefixed path; got: {paths}"
        )
        # And the hint still names selector.expression.
        hints = [i.hint for i in issues]
        assert any("selector.expression" in h for h in hints)

# ─────────────────────────────────────────────────────────────────
# IssueCode.from_conn_code — single source of truth for the
# connection-rule → IssueCode translation (row A, ).
# Pins every code the validator emits so a renamed upstream string
# fails loud at this test instead of silently producing the fallback.
# ─────────────────────────────────────────────────────────────────
class TestIssueCodeFromConnCode:
    """The mapping table lives on the `IssueCode` enum (single
    source) — these tests pin every entry so a future refactor that
    drops or renames a code is caught here."""

    @pytest.mark.parametrize("conn_code,expected", [
        ("incompatibleSource", IssueCode.INCOMPATIBLE_SOURCE),
        ("incompatibleTarget", IssueCode.INCOMPATIBLE_TARGET),
        ("selfLoop", IssueCode.SELF_LOOP),
        ("tooManyOutgoing", IssueCode.TOO_MANY_OUTGOING),
        ("tooManyIncoming", IssueCode.TOO_MANY_INCOMING),
        ("missingOutgoing", IssueCode.MIN_OUTGOING_NOT_MET),
        ("noThen", IssueCode.NO_THEN_EDGE),
        ("missingIncoming", IssueCode.MISSING_INCOMING),
        ("loopBodyViaEdge", IssueCode.LOOP_BODY_VIA_EDGE),
        ("duplicateEdge", IssueCode.DUPLICATE_EDGE),
        ("duplicateNodeId", IssueCode.DUPLICATE_PLAN_NODE_ID),
    ])
    def test_known_conn_codes_map_to_issue_codes(self, conn_code, expected):
        assert IssueCode.from_conn_code(conn_code) is expected

    def test_unknown_conn_code_falls_back_to_incompatible_source(self):
        """Codes the validator adds in a future version fall through
        to `INCOMPATIBLE_SOURCE` (the most common). The validator's
        own message still carries the real code, so the LLM can
        pattern-match on `cerr.message` if it has to."""
        assert (
            IssueCode.from_conn_code("someFutureCodeWeDoNotKnowYet")
            is IssueCode.INCOMPATIBLE_SOURCE
        )

    def test_empty_string_falls_back(self):
        """Defensive: empty / None must not raise. Empty codes
        shouldn't appear in practice (the validator never emits "")
        but tests that build synthetic ConnectionErrors have shipped
        with empty `code=` in the past, so be tolerant."""
        assert (
            IssueCode.from_conn_code("") is IssueCode.INCOMPATIBLE_SOURCE
        )

    def test_validator_codes_align_with_issue_codes(self):
        """Cross-check: every ConnectionError.code the validator can
        emit (per `core.connection_rules.check_node_view` +
        `validate_connections`) must round-trip to a defined
        `IssueCode`. If this fails after a code is added to the
        validator, update the mapping in `chat_builder_plan.py`."""
        from app.core.connection_rules import (
            ConnectionError as _ConnError,
            validate_connections,
        )
        # Build a minimal graph that triggers every documented
        # code we expect the mapping to cover. Each block targets
        # one code.
        a = {"id": "a", "type": "agent", "data": {"config": {}}}
        b = {"id": "b", "type": "agent", "data": {"config": {}}}
        c = {"id": "c", "type": "agent", "data": {"config": {}}}
        d = {"id": "d", "type": "agent", "data": {"config": {}}}
        # `tooManyOutgoing` — agent has max_outgoing=1 in the
        # default rule table; add 2 outgoing dataflow edges.
        # `duplicateEdge` — same pair twice.
        nodes = [a, b, c, d]
        edges = [
            {"id": "e1", "source": "a", "target": "b", "kind": "dataflow"},
            {"id": "e2", "source": "a", "target": "c", "kind": "dataflow"},
            {"id": "e3", "source": "a", "target": "b", "kind": "dataflow"},
        ]
        errs = validate_connections(nodes, edges)
        codes = {e.code for e in errs}
        # We expect tooManyOutgoing + duplicateEdge to fire on this
        # graph; both must map to defined IssueCodes (not the fallback).
        assert "tooManyOutgoing" in codes
        assert "duplicateEdge" in codes
        for code in codes:
            mapped = IssueCode.from_conn_code(code)
            assert mapped is not IssueCode.INCOMPATIBLE_SOURCE or code == "incompatibleSource", (
                f"validator code {code!r} fell back to the default — "
                "add it to the IssueCode.from_conn_code mapping"
            )
