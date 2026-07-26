"""Runtime tool-wiring tests.

These pin the new invariant:

  - tool-source nodes (`tools` / `http` / `mcp`) are NEVER compiled
    into their own `Step` in the agno workflow — they're definitions
    that an Agent consumes via `tools=[...]`.
  - An Agent with attached tools gets a real `tools=[...]` list passed
    to its `Agent(...)` constructor.
  - The pre-migration `cfg.toolsRef` path still works (legacy
    workflows haven't been migrated yet, but the runtime keeps
    honouring it).
  - Tool-source nodes connected via a `tool_attachment` edge
    produce the same wiring as the `cfg.toolsRef` path.

These tests use `build_workflow(...)` directly rather than the full
`execute(...)` so we can introspect the compiled `Workflow` object.
"""
from __future__ import annotations

import pytest

def _n(id_: str, type_: str, **cfg) -> dict:
    return {
        "id": id_, "type": type_, "position": {"x": 0, "y": 0},
        "data": {"label": id_, "config": cfg},
    }

def _e(src: str, tgt: str, *, kind: str | None = None) -> dict:
    d = {"id": f"e-{src}-{tgt}", "source": src, "target": tgt}
    if kind is not None:
        d["kind"] = kind
    return d

# ─────────────────────────────────────────────────────────────────
# tool-source nodes are not Steps
# ─────────────────────────────────────────────────────────────────
class TestToolSourceNotInSteps:
    """A `tool` (source='function') node connected to an agent via cfg.toolsRef must NOT
    appear as a top-level step in the compiled Workflow. The agent
    gets the tools via its own `tools=[...]` list instead."""

    def test_tools_node_not_in_workflow_steps(self, seeded_default_preset):
        from app.core.compile import build_workflow

        nodes = [
            _n("a", "agent"),
            _n("t", "tool", source="function", functions=[]),
        ]
        edges: list[dict] = []  # cfg.toolsRef on `a` is the only wiring
        # Set toolsRef on the agent
        nodes[0]["data"]["config"]["toolsRef"] = ["t"]

        wf = build_workflow("wf-tools-not-step", "wf-tools-not-step", nodes, edges)
        step_ids = [s.step_id for s in (wf.steps or []) if getattr(s, "step_id", None)]
        assert "a" in step_ids, "agent must still be a step"
        assert "t" not in step_ids, (
            f"tools node must NOT be a top-level step; got {step_ids}"
        )

    def test_http_node_not_in_workflow_steps(self, seeded_default_preset):
        from app.core.compile import build_workflow

        nodes = [
            _n("a", "agent"),
            _n("h", "tool", source="http", baseUrl="https://api.example.com"),
        ]
        edges = []
        nodes[0]["data"]["config"]["toolsRef"] = ["h"]

        wf = build_workflow("wf-http-not-step", "wf-http-not-step", nodes, edges)
        step_ids = [s.step_id for s in (wf.steps or []) if getattr(s, "step_id", None)]
        assert "a" in step_ids
        assert "h" not in step_ids

    def test_mcp_node_not_in_workflow_steps(self, seeded_default_preset):
        from app.core.compile import build_workflow

        # Use a non-empty (but unknown) serverId — the recent
        # missing `serverId` raise `CompileError` instead of silently
        # skipping, so this test now exercises the "config valid but
        # no matching DB row" path which still returns no tools and
        # therefore keeps the mcp node out of `wf.steps`.
        nodes = [
            _n("a", "agent"),
            _n("m", "tool", source="mcp", serverId="ghost-server-id"),
        ]
        edges = []
        nodes[0]["data"]["config"]["toolsRef"] = ["m"]

        wf = build_workflow("wf-mcp-not-step", "wf-mcp-not-step", nodes, edges)
        step_ids = [s.step_id for s in (wf.steps or []) if getattr(s, "step_id", None)]
        assert "a" in step_ids
        assert "m" not in step_ids

# ─────────────────────────────────────────────────────────────────
# agent.tools is populated from cfg.toolsRef or tool_attachment edge
# ─────────────────────────────────────────────────────────────────
class TestAgentToolsWiring:
    """An agent with attached tools must end up with a non-empty
    `tools=[...]` list on the compiled `Agent` object."""

    def test_agent_gets_function_from_tools_node(self, seeded_default_preset):
        from app.core.compile import build_workflow
        from agno.workflow import Step

        nodes = [
            _n("a", "agent"),
            _n("t", "tool", source="function", functions=[
                {"name": "add", "description": "add two ints", "code": "def add(a, b):\n    return a + b\n"},
            ]),
        ]
        edges = []
        nodes[0]["data"]["config"]["toolsRef"] = ["t"]

        wf = build_workflow("wf-agent-tools", "wf-agent-tools", nodes, edges)
        agent_step = next(s for s in wf.steps if isinstance(s, Step) and s.step_id == "a")
        agent = agent_step.agent
        # Agent must carry the user's tool function.
        assert agent.tools, "agent.tools must be populated from cfg.toolsRef"
        assert len(agent.tools) >= 1

    def test_agent_gets_function_from_tool_attachment_edge(self, seeded_default_preset):
        from app.core.compile import build_workflow
        from agno.workflow import Step

        nodes = [
            _n("a", "agent"),
            _n("t", "tool", source="function", functions=[
                {"name": "sub", "description": "subtract", "code": "def sub(a, b):\n    return a - b\n"},
            ]),
        ]
        edges = [_e("t", "a", kind="tool_attachment")]
        # No cfg.toolsRef — the edge alone wires the tool.

        wf = build_workflow("wf-edge-tools", "wf-edge-tools", nodes, edges)
        agent_step = next(s for s in wf.steps if isinstance(s, Step) and s.step_id == "a")
        agent = agent_step.agent
        assert agent.tools, "agent.tools must be populated from a tool_attachment edge"
        assert len(agent.tools) >= 1

    def test_agent_without_tools_has_empty_tools_list(self, seeded_default_preset):
        from app.core.compile import build_workflow
        from agno.workflow import Step

        nodes = [_n("a", "agent")]
        edges = []
        wf = build_workflow("wf-no-tools", "wf-no-tools", nodes, edges)
        agent_step = next(s for s in wf.steps if isinstance(s, Step) and s.step_id == "a")
        agent = agent_step.agent
        # No tools attached → Agent(tools=[]) leaves tools as empty list
        # (or None — both are legal in agno). Assert it's not populated.
        assert not agent.tools or len(agent.tools) == 0

    def test_dangling_tool_ref_is_silently_dropped(self, seeded_default_preset):
        """A cfg.toolsRef pointing at a non-existent tool id must NOT
        crash the build — the agent just runs without that tool."""
        from app.core.compile import build_workflow
        from agno.workflow import Step

        nodes = [_n("a", "agent")]
        nodes[0]["data"]["config"]["toolsRef"] = ["ghost"]
        edges = []
        wf = build_workflow("wf-dangling-ref", "wf-dangling-ref", nodes, edges)
        agent_step = next(s for s in wf.steps if isinstance(s, Step) and s.step_id == "a")
        agent = agent_step.agent
        # No real tools were built.
        assert not agent.tools or len(agent.tools) == 0

# ─────────────────────────────────────────────────────────────────
# factory output shapes
# ─────────────────────────────────────────────────────────────────
class TestToolFactories:
    """Unit tests for `app.core.tool_factories.build_tools_for_node` —
    the runtime analogue of the generator's `tools_expr`."""

    def test_tools_node_returns_function_list(self):
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="t", type="tool", data={"label": "t", "config": {"source": "function", "functions": [
                {"name": "noop", "description": "no-op", "code": "def noop():\n    return 1\n"},
            ]}},
        )
        out = build_tools_for_node(node, {"t": node})
        assert len(out) == 1

    def test_tools_node_skips_functions_without_code(self):
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="t", type="tool", data={"label": "t", "config": {"source": "function", "functions": [
                {"name": "good", "description": "", "code": "def good():\n    return 1\n"},
                {"name": "bad", "description": "", "code": ""},  # empty code
            ]}},
        )
        out = build_tools_for_node(node, {"t": node})
        assert len(out) == 1

    def test_http_node_missing_base_url_raises(self):
        # Configuration-error invariant: a missing `baseUrl` is a configuration
        # error, not a silent-skip case. The user gets a `CompileError`
        # that the runtime surfaces as an SSE `error` event so they
        # see WHY the agent has no tool to call.
        from app.core.ir import IRNode
        from app.core.compile.errors import CompileError
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="h", type="tool", data={"label": "h", "config": {"source": "http", "baseUrl": ""}},
        )
        with pytest.raises(CompileError, match="missing `baseUrl`"):
            build_tools_for_node(node, {"h": node})

    def test_http_node_with_base_url_returns_one_function(self):
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="h", type="tool", data={"label": "h", "config": {
                "source": "http",
                "toolName": "fetch_user",
                "baseUrl": "https://api.example.com",
                "path": "/users/1",
                "method": "GET",
            }},
        )
        out = build_tools_for_node(node, {"h": node})
        assert len(out) == 1

    def test_mcp_node_missing_server_id_raises(self):
        # Configuration-error invariant: mirror of the http test above — a
        # missing `serverId` is a configuration error, surfaced via
        # `CompileError` instead of being silently skipped.
        from app.core.ir import IRNode
        from app.core.compile.errors import CompileError
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="m", type="tool", data={"label": "m", "config": {"source": "mcp", "serverId": ""}},
        )
        with pytest.raises(CompileError, match="missing `serverId`"):
            build_tools_for_node(node, {"m": node})

    def test_mcp_node_unknown_server_returns_empty(self):
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        node = IRNode(id="m", type="tool", data={"label": "m", "config": {"source": "mcp", "serverId": "ghost-server-id"}},
        )
        out = build_tools_for_node(node, {"m": node})
        # No matching server row → log + skip → empty list.
        # (Different from missing `serverId` — see test above. Here
        # the config is valid, the DB just doesn't have a row.)
        assert out == []

    def test_unknown_type_returns_empty(self):
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node

        # Tool-source types only — anything else is unexpected.
        # We use `agent` here, which is NOT in the dispatch.
        node = IRNode(
            id="a", type="agent",
            data={"label": "a", "config": {}},
        )
        out = build_tools_for_node(node, {"a": node})
        assert out == []

# ─────────────────────────────────────────────────────────────────
# Back-compat: legacy `cfg.toolsRef` continues to work
# ─────────────────────────────────────────────────────────────────
class TestLegacyToolsRefBackCompat:
    """Pre-Phase-1 workflows used `cfg.toolsRef` (no typed edge). The
    IR promotes cfg entries into `tool_attachments` as a fallback, so
    the runtime and the generator both still wire tools from it."""

    def test_legacy_cfg_tools_ref_produces_agent_tools(self, seeded_default_preset):
        from app.core.compile import build_workflow
        from agno.workflow import Step

        nodes = [
            _n("a", "agent"),
            _n("t", "tool", source="function", functions=[
                {"name": "echo", "description": "echo", "code": "def echo(x):\n    return x\n"},
            ]),
        ]
        # No tool_attachment edges — only cfg.toolsRef.
        nodes[0]["data"]["config"]["toolsRef"] = ["t"]
        edges = []

        wf = build_workflow("wf-legacy", "wf-legacy", nodes, edges)
        agent_step = next(s for s in wf.steps if isinstance(s, Step) and s.step_id == "a")
        agent = agent_step.agent
        assert agent.tools, "legacy cfg.toolsRef must still wire the agent's tools"

# ─────────────────────────────────────────────────────────────────
# Preset toolkits — built-in agno Toolkits wrapped
# via Function.from_callable. Chosen over user-functions because the
# preset toolkit modules (agno.tools.tavily / .duckduckgo / .calculator
# / .arxiv) need `from agno.tools.X import Y` — which the
# safe-builtins sandbox can't service.
# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
# Preset toolkit factories: the 5 preset tool
# types collapsed into the `tool` node's `preset` config discriminator.
# Per-preset metadata (toolkit_class / toolkit_methods / default_config)
# now lives in `app.core.strategies.tool.PRESET_REGISTRY` — the tests
# below pin the registry shape + exercise `build_toolkit_for_preset`
# directly (which is the kernel the dispatcher calls once it resolves
# the preset from the registry).
#
# Only the `calculator` preset is exercised live here because the
# other three (`tavily_search` / `duckduckgo` / `arxiv_search`) need
# optional deps (`ddgs`) or paid API keys at import time. The
# registry surface tests below cover all 4. `wikipedia` is an HTTP
# preset and is exercised by `tests/test_node_types.py` /
# `tests/test_strategies.py` via `ToolStrategy` + `PRESET_REGISTRY`.
#
# Preset toolkit tests are NOT sandboxed via the safe-builtins
# fixture because the agno toolkits (`from agno.tools.calculator
# import CalculatorTools`) need `from agno.tools.X import Y` — which the
# safe-builtins sandbox can't service.
# ─────────────────────────────────────────────────────────────────
class TestPresetRegistryToolkitSurface:
    """Preset collapse: the per-preset toolkit class +
    method list now lives in `PRESET_REGISTRY` (in
    `app.core.strategies.tool`) — no more `NodeTypeSpec.toolkit_class`
    / `.toolkit_methods` fields. These tests pin the registry shape
    for the 4 toolkit presets (the wikipedia preset is HTTP, not a
    toolkit)."""

    def test_registry_has_4_toolkit_presets_with_classes(self):
        from app.core.strategies.tool import PRESET_REGISTRY

        expectations = {
            "tavily_search": (
                "agno.tools.tavily.TavilyTools",
                ["web_search_using_tavily"],
            ),
            "duckduckgo": (
                "agno.tools.duckduckgo.DuckDuckGoTools",
                ["web_search"],
            ),
            "calculator": (
                "agno.tools.calculator.CalculatorTools",
                ["add", "subtract", "multiply", "divide"],
            ),
            "arxiv_search": (
                "agno.tools.arxiv.ArxivTools",
                ["search_arxiv_and_return_articles"],
            ),
        }
        for name, (cls_path, methods) in expectations.items():
            spec = PRESET_REGISTRY[name]
            assert spec.toolkit_class == cls_path, (
                f"{name} toolkit_class drifted: {spec.toolkit_class!r}"
            )
            assert list(spec.toolkit_methods) == methods, (
                f"{name} toolkit_methods drifted: {list(spec.toolkit_methods)!r}"
            )

    def test_node_types_manifest_has_no_preset_entries(self):
        """Preset collapse: the 5 presets are no longer
        separate manifest entries — they're a config discriminator on
        the unified `tool` node. `NODE_TYPES` has exactly 7 base
        types (the 6 prior + `knowledge`, new in
        [[gleaming-munching-grove]])."""
        from app.core.node_types import NODE_TYPES

        assert "wikipedia" not in NODE_TYPES
        assert "tavily_search" not in NODE_TYPES
        assert "duckduckgo" not in NODE_TYPES
        assert "calculator" not in NODE_TYPES
        assert "arxiv_search" not in NODE_TYPES
        assert set(NODE_TYPES) == {
            "agent", "branch", "flow", "loop", "ask", "tool", "knowledge",
        }

    def test_tool_strategy_resolves_to_toolstrategy(self):
        """Preset collapse: preset tool nodes all share the
        unified `tool` entry's `ToolStrategy` (not a separate
        `PresetToolkitStrategy` which was deleted in Step 1+2)."""
        from app.core.node_types import NODE_TYPES
        from app.core.strategies.tool import ToolStrategy

        assert isinstance(NODE_TYPES["tool"].strategy, ToolStrategy)

class TestPresetToolkitFactories:
    """Preset toolkit factories exercise the
    `build_toolkit_for_preset(nid, preset_spec, node)` kernel
    directly. The dispatcher (`ToolStrategy.build_tools`) is what
    chat-time / run-time calls — it resolves `cfg.preset` from
    `PRESET_REGISTRY` and forwards here. We test the kernel in
    isolation so a future dispatcher change can't silently break
    the contract."""

    def test_calculator_preset_returns_4_functions(self):
        """The calculator preset has 4 declared methods (add /
        subtract / multiply / divide). All 4 must surface as
        `Function` instances the agent can attach."""
        from app.core.ir import IRNode
        from app.core.strategies.tool import PRESET_REGISTRY
        from app.core.tool_factories import build_toolkit_for_preset
        from agno.tools.function import Function

        preset_spec = PRESET_REGISTRY["calculator"]
        node = IRNode(
            id="calc", type="tool",
            data={"label": "calc", "config": {"preset": "calculator"}},
        )
        tools = build_toolkit_for_preset("calc", preset_spec, node)
        assert len(tools) == 4, (
            f"calculator should expose 4 methods, got {len(tools)}"
        )
        for fn in tools:
            assert isinstance(fn, Function), (
                f"expected Function, got {type(fn).__name__}"
            )
        names = sorted(fn.name for fn in tools)
        assert names == ["add", "divide", "multiply", "subtract"]

    def test_calculator_preset_functions_call_through_to_toolkit(self):
        """`Function.from_callable(toolkit.method, ...)` must keep the
        bound method's behavior — the function still computes 2+3=5
        via the toolkit's own implementation (we don't re-implement
        the math here)."""
        import json

        from app.core.ir import IRNode
        from app.core.strategies.tool import PRESET_REGISTRY
        from app.core.tool_factories import build_toolkit_for_preset

        preset_spec = PRESET_REGISTRY["calculator"]
        node = IRNode(
            id="calc", type="tool",
            data={"label": "calc", "config": {"preset": "calculator"}},
        )
        tools = {fn.name: fn for fn in build_toolkit_for_preset("calc", preset_spec, node)}
        # CalculatorTools.add returns a JSON-encoded string
        # (`{"operation": ..., "result": ...}`). Parse it so the
        # assertion is independent of how the toolkit serialises.
        result = json.loads(tools["add"].entrypoint(2, 3))
        assert result["operation"] == "addition"
        assert result["result"] == 5.0
        # And the divide path stays callable.
        result = json.loads(tools["divide"].entrypoint(10, 2))
        assert result["result"] == 5.0

    def test_dispatcher_routes_preset_through_tool_strategy(self):
        """`build_tools_for_node(...)` must dispatch a
        `type='tool'` + `preset='calculator'` node through
        `ToolStrategy.build_tools` and ultimately through
        `build_toolkit_for_preset`. We monkeypatch
        `ToolStrategy.build_tools` to verify the dispatch path."""
        from app.core.ir import IRNode
        from app.core.tool_factories import build_tools_for_node
        from app.core.strategies import tool as tool_strategy_mod

        captured = {}

        def fake_build_tools(self, nid, ir_node, ir_nodes, *, user_id=None):
            captured["nid"] = nid
            captured["user_id"] = user_id
            captured["preset"] = (ir_node.data or {}).get("config", {}).get("preset")
            return ["fn"]

        original = tool_strategy_mod.ToolStrategy.build_tools
        tool_strategy_mod.ToolStrategy.build_tools = fake_build_tools
        try:
            node = IRNode(
                id="calc", type="tool",
                data={"label": "calc", "config": {"preset": "calculator"}},
            )
            out = build_tools_for_node(node, {"calc": node}, user_id="alice")
            assert out == ["fn"]
            assert captured["nid"] == "calc"
            assert captured["user_id"] == "alice"
            assert captured["preset"] == "calculator"
        finally:
            tool_strategy_mod.ToolStrategy.build_tools = original

    def test_preset_tool_node_does_not_appear_as_workflow_step(self, seeded_default_preset):
        """Mirrors the http/mcp/tools rules: preset tool nodes are
        definitions the agent consumes via `tools=[...]`, not steps
        in the workflow topology. Preset collapse: the node
        is now `type='tool'` + `config.preset='calculator'`."""
        from app.core.compile import build_workflow

        nodes = [
            _n("a", "agent"),
            _n("calc", "tool", preset="calculator"),
        ]
        nodes[0]["data"]["config"]["toolsRef"] = ["calc"]
        wf = build_workflow("wf-calc-not-step", "wf-calc-not-step", nodes, [])
        step_ids = [s.step_id for s in (wf.steps or []) if getattr(s, "step_id", None)]
        assert "a" in step_ids
        assert "calc" not in step_ids

    def test_unknown_toolkit_class_returns_empty(self, monkeypatch):
        """If the registry points at a toolkit module that can't be
        imported, `build_toolkit_for_preset` returns `[]` and logs a
        warning. The agent runs without the preset rather than
        crashing the whole workflow."""
        from dataclasses import replace

        from app.core.ir import IRNode
        from app.core.strategies import tool as tool_strategy_mod

        # Mutate the registry entry's toolkit_class to point at a
        # non-existent module. The registry is module-level state,
        # so save the original and restore after the test.
        original = tool_strategy_mod.PRESET_REGISTRY["calculator"]
        tool_strategy_mod.PRESET_REGISTRY["calculator"] = replace(
            original,
            toolkit_class="agno.tools.totally_nonexistent.NoSuchToolkit",
        )
        try:
            node = IRNode(
                id="calc", type="tool",
                data={"label": "calc", "config": {"preset": "calculator"}},
            )
            out = tool_strategy_mod.ToolStrategy().build_tools(
                "calc", node, {"calc": node},
            )
            assert out == []
        finally:
            tool_strategy_mod.PRESET_REGISTRY["calculator"] = original

class TestPresetToolkitPerNodeConfig:
    """Per-node config on preset toolkits.

    A `tool` + `preset='<name>'` node carries two structured knobs
    read by `build_toolkit_for_preset(...)`:

      - `enabled_methods`: filter list of toolkit methods to expose
      - `toolkit_options`: kwargs to the toolkit constructor

    Both are declared on `ToolNodeConfig` and consumed by the
    unified `ToolStrategy` (via `PRESET_REGISTRY` resolution)."""

    def test_enabled_methods_filters_exposed_functions(self):
        """Selecting 2 of 4 calculator methods → exactly 2 functions
        returned, in the order they appear in `enabled_methods`."""
        from app.core.ir import IRNode
        from app.core.strategies.tool import PRESET_REGISTRY
        from app.core.tool_factories import build_toolkit_for_preset

        preset_spec = PRESET_REGISTRY["calculator"]
        node = IRNode(
            id="calc", type="tool",
            data={"label": "calc", "config": {
                "preset": "calculator",
                "enabled_methods": ["add", "multiply"],
            }},
        )
        tools = build_toolkit_for_preset("calc", preset_spec, node)
        names = sorted(fn.name for fn in tools)
        assert names == ["add", "multiply"], (
            f"expected only enabled methods, got {names}"
        )

    def test_enabled_methods_empty_exposes_all(self):
        """Empty (or missing) `enabled_methods` is the sentinel
        meaning "expose every PRESET_REGISTRY-declared method"."""
        from app.core.ir import IRNode
        from app.core.strategies.tool import PRESET_REGISTRY
        from app.core.tool_factories import build_toolkit_for_preset

        preset_spec = PRESET_REGISTRY["calculator"]
        node = IRNode(
            id="calc", type="tool",
            data={"label": "calc", "config": {
                "preset": "calculator",
                "enabled_methods": [],
            }},
        )
        tools = build_toolkit_for_preset("calc", preset_spec, node)
        names = sorted(fn.name for fn in tools)
        assert names == ["add", "divide", "multiply", "subtract"]

    def test_enabled_methods_ignores_unknown_names(self):
        """Unknown method names are dropped (logged as warning), not
        raised — the registry is the source of truth for the allowed
        set; client typos shouldn't crash the runtime."""
        from app.core.ir import IRNode
        from app.core.strategies.tool import PRESET_REGISTRY
        from app.core.tool_factories import build_toolkit_for_preset

        preset_spec = PRESET_REGISTRY["calculator"]
        node = IRNode(
            id="calc", type="tool",
            data={"label": "calc", "config": {
                "preset": "calculator",
                "enabled_methods": ["add", "totally_made_up"],
            }},
        )
        tools = build_toolkit_for_preset("calc", preset_spec, node)
        names = sorted(fn.name for fn in tools)
        assert names == ["add"]

    def test_toolkit_options_passed_to_constructor(self):
        """`toolkit_options` are forwarded as **kwargs to the toolkit
        constructor. Calculator accepts no kwargs (defaults), so we
        verify the call succeeds with an explicit empty dict and
        returns all 4 methods. (A typed assertion with a kwarg-
        accepting toolkit is monkeypatched below.)"""
        from app.core.ir import IRNode
        from app.core.strategies.tool import PRESET_REGISTRY
        from app.core.tool_factories import build_toolkit_for_preset

        preset_spec = PRESET_REGISTRY["calculator"]
        node = IRNode(
            id="calc", type="tool",
            data={"label": "calc", "config": {
                "preset": "calculator",
                "toolkit_options": {},
            }},
        )
        tools = build_toolkit_for_preset("calc", preset_spec, node)
        assert len(tools) == 4

    def test_toolkit_options_kwargs_flow_into_constructor(self, monkeypatch):
        """Monkeypatch the toolkit class to record the kwargs it
        receives. This is the round-trip test for the frontend form:
        `toolkitOptions.api_key = "..."` → `TavilyTools(api_key=...)`.

        Tavily isn't installed in the test env, so we install a
        `FakeTavilyTools` class into a fake module that
        `tool_factories.importlib.import_module` resolves to. The
        fake module replaces `agno.tools.tavily` and exposes the
        class with the right shape (`web_search_using_tavily` callable)."""
        import sys
        import types
        from dataclasses import replace

        from app.core.ir import IRNode
        from app.core.strategies import tool as tool_strategy_mod
        from app.core.tool_factories import build_toolkit_for_preset

        captured: dict = {}

        class FakeTavilyTools:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def web_search_using_tavily(self, *a, **kw):  # noqa: D401
                return "ok"

        fake_mod = types.ModuleType("agno.tools.tavily")
        fake_mod.TavilyTools = FakeTavilyTools  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agno.tools.tavily", fake_mod)

        # Make sure the toolkit_class path matches what we just stubbed.
        original = tool_strategy_mod.PRESET_REGISTRY["tavily_search"]
        tool_strategy_mod.PRESET_REGISTRY["tavily_search"] = replace(
            original,
            toolkit_class="agno.tools.tavily.TavilyTools",
        )
        try:
            preset_spec = tool_strategy_mod.PRESET_REGISTRY["tavily_search"]
            node = IRNode(
                id="tav", type="tool",
                data={"label": "tav", "config": {
                    "preset": "tavily_search",
                    "toolkit_options": {"api_key": "sk-test", "enable_search": True},
                }},
            )
            tools = build_toolkit_for_preset("tav", preset_spec, node)
        finally:
            tool_strategy_mod.PRESET_REGISTRY["tavily_search"] = original
        assert captured == {"api_key": "sk-test", "enable_search": True}, (
            f"toolkit options did not reach constructor; got {captured}"
        )
        assert len(tools) == 1
        assert tools[0].name == "web_search_using_tavily"

    def test_tool_node_config_declares_preset_fields(self):
        """`ToolNodeConfig` must declare `preset`, `enabled_methods`,
        and `toolkit_options` so a `tool` + `preset='<name>'` node
        has a schema that accepts those fields. Without `preset`,
        Pydantic drops it via `extra='ignore'` and the frontend
        form's writes vanish silently."""
        from app.schemas.node_configs import ToolNodeConfig

        cfg = ToolNodeConfig.model_validate({
            "preset": "calculator",
            "enabled_methods": ["add"],
            "toolkit_options": {"api_key": "x"},
        })
        assert cfg.preset == "calculator"
        assert cfg.enabled_methods == ["add"]
        assert cfg.toolkit_options == {"api_key": "x"}

    def test_tool_node_config_defaults_are_empty(self):
        """Default `preset` / `enabled_methods` / `toolkit_options`
        are empty — plain `tool` (source='function') nodes that don't
        set `preset` keep working unchanged. Preset collapse:
        added `preset` (default None) to `ToolNodeConfig`."""
        from app.schemas.node_configs import ToolNodeConfig

        cfg = ToolNodeConfig.model_validate({"source": "function", "functions": []})
        assert cfg.preset is None
        assert cfg.enabled_methods == []
        assert cfg.toolkit_options == {}

    def test_node_types_endpoint_no_longer_exposes_toolkit_methods(self):
        """Preset collapse: the `/api/v1/node-types` endpoint
        no longer surfaces per-preset `toolkitMethods` — the per-preset
        toolkit method list now lives in `PRESET_REGISTRY` and is
        consumed by `ToolStrategy` (no longer per-node manifest data).
        The legacy `toolkitMethods` field was removed from the
        response in step 5+6."""
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/api/v1/node-types")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        # No preset entries exist any more.
        assert "tavily_search" not in entries
        assert "duckduckgo" not in entries
        assert "calculator" not in entries
        assert "arxiv_search" not in entries
        assert "wikipedia" not in entries
        # The merged `tool` entry has no `toolkitMethods` field.
        assert "toolkitMethods" not in entries["tool"]
