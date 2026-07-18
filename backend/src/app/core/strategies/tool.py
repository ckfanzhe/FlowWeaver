"""ToolStrategy — unified `tool` node (replaces HttpToolStrategy +
McpToolStrategy + UserFunctionsToolStrategy).

: the prior `http` + `mcp` + `tools` node types
collapse to a single `tool` type whose `config.source` discriminator
selects between three tool-emit primitives:

  - `'http'`     → `Function.from_callable(<http wrapper>)` (carried
                   over verbatim from HttpToolStrategy)
  - `'mcp'`      → `MCPTools(...)` (carried over verbatim from
                   McpToolStrategy)
  - `'function'` → `Function.from_callable(...)` per user-defined
                   function (carried over verbatim from
                   UserFunctionsToolStrategy)

The `tool` node is a tool-source type — it does NOT appear in
`wf.steps`. Instead it gets attached to one or more agents via the
agent's `tools=[...]` list.

The three legacy strategies (`HttpToolStrategy`, `McpToolStrategy`,
`UserFunctionsToolStrategy`) are deleted in step 4; the helpers they
called (`build_http_function`, `build_mcp_tools`,
`build_tools_user_functions`) live in `tool_factories.py` and are
reused here as-is.

: the prior 5 preset node types (`wikipedia` /
`tavily_search` / `duckduckgo` / `calculator` / `arxiv_search`)
collapse to `cfg.preset` discriminator on this same `tool` node.
`PRESET_REGISTRY` (below) holds the per-preset metadata; `preset`
is checked BEFORE `source`, with two outcomes:

  - HTTP preset (`wikipedia`): `cfg.source` is forced to `'http'`
    and `cfg` is shallow-merged with the preset's `default_config`
    defaults, then falls through to the existing HTTP-source path.
  - Toolkit preset (`tavily_search` / `duckduckgo` / `calculator` /
    `arxiv_search`): dispatch to `build_toolkit_for_preset(...)` in
    `tool_factories.py`, which instantiates the toolkit class and
    wraps each declared method with `Function.from_callable(...)`.

The 5 prior `extends: "tool"` preset manifest entries are deleted
(see `shared/nodes.manifest.json` migration); legacy
envelopes are rewritten by `_compat.migrate_envelope` on the read
path.

`_normalize_cfg` runs the raw config through `ToolNodeConfig.model_validate`
so the F7 `mode='before'` `_migrate_legacy_*` validators fire (legacy
`http|mcp|tools` envelopes → `tool` + `source`). Same pattern as
`BranchStrategy._normalize_cfg`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Mapping, Optional

from .base import NodeStrategy

log = logging.getLogger(__name__)

def _normalize_cfg(cfg: dict) -> dict:
    """Run the raw config through `ToolNodeConfig.model_validate` so any
    `_migrate_legacy_*` validators fire and Pydantic fills in defaults.
    Returns the dumped dict so downstream field reads (`cfg["source"]`,
    `cfg["baseUrl"]`, …) work as expected.

    : both `build()` and `to_source()` route
    through this so an envelope written against the prior `http|mcp|tools`
    types (without an explicit `source`) lands on the correct
    function-source primitive once `_compat.LEGACY_NODE_ALIASES` flips
    the type to `tool` and the schema's default `source='function'`
    kicks in. Future per-source validators hook in here.

    : `preset` field added — see PRESET_REGISTRY
    below for the 5 collapsed presets.
    """
    from app.schemas.node_configs import ToolNodeConfig
    return ToolNodeConfig.model_validate(cfg).model_dump(by_alias=True)

# ─────────────────────────────────────────────────────────────────
# — Preset registry (replaces the deleted 5 preset manifest
# entries). Lives here (not in the manifest) because the unified `tool`
# node owns preset dispatch; per-preset `toolkit_class` / `default_config`
# is runtime + tooling concern, not a node-type identity. Legacy
# envelopes still reference the 5 preset names — `_compat.migrate_envelope`
# rewrites them to `tool` + `preset: "<name>"` on read, so this dict is
# the single dispatch table.
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PresetSpec:
    """Per-preset runtime metadata.

    Two shapes:
      - HTTP-source preset (wikipedia): `toolkit_class=None`,
        `default_source='http'` + `default_config` carries the HTTP
        defaults (baseUrl, path, toolName). After applying defaults
        falls through to the existing `build_http_function(...)` path.
      - Toolkit-class preset (tavily_search / duckduckgo / calculator
        / arxiv_search): `toolkit_class=<agno.tools.X.Y>` +
        `toolkit_methods=[...]` + `default_source=None` (preset doesn't
        touch `source` — toolkit preset is its own emit primitive).
    """
    display_name: str
    toolkit_class: Optional[str] = None
    toolkit_methods: tuple[str, ...] = ()
    default_source: Optional[str] = None  # None = don't override `source`
    default_config: Mapping[str, Any] = field(default_factory=dict)

PRESET_REGISTRY: dict[str, PresetSpec] = {
    # Wikipedia — HTTP-source preset with explicit HTTP defaults.
    "wikipedia": PresetSpec(
        display_name="Wikipedia",
        default_source="http",
        default_config={
            "toolName": "wikipedia_search",
            "toolDescription": "Search Wikipedia for articles matching a query",
            "method": "GET",
            "baseUrl": "https://en.wikipedia.org",
            "path": "/w/api.php?action=query&list=search&srsearch={query}&format=json&utf8=1&srlimit=5",
            "headers": {},
            "queryParams": {},
            "authToken": "",
            "bodySchema": "",
        },
    ),
    # 4 toolkit-class presets (phase / P2).
    "tavily_search": PresetSpec(
        display_name="Tavily Search",
        toolkit_class="agno.tools.tavily.TavilyTools",
        toolkit_methods=("web_search_using_tavily",),
    ),
    "duckduckgo": PresetSpec(
        display_name="DuckDuckGo",
        toolkit_class="agno.tools.duckduckgo.DuckDuckGoTools",
        toolkit_methods=("web_search",),
    ),
    "calculator": PresetSpec(
        display_name="Calculator",
        toolkit_class="agno.tools.calculator.CalculatorTools",
        toolkit_methods=("add", "subtract", "multiply", "divide"),
    ),
    "arxiv_search": PresetSpec(
        display_name="arXiv Search",
        toolkit_class="agno.tools.arxiv.ArxivTools",
        toolkit_methods=("search_arxiv_and_return_articles",),
    ),
}

def _apply_preset_defaults(cfg: dict, preset_name: str) -> Optional[PresetSpec]:
    """Apply `PRESET_REGISTRY[preset_name]` defaults to `cfg`.

    Returns the matched `PresetSpec` so the caller can branch on
    `toolkit_class` (toolkit preset) vs `default_source` (HTTP preset).
    Returns `None` if `preset_name` is unknown — caller logs a warning
    and falls through to source-based dispatch (so a stale preset name
    in a legacy envelope doesn't hard-crash the build).

    Mutates `cfg` in place (shallow merge; user values win) and also
    returns it for chaining. Sets `cfg["source"]` to the preset's
    `default_source` if the preset declares one (wikipedia's HTTP path).
    """
    spec = PRESET_REGISTRY.get(preset_name)
    if spec is None:
        return None
    for k, v in spec.default_config.items():
        cfg.setdefault(k, v)
    if spec.default_source:
        cfg["source"] = spec.default_source
    return spec

class ToolStrategy(NodeStrategy):
    """Unified `tool` node — `source` discriminator picks the
    tool-emit primitive (`'http'` | `'mcp'` | `'function'`)."""

    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "tool_source"
    COMPOUND_PASS: ClassVar[Optional[int]] = None
    IS_TOOL_SOURCE: ClassVar[bool] = True
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "none"

    # ─────────────────────────────────────────────────────────────
    # build() — runtime construction (attachable tools for an agent)
    # ─────────────────────────────────────────────────────────────
    def build(self, nid: str, node: dict, ctx: Any) -> list:
        """Build the agno tool list for one `tool` node.

        Returns a one-element list (or empty) depending on `source` /
        `preset` and the config's validity. Dispatches to the matching
        factory in `tool_factories` (carried over from the deleted
        HttpToolStrategy / McpToolStrategy / UserFunctionsToolStrategy
        — they share the same factories).

        : if `cfg.preset` is set and known,
        `cfg` is first rewritten with preset defaults (HTTP preset) or
        the call is routed to `build_toolkit_for_preset(...)` (toolkit
        preset). Unknown preset names log a warning and fall through.
        """
        from app.core.tool_factories import build_tools_for_node
        from app.core.ir import IRNode

        cfg = _normalize_cfg((node.get("data") or {}).get("config") or {})
        ir_node = IRNode(
            id=nid,
            type="tool",
            data={**(node.get("data") or {}), "config": cfg},
        )
        user_id = getattr(ctx, "user_id", None)
        return build_tools_for_node(ir_node, ctx.ir.node_map, user_id=user_id)

    # ─────────────────────────────────────────────────────────────
    # to_source() — code-gen (Python source emitted before wf.steps)
    # ─────────────────────────────────────────────────────────────
    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit the runtime primitive as Python source for `tool` nodes.

        Dispatches on `cfg["source"]`:
          - `'http'`: emit `<wrapper>` function source + `import requests`
          - `'mcp'`:  emit `<nid>_mcp = MCPTools(...)`
          - `'function'`: emit the user-defined function defs

        : toolkit presets (tavily / duckduckgo /
        calculator / arxiv_search) emit an empty string here — the
        toolkit-class instantiation happens at runtime via
        `build_toolkit_for_preset` and is attached to the agent
        through the standard tool-wiring pass; no Python source
        emission is needed. The wikipedia preset (HTTP) falls through
        to `_to_source_http` after preset defaults are applied.

        The wiring (attaching tools to an agent) is handled by the
        pipeline's later pass — this method only emits the raw
        definitions so they exist in the module's namespace before the
        agent that consumes them.
        """
        cfg = _normalize_cfg((node.get("data") or {}).get("config") or {})
        preset = cfg.get("preset")
        if preset:
            spec = _apply_preset_defaults(cfg, preset)
            if spec and spec.toolkit_class:
                # Toolkit preset — no source emission (same as the
                # deleted PresetToolkitStrategy.to_source returning "").
                return ""
        source = cfg.get("source") or "function"
        if source == "http":
            return self._to_source_http(nid, ctx)
        if source == "mcp":
            return self._to_source_mcp(nid, node, ctx, cfg)
        return self._to_source_function(ctx)

    # ─────────────────────────────────────────────────────────────
    # build_tools() — factory dispatch (phase pattern)
    # ─────────────────────────────────────────────────────────────
    def build_tools(
        self,
        nid: str,
        ir_node: Any,
        ir_nodes: dict,
        *,
        user_id: Optional[str] = None,
    ) -> list:
        """Build attachable agno tools. Dispatched by
        `build_tools_for_node` via the strategy's `IS_TOOL_SOURCE=True`.

         — preset dispatch chain:
          1. `cfg.preset` set + toolkit preset → `build_toolkit_for_preset(...)`
          2. `cfg.preset` set + HTTP preset (wikipedia) → fall through
             with `source='http'` + default_config merged
          3. `cfg.preset` set + unknown → log + fall through to source path
          4. `cfg.preset` unset → existing source-based dispatch:
             `'http'` → `build_http_function(ir_node)`;
             `'mcp'`  → `build_mcp_tools(ir_node, user_id=user_id)`;
             `'function'` → `build_tools_user_functions(ir_node)`.
        """
        from app.core.tool_factories import (
            build_http_function,
            build_mcp_tools,
            build_toolkit_for_preset,
            build_tools_user_functions,
        )

        cfg = _normalize_cfg(ir_node.data.get("config") or {})
        preset = cfg.get("preset")
        if preset:
            spec = _apply_preset_defaults(cfg, preset)
            if spec is None:
                log.warning(
                    "tool node %s: unknown preset %r; falling through to "
                    "source-based dispatch (source=%r)",
                    nid, preset, cfg.get("source"),
                )
            elif spec.toolkit_class:
                # Re-stamp cfg on ir_node so build_toolkit_for_preset sees
                # any default_config values we just merged in.
                ir_node.data["config"] = cfg
                return list(
                    build_toolkit_for_preset(nid, spec, ir_node) or []
                )
            # else: HTTP preset (or unknown — fall through to source path)
            ir_node.data["config"] = cfg

        source = cfg.get("source") or "function"
        if source == "http":
            fn = build_http_function(ir_node)
            return [fn] if fn is not None else []
        if source == "mcp":
            mcp = build_mcp_tools(ir_node, user_id=user_id)
            return [mcp] if mcp is not None else []
        return list(build_tools_user_functions(ir_node) or [])

    # ─────────────────────────────────────────────────────────────
    # Per-source to_source() helpers (carried over from the deleted
    # strategies; behaviour preserved verbatim so the emitted Python
    # bytes stay byte-stable for existing workflows).
    # ─────────────────────────────────────────────────────────────
    def _to_source_http(self, nid: str, ctx: Any) -> str:
        """Emit the runtime HTTP wrapper function definition.

        Carried over from `HttpToolStrategy.to_source`. The pipeline's
        pass 0 has already rendered the wrapper function symbols; here
        we emit the function source so the export can be
        `python workflow.py`-runnable. We also emit `import requests`
        so the generated module is self-contained — without it
        `requests.get(...)` inside the wrapper raises NameError.
        """
        from app.core.compile._helpers.http_wrappers import (
            http_wrapper_block,
            http_wrappers_metadata,
        )

        meta = http_wrappers_metadata(ctx.nodes_by_id)
        by_id = {m["node_id"]: m for m in meta}
        w = by_id.get(nid)
        if not w:
            return ""
        return "import requests\n\n" + http_wrapper_block(w)

    def _to_source_mcp(self, nid: str, node: dict, ctx: Any, cfg: dict) -> str:
        """Emit `<nid>_mcp = MCPTools(...)`.

        Carried over from `McpToolStrategy.to_source`. :
        per-user binding — the export's MCP lookup is scoped to the
        workflow owner so the rendered source can only reach servers
        the owner (or the platform) controls.
        """
        from app.core.compile._helpers.mcp_lookup import mcp_target_for_export
        from app.core.compile._helpers.utils import q

        server_id = cfg.get("serverId") or ""
        prefix = cfg.get("toolNamePrefix") or ""
        command, args, url = mcp_target_for_export(
            server_id,
            user_id=getattr(ctx, "user_id", None),
        )
        if url:
            return (
                f"{nid}_mcp = MCPTools(\n"
                f"    url={q(url)},\n"
                f"    transport={q('sse')},\n"
                f"    tool_name_prefix={q(prefix)},\n"
                f")\n"
            )
        return (
            f"{nid}_mcp = MCPTools(\n"
            f"    command={q(command or 'echo')},\n"
            f"    args={q(args or [])},\n"
            f"    tool_name_prefix={q(prefix)},\n"
            f")\n"
        )

    def _to_source_function(self, ctx: Any) -> str:
        """Emit the user-defined function definitions.

        Carried over from `UserFunctionsToolStrategy.to_source`. The
        wiring (`tools=[Function.from_callable(...)]`) is handled by
        the pipeline's pass 3. This method only emits the raw function
        bodies so they exist in the module's namespace before the
        agent that's supposed to call them.
        """
        from app.core.compile._helpers.tools_expr import iter_tool_function_blocks

        # Use ctx.nodes_by_id (dict-view) — the legacy `ctx.ir.node_map`
        # carries IRNode instances that aren't subscriptable.
        blocks = []
        for tool in iter_tool_function_blocks(ctx.nodes_by_id):
            if tool["name"]:
                blocks.append(f"{tool['code']}\n")
        return "".join(blocks)

__all__ = ["ToolStrategy", "PRESET_REGISTRY", "PresetSpec"]