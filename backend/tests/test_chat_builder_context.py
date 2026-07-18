"""Tests for the chat builder context-assembly kernel.

The kernel (`app.services.chat_builder_context`) decides how much
of the workflow state to inline into the LLM prompt. The naive
"dump the full JSON" approach blew up the prompt past the
32K context-window cap, so the kernel truncates aggressively.

These tests pin the truncation rules: per-node config caps,
per-shape node/edge counts, and a hard total-character replacement
when the workflow is too large to show at all.
"""
from __future__ import annotations

from app.services.chat_builder_context import (
    MAX_CONFIG_CHARS,
    MAX_EDGES_IN_CONTEXT,
    MAX_NODES_IN_CONTEXT,
    MAX_TOTAL_CONTEXT_CHARS,
    render_workflow_context,
)

def _make_node(i: int, *, config_chars: int = 50) -> dict:
    return {
        "id": f"n{i}",
        "type": "agent",
        "label": f"Agent {i}",
        "config": {"instructions": "x" * config_chars},
    }

def _make_edge(i: int) -> dict:
    return {"source": f"n{i}", "target": f"n{i + 1}", "kind": "dataflow"}

def test_render_workflow_context_empty_workflow():
    """Both lists empty → friendly placeholders, no JSON dump."""
    out = render_workflow_context([], [])
    assert "no nodes" in out
    assert "no edges" in out

def test_render_workflow_context_small_workflow_inlines_all():
    """Under the node/edge cap, every node id appears in the output."""
    nodes = [_make_node(i) for i in range(5)]
    edges = [_make_edge(i) for i in range(4)]
    out = render_workflow_context(nodes, edges)
    for n in nodes:
        assert f"id={n['id']!r}" in out
    assert "5 total" in out
    assert "4 total" in out

def test_render_workflow_context_truncates_long_node_config():
    """A node with a > 500-char config must be truncated with a
    `... [+N chars]` marker so the LLM doesn't see the whole
    prompt-sized string."""
    nodes = [_make_node(0, config_chars=MAX_CONFIG_CHARS + 500)]
    out = render_workflow_context(nodes, [])
    # The truncation marker must appear; the suffix char count
    # reflects the full JSON length minus the kept 500 chars.
    assert "... [+" in out and " chars]" in out
    # The raw config string must NOT bleed past the truncation point.
    assert "x" * (MAX_CONFIG_CHARS + 1) not in out

def test_render_workflow_context_omits_middle_nodes_when_too_many():
    """Over `MAX_NODES_IN_CONTEXT`, the first (N-5) and last 5 are
    shown with an `[... N more omitted ...]` middle line."""
    n = MAX_NODES_IN_CONTEXT + 10
    nodes = [_make_node(i) for i in range(n)]
    out = render_workflow_context(nodes, [])
    # First 15 should appear (N-5 = 20-5 = 15)
    for i in range(MAX_NODES_IN_CONTEXT - 5):
        assert f"id='n{i}'" in out
    # Last 5 should appear
    for i in range(n - 5, n):
        assert f"id='n{i}'" in out
    # Middle nodes should NOT appear
    assert "id='n16'" not in out
    assert "id='n17'" not in out
    # Omitted marker with the right count
    assert "[10 more nodes omitted" in out

def test_render_workflow_context_replaces_with_summary_when_total_too_large():
    """If the rendered output exceeds `MAX_TOTAL_CONTEXT_CHARS`, the
    workflow section is replaced with a plain-text summary that
    tells the LLM to use `plan_workflow` for whole-graph replacement."""
    # 200 nodes with 5000-char configs = 1M+ chars of node data.
    # Edge list pairs them up. The per-node-config cap (500 chars)
    # squashes each to ~525 chars, but with 200 nodes shown
    # head + tail (15 + 5 = 20) we still get ~10.5K chars — under
    # the cap. So we ALSO need a low per-call `max_total_chars` to
    # simulate the small-window gateway.
    big_nodes = [
        _make_node(i, config_chars=5000) for i in range(200)
    ]
    big_edges = [_make_edge(i) for i in range(199)]
    out = render_workflow_context(
        big_nodes, big_edges, max_total_chars=2_000,
    )
    assert "too large to show in context" in out
    assert "plan_workflow" in out
    # The raw node JSON must NOT leak through.
    assert "instructions" not in out
    assert len(out) < 2_000

def test_render_workflow_context_omits_middle_edges_when_too_many():
    """Same head/tail rule for edges as for nodes."""
    n = MAX_EDGES_IN_CONTEXT + 5
    edges = [_make_edge(i) for i in range(n)]
    out = render_workflow_context([], edges)
    assert "more edges omitted" in out
    # First edge should appear
    assert "n0' -> 'n1'" in out
    # Last edge should appear
    assert f"n{n - 1}' -> 'n{n}'" in out
    # Middle should not
    for i in range(MAX_EDGES_IN_CONTEXT, n - 5):
        assert f"n{i}' -> 'n{i + 1}'" not in out

def test_render_workflow_context_keeps_source_handle_for_routing():
    """Round-2 : router edges carry `sourceHandle` to
    distinguish branches. The context renderer must preserve that
    so the LLM can see the wiring shape."""
    nodes = [_make_node(0), _make_node(1), _make_node(2)]
    edges = [
        {"source": "n0", "target": "n1", "kind": "dataflow",
         "sourceHandle": "yes"},
        {"source": "n0", "target": "n2", "kind": "dataflow",
         "sourceHandle": "no"},
    ]
    out = render_workflow_context(nodes, edges)
    assert "sourceHandle='yes'" in out
    assert "sourceHandle='no'" in out
