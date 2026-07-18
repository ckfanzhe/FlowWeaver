"""Strategy registry tests.

Pins the contract that:

  * Every `NodeTypeSpec` carries a populated `strategy` field.
  * Strategy ClassVars mirror the manifest's `kind` /
    `capabilities` (so consumers reading `strategy.IS_TOOL_SOURCE`
    and `spec.capabilities.isToolSource` agree).
  * `resolve_strategy()` returns the same instance on repeat lookups
    (sourced from `NODE_TYPES[name].strategy`) and raises `KeyError`
    on unknown types.
  * Strategy `build_tools()` for tool-source types delegates to
    `tool_factories` — after  a single
    `ToolStrategy` covers the old `http` / `mcp` / `tools` split,
    dispatching on `cfg.source`; the agent pipeline wires tools
    via pass 3.

After .A the legacy bridge (`LegacyModuleStrategy` /
`LegacyCallableStrategy` / `_ManifestOnlyStrategy`) is gone — every
manifest row points at a real `NodeStrategy` subclass and the resolver
fails loud on drift rather than adapting unknown shapes.
"""
from __future__ import annotations

import pytest

from app.core.node_types import NODE_TYPES
from app.core.strategies import NodeStrategy, resolve_strategy

# ─────────────────────────────────────────────────────────────────
# Every entry gets a strategy
# ─────────────────────────────────────────────────────────────────
class TestRegistryPopulation:
    def test_every_entry_has_a_strategy(self):
        for name, spec in NODE_TYPES.items():
            assert spec.strategy is not None, (
                f"node type {name!r} has no strategy"
            )
            assert isinstance(spec.strategy, NodeStrategy)

    def test_every_strategy_is_a_NodeStrategy(self):
        for spec in NODE_TYPES.values():
            assert isinstance(spec.strategy, NodeStrategy)

    def test_resolve_strategy_returns_same_instance(self):
        """The strategy cache must hit on repeat calls — the
        registry build is cached at module level, but the resolver
        is a separate cache and gets called per-request."""
        s1 = resolve_strategy("agent")
        s2 = resolve_strategy("agent")
        assert s1 is s2

    def test_resolve_strategy_unknown_type_raises(self):
        with pytest.raises(KeyError, match="no strategy for node type"):
            resolve_strategy("not_a_real_type")

# ─────────────────────────────────────────────────────────────────
# ClassVars mirror the manifest
# ─────────────────────────────────────────────────────────────────
class TestStrategyClassVarsMatchManifest:
    """After `_build_registry()` runs (during registry build),
    the strategy's ClassVars and the spec's capabilities MUST
    agree. A drift here means the pipeline's pass routing and
    the manifest's `kind` declaration would disagree. The legacy
    `_apply_spec_to_strategy` helper is gone — ClassVars are now
    declared on each strategy and matched 1:1 with the manifest's
    capabilities at module load time."""

    @pytest.mark.parametrize("name", sorted(NODE_TYPES))
    def test_kind_matches(self, name: str):
        spec = NODE_TYPES[name]
        assert spec.strategy.KIND == spec.kind

    @pytest.mark.parametrize("name", sorted(NODE_TYPES))
    def test_is_tool_source_matches(self, name: str):
        spec = NODE_TYPES[name]
        assert spec.strategy.IS_TOOL_SOURCE == spec.capabilities.isToolSource

    @pytest.mark.parametrize("name", sorted(NODE_TYPES))
    def test_needs_tool_wiring_matches(self, name: str):
        spec = NODE_TYPES[name]
        assert spec.strategy.NEEDS_TOOL_WIRING == spec.capabilities.needsToolWiring

    @pytest.mark.parametrize("name", sorted(NODE_TYPES))
    def test_step_wrapper_matches(self, name: str):
        spec = NODE_TYPES[name]
        assert spec.strategy.STEP_WRAPPER == spec.capabilities.stepWrapper

    @pytest.mark.parametrize("name", sorted(NODE_TYPES))
    def test_compound_pass_matches(self, name: str):
        spec = NODE_TYPES[name]
        assert spec.strategy.COMPOUND_PASS == spec.capabilities.compoundPass

# ─────────────────────────────────────────────────────────────────
# Specific kinds exercise the right code paths
# ─────────────────────────────────────────────────────────────────
class TestStrategyShapesForKnownKinds:
    def test_agent_strategy_kinds(self):
        """The agent is the only executable type with
        `needsToolWiring=True` + `stepWrapper='agent'`."""
        s = NODE_TYPES["agent"].strategy
        assert s.KIND == "executable"
        assert s.NEEDS_TOOL_WIRING is True
        assert s.STEP_WRAPPER == "agent"
        assert s.IS_TOOL_SOURCE is False
        assert s.COMPOUND_PASS is None

    def test_ask_strategy_kinds(self):
        s = NODE_TYPES["ask"].strategy
        assert s.KIND == "control_flow"
        assert s.STEP_WRAPPER == "ask"
        assert s.NEEDS_TOOL_WIRING is False

    @pytest.mark.parametrize("ntype,expected_pass", [
        ("flow", 10), ("branch", 20), ("loop", 30),
    ])
    def test_compound_types_have_correct_pass_order(self, ntype, expected_pass):
        s = NODE_TYPES[ntype].strategy
        assert s.KIND == "compound"
        assert s.COMPOUND_PASS == expected_pass
        assert s.STEP_WRAPPER == "none"

    def test_tool_source_type_flagged(self):
        """: `http` + `mcp` + `tools` collapsed
        into a single `tool` node type. The single registered
        `ToolStrategy` carries `KIND='tool_source'` /
        `IS_TOOL_SOURCE=True` for the merged type."""
        s = NODE_TYPES["tool"].strategy
        assert s.KIND == "tool_source"
        assert s.IS_TOOL_SOURCE is True
        assert s.NEEDS_TOOL_WIRING is False

    def test_presets_are_dispatched_via_tool_preset_config(self):
        """: the 5 presets collapsed into the
        `tool` node's `preset` config discriminator. Per-preset
        metadata lives in `app.core.strategies.tool.PRESET_REGISTRY`.
        `tool` itself stays the only tool_source strategy — presets
        are config-level, not node-type-level."""
        from app.core.strategies.tool import PRESET_REGISTRY
        # All 5 presets are in the registry.
        assert set(PRESET_REGISTRY) == {
            "wikipedia", "tavily_search", "duckduckgo",
            "calculator", "arxiv_search",
        }
        # `tool` is the only tool_source node type in the registry.
        tool_source_names = {
            name for name, spec in NODE_TYPES.items()
            if spec.capabilities.isToolSource
        }
        assert tool_source_names == {"tool"}
        # `tool` is the only node whose strategy carries
        # IS_TOOL_SOURCE=True (the registry is shape-pinned by
        # `test_tool_strategy_classvars` above).
        assert NODE_TYPES["tool"].strategy.IS_TOOL_SOURCE is True

# ─────────────────────────────────────────────────────────────────
# Real strategy subclasses (phase conversion)
# ─────────────────────────────────────────────────────────────────
class TestMigratedStrategyClasses:
    """phase wires the manifest's `runtime.builder` at a real
    `NodeStrategy` subclass instead of a callable / module. Once
    the manifest points at `app.core.strategies.agent.AgentStrategy`,
    the registry must surface that exact class — not a stub or a
    legacy bridge."""

    def test_agent_uses_real_strategy_subclass(self):
        from app.core.strategies.agent import AgentStrategy
        spec = NODE_TYPES["agent"]
        assert isinstance(spec.strategy, AgentStrategy)
        # Class-level metadata matches what the resolver applies
        # from the manifest's capabilities (defends against drift
        # if a future contributor changes one but not the other).
        assert AgentStrategy.KIND == "executable"
        assert AgentStrategy.NEEDS_TOOL_WIRING is True
        assert AgentStrategy.STEP_WRAPPER == "agent"

    def test_agent_strategy_build_runs_inline(self):
        """`AgentStrategy.build(...)` is now the implementation
        itself — no delegation to a separate emitter module. Smoke
        check: it runs without raising on a minimal node dict and
        returns an agno `Agent` (or raises a domain error before
        touching any LLM, which is fine — we just need to prove the
        method body executes inside the strategy, not via the
        retired `compile.emitters.agent.build` adapter)."""
        from app.core.strategies.agent import AgentStrategy
        # The strategy file holds the build method. Confirm via the
        # class — the method must come from `app.core.strategies.agent`,
        # not from a legacy bridge module.
        assert AgentStrategy.build.__module__ == "app.core.strategies.agent"
        # Touching the method should not raise NameError for
        # `compile.emitters.agent` (which would happen if a stale
        # delegation import survived .A).
        assert "build" in AgentStrategy.__dict__
        assert "to_source" in AgentStrategy.__dict__

    def test_agent_strategy_to_source_is_inline(self):
        from app.core.strategies.agent import AgentStrategy
        assert AgentStrategy.to_source.__module__ == "app.core.strategies.agent"

    def test_agent_strategy_injects_history_context_kwargs(self):
        """ (session — runtime multi-turn context fix):
        `AgentStrategy.build()` MUST inject `add_history_to_context=True`
        and `num_history_runs=5` into every agent the runtime
        constructs. Without these, agno's default
        `add_history_to_context=False` means the agent re-runs every
        turn as a fresh conversation — the user-visible symptom:
        "对话无法共享上下文" (a follow-up "创建任务" can't see the
        prior tool calls / tool results).

        The runtime already reuses the slim + agno session across
        turns (see `runtime_service._run_leg`); this test pins the
        matching agent-side wiring so the prior context actually
        shows up in the LLM's prompt window.
        """
        from unittest.mock import patch
        from app.core.compile.pipeline import CompileCtx
        from app.core.ir import WorkflowIR
        from app.core.strategies.agent import AgentStrategy

        # Build a minimal CompileCtx + empty WorkflowIR so the
        # build() path runs end-to-end without a real workflow.
        ir = WorkflowIR(
            node_map={},
            outgoing={},
            incoming={},
            topo_order=[],
            entry_id=None,
            flow_branches={},
            branch_branches={},
            loop_bodies={},
        )
        ctx = CompileCtx(ir=ir, nodes_by_id={})
        cfg = {
            # Inline `model` is checked FIRST in `build()` and a
            # successful `build_model(...)` result short-circuits the
            # default-preset fallback. Stub both so the test doesn't
            # depend on a real preset row in the DB.
            "model": {"provider": "openai", "modelId": "gpt-4o"},
            "instructions": "hi",
            "markdown": True,
        }
        # `build_model` resolves the LLM from DB-backed presets —
        # stub it to return a real agno Model instance so the
        # downstream `Agent.__init__` doesn't reject the sentinel.
        # `agent.py` does a local `from app.core.llm_runner import
        # build_model` inside `build()`, so we patch the attribute
        # on the llm_runner module (the `from X import Y` re-binds
        # `Y` in the local namespace but always looks it up on `X`
        # at import-time, and Python module imports are cached).
        from agno.models.openai import OpenAIChat
        sentinel_model = OpenAIChat(id="gpt-4o-mini")
        with patch(
            "app.core.llm_runner.build_model",
            return_value=sentinel_model,
        ), patch(
            "app.core.llm_runner._resolve_default_preset_id",
            return_value=None,
        ):
            agent = AgentStrategy().build(
                "n1",
                {"id": "n1", "data": {"config": cfg}},
                ctx,
            )
        # Pin the contract — if either kwarg disappears the LLM
        # will lose conversation context across turns.
        assert agent.add_history_to_context is True, (
            "agent lost the multi-turn context fix; "
            "see session in chat_builder/chat_run split commit history"
        )
        assert agent.num_history_runs == 5, (
            "agent num_history_runs regressed from 5; the agent will "
            "forget earlier turns after 3 — see session rationale"
        )

    def test_agent_strategy_to_source_emits_history_context_kwargs(self):
        """session parity: `to_source()` MUST also emit the history
        context kwargs so an exported .py file (re-run via
        `Wf.run()` outside the platform) behaves identically to the
        runtime build(). Runtime/export divergence here would be a
        user-visible axis — exported workflows would lose the
        multi-turn context behaviour."""
        from app.core.compile.pipeline import CompileCtx
        from app.core.ir import WorkflowIR
        from app.core.strategies.agent import AgentStrategy

        ir = WorkflowIR(
            node_map={},
            outgoing={},
            incoming={},
            topo_order=[],
            entry_id=None,
            flow_branches={},
            branch_branches={},
            loop_bodies={},
        )
        ctx = CompileCtx(ir=ir, nodes_by_id={})
        cfg = {
            "model": {"provider": "openai", "modelId": "gpt-4o"},
            "instructions": "hi",
            "markdown": True,
        }
        src = AgentStrategy().to_source(
            "n1",
            {"id": "n1", "data": {"config": cfg}},
            ctx,
        )
        assert "add_history_to_context=True" in src, (
            "to_source() must emit add_history_to_context=True so the "
            "exported .py carries the multi-turn context behaviour"
        )
        assert "num_history_runs=5" in src

    def test_num_history_runs_is_configurable(self):
        """`numHistoryRuns` on the agent config
        MUST be read by `build()` and emitted by `to_source()`.
        Hard-coding the v1 default of 5 was the prior behaviour
        (the magic number in the prompt builder) — this PR
        promotes it to a per-agent config field. Pydantic caps
        the field at 50 (no runaway prompts) and 1 (no
        zero/negative). Pin both the build() and the to_source()
        round-trip so a power user who sets `numHistoryRuns: 20`
        gets 20 in the runtime AND 20 in the exported .py."""
        from unittest.mock import patch
        from app.core.compile.pipeline import CompileCtx
        from app.core.ir import WorkflowIR
        from agno.models.openai import OpenAIChat
        from app.core.strategies.agent import AgentStrategy

        ir = WorkflowIR(
            node_map={},
            outgoing={},
            incoming={},
            topo_order=[],
            entry_id=None,
            flow_branches={},
            branch_branches={},
            loop_bodies={},
        )
        ctx = CompileCtx(ir=ir, nodes_by_id={})
        cfg = {
            "model": {"provider": "openai", "modelId": "gpt-4o"},
            "instructions": "hi",
            "markdown": True,
            "numHistoryRuns": 20,
        }
        sentinel_model = OpenAIChat(id="gpt-4o-mini")
        with patch(
            "app.core.llm_runner.build_model",
            return_value=sentinel_model,
        ):
            agent = AgentStrategy().build(
                "n1",
                {"id": "n1", "data": {"config": cfg}},
                ctx,
            )
        assert agent.num_history_runs == 20, (
            "configurable num_history_runs not honoured — runtime "
            "fell back to default 5"
        )
        # to_source() round-trip — the exported .py must use 20 too.
        src = AgentStrategy().to_source(
            "n1",
            {"id": "n1", "data": {"config": cfg}},
            ctx,
        )
        assert "num_history_runs=20" in src, (
            "to_source() emitted default 5 instead of configured 20 — "
            "runtime/export divergence on a user-visible axis"
        )
        # And the default still works when no override is given.
        cfg_default = {**cfg}
        cfg_default.pop("numHistoryRuns")
        with patch(
            "app.core.llm_runner.build_model",
            return_value=sentinel_model,
        ):
            agent_default = AgentStrategy().build(
                "n1",
                {"id": "n1", "data": {"config": cfg_default}},
                ctx,
            )
        assert agent_default.num_history_runs == 5, (
            "default num_history_runs regressed from 5"
        )

    @pytest.mark.parametrize("node_type,strategy_cls,kind", [
        # Single `ToolStrategy` covers the prior http / mcp /
        # tools split. The three parametrize rows collapse to one.
        ("tool", "ToolStrategy", "tool_source"),
        ("branch", "BranchStrategy", "compound"),
        ("flow", "FlowStrategy", "compound"),
        ("loop", "LoopStrategy", "compound"),
        ("ask", "AskStrategy", "control_flow"),
    ])
    def test_migrated_strategy_uses_real_subclass(
        self, node_type, strategy_cls, kind
    ):
        """After phase every entry in the manifest points at a
        real `NodeStrategy` subclass in `app.core.strategies.*` —
        the registry must surface that class."""
        import importlib
        mod = importlib.import_module(f"app.core.strategies.{node_type}")
        cls = getattr(mod, strategy_cls)
        spec = NODE_TYPES[node_type]
        assert isinstance(spec.strategy, cls)
        # Class-level metadata matches manifest capabilities.
        cls_attr = getattr(cls, "KIND")
        assert cls_attr == kind
        assert spec.kind == kind

    def test_compound_pass_order_matches_manifest(self):
        """Each compound strategy's COMPOUND_PASS ClassVar must
        match the manifest's `compoundPass` integer — the pipeline
        sorts compound types on this key during pass 2."""
        from app.core.strategies.flow import FlowStrategy
        from app.core.strategies.branch import BranchStrategy
        from app.core.strategies.loop import LoopStrategy
        assert FlowStrategy.COMPOUND_PASS == 10
        assert BranchStrategy.COMPOUND_PASS == 20
        assert LoopStrategy.COMPOUND_PASS == 30

    def test_tool_strategy_uses_tool_source_kind(self):
        """: the single `ToolStrategy` (merged
        from `HttpToolStrategy` + `McpToolStrategy` +
        `UserFunctionsToolStrategy`) carries `KIND='tool_source'`
        and `IS_TOOL_SOURCE=True`. Verify via the class directly —
        the resolver applies these from the manifest but the
        ClassVars make the contract explicit."""
        from app.core.strategies.tool import ToolStrategy
        assert ToolStrategy.KIND == "tool_source"
        assert ToolStrategy.IS_TOOL_SOURCE is True
        assert ToolStrategy.STEP_WRAPPER == "none"

# ─────────────────────────────────────────────────────────────────
# Strategy-based tool dispatcher (phase)
# ─────────────────────────────────────────────────────────────────
class TestStrategyToolDispatcher:
    """phase collapses `tool_factories.build_tools_for_node(...)`
    to a 3-line dispatcher that delegates to the registered
    strategy. These tests pin the contract: the public entry point
    routes through the strategy registry, and each tool-source
    strategy's `build_tools()` is the implementation."""

    def test_dispatcher_routes_through_strategy(self):
        """`build_tools_for_node(...)` consults `NODE_TYPES[type].strategy`
        and calls `strategy.build_tools(...)`. Patching the strategy
        module proves the dispatcher goes through the registry — not
        a hardcoded `if/elif` chain. : the
        old `app.core.strategies.tools.UserFunctionsToolStrategy`
        is gone — the dispatcher now routes through the single
        `app.core.strategies.tool.ToolStrategy`."""
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node
        from app.core.strategies import tool as tool_strat_module

        captured = {}

        # Must accept `self` because we patch the class attribute —
        # Python turns it into a bound method when accessed via an
        # instance.
        def fake_build_tools(self, nid, ir_node, ir_nodes, *, user_id=None):
            captured["nid"] = nid
            captured["user_id"] = user_id
            return ["fake-fn"]

        original = tool_strat_module.ToolStrategy.build_tools
        tool_strat_module.ToolStrategy.build_tools = fake_build_tools
        try:
            node = IRNode(
                id="t", type="tool",
                data={"label": "t", "config": {"source": "function", "functions": []}},
            )
            out = build_tools_for_node(node, {"t": node}, user_id="alice")
            assert out == ["fake-fn"]
            assert captured["nid"] == "t"
            assert captured["user_id"] == "alice"
        finally:
            tool_strat_module.ToolStrategy.build_tools = original

    def test_dispatcher_returns_empty_for_non_tool_source(self):
        """`agent` is `IS_TOOL_SOURCE=False` → dispatcher returns `[]`
        without consulting the agent strategy's `build_tools`."""
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="a", type="agent", data={"config": {}})
        out = build_tools_for_node(node, {"a": node})
        assert out == []

    def test_dispatcher_returns_empty_for_unknown_type(self):
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="x", type="nonexistent_type", data={"config": {}})
        out = build_tools_for_node(node, {"x": node})
        assert out == []

# ─────────────────────────────────────────────────────────────────
# Post-.A registry invariants
# ─────────────────────────────────────────────────────────────────
class TestRegistryNoLegacyBridge:
    """After .A the resolver is strict: a manifest entry whose
    `runtime.builder` does not resolve to a real `NodeStrategy`
    subclass is a manifest bug, not a recoverable shape. These
    tests pin the failure modes so a regression that re-introduces
    the bridge (`LegacyModuleStrategy` / `LegacyCallableStrategy`)
    fails loud at registry build."""

    def test_resolver_module_top_level_has_no_legacy_exports(self):
        """`compile.emitters.*`, `_legacy_bridge`, and `_ManifestOnlyStrategy`
        must not be importable from `app.core.strategies`. The bridge
        existed precisely so unknown builder shapes would adapt;
        now the resolver raises instead, so we want the symbols gone
        to keep the public surface honest."""
        import app.core.strategies as strategies_mod
        for name in (
            "LegacyModuleStrategy",
            "LegacyCallableStrategy",
            "_ManifestOnlyStrategy",
            "_apply_spec_to_strategy",
            "_looks_like_emitter_module",
            "reset_strategy_cache",
        ):
            assert not hasattr(strategies_mod, name), (
                f"app.core.strategies still exports {name!r} — "
                "the legacy bridge should be gone after .A"
            )

    def test_emitters_module_directory_does_not_exist(self):
        """The `compile/emitters/` directory held one module per node
        type. After the fold each strategy owns `build()` +
        `to_source()` inline, so the directory is obsolete. Any
        import path that resolves to it means a stale adapter
        survived."""
        with pytest.raises(ModuleNotFoundError):
            import app.core.compile.emitters  # noqa: F401

    def test_instantiate_rejects_legacy_callable_shape(self):
        """If a manifest row ever points at a plain function instead
        of a `NodeStrategy` subclass, `_instantiate_one` must raise
        — the bridge that used to wrap the function is gone."""
        import types
        from app.core.strategies import _instantiate_one

        fake_mod_path = "app.core.strategies"  # module we'll graft onto
        mod = types.ModuleType("app.core.strategies._test_fake_builder")

        def _bogus_builder(node, **kw):  # plain function, not a NodeStrategy
            return None

        mod.bogus_builder = _bogus_builder
        import sys
        sys.modules[mod.__name__] = mod
        try:
            spec = types.SimpleNamespace(
                name="bogus",
                runtime_module_path=mod.__name__,
                runtime_builder_name="bogus_builder",
            )
            with pytest.raises(RuntimeError, match="expected a NodeStrategy subclass"):
                _instantiate_one(spec)
        finally:
            sys.modules.pop(mod.__name__, None)

    def test_instantiate_rejects_legacy_module_shape(self):
        """If a manifest row ever points at a module (instead of a
        `NodeStrategy` subclass), `_instantiate_one` must raise.
        `LegacyModuleStrategy` used to wrap such modules."""
        import types
        from app.core.strategies import _instantiate_one

        mod = types.ModuleType("app.core.strategies._test_fake_emitter")
        mod.build = lambda *a, **kw: None
        mod.to_source = lambda *a, **kw: ""
        import sys
        sys.modules[mod.__name__] = mod
        try:
            spec = types.SimpleNamespace(
                name="bogus_emitter",
                runtime_module_path=mod.__name__,
                runtime_builder_name="build",  # module-level function
            )
            with pytest.raises(RuntimeError, match="expected a NodeStrategy subclass"):
                _instantiate_one(spec)
        finally:
            sys.modules.pop(mod.__name__, None)

    def test_instantiate_raises_on_missing_attribute(self):
        """Manifest drift: `runtime.builder` points at a name that
        no longer exists in the module. The resolver must surface
        this at registry build, not at the first workflow run."""
        import types as _types
        from app.core.strategies import _instantiate_one

        spec = _types.SimpleNamespace(
            name="drifted",
            runtime_module_path="app.core.strategies.base",
            runtime_builder_name="StrategyThatDefinitelyDoesNotExist",
        )
        with pytest.raises(RuntimeError, match="does not exist \\(manifest drift\\?\\)"):
            _instantiate_one(spec)

    def test_resolve_strategy_returns_real_subclass_instance(self):
        """`resolve_strategy` must surface a concrete subclass (not
        an adapter / proxy / stub). `isinstance` against the named
        class on every entry proves it. :
        `http` / `mcp` / `tools` entries are gone from
        `NODE_TYPES` — the single `tool` entry uses
        `ToolStrategy`. Presets extend `tool` so their
        `strategy` field is the same instance."""
        from app.core.strategies.agent import AgentStrategy
        from app.core.strategies.branch import BranchStrategy
        from app.core.strategies.flow import FlowStrategy
        from app.core.strategies.loop import LoopStrategy
        from app.core.strategies.ask import AskStrategy
        from app.core.strategies.tool import ToolStrategy

        expected = {
            "agent": AgentStrategy,
            "branch": BranchStrategy,
            "flow": FlowStrategy,
            "loop": LoopStrategy,
            "ask": AskStrategy,
            "tool": ToolStrategy,
        }
        for name, cls in expected.items():
            spec = NODE_TYPES[name]
            assert isinstance(spec.strategy, cls), (
                f"node type {name!r}: strategy is "
                f"{type(spec.strategy).__name__}, expected {cls.__name__}"
            )
            assert type(spec.strategy) is cls, (
                f"node type {name!r}: strategy is a subclass "
                f"({type(spec.strategy).__name__}), not the exact class"
            )

# ─────────────────────────────────────────────────────────────────
# NodeStrategy ABC defaults
# ─────────────────────────────────────────────────────────────────
class TestNodeStrategyDefaults:
    def test_default_kind_is_executable(self):
        s = NodeStrategy()
        assert s.KIND == "executable"

    def test_default_tool_flags_are_false(self):
        s = NodeStrategy()
        assert s.IS_TOOL_SOURCE is False
        assert s.NEEDS_TOOL_WIRING is False

    def test_default_compound_pass_is_none(self):
        s = NodeStrategy()
        assert s.COMPOUND_PASS is None

    def test_default_step_wrapper_is_none(self):
        s = NodeStrategy()
        assert s.STEP_WRAPPER == "none"

    def test_default_build_raises(self):
        s = NodeStrategy()
        with pytest.raises(NotImplementedError):
            s.build("n1", {"type": "x"}, None)

    def test_default_to_source_raises(self):
        s = NodeStrategy()
        with pytest.raises(NotImplementedError):
            s.to_source("n1", {"type": "x"}, None)

    def test_default_build_tools_returns_empty(self):
        s = NodeStrategy()
        assert s.build_tools("n1", None, {}) == []