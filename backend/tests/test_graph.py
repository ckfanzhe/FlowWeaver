"""Graph utility tests — topology, adjacency, validation."""
from __future__ import annotations

import pytest

from app.core.graph import (
    GraphError,
    build_adjacency,
    find_start,
    topo_sort,
    validate_workflow,
)

def _nodes():
    return [
        {"id": "a", "type": "agent", "data": {}},
        {"id": "b", "type": "agent", "data": {}},
    ]

def _edges_linear():
    return [
        {"id": "e1", "source": "a", "target": "b"},
    ]

def test_build_adjacency_indexes_correctly():
    nm, out, inc = build_adjacency(_nodes(), _edges_linear())
    assert set(nm.keys()) == {"a", "b"}
    assert out["a"] == ["b"]
    assert inc["b"] == ["a"]

def test_topo_sort_linear_order():
    nm, out, _ = build_adjacency(_nodes(), _edges_linear())
    order = topo_sort(nm, out)
    assert order.index("a") < order.index("b")

def test_topo_sort_branching():
    nodes = [
        {"id": "rt", "type": "router", "data": {}},
        {"id": "x", "type": "agent", "data": {}},
        {"id": "y", "type": "agent", "data": {}},
    ]
    edges = [
        {"id": "ex", "source": "rt", "target": "x"},
        {"id": "ey", "source": "rt", "target": "y"},
    ]
    nm, out, _ = build_adjacency(nodes, edges)
    order = topo_sort(nm, out)
    # rt must be first; before x/y (which have no order between them)
    assert order[0] == "rt"
    assert order.index("rt") < order.index("x")
    assert order.index("rt") < order.index("y")

def test_topo_sort_cycle_raises():
    nodes = [
        {"id": "a", "type": "agent", "data": {}},
        {"id": "b", "type": "agent", "data": {}},
    ]
    edges = [
        {"id": "e1", "source": "a", "target": "b"},
        {"id": "e2", "source": "b", "target": "a"},
    ]
    nm, out, _ = build_adjacency(nodes, edges)
    with pytest.raises(GraphError):
        topo_sort(nm, out)

def test_find_start_falls_back_to_no_incoming():
    nodes = [
        {"id": "x", "type": "agent", "data": {}},
        {"id": "y", "type": "agent", "data": {}},
    ]
    edges = [{"id": "e", "source": "x", "target": "y"}]
    nm, _, inc = build_adjacency(nodes, edges)
    # x has no incoming; should be picked
    from app.core.graph import Node
    nm = {n["id"]: Node(id=n["id"], type=n["type"], data=n["data"]) for n in nodes}
    assert find_start(nm, inc) == "x"

def test_validate_workflow_accepts_valid():
    validate_workflow(_nodes(), _edges_linear())  # no exception

def test_validate_workflow_rejects_unknown_type():
    bad = [{"id": "x", "type": "wat", "data": {}}]
    with pytest.raises(GraphError):
        validate_workflow(bad, [])

def test_validate_workflow_rejects_bad_edge():
    bad_edge = [{"id": "e", "source": "ghost", "target": "in"}]
    with pytest.raises(GraphError):
        validate_workflow(_nodes(), bad_edge)