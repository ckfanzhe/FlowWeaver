"""Tests for `app.core.ir` — the shared workflow IR.

These tests pin down the behaviour that BOTH the runtime executor and
the code generator depend on. If any of these break, BOTH consumers
must be re-verified.
"""
from __future__ import annotations

import pytest

from app.core.graph import GraphError
from app.core.ir import (
    COMPOUND_TYPES,
    EXECUTABLE_TYPES,
    WorkflowIR,
    build_ir,
)

def _n(id_: str, type_: str, **cfg) -> dict:
    return {"id": id_, "type": type_, "position": {"x": 0, "y": 0},
            "data": {"label": id_, "config": cfg}}

def _e(src: str, tgt: str, eid: str | None = None, *, kind: str | None = None) -> dict:
    d = {"id": eid or f"e-{src}-{tgt}", "source": src, "target": tgt}
    if kind is not None:
        d["kind"] = kind
    return d

# ─────────────────────────────────────────────────────────────────
# Basic construction
# ─────────────────────────────────────────────────────────────────
class TestBasic:
    def test_single_agent_no_edges(self):
        ir = build_ir([_n("a", "agent", instructions="hi")], [])
        assert isinstance(ir, WorkflowIR)
        assert ir.entry_id == "a"
        assert ir.topo_order == ["a"]
        assert ir.nested_children == frozenset()
        assert ir.top_level_step_ids() == ["a"]

    def test_linear_chain(self):
        nodes = [
            _n("a", "agent"),
            _n("b", "agent"),
            _n("c", "agent"),
        ]
        edges = [_e("a", "b"), _e("b", "c")]
        ir = build_ir(nodes, edges)
        assert ir.entry_id == "a"
        assert ir.topo_order == ["a", "b", "c"]
        assert ir.top_level_step_ids() == ["a", "b", "c"]

    def test_tool_source_node_not_executable(self):
        nodes = [
            _n("ag", "agent", toolsRef=["t"]),
            _n("t", "tool", source="function", functions=[]),
        ]
        ir = build_ir(nodes, [])
        # `tool` is NOT in EXECUTABLE_TYPES — must not appear at top level (it's a tool_source, not an executable step).
        assert "t" not in ir.top_level_step_ids()
        assert "ag" in ir.top_level_step_ids()

    def test_cycle_raises(self):
        nodes = [_n("a", "agent"), _n("b", "agent")]
        edges = [_e("a", "b"), _e("b", "a")]
        with pytest.raises(GraphError):
            build_ir(nodes, edges)

    def test_dangling_edge_raises(self):
        nodes = [_n("a", "agent")]
        edges = [_e("a", "ghost")]
        with pytest.raises(GraphError):
            build_ir(nodes, edges)

# ─────────────────────────────────────────────────────────────────
# Parallel branches — the bug from BUG_FIX §"并行化搜索"
# ─────────────────────────────────────────────────────────────────
class TestParallelBranches:
    def test_three_branches_captured_in_order(self):
        nodes = [
            _n("p", "flow", mode="parallel", branches=[
                {"target": "a_pro"}, {"target": "a_con"}, {"target": "a_sum"},
            ]),
            _n("a_pro", "agent"), _n("a_con", "agent"), _n("a_sum", "agent"),
        ]
        edges = [
            _e("p", "a_pro"), _e("p", "a_con"), _e("p", "a_sum"),
        ]
        ir = build_ir(nodes, edges)
        assert ir.flow_branches["p"] == ["a_pro", "a_con", "a_sum"]
        # All three branches must be marked as nested children.
        assert ir.nested_children == frozenset({"a_pro", "a_con", "a_sum"})
        # Top level must contain the parallel, NOT the branch agents.
        assert ir.top_level_step_ids() == ["p"]

    def test_parallel_branches_preserve_edge_order(self):
        nodes = [
            _n("p", "flow", mode="parallel"),
            _n("a", "agent"), _n("b", "agent"), _n("c", "agent"),
        ]
        # Insert edges in shuffled order — IR must keep the order the
        # user drew them (which is the order they appear in `edges`).
        edges = [_e("p", "c"), _e("p", "a"), _e("p", "b")]
        ir = build_ir(nodes, edges)
        assert ir.flow_branches["p"] == ["c", "a", "b"]

# ─────────────────────────────────────────────────────────────────
# Branch (if-else) branches — if/else dispatch via second edge
# wins, mirroring the original BUG_FIX scenario for the `condition`
# node before it was collapsed into `branch` with `mode='if-else'`.
# The `ir.condition_branches` reader was renamed to
# `ir.get_branch_branches` (a `(then, else)` tuple accessor) — see
# `ir.py` for the alias chain.
# ─────────────────────────────────────────────────────────────────
class TestConditionBranches:
    def test_else_via_second_edge_wins(self):
        nodes = [
            _n("br", "branch", mode="if-else",
               evaluator={"mode": "function", "expression": "contains:urgent"},
               elseTarget="stale"),
            _n("then_ag", "agent"),
            _n("else_ag", "agent"),
        ]
        edges = [_e("br", "then_ag"), _e("br", "else_ag")]
        ir = build_ir(nodes, edges)
        assert ir.get_branch_branches("br") == ("then_ag", "else_ag")
        # Both branches must be marked nested.
        assert ir.nested_children == frozenset({"then_ag", "else_ag"})
        # Top level: only the condition (branches are nested).
        assert ir.top_level_step_ids() == ["br"]

    def test_else_falls_back_to_cfg_when_no_second_edge(self):
        nodes = [
            _n("br", "branch", mode="if-else",
               evaluator={"mode": "function", "expression": "contains:urgent"},
               elseTarget="else_ag"),
            _n("then_ag", "agent"),
            _n("else_ag", "agent"),
        ]
        edges = [_e("br", "then_ag")]  # no second edge — else is cfg-only
        ir = build_ir(nodes, edges)
        assert ir.get_branch_branches("br") == ("then_ag", "else_ag")

    def test_condition_no_else_has_only_then(self):
        nodes = [
            _n("br", "branch", mode="if-else",
               evaluator={"mode": "function", "expression": "contains:urgent"}),
            _n("then_ag", "agent"),
        ]
        edges = [_e("br", "then_ag")]
        ir = build_ir(nodes, edges)
        assert ir.get_branch_branches("br") == ("then_ag", None)
        assert ir.nested_children == frozenset({"then_ag"})

    def test_get_branch_targets_filters_nones(self):
        nodes = [
            _n("br", "branch", mode="if-else",
               evaluator={"mode": "function", "expression": "contains:urgent"}),
            _n("then_ag", "agent"),
        ]
        edges = [_e("br", "then_ag")]
        ir = build_ir(nodes, edges)
        assert ir.get_branch_targets("br") == ["then_ag"]

# ─────────────────────────────────────────────────────────────────
# Loop bodies — the bug from BUG_FIX §"loop body 二次执行"
# ─────────────────────────────────────────────────────────────────
class TestLoopBodies:
    def test_body_via_cfg_preferred(self):
        nodes = [
            _n("lp", "loop", maxIterations=3, bodyTarget="iter"),
            _n("iter", "agent"),
        ]
        edges = []  # no edge — body is cfg-only
        ir = build_ir(nodes, edges)
        assert ir.loop_bodies["lp"] == "iter"
        assert "iter" in ir.nested_children
        assert ir.top_level_step_ids() == ["lp"]

    def test_body_via_edge_fallback(self):
        nodes = [
            _n("lp", "loop", maxIterations=3),  # no bodyTarget
            _n("iter", "agent"),
        ]
        edges = [_e("lp", "iter")]
        ir = build_ir(nodes, edges)
        assert ir.loop_bodies["lp"] == "iter"

    def test_loop_with_post_loop_continuation(self):
        """A loop's outgoing edge should NOT be treated as the body — the
        body is `cfg.bodyTarget`. The outgoing edge is the post-loop
        continuation (which SHOULD be at top level)."""
        nodes = [
            _n("lp", "loop", maxIterations=3, bodyTarget="iter"),
            _n("iter", "agent"),
            _n("post", "agent"),  # post-loop continuation
        ]
        edges = [_e("lp", "post")]  # NOT lp → iter; body is via cfg.
        ir = build_ir(nodes, edges)
        assert ir.loop_bodies["lp"] == "iter"
        assert ir.nested_children == frozenset({"iter"})
        # `post` is NOT a nested child — it's the post-loop continuation.
        assert "post" not in ir.nested_children
        # Top level: [lp, post]
        assert ir.top_level_step_ids() == ["lp", "post"]

    def test_loop_no_body(self):
        nodes = [_n("lp", "loop", maxIterations=3)]
        ir = build_ir(nodes, [])
        assert ir.loop_bodies["lp"] is None
        assert ir.nested_children == frozenset()

# ─────────────────────────────────────────────────────────────────
# tool_refs
# ─────────────────────────────────────────────────────────────────
class TestToolRefs:
    def test_agent_with_tools_ref(self):
        nodes = [
            _n("ag", "agent", toolsRef=["nt", "nh"]),
            _n("nt", "tool", source="function", functions=[]),
            _n("nh", "tool", source="http"),
        ]
        ir = build_ir(nodes, [])
        assert ir.tool_refs["ag"] == ["nt", "nh"]

    def test_tools_ref_dedupes(self):
        nodes = [
            _n("ag", "agent", toolsRef=["nt", "nt", "nh"]),
            _n("nt", "tool", source="function"),
            _n("nh", "tool", source="http"),
        ]
        ir = build_ir(nodes, [])
        assert ir.tool_refs["ag"] == ["nt", "nh"]

    def test_tools_ref_filters_unknown_ids(self):
        nodes = [
            _n("ag", "agent", toolsRef=["nt", "ghost"]),
            _n("nt", "tool", source="function"),
        ]
        ir = build_ir(nodes, [])
        # `ghost` is not in node_map → dropped.
        assert ir.tool_refs["ag"] == ["nt"]

    def test_non_agent_has_no_tool_refs(self):
        nodes = [_n("br", "branch", mode="if-else")]
        ir = build_ir(nodes, [])
        assert ir.tool_refs == {}

# ─────────────────────────────────────────────────────────────────
# Top-level step order — the critical invariant
# ─────────────────────────────────────────────────────────────────
class TestTopLevelSteps:
    def test_parallel_research_scenario(self):
        """The original BUG_FIX scenario: a 3-branch parallel. The
        parallel node has 3 agent branches. Before the fix, the
        generator emitted all 3 agents AT TOP LEVEL too, causing them
        to execute twice. After the fix, only `p` appears at top level.

        (Originally pinned against `tpl-parallel-research`; that template
        was retired in a later gallery refresh since it duplicated
        `tpl-parallel-summary`'s shape without the synthesizer sibling.)
        """
        nodes = [
            _n("p", "flow", mode="parallel", branches=[{"target": "a_pro"}, {"target": "a_con"}, {"target": "a_sum"}]),
            _n("a_pro", "agent"), _n("a_con", "agent"), _n("a_sum", "agent"),
        ]
        edges = [_e("p", "a_pro"), _e("p", "a_con"), _e("p", "a_sum")]
        ir = build_ir(nodes, edges)
        assert ir.top_level_step_ids() == ["p"]

    def test_conditional_greeting_scenario(self):
        """The BUG_FIX scenario: condition with else branch — both
        branches must be nested, condition at top level only."""
        nodes = [
            _n("ag", "agent"),
            _n("br", "branch", mode="if-else",
               evaluator={"mode": "function", "expression": "contains:urgent"},
               elseTarget="else_ag"),
            _n("then_ag", "agent"),
            _n("else_ag", "agent"),
        ]
        edges = [
            _e("ag", "br"),
            _e("br", "then_ag"),
            _e("br", "else_ag"),
        ]
        ir = build_ir(nodes, edges)
        # `ag` is the entry (no incoming). `br` is at top level. The
        # branches `then_ag` and `else_ag` are nested.
        assert ir.top_level_step_ids() == ["ag", "br"]
        assert ir.nested_children == frozenset({"then_ag", "else_ag"})

    def test_complex_mixed_workflow(self):
        """A workflow with parallel + condition + loop + tools. All
        children must be correctly classified."""
        nodes = [
            _n("ag", "agent", toolsRef=["nt"]),
            _n("nt", "tool", source="function"),
            _n("br", "branch", mode="if-else",
               evaluator={"mode": "function", "expression": "contains:urgent"}),
            _n("then_ag", "agent"),
            _n("else_ag", "agent"),
            _n("lp", "loop", bodyTarget="refine"),
            _n("refine", "agent"),
            _n("p", "flow", mode="parallel", branches=[{"target": "a1"}, {"target": "a2"}]),
            _n("a1", "agent"),
            _n("a2", "agent"),
        ]
        edges = [
            _e("ag", "br"),
            _e("br", "then_ag"),
            _e("br", "else_ag"),
            _e("ag", "lp"),
            _e("lp", "p"),
            _e("p", "a1"),
            _e("p", "a2"),
        ]
        ir = build_ir(nodes, edges)
        assert ir.nested_children == frozenset({
            "then_ag", "else_ag", "refine", "a1", "a2",
        })
        assert ir.top_level_step_ids() == ["ag", "br", "lp", "p"]

# ─────────────────────────────────────────────────────────────────
# Misc invariants
# ─────────────────────────────────────────────────────────────────
class TestInvariants:
    def test_executable_types_complete(self):
        """EXECUTABLE_TYPES must cover all 5 runtime-step-producing types
        after the node-type collapses:
          - `parallel` + `steps` → `flow`
          - `router` + `condition` → `branch`
          - `human_input` → `ask`"""
        assert EXECUTABLE_TYPES == frozenset({
            "agent", "ask", "branch", "flow", "loop",
        })

    def test_compound_types_subset(self):
        """COMPOUND_TYPES is a subset of EXECUTABLE_TYPES — compounds are
        executable."""
        assert COMPOUND_TYPES <= EXECUTABLE_TYPES

    def test_is_nested_child(self):
        nodes = [
            _n("p", "flow", mode="parallel"),
            _n("a", "agent"),
        ]
        edges = [_e("p", "a")]
        ir = build_ir(nodes, edges)
        assert ir.is_nested_child("a")
        assert not ir.is_nested_child("p")

    def test_get_branch_targets_for_unknown_returns_empty(self):
        ir = build_ir([_n("a", "agent")], [])
        assert ir.get_branch_targets("nonexistent") == []

# ─────────────────────────────────────────────────────────────────
# Parallel fan-in / fan-out — N branches → 1 aggregator
# ─────────────────────────────────────────────────────────────────
class TestParallelFanIn:
    """The user-facing topology the canvas supports:
    one parallel node fans out to N branches, and ALL N branches
    converge back into a single downstream aggregator. The IR must
    treat the N branches as nested children of the parallel (so they
    don't appear at top level twice), while the aggregator sits at
    top level with N incoming sources.
    """

    def test_three_branches_all_converge_to_aggregator(self):
        """plan → parallel → {a, b, c} → summary, with a, b, c all
        having an edge to summary. The IR must list all three
        branches as nested children of `p`, and `summary` as a
        top-level step with 3 incoming edges."""
        nodes = [
            _n("plan", "agent"),
            _n("p", "flow", mode="parallel"),
            _n("a", "agent"), _n("b", "agent"), _n("c", "agent"),
            _n("summary", "agent"),
        ]
        edges = [
            _e("plan", "p"),
            _e("p", "a"), _e("p", "b"), _e("p", "c"),
            _e("a", "summary"), _e("b", "summary"), _e("c", "summary"),
        ]
        ir = build_ir(nodes, edges)
        # All three parallel branches are nested (children of `p`).
        assert ir.flow_branches["p"] == ["a", "b", "c"]
        assert ir.nested_children == frozenset({"a", "b", "c"})
        # Aggregator is at top level (it's NOT nested).
        assert "summary" in ir.top_level_step_ids()
        # `summary` has 3 incoming edges (one from each branch).
        assert ir.incoming["summary"] == ["a", "b", "c"]
        # Top-level: plan → p → summary, in topo order.
        assert ir.top_level_step_ids() == ["plan", "p", "summary"]

    def test_aggregator_with_only_one_branch_edge_still_compiles(self):
        """Backward-compat: a parallel → 3 branches → aggregator where
        only ONE branch has an edge to the aggregator. The other two
        branches are still nested children (they execute inside the
        parallel) but their outputs are dropped on the canvas. Runtime
        can still execute this — the aggregator sees only the one
        branch's output."""
        nodes = [
            _n("plan", "agent"),
            _n("p", "flow", mode="parallel"),
            _n("a", "agent"), _n("b", "agent"), _n("c", "agent"),
            _n("summary", "agent"),
        ]
        edges = [
            _e("plan", "p"),
            _e("p", "a"), _e("p", "b"), _e("p", "c"),
            _e("a", "summary"),  # only a → summary
        ]
        ir = build_ir(nodes, edges)
        assert ir.flow_branches["p"] == ["a", "b", "c"]
        assert ir.nested_children == frozenset({"a", "b", "c"})
        assert ir.incoming["summary"] == ["a"]

    def test_ir_aggregator_has_aggregator_as_top_level_target(self):
        """The aggregator must appear in the IR's reachable top-level
        steps so the runtime / generator both wire it correctly into
        `Workflow(steps=[...])`."""
        nodes = [
            _n("plan", "agent"),
            _n("p", "flow", mode="parallel"),
            _n("a", "agent"), _n("b", "agent"),
            _n("agg", "agent"),
        ]
        edges = [
            _e("plan", "p"),
            _e("p", "a"), _e("p", "b"),
            _e("a", "agg"), _e("b", "agg"),
        ]
        ir = build_ir(nodes, edges)
        # `agg` is reachable from `plan` via `p → {a, b}` even though
        # `p` itself is the parallel node (not agg directly).
        assert "agg" in ir.top_level_step_ids()
        # Both branches contribute to the aggregator's incoming list.
        assert sorted(ir.incoming["agg"]) == ["a", "b"]

    def test_two_independent_parallel_sections_each_with_fan_in(self):
        """Two parallel sections in series, each with fan-in:
            plan → p1 → {a1, b1} → agg1 → p2 → {a2, b2} → agg2
        IR must keep both aggregators at top level, all four
        branches nested, and the two parallel nodes also at top
        level."""
        nodes = [
            _n("plan", "agent"),
            _n("p1", "flow", mode="parallel"), _n("a1", "agent"), _n("b1", "agent"),
            _n("agg1", "agent"),
            _n("p2", "flow", mode="parallel"), _n("a2", "agent"), _n("b2", "agent"),
            _n("agg2", "agent"),
        ]
        edges = [
            _e("plan", "p1"),
            _e("p1", "a1"), _e("p1", "b1"),
            _e("a1", "agg1"), _e("b1", "agg1"),
            _e("agg1", "p2"),
            _e("p2", "a2"), _e("p2", "b2"),
            _e("a2", "agg2"), _e("b2", "agg2"),
        ]
        ir = build_ir(nodes, edges)
        # Top-level only the entry, both parallels, both aggregators.
        assert ir.top_level_step_ids() == ["plan", "p1", "agg1", "p2", "agg2"]
        # All four branches are nested.
        assert ir.nested_children == frozenset({"a1", "b1", "a2", "b2"})
        # Both aggregators have 2 incoming edges each.
        assert sorted(ir.incoming["agg1"]) == ["a1", "b1"]
        assert sorted(ir.incoming["agg2"]) == ["a2", "b2"]

# ─────────────────────────────────────────────────────────────────
# Parallel emitter — IR-driven (single source of truth)
# ─────────────────────────────────────────────────────────────────
class TestFlowEmitterIRDriven:
    """The Python exporter's flow emitter must derive branches from
    `ctx['ir'].flow_branches` (edge-based) rather than from
    `cfg.branches`. This prevents drift if a user edits edges without
    touching cfg (or vice versa).
    """

    def _agent(self, id_, **cfg):
        """Build an agent node with a minimal model so the imports
        collector is happy."""
        cfg.setdefault("model", {"provider": "openai", "modelId": "gpt-4o-mini"})
        return _n(id_, "agent", **cfg)

    def _render(self, nodes, edges):
        from app.core.compile import to_python_source as render_python
        return render_python({"name": "test", "nodes": nodes, "edges": edges})

    def test_emitter_uses_edge_branches_when_cfg_branches_missing(self):
        """cfg.branches is empty but edges point at 3 agents. The
        exported `Parallel(...)` call must contain all 3."""
        nodes = [
            self._agent("plan"),
            _n("p", "flow", mode="parallel"),  # no cfg.branches
            self._agent("a"), self._agent("b"), self._agent("c"),
            self._agent("sum"),
        ]
        edges = [
            _e("plan", "p"),
            _e("p", "a"), _e("p", "b"), _e("p", "c"),
            _e("a", "sum"), _e("b", "sum"), _e("c", "sum"),
        ]
        code = self._render(nodes, edges)
        # All three branches appear in the Parallel(...) call.
        assert 'p_parallel = Parallel(' in code
        line = next(l for l in code.splitlines() if 'p_parallel = Parallel(' in l)
        assert 'agent=a_agent' in line
        assert 'agent=b_agent' in line
        assert 'agent=c_agent' in line

    def test_emitter_follows_edges_not_cfg_when_both_present(self):
        """When cfg.branches is wrong (lists only 1 of 3 targets) but
        edges point at all 3, the emitter must use the edges (3), not
        the cfg (1)."""
        nodes = [
            self._agent("plan"),
            _n("p", "flow", mode="parallel", branches=[{"target": "a"}]),  # cfg only mentions a
            self._agent("a"), self._agent("b"), self._agent("c"),
        ]
        edges = [
            _e("plan", "p"),
            _e("p", "a"), _e("p", "b"), _e("p", "c"),
        ]
        code = self._render(nodes, edges)
        line = next(l for l in code.splitlines() if 'p_parallel = Parallel(' in l)
        assert 'agent=a_agent' in line
        assert 'agent=b_agent' in line
        assert 'agent=c_agent' in line

    def test_emitter_emits_all_three_branches_with_fan_in(self):
        """End-to-end: the canonical `parallel → 3 branches → 1
        aggregator` topology exports as `Workflow(steps=[plan, p_parallel,
        sum])` with the aggregator at top level (NOT nested)."""
        nodes = [
            self._agent("plan"),
            _n("p", "flow", mode="parallel"),
            self._agent("a"), self._agent("b"), self._agent("c"),
            self._agent("sum"),
        ]
        edges = [
            _e("plan", "p"),
            _e("p", "a"), _e("p", "b"), _e("p", "c"),
            _e("a", "sum"), _e("b", "sum"), _e("c", "sum"),
        ]
        code = self._render(nodes, edges)
        # Top-level steps: plan_step, p_parallel, sum_step. NOT a/b/c.
        assert '_steps.append(plan_step)' in code
        assert '_steps.append(p_parallel)' in code
        assert '_steps.append(sum_step)' in code
        # Branch agents must NOT be appended at top level.
        for branch in ('a', 'b', 'c'):
            assert f'_steps.append({branch}_step)' not in code

# ─────────────────────────────────────────────────────────────────
# Connection-rules acceptance for N → 1 fan-in
# ─────────────────────────────────────────────────────────────────
class TestFanInConnectionRules:
    """3 → 1 fan-in must be accepted by the connection rules: the
    aggregator (an agent) has `max_incoming=null`, so N parallel
    branches converging on it is legal."""

    def test_three_to_one_fan_in_accepted(self):
        from app.core.connection_rules import validate_connections
        nodes = [
            {"id": "plan", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "p", "type": "flow", "position": {"x": 0, "y": 0}, "data": {"config": {"mode": "parallel"}}},
            {"id": "a", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "b", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "c", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "sum", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
        ]
        edges = [
            {"id": "e1", "source": "plan", "target": "p"},
            {"id": "e2", "source": "p", "target": "a"},
            {"id": "e3", "source": "p", "target": "b"},
            {"id": "e4", "source": "p", "target": "c"},
            {"id": "e5", "source": "a", "target": "sum"},
            {"id": "e6", "source": "b", "target": "sum"},
            {"id": "e7", "source": "c", "target": "sum"},
        ]
        assert validate_connections(nodes, edges) == []

    def test_aggregator_max_outgoing_one_still_holds(self):
        """A `sum` agent with 3 incoming AND 1 outgoing is legal.
        Adding a SECOND outgoing edge must be rejected (agent has
        max_outgoing=1)."""
        from app.core.connection_rules import validate_connections
        nodes = [
            {"id": "plan", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "p", "type": "flow", "position": {"x": 0, "y": 0}, "data": {"config": {"mode": "parallel"}}},
            {"id": "a", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "b", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "sum", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "x", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "y", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
        ]
        edges = [
            {"id": "e1", "source": "plan", "target": "p"},
            {"id": "e2", "source": "p", "target": "a"},
            {"id": "e3", "source": "p", "target": "b"},
            {"id": "e4", "source": "a", "target": "sum"},
            {"id": "e5", "source": "b", "target": "sum"},
            {"id": "e6", "source": "sum", "target": "x"},
            {"id": "e7", "source": "sum", "target": "y"},  # 2 outgoing from agent
        ]
        errs = validate_connections(nodes, edges)
        codes = [e.code for e in errs]
        assert "tooManyOutgoing" in codes

    def test_ir_is_frozen(self):
        """WorkflowIR is `@dataclass(frozen=True)` — its own attributes
        can't be mutated after construction. Nested dicts in `data`
        remain mutable (we don't deep-freeze), but assignment to the
        IR's own fields raises."""
        ir = build_ir([_n("a", "agent")], [])
        with pytest.raises((AttributeError, Exception)):
            ir.entry_id = "other"  # type: ignore[misc]

# ─────────────────────────────────────────────────────────────────
# tool_attachments — tool source → agent wiring
# ─────────────────────────────────────────────────────────────────
class TestToolAttachments:
    """Tool-source → agent wiring is captured as `kind="tool_attachment"`
    edges. The IR must:
      * put those edges into `tool_attachments[agent_id]` (NOT into
        `outgoing` / `incoming` / topo)
      * not affect nested_children (a tool source never appears as a
        top-level step regardless of attachments)
      * fall back to `cfg.toolsRef` when no typed edge is present
        (back-compat for pre-migration workflows)
      * de-dupe within an agent and preserve first-seen order
    """

    def test_tool_edge_goes_into_tool_attachments(self):
        nodes = [
            _n("ag", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges = [_e("t", "ag", kind="tool_attachment")]
        ir = build_ir(nodes, edges)
        assert ir.tool_attachments.get("ag") == ["t"]
        # The edge is NOT in the dataflow topology.
        assert "t" not in ir.outgoing or "ag" not in ir.outgoing.get("t", [])
        assert "ag" not in ir.incoming or "t" not in ir.incoming.get("ag", [])

    def test_tool_edge_does_not_change_topo(self):
        nodes = [
            _n("ag1", "agent"),
            _n("ag2", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges = [
            _e("ag1", "ag2"),                 # dataflow
            _e("t", "ag1", kind="tool_attachment"),
            _e("t", "ag2", kind="tool_attachment"),
        ]
        ir = build_ir(nodes, edges)
        # Top-level executables: only the agents. The tool source is
        # excluded because it's not executable.
        assert ir.top_level_step_ids() == ["ag1", "ag2"]
        # The dataflow edge is the only contributor to outgoing/incoming.
        assert ir.outgoing == {"ag1": ["ag2"], "ag2": [], "t": []}
        assert ir.incoming == {"ag1": [], "ag2": ["ag1"], "t": []}
        # The two tool attachments live in the new table.
        assert ir.tool_attachments.get("ag1") == ["t"]
        assert ir.tool_attachments.get("ag2") == ["t"]

    def test_tool_source_is_not_a_top_level_step(self):
        nodes = [
            _n("ag", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges = [_e("t", "ag", kind="tool_attachment")]
        ir = build_ir(nodes, edges)
        assert ir.top_level_step_ids() == ["ag"]
        assert "t" not in ir.top_level_step_ids()

    def test_multiple_agents_attach_same_tool(self):
        nodes = [
            _n("ag1", "agent"),
            _n("ag2", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges = [
            _e("t", "ag1", kind="tool_attachment"),
            _e("t", "ag2", kind="tool_attachment"),
        ]
        ir = build_ir(nodes, edges)
        assert ir.tool_attachments.get("ag1") == ["t"]
        assert ir.tool_attachments.get("ag2") == ["t"]

    def test_multiple_tools_attach_same_agent(self):
        nodes = [
            _n("ag", "agent"),
            _n("t1", "tool", source="function", functions=[]),
            _n("t2", "tool", source="function", functions=[]),
        ]
        edges = [
            _e("t1", "ag", kind="tool_attachment"),
            _e("t2", "ag", kind="tool_attachment"),
        ]
        ir = build_ir(nodes, edges)
        assert ir.tool_attachments.get("ag") == ["t1", "t2"]

    def test_duplicate_tool_edges_dedup(self):
        """Two `tool_attachment` edges for the same source → same agent
        collapse to one entry (preserving order)."""
        nodes = [
            _n("ag", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges = [
            _e("t", "ag", eid="e1", kind="tool_attachment"),
            _e("t", "ag", eid="e2", kind="tool_attachment"),
        ]
        ir = build_ir(nodes, edges)
        assert ir.tool_attachments.get("ag") == ["t"]

    def test_dataflow_edge_to_agent_does_not_attach_tool(self):
        """A dataflow edge from a tool-source node to an agent is
        structurally rejected by the connection rules, but even if it
        sneaks through (e.g. legacy JSON without kind), the IR should
        NOT add it to tool_attachments."""
        nodes = [
            _n("ag", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges = [_e("t", "ag")]  # kind=None → dataflow
        ir = build_ir(nodes, edges)
        # The edge lands in outgoing/incoming (tool is in dataflow topo).
        assert ir.outgoing.get("t") == ["ag"]
        # But it's NOT in tool_attachments.
        assert "ag" not in ir.tool_attachments

    def test_cfg_toolsref_fallback_when_no_edge(self):
        """Pre-migration workflows: `cfg.toolsRef` is the only signal.
        The IR must mirror those refs into `tool_attachments`."""
        nodes = [
            _n("ag", "agent", toolsRef=["t"]),
            _n("t", "tool", source="function", functions=[]),
        ]
        ir = build_ir(nodes, [])
        # tool_refs (legacy table) AND tool_attachments (new table) both
        # carry the reference so consumers can read either.
        assert ir.tool_refs.get("ag") == ["t"]
        assert ir.tool_attachments.get("ag") == ["t"]

    def test_edge_wins_over_cfg_when_both_present(self):
        """An agent with both a cfg.toolsRef entry and a typed edge
        for the SAME tool: the edge is the canonical source of truth.
        `tool_refs` still mirrors cfg (for back-compat); the new
        `tool_attachments` table gets the edge list and falls back to
        cfg for tools no edge covers yet."""
        nodes = [
            _n("ag", "agent", toolsRef=["t_edge", "t_cfg"]),
            _n("t_edge", "tool", source="function", functions=[]),
            _n("t_cfg", "tool", source="function", functions=[]),
        ]
        edges = [_e("t_edge", "ag", kind="tool_attachment")]
        ir = build_ir(nodes, edges)
        # tool_refs: cfg-only view, both refs present.
        assert ir.tool_refs.get("ag") == ["t_edge", "t_cfg"]
        # tool_attachments: edge wins, cfg fallback appends the rest.
        assert ir.tool_attachments.get("ag") == ["t_edge", "t_cfg"]

    def test_unknown_tool_source_dropped_silently(self):
        """Dangling tool-edges (target not an agent / source not a node)
        are dropped — the validator rejects them on save but the IR
        builder is tolerant."""
        nodes = [_n("ag", "agent")]
        edges = [_e("ghost", "ag", kind="tool_attachment")]
        ir = build_ir(nodes, edges)
        assert ir.tool_attachments == {}

    def test_tool_attachment_to_non_agent_dropped(self):
        """A tool_attachment edge targeting a non-agent (e.g. another
        tool node) is silently ignored — the validator already
        rejects these, the IR is just robust to bad input."""
        nodes = [
            _n("t1", "tool", source="function", functions=[]),
            _n("t2", "tool", source="function", functions=[]),
        ]
        edges = [_e("t1", "t2", kind="tool_attachment")]
        ir = build_ir(nodes, edges)
        assert ir.tool_attachments == {}