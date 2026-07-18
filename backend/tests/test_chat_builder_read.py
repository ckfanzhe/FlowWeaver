"""Tests for `app.services.chat_builder_read` — the F3 read-tools layer.

Two layers:

  1. Pure-function tests — `summarise_graph_state` /
     `summarise_connection_rules`. No DB / session / fixtures;
     pure data in, pure data out. Pinned so future schema /
     rule-table changes surface as test failures before they
     break the LLM.

  2. Tool-surface tests — both tools are exposed via
     `_build_tools_for_session` and return the expected JSON
     shape. Pins the LLM-facing contract.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.auth import CurrentUser
from app.db.models import User, Workflow
from app.services import chat_builder_service as cbs
from app.services import member_service
from app.services.chat_builder_read import (
    summarise_graph_state,
    summarise_connection_rules,
    get_graph_state_tool,
    get_connection_rules_tool,
)

# ─────────────────────────────────────────────────────────────────
# Pure: summarise_graph_state
# ─────────────────────────────────────────────────────────────────
class TestSummariseGraphState:
    """`summarise_graph_state` returns the staged graph as a
    structured summary the LLM can read."""

    def test_empty_graph(self):
        out = summarise_graph_state([], [])
        assert out["counts"]["nodes"] == 0
        assert out["counts"]["edges"] == 0
        assert out["entry_points"] == []
        assert out["terminal_nodes"] == []
        assert out["orphans"] == []

    def test_single_node_is_orphan(self):
        nodes = [{"id": "n1", "type": "agent", "data": {"label": "N1"}}]
        out = summarise_graph_state(nodes, [])
        assert out["counts"]["nodes"] == 1
        # No edges → it's an entry (no incoming) AND terminal
        # (no outgoing) AND an orphan (both).
        assert "n1" in out["entry_points"]
        assert "n1" in out["terminal_nodes"]
        assert "n1" in out["orphans"]

    def test_two_node_chain(self):
        nodes = [
            {"id": "a1", "type": "agent", "data": {"label": "A1"}},
            {"id": "a2", "type": "agent", "data": {"label": "A2"}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "a2", "kind": "dataflow"},
        ]
        out = summarise_graph_state(nodes, edges)
        # a1 is entry, a2 is terminal, neither is orphan.
        assert out["entry_points"] == ["a1"]
        assert out["terminal_nodes"] == ["a2"]
        assert out["orphans"] == []

    def test_per_node_includes_degree_counts(self):
        """Each per_node entry carries outgoing/incoming counts so
        the LLM can spot over-wired nodes (agent has max_outgoing=1)
        before issuing a plan."""
        nodes = [
            {"id": "a1", "type": "agent", "data": {"label": "A1"}},
            {"id": "a2", "type": "agent", "data": {"label": "A2"}},
            {"id": "a3", "type": "agent", "data": {"label": "A3"}},
        ]
        edges = [
            {"id": "e1", "source": "a1", "target": "a2", "kind": "dataflow"},
            {"id": "e2", "source": "a1", "target": "a3", "kind": "dataflow"},
        ]
        out = summarise_graph_state(nodes, edges)
        per_node = {pn["id"]: pn for pn in out["per_node"]}
        assert per_node["a1"]["outgoing_count"] == 2  # over the agent's max_outgoing=1
        assert per_node["a2"]["incoming_count"] == 1
        assert per_node["a3"]["incoming_count"] == 1

    def test_tool_attachment_edges_excluded_from_dataflow_counts(self):
        """Tool attachment edges (`kind='tool_attachment'`) wire a
        tool source to an agent — they're NOT dataflow edges and
        must not count as incoming/outgoing dataflow. This pins the
        bug fix for "router has 1 outgoing edge — wait, that's a
        tool_attachment" confusion."""
        nodes = [
            {"id": "a1", "type": "agent", "data": {"label": "A1"}},
            {"id": "w1", "type": "wikipedia", "data": {"label": "W1"}},
        ]
        edges = [
            {"id": "e1", "source": "w1", "target": "a1", "kind": "tool_attachment"},
        ]
        out = summarise_graph_state(nodes, edges)
        # Counts split correctly.
        assert out["counts"]["dataflow_edges"] == 0
        assert out["counts"]["tool_attachment_edges"] == 1
        per_node = {pn["id"]: pn for pn in out["per_node"]}
        # Tool attachment doesn't count as incoming/outgoing dataflow.
        assert per_node["w1"]["outgoing_count"] == 0
        assert per_node["a1"]["incoming_count"] == 0

    def test_type_counts_breakdown(self):
        nodes = [
            {"id": "a1", "type": "agent", "data": {"label": ""}},
            {"id": "a2", "type": "agent", "data": {"label": ""}},
            {"id": "r1", "type": "router", "data": {"label": ""}},
        ]
        out = summarise_graph_state(nodes, [])
        assert out["type_counts"] == {"agent": 2, "router": 1}

    def test_get_graph_state_tool_returns_valid_json(self):
        """The tool wrapper returns a JSON string the LLM can parse."""
        nodes = [
            {"id": "a1", "type": "agent", "data": {"label": "A1"}},
        ]
        out = get_graph_state_tool(nodes, [])
        parsed = json.loads(out)
        assert "counts" in parsed
        assert "per_node" in parsed

# ─────────────────────────────────────────────────────────────────
# Pure: summarise_connection_rules
# ─────────────────────────────────────────────────────────────────
class TestSummariseConnectionRules:
    """The rule summary is the LLM's "what can connect to what"
    reference. It must match what `validate_connections` actually
    allows (catches rule-table drift)."""

    def test_every_manifest_type_has_a_rule(self):
        """The rule table covers all 6 manifest entries. Pre-F3
        the LLM had to guess at connection rules for presets; this
        test pins that every base type is documented.

        The node-type collapse merged several legacy types into one:
        `parallel`+`steps` → `flow`, `router`+`condition` → `branch`,
        `http`+`mcp`+`tools` → `tool`, `human_input` → `ask`.
        The 5 presets collapsed into the `tool` node's `preset`
        discriminator — they no longer appear as separate entries
        in the rule table."""
        out = summarise_connection_rules()
        for required in (
            "agent", "branch", "flow", "loop", "ask", "tool",
        ):
            assert required in out["by_type"], (
                f"connection rule missing for {required!r}; "
                f"got {sorted(out['by_type'])}"
            )

    def test_agent_max_outgoing_is_one(self):
        """Agent has max_outgoing=1 — the LLM uses this to know
        it needs a Router / Parallel in front of an Agent that
        should branch."""
        out = summarise_connection_rules()
        assert out["by_type"]["agent"]["max_outgoing"] == 1

    def test_branch_min_outgoing_is_zero(self):
        """Branch's connection-layer rule is `min_outgoing=0` (lenient —
        matches the prior `router` shape). `if-else`'s stricter
        `min_outgoing=1, max_outgoing=2` is enforced at the strategy
        / IR layer (`BranchStrategy._build_if_else` raises if no
        `then` target), not at the connection layer. The LLM-facing
        contract is: "branch needs ≥ 1 outgoing at runtime", so this
        test pins the *lenient* connection-layer value and the
        comment explains where the strict runtime check lives.
        """
        out = summarise_connection_rules()
        assert out["by_type"]["branch"]["min_outgoing"] == 0

    def test_tool_source_types_have_no_dataflow(self):
        """Tool sources cannot be on the
        dataflow graph — their allowed_source_types /
        allowed_target_types are empty. The
        5 presets collapsed into the `tool` node's `preset`
        discriminator — only `tool` itself is in the
        `tool_source` group now."""
        out = summarise_connection_rules()
        for tool_source in ("tool",):
            rule = out["by_type"][tool_source]
            assert rule["allowed_source_types"] == []
            assert rule["allowed_target_types"] == []
            assert rule["max_outgoing"] == 0

    def test_executable_types_can_connect_to_executable(self):
        """Agent / branch / etc. can connect to other executable
        types (per `@executable` group). The loader expands the
        alias to a concrete list — verify it includes the
        well-known executable types."""
        out = summarise_connection_rules()
        rule = out["by_type"]["agent"]
        # The `router` + `condition` pair collapsed into `branch`.
        for expected in (
            "agent", "branch", "flow", "loop", "ask",
        ):
            assert expected in rule["allowed_source_types"], (
                f"agent allowed_source_types missing {expected!r}; "
                f"got {rule['allowed_source_types']}"
            )
            assert expected in rule["allowed_target_types"]

    def test_groups_field_present(self):
        """The groups block (`@executable` / `@tool_source` aliases)
        is surfaced so the LLM can interpret @-prefixed names that
        appear in error messages. Only `tool`
        remains in `groups.tool_source` — the 5 preset tool types
        collapsed into the `tool` node's `preset` discriminator."""
        out = summarise_connection_rules()
        assert "executable" in out["groups"]
        assert "tool_source" in out["groups"]
        assert "agent" in out["groups"]["executable"]
        assert "tool" in out["groups"]["tool_source"]
        assert "wikipedia" not in out["groups"]["tool_source"]

    def test_get_connection_rules_tool_returns_valid_json(self):
        out = json.loads(get_connection_rules_tool())
        assert "by_type" in out
        assert "groups" in out

# ─────────────────────────────────────────────────────────────────
# Tool surface — both tools are exposed to the LLM
# ─────────────────────────────────────────────────────────────────
class TestReadToolsExposedToLLM:
    """`get_graph_state` and `get_connection_rules` must appear in
    the list of `Function` objects built by
    `_build_tools_for_session`. This pins what the LLM sees."""

    def _session(self, db):
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
        return cbs._load_or_create_session(
            db, wid,
            CurrentUser(id="alice@example.com", tenant_id="tenant-default"),
        )

    def test_both_read_tools_registered(self, db):
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        names = {f.name for f in funcs}
        assert "get_graph_state" in names
        assert "get_connection_rules" in names

    def test_get_graph_state_tool_callable(self, db):
        """Calling the tool via the registered entry point returns
        the staged graph summary."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        gs = by_name["get_graph_state"]
        out = gs.entrypoint()
        parsed = json.loads(out)
        assert "counts" in parsed
        assert parsed["counts"]["nodes"] == 1

    def test_get_connection_rules_tool_callable(self, db):
        """Calling the connection-rules tool returns the rule
        table."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        cr = by_name["get_connection_rules"]
        out = cr.entrypoint()
        parsed = json.loads(out)
        assert "by_type" in parsed
        assert "groups" in parsed

    def test_get_graph_state_workflow_id_optional(self, db):
        """`workflow_id` defaults to the session's — the LLM
        shouldn't have to pass it (F1 / F2 lock-in)."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        gs = by_name["get_graph_state"]
        # Calling without `workflow_id` works because the wrapper
        # falls back to the session's id.
        out = gs.entrypoint()
        parsed = json.loads(out)
        assert "counts" in parsed

    def test_get_graph_state_rejects_foreign_workflow_id(self, db):
        """Defensive: passing a different `workflow_id` raises a
        `ToolCallRejected` (mirrors the imperative tools' contract)."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        gs = by_name["get_graph_state"]
        with pytest.raises(Exception):
            # Wrong workflow_id — should be rejected by the wrapper.
            gs.entrypoint(workflow_id="wf-not-this-session")