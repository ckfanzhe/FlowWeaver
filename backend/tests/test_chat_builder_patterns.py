"""Tests for `app.services.chat_builder_patterns` — the F4
high-level pattern primitives.

Three layers:
  1. Pure-function tests (`build_react_agent_plan` /
     `build_router_pattern_plan` / `build_retry_loop_plan`) — pin
     the plan DSL each pattern produces so a future refactor
     doesn't silently change the LLM-facing semantics.
  2. Session-bound tests — drive the chat builder's tool wrappers
     (`create_react_agent` / `create_router_pattern` /
     `create_retry_loop`) and assert the staged-state contract.
  3. Tool-surface tests — verify all three patterns are exposed to
     the LLM via `_build_tools_for_session`.
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
from app.services.chat_builder_patterns import (
    build_react_agent_plan,
    build_retry_loop_plan,
    build_router_pattern_plan,
)

# ─────────────────────────────────────────────────────────────────
# Pure: build_react_agent_plan
# ─────────────────────────────────────────────────────────────────
class TestBuildReactAgentPlan:
    """`create_react_agent` builds an agent + tool sources + the
    wiring edges. The plan it produces is what the LLM expects to
    see validated."""

    def test_single_tool_produces_two_nodes(self):
        plan = build_react_agent_plan(
            instructions="Help me.",
            # Presets are no longer separate types — the pattern
            # emits `type='tool'` + `config.preset='wikipedia'`.
            tools=[{"type": "tool", "config": {"preset": "wikipedia"}}],
        )
        d = plan.model_dump()
        assert len(d["nodes"]) == 2
        # One agent + one tool source.
        types = [n["type"] for n in d["nodes"]]
        assert "agent" in types
        assert "tool" in types
        # The tool node carries the preset discriminator.
        tool_node = next(n for n in d["nodes"] if n["type"] == "tool")
        assert tool_node["data"]["config"]["preset"] == "wikipedia"

    def test_multiple_tools_produce_one_edge_per_tool(self):
        plan = build_react_agent_plan(
            instructions="Help me.",
            tools=[
                {"type": "tool", "config": {"preset": "wikipedia"}},
                {"type": "tool", "config": {"preset": "calculator"}},
                {"type": "tool", "config": {"preset": "duckduckgo"}},
            ],
        )
        d = plan.model_dump()
        assert len(d["nodes"]) == 4  # agent + 3 tools
        assert len(d["edges"]) == 3
        for edge in d["edges"]:
            # Every edge is tool → agent with tool_attachment kind.
            assert edge["kind"] == "tool_attachment"
            target = edge["target"]
            # The target is the agent (not a tool).
            target_node = next(n for n in d["nodes"] if n["id"] == target)
            assert target_node["type"] == "agent"

    def test_tool_attachment_kind_is_correct(self):
        """`tool_attachment` is the magic value the connection-rule
        table accepts for tool-source → agent wiring. A bare
        `dataflow` edge from a tool source would fail validation."""
        plan = build_react_agent_plan(
            instructions="x",
            tools=[{"type": "tool", "config": {"preset": "wikipedia"}}],
        )
        d = plan.model_dump()
        for edge in d["edges"]:
            assert edge["kind"] == "tool_attachment", (
                f"edge kind is {edge['kind']!r}; tool sources must "
                "use kind='tool_attachment'"
            )

    def test_explicit_id_is_used(self):
        plan = build_react_agent_plan(
            instructions="x",
            tools=[{"type": "tool", "id": "wp1", "config": {"preset": "wikipedia"}}],
            id="my-agent",
        )
        d = plan.model_dump()
        ids = [n["id"] for n in d["nodes"]]
        assert "my-agent" in ids
        assert "wp1" in ids

    def test_max_tool_calls_sets_config(self):
        plan = build_react_agent_plan(
            instructions="x",
            tools=[],
            max_iterations=5,
        )
        d = plan.model_dump()
        agent = next(n for n in d["nodes"] if n["type"] == "agent")
        assert agent["data"]["config"]["toolCallLimit"] == 5

    def test_unknown_tool_type_raises(self):
        with pytest.raises(ValueError) as ei:
            build_react_agent_plan(
                instructions="x",
                tools=[{"type": "unicorn"}],
            )
        assert "unknown node type" in str(ei.value).lower()

    def test_tool_without_type_raises(self):
        with pytest.raises(ValueError):
            build_react_agent_plan(
                instructions="x",
                tools=[{"label": "no-type"}],
            )

# ─────────────────────────────────────────────────────────────────
# Pure: build_router_pattern_plan
# ─────────────────────────────────────────────────────────────────
class TestBuildRouterPatternPlan:
    """`create_router_pattern` builds a router with N branches."""

    def test_single_branch(self):
        plan = build_router_pattern_plan(
            branches=[
                {"type": "agent", "config": {"instructions": "x"}},
            ],
        )
        d = plan.model_dump()
        assert len(d["nodes"]) == 2  # branch + 1 branch
        types = [n["type"] for n in d["nodes"]]
        assert types.count("branch") == 1
        assert types.count("agent") == 1

    def test_multiple_branches_emit_one_edge_each(self):
        plan = build_router_pattern_plan(
            branches=[
                {"type": "agent", "config": {"instructions": "a"}},
                {"type": "agent", "config": {"instructions": "b"}},
                {"type": "agent", "config": {"instructions": "c"}},
            ],
        )
        d = plan.model_dump()
        # One branch + 3 branches = 4 nodes; 3 dataflow edges.
        assert len(d["nodes"]) == 4
        assert len(d["edges"]) == 3
        for edge in d["edges"]:
            assert edge["kind"] == "dataflow"

    def test_router_branches_field_mirrors_targets(self):
        """The branch's `branches` config must list every downstream
        branch with its target id. Pydantic validates this on the
        way through `_plan_workflow`'s `WorkflowNode.model_validate`."""
        plan = build_router_pattern_plan(
            branches=[
                {"type": "agent", "label": "Helper A"},
                {"type": "agent", "label": "Helper B"},
            ],
        )
        d = plan.model_dump()
        branch = next(n for n in d["nodes"] if n["type"] == "branch")
        # Branch is now emitted with `mode='switch'`.
        assert branch["data"]["config"]["mode"] == "switch"
        branches = branch["data"]["config"]["branches"]
        assert len(branches) == 2
        # Each branch entry carries label + target + condition.
        for branch in branches:
            assert "label" in branch
            assert "target" in branch
            assert "condition" in branch

    def test_selector_mode_propagates(self):
        plan = build_router_pattern_plan(
            branches=[{"type": "agent", "config": {"instructions": ""}}],
            selector_mode="cel",
            selector_expression="input.kind == 'urgent'",
        )
        d = plan.model_dump()
        branch = next(n for n in d["nodes"] if n["type"] == "branch")
        sel = branch["data"]["config"]["selector"]
        assert sel["mode"] == "cel"
        assert sel["expression"] == "input.kind == 'urgent'"

    def test_delete_existing_marks_router_for_removal(self):
        plan = build_router_pattern_plan(
            branches=[{"type": "agent", "config": {"instructions": ""}}],
            id="r1",
            delete_existing_router=True,
        )
        d = plan.model_dump()
        assert "r1" in d["delete_nodes"]

    def test_unknown_branch_type_raises(self):
        with pytest.raises(ValueError):
            build_router_pattern_plan(
                branches=[{"type": "unicorn"}],
            )

# ─────────────────────────────────────────────────────────────────
# Pure: build_retry_loop_plan
# ─────────────────────────────────────────────────────────────────
class TestBuildRetryLoopPlan:
    """`create_retry_loop` wraps an agent in a loop. The loop's
    `body_target` carries the wiring signal — no dataflow edge."""

    def test_two_nodes(self):
        plan = build_retry_loop_plan(
            instructions="Retry me.",
            max_iterations=3,
        )
        d = plan.model_dump()
        assert len(d["nodes"]) == 2
        types = [n["type"] for n in d["nodes"]]
        assert "loop" in types
        assert "agent" in types

    def test_no_dataflow_edges(self):
        """`loopBodyViaEdge` is a structured Issue — the connection
        rule table forbids dataflow edges from a loop to its body
        (the loop's `body_target` is the wiring signal). The
        pattern emits zero edges by design."""
        plan = build_retry_loop_plan(
            instructions="x",
            max_iterations=3,
        )
        d = plan.model_dump()
        assert d["edges"] == []

    def test_loop_body_target_set(self):
        plan = build_retry_loop_plan(
            instructions="x",
            agent_id="my-agent",
            loop_id="my-loop",
        )
        d = plan.model_dump()
        loop = next(n for n in d["nodes"] if n["type"] == "loop")
        assert loop["data"]["config"]["bodyTarget"] == "my-agent"

    def test_max_iterations_propagates(self):
        plan = build_retry_loop_plan(
            instructions="x",
            max_iterations=42,
        )
        d = plan.model_dump()
        loop = next(n for n in d["nodes"] if n["type"] == "loop")
        assert loop["data"]["config"]["maxIterations"] == 42

    def test_end_condition_propagates(self):
        plan = build_retry_loop_plan(
            instructions="x",
            max_iterations=5,
            end_condition="DONE",
        )
        d = plan.model_dump()
        loop = next(n for n in d["nodes"] if n["type"] == "loop")
        assert loop["data"]["config"]["endCondition"] == "DONE"

# ─────────────────────────────────────────────────────────────────
# Session-bound handlers — drive the LLM-facing tool wrappers
# ─────────────────────────────────────────────────────────────────
def _setup(db):
    """Stand in for the empty_workflow fixture (we keep this test
    file self-contained — fixtures in test_chat_builder.py aren't
    visible across modules)."""
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
    user = CurrentUser(id="alice@example.com", tenant_id="tenant-default")
    return cbs._load_or_create_session(db, wid, user), wid

class TestCreateReactAgentHandler:
    """`create_react_agent` is the LLM-facing wrapper. It builds
    a plan and routes it through `_plan_workflow`."""

    def test_valid_call_adds_to_staged(self, db):
        session, wf_id = _setup(db)
        result = cbs._create_react_agent_via_wrapper(
            session, wf_id,
            instructions="Help me search.",
            # Presets are no longer separate types — the call
            # emits `type='tool'` + `config.preset=<name>`.
            tools=[
                {"type": "tool", "config": {"preset": "wikipedia"}},
                {"type": "tool", "config": {"preset": "calculator"}},
            ],
        )
        out = json.loads(result)
        assert out["ok"] is True
        # Staged state has the agent + 2 tool sources (each type='tool').
        types = [n["type"] for n in session.staged_nodes]
        assert "agent" in types
        assert types.count("tool") == 2
        # Verify the preset discriminators were set on the tool nodes.
        presets = [
            (n["data"]["config"]["preset"])
            for n in session.staged_nodes if n["type"] == "tool"
        ]
        assert set(presets) == {"wikipedia", "calculator"}
        # Tool-attachment edges wired.
        tool_edges = [
            e for e in session.staged_edges
            if e.get("kind") == "tool_attachment"
        ]
        assert len(tool_edges) == 2

    def test_invalid_tool_type_does_not_mutate(self, db):
        """An unknown tool type fails the pattern builder BEFORE
        `_plan_workflow` is called, so the staged state is
        untouched."""
        session, wf_id = _setup(db)
        before_nodes = copy.deepcopy(session.staged_nodes)
        before_changes = list(session.pending_changes)
        result = cbs._create_react_agent_via_wrapper(
            session, wf_id,
            instructions="x",
            tools=[{"type": "unicorn"}],
        )
        out = json.loads(result)
        assert out["ok"] is False
        assert out["state_unchanged"] is True
        assert session.staged_nodes == before_nodes
        assert session.pending_changes == before_changes

class TestCreateRouterPatternHandler:
    def test_valid_call_adds_router_and_branches(self, db):
        session, wf_id = _setup(db)
        result = cbs._create_router_pattern_via_wrapper(
            session, wf_id,
            branches=[
                {"type": "agent", "config": {"instructions": "a"}},
                {"type": "agent", "config": {"instructions": "b"}},
            ],
            selector_mode="function",
        )
        out = json.loads(result)
        assert out["ok"] is True
        # 1 seed agent + 1 router + 2 branches = 4 nodes;
        # 2 dataflow edges (the seed agent stays isolated).
        assert len(session.staged_nodes) == 4
        assert len(session.staged_edges) == 2

    def test_replace_existing_removes_old_router(self, db):
        session, wf_id = _setup(db)
        # First call: add router r1.
        cbs._create_router_pattern_via_wrapper(
            session, wf_id,
            branches=[{"type": "agent", "config": {"instructions": "a"}}],
            router_id="r1",
        )
        # Second call: replace r1.
        result = cbs._create_router_pattern_via_wrapper(
            session, wf_id,
            branches=[{"type": "agent", "config": {"instructions": "b"}}],
            router_id="r1",
            replace_existing=True,
        )
        out = json.loads(result)
        assert out["ok"] is True
        # Old r1 is gone, new r1 is present (id re-used).
        r1_nodes = [
            n for n in session.staged_nodes if n["id"] == "r1"
        ]
        assert len(r1_nodes) == 1

class TestCreateRetryLoopHandler:
    def test_valid_call_adds_loop_and_agent(self, db):
        session, wf_id = _setup(db)
        result = cbs._create_retry_loop_via_wrapper(
            session, wf_id,
            instructions="Retry me.",
            max_iterations=4,
            end_condition="OK",
        )
        out = json.loads(result)
        assert out["ok"] is True
        types = [n["type"] for n in session.staged_nodes]
        assert "loop" in types
        assert "agent" in types
        # No dataflow edges — the loop's body_target is the wiring.
        assert session.staged_edges == []

    def test_invalid_max_iterations_rejected(self, db):
        """`max_iterations` is bounded 1..1000 by Pydantic
        (LoopNodeConfig). The wrapper catches the validation
        error and returns structured JSON with `ok:False`,
        `state_unchanged:True`. The staged state is untouched
        so the LLM can self-correct."""
        session, wf_id = _setup(db)
        before_nodes = copy.deepcopy(session.staged_nodes)
        before_changes = list(session.pending_changes)
        result = cbs._create_retry_loop_via_wrapper(
            session, wf_id,
            instructions="x",
            max_iterations=99999,
        )
        out = json.loads(result)
        assert out["ok"] is False
        assert out["state_unchanged"] is True
        assert out["issues"]  # has at least one structured Issue
        # Staged state untouched.
        assert session.staged_nodes == before_nodes
        assert session.pending_changes == before_changes

# ─────────────────────────────────────────────────────────────────
# Tool surface — all three patterns exposed to the LLM
# ─────────────────────────────────────────────────────────────────
class TestPatternToolsExposedToLLM:
    def test_all_three_patterns_registered(self, db):
        session, wf_id = _setup(db)
        funcs = cbs._build_tools_for_session(session)
        names = {f.name for f in funcs}
        assert "create_react_agent" in names
        assert "create_router_pattern" in names
        assert "create_retry_loop" in names

    def test_create_react_agent_has_tools_param(self, db):
        session, wf_id = _setup(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        cra = by_name["create_react_agent"]
        props = (cra.parameters or {}).get("properties") or {}
        assert "tools" in props
        assert "instructions" in props

    def test_create_router_pattern_has_branches_param(self, db):
        session, wf_id = _setup(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        crp = by_name["create_router_pattern"]
        props = (crp.parameters or {}).get("properties") or {}
        assert "branches" in props
        assert "selector_mode" in props

    def test_attach_tool_and_detach_tool_registered(self, db):
        """Both imperative tools are surfaced to the LLM
        alongside connect_nodes / disconnect. They carry the right
        params for the common call shape."""
        session, wf_id = _setup(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        assert "attach_tool" in by_name
        assert "detach_tool" in by_name
        # attach_tool must expose agent_id + tool_type
        attach_props = (
            by_name["attach_tool"].parameters or {}
        ).get("properties") or {}
        assert "agent_id" in attach_props
        assert "tool_type" in attach_props
        assert "tool_config" in attach_props
        # detach_tool must expose edge_id
        detach_props = (
            by_name["detach_tool"].parameters or {}
        ).get("properties") or {}
        assert "edge_id" in detach_props

# ─────────────────────────────────────────────────────────────────
# F7  — `attach_tool` / `detach_tool` behaviour
# ─────────────────────────────────────────────────────────────────
class TestAttachToolDetachTool:
    def test_attach_tool_wires_new_tool_attachment_edge_to_existing_agent(self, db):
        """A bare workflow (one agent, no tools) — after `attach_tool`,
        the agent has exactly one tool_attachment edge to a new
        tool node carrying the requested preset discriminator.

        The wikipedia preset no longer exists
        as a separate node type — `attach_tool` still accepts
        `tool_type='wikipedia'` for back-compat, but writes
        `type='tool'` + `config.preset='wikipedia'`."""
        session, wf_id = _setup(db)
        # _setup seeds an "a1" agent + an "a2" agent; pick a1.
        result = cbs._attach_tool(
            session, wf_id,
            agent_id="a1",
            tool_type="wikipedia",
            tool_config={},
        )
        # New tool node must exist in the staged graph as `type='tool'`
        # with `preset='wikipedia'` (post-merge shape).
        new_nodes = [
            n for n in session.staged_nodes
            if n.get("type") == "tool"
            and (n.get("data") or {}).get("config", {}).get("preset") == "wikipedia"
        ]
        assert len(new_nodes) == 1
        new_id = new_nodes[0]["id"]
        # Exactly one tool_attachment edge from the new node → a1.
        ta_edges = [
            e for e in session.staged_edges
            if e.get("kind") == "tool_attachment"
            and e.get("source") == new_id
            and e.get("target") == "a1"
        ]
        assert len(ta_edges) == 1
        # Result payload echoes the new node + edge ids.
        import json as _json
        parsed = _json.loads(result)
        assert "ok" in parsed

    def test_attach_tool_rejects_non_agent_target(self, db):
        """If the target node isn't type 'agent', reject with a
        ToolCallRejected — the LLM should fall back to plan_workflow
        for non-agent tool wiring. Also rejects non-existent ids."""
        session, wf_id = _setup(db)
        # Missing node → rejected by _find_staged_node.
        with pytest.raises(cbs.ToolCallRejected):
            cbs._attach_tool(
                session, wf_id,
                agent_id="missing_node",
                tool_type="wikipedia",
            )
        # Add a branch (router → branch in ); attach_tool to a
        # non-agent node → rejected. `WorkflowNode.model_validate` runs
        # `_compat.migrate_node_dict` so the legacy `type: "router"`
        # is rewritten to `branch` + `mode: "switch"` before the
        # error message is built.
        from app.schemas.workflow import WorkflowNode
        router_dict = WorkflowNode.model_validate({
            "id": "r1", "type": "router",
            "data": {"label": "R", "config": {"branches": []}},
        }).model_dump()
        session.staged_nodes.append(router_dict)
        with pytest.raises(cbs.ToolCallRejected) as exc_info:
            cbs._attach_tool(
                session, wf_id,
                agent_id="r1",
                tool_type="wikipedia",
            )
        msg = str(exc_info.value)
        assert "must be of type 'agent'" in msg or "branch" in msg or "router" in msg

    def test_detach_tool_removes_only_tool_attachment_edge(self, db):
        """`detach_tool` on a tool_attachment edge id removes only the
        edge — the source tool node and target agent node remain."""
        session, wf_id = _setup(db)
        cbs._attach_tool(
            session, wf_id,
            agent_id="a1",
            tool_type="wikipedia",
            tool_config={},
        )
        # Find the new edge.
        ta_edges = [
            e for e in session.staged_edges
            if e.get("kind") == "tool_attachment"
            and e.get("target") == "a1"
        ]
        assert len(ta_edges) == 1
        edge_id = ta_edges[0]["id"]
        new_node_id = ta_edges[0]["source"]
        # Snapshot staged_nodes ids for later comparison.
        before_node_ids = {n["id"] for n in session.staged_nodes}
        # Detach.
        cbs._detach_tool(session, wf_id, edge_id=edge_id)
        # Edge gone, both nodes remain.
        after_edges = [
            e for e in session.staged_edges
            if e.get("id") == edge_id
        ]
        assert after_edges == []
        after_node_ids = {n["id"] for n in session.staged_nodes}
        assert before_node_ids == after_node_ids
        # The tool (preset=wikipedia) node and a1 are both still there.
        assert new_node_id in after_node_ids
        assert "a1" in after_node_ids

    def test_detach_tool_rejects_dataflow_edge(self, db):
        """`detach_tool` refuses to remove dataflow edges — that's
        `disconnect`'s job. Prevents accidental removal of control flow."""
        session, wf_id = _setup(db)
        # _setup seeds only a1 (no edges). Append a dataflow edge.
        session.staged_edges.append({
            "id": "df1", "source": "a1", "target": "a1",
            "kind": "dataflow",
        })
        with pytest.raises(cbs.ToolCallRejected) as exc_info:
            cbs._detach_tool(session, wf_id, edge_id="df1")
        assert "not a tool_attachment" in str(exc_info.value)

    def test_system_prompt_mentions_attach_tool(self):
        """The system prompt must teach the LLM about
        attach_tool / detach_tool, otherwise the LLM keeps falling
        back to create_react_agent's implicit (wrong-target)
        attachment."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        assert "attach_tool" in prompt
        assert "detach_tool" in prompt
        # The crucial teaching: create_react_agent only attaches to
        # the agent it creates, not to existing agents.
        assert "only attaches the listed tools to THAT agent" in prompt \
            or "create_react_agent" in prompt and "existing agent" in prompt

    def test_create_retry_loop_has_max_iterations_param(self, db):
        session, wf_id = _setup(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        crl = by_name["create_retry_loop"]
        props = (crl.parameters or {}).get("properties") or {}
        assert "max_iterations" in props
        assert "instructions" in props