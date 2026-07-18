"""Tests for the one-shot cfg.toolsRef → tool_attachment migration.

These cover the pure logic in `migrate_tools_ref_to_tool_attachment.py`.
The DB-touching path is exercised by an integration smoke test that
just calls `migrate_workflow(...)` on a synthetic (nodes, edges) pair
— exactly what the script does for every workflow row.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the migration module by file path (it lives under scripts/, not
# under the app package). Using importlib keeps the test independent
# of any setup.py changes.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_tools_ref_to_tool_attachment.py"
_spec = importlib.util.spec_from_file_location("migrate_tools_ref", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["migrate_tools_ref"] = _mod
_spec.loader.exec_module(_mod)

migrate_workflow = _mod.migrate_workflow

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _agent(id_: str, *, tools_ref: list[str] | None = None) -> dict:
    cfg: dict = {}
    if tools_ref is not None:
        cfg["toolsRef"] = tools_ref
    return {
        "id": id_, "type": "agent",
        "position": {"x": 0, "y": 0},
        "data": {"label": id_, "config": cfg},
    }

def _tool(id_: str, ttype: str = "tool") -> dict:
    """Tool-source collapse: the old `tools` / `http` / `mcp`
    types all collapse to `tool`. The default here is the
    `source='function'` (formerly `tools`) shape. For http/mcp
    shapes the test passes a different `ttype` string — both
    routes exercise the same `tool` migration."""
    src = "function" if ttype == "tool" else (
        "http" if ttype == "http" else "mcp"
    )
    return {
        "id": id_, "type": ttype,
        "position": {"x": 0, "y": 0},
        "data": {"label": id_, "config": {"source": src, "functions": []}},
    }

def _edges(edges: list[dict]) -> list[dict]:
    return list(edges)

# ─────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────
class TestHappyPath:
    def test_adds_edge_for_one_toolsref(self):
        nodes = [_agent("a", tools_ref=["t"]), _tool("t")]
        edges: list[dict] = []
        _, new_edges, lines = migrate_workflow(nodes, edges, drop_cfg=False, ts_ms=1000)
        assert len(new_edges) == 1
        e = new_edges[0]
        assert e["source"] == "t"
        assert e["target"] == "a"
        assert e["kind"] == "tool_attachment"
        assert "added 1 tool_attachment edges" in lines[0]

    def test_keeps_cfg_toolsref_by_default(self):
        nodes = [_agent("a", tools_ref=["t"]), _tool("t")]
        _, _, _ = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        # cfg.toolsRef is preserved so a one-line DB revert is possible.
        assert nodes[0]["data"]["config"]["toolsRef"] == ["t"]

    def test_drops_cfg_toolsref_when_asked(self):
        nodes = [_agent("a", tools_ref=["t"]), _tool("t")]
        _, _, lines = migrate_workflow(nodes, [], drop_cfg=True, ts_ms=1000)
        assert "toolsRef" not in nodes[0]["data"]["config"]
        assert any("dropped cfg.toolsRef" in ln for ln in lines)

    def test_adds_edge_for_each_ref(self):
        nodes = [
            _agent("a", tools_ref=["t1", "t2", "http_node"]),
            _tool("t1"),
            _tool("t2"),
            _tool("http_node", "http"),
        ]
        _, new_edges, _ = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert len(new_edges) == 3
        added_pairs = {(e["source"], e["target"]) for e in new_edges}
        assert added_pairs == {("t1", "a"), ("t2", "a"), ("http_node", "a")}

# ─────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────
class TestIdempotency:
    def test_running_twice_is_no_op(self):
        nodes = [_agent("a", tools_ref=["t"]), _tool("t")]
        _, new_edges, _ = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        first_count = len(new_edges)
        # Re-run on the same (nodes, edges) — the cfg.toolsRef is still
        # there (we're not dropping it). The migration must NOT add a
        # second edge.
        _, new_edges2, lines2 = migrate_workflow(
            nodes, new_edges, drop_cfg=False, ts_ms=2000,
        )
        assert len(new_edges2) == first_count
        assert any("already covered" in ln for ln in lines2)

    def test_existing_dataflow_edge_does_not_count_as_attachment(self):
        # If a tool→agent dataflow edge already exists (rare — it
        # would have been rejected by the validator, but legacy
        # rows might have it), the migration still adds the typed
        # edge because dataflow != tool_attachment.
        nodes = [_agent("a", tools_ref=["t"]), _tool("t")]
        existing = [{
            "id": "e-dataflow", "source": "t", "target": "a",
            "kind": "dataflow",
        }]
        _, new_edges, _ = migrate_workflow(
            nodes, existing, drop_cfg=False, ts_ms=1000,
        )
        # Both edges present — dataflow + tool_attachment.
        assert len(new_edges) == 2
        kinds = {e.get("kind") for e in new_edges}
        assert "tool_attachment" in kinds
        assert "dataflow" in kinds

# ─────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────
class TestEdgeCases:
    def test_no_agents_is_noop(self):
        nodes = [_tool("t")]
        _, new_edges, lines = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert new_edges == []
        assert lines == []

    def test_agent_without_toolsref_is_noop(self):
        nodes = [_agent("a"), _tool("t")]
        _, new_edges, lines = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert new_edges == []
        assert lines == []

    def test_empty_toolsref_list_is_noop(self):
        nodes = [_agent("a", tools_ref=[]), _tool("t")]
        _, new_edges, lines = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert new_edges == []
        assert lines == []

    def test_stale_ref_to_missing_node_skipped(self):
        # cfg.toolsRef mentions a tool id that doesn't exist in this
        # workflow. Migration should not crash and should not emit an
        # edge to a non-existent target.
        nodes = [_agent("a", tools_ref=["ghost"]), _tool("t")]
        _, new_edges, lines = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert new_edges == []
        assert any("no valid toolsRef" in ln for ln in lines)

    def test_ref_to_non_tool_node_skipped(self):
        # cfg.toolsRef mentions a node that IS in the workflow but
        # isn't a tool-source type. The migration only adds edges for
        # valid tool types (tools / http / mcp).
        nodes = [
            _agent("a", tools_ref=["other_agent"]),
            _agent("other_agent"),
        ]
        _, new_edges, lines = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert new_edges == []
        assert any("no valid toolsRef" in ln for ln in lines)

    def test_refs_to_multiple_tool_types_all_handled(self):
        nodes = [
            _agent("a", tools_ref=["t", "h", "m"]),
            _tool("t"),
            _tool("h", "http"),
            _tool("m", "mcp"),
        ]
        _, new_edges, _ = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert len(new_edges) == 3
        added_pairs = {(e["source"], e["target"]) for e in new_edges}
        assert added_pairs == {("t", "a"), ("h", "a"), ("m", "a")}

    def test_multiple_agents_each_get_their_own_edges(self):
        nodes = [
            _agent("a1", tools_ref=["t"]),
            _agent("a2", tools_ref=["t"]),
            _tool("t"),
        ]
        _, new_edges, _ = migrate_workflow(nodes, [], drop_cfg=False, ts_ms=1000)
        assert len(new_edges) == 2
        assert {(e["source"], e["target"]) for e in new_edges} == {
            ("t", "a1"),
            ("t", "a2"),
        }

    def test_edge_id_is_deterministic_from_timestamp(self):
        nodes = [_agent("a", tools_ref=["t"]), _tool("t")]
        _, edges_a, _ = migrate_workflow(
            [dict(n) for n in nodes], [], drop_cfg=False, ts_ms=12345,
        )
        _, edges_b, _ = migrate_workflow(
            [dict(n) for n in nodes], [], drop_cfg=False, ts_ms=12345,
        )
        assert edges_a[0]["id"] == edges_b[0]["id"]

# ─────────────────────────────────────────────────────────────────
# CLI entry point (smoke test — exercises the argument parser)
# ─────────────────────────────────────────────────────────────────
class TestCLI:
    def test_cli_help(self, capsys):
        from scripts.migrate_tools_ref_to_tool_attachment import main
        with pytest.raises(SystemExit):
            main(["--help"])
        # argparse writes to stderr for --help; just verify no crash.
        assert True