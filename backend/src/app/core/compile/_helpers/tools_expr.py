"""Tool-source node emission.

A `tool` node holds user-written function defs (when
`config.source == 'function'`), a single `MCPTools(...)` reference
(`source == 'mcp'`), or a wrapper function rendered separately in
`http_wrappers.py` (`source == 'http'`). This module stitches them
together: given an Agent's `toolsRef` list, it returns a single Python
list-expression that an `Agent(tools=...)` parameter can use directly.

: the prior `tools` + `mcp` + `http` node types
collapse to `tool` with a `source` discriminator. The 3 prior
type-string branches in `tools_expr` become one `tool` branch
dispatched on `config.source` (matching the new `ToolStrategy` shape).
"""
from __future__ import annotations

from typing import Iterable

from .http_wrappers import http_wrappers_metadata
from .mcp_lookup import mcp_target_for_export
from .utils import q, safe_ident

# ─────────────────────────────────────────────────────────────────
# User-written tools (the `tool` node with `source='function'`)
# ─────────────────────────────────────────────────────────────────
def iter_tool_function_blocks(nodes_by_id: dict[str, dict]) -> Iterable[dict]:
    """Yield raw Python code blocks from every `tool` function-mode
    node's `functions[]`.

    These are the *function definitions* — emitted ABOVE all other nodes
    so Agents can reference them. The wiring
    (`tools=[Function.from_callable(...)]`) is a separate concern
    handled by `tools_expr` below.

    : filter narrowed from `n["type"] == "tools"`
    to `n["type"] == "tool" and cfg.source == "function"` so MCP /
    HTTP-mode tool nodes don't emit user-function blocks (their tools
    are produced elsewhere — `MCPTools(...)` in `_to_source_mcp`,
    `<wrapper>` in `_to_source_http`).
    """
    for node in nodes_by_id.values():
        if node["type"] != "tool":
            continue
        cfg = (node.get("data") or {}).get("config") or {}
        if cfg.get("source", "function") != "function":
            continue
        for fn in cfg.get("functions", []):
            yield {
                "code": (fn.get("code") or "").rstrip() + "\n",
                "name": fn.get("name") or "tool",
            }

# ─────────────────────────────────────────────────────────────────
# Per-tool-ref expression
# ─────────────────────────────────────────────────────────────────
def tools_expr(
    tref: str,
    nodes_by_id: dict[str, dict],
    http_wrappers_by_id: dict[str, dict],
) -> str:
    """Render the tool expression for one tool-node ref.

    Returns a Python expression suitable for inclusion inside an
    `Agent(tools=[...])` parameter. For a `tool` node, dispatch on
    `cfg.source` (set by the strategy at build time):

      - `source='function'` → `[Function.from_callable(...), ...]`
      - `source='mcp'`      → `<tref>_mcp` (the MCPTools instance)
      - `source='http'`     → `Function.from_callable(<wrapper_func>, ...)`

    Returns the literal string `"None"` if the ref doesn't resolve —
    callers splice non-None expressions together.
    """
    node = nodes_by_id.get(tref)
    if not node:
        return "None"
    ntype = node["type"]
    if ntype == "tool":
        cfg = (node.get("data") or {}).get("config") or {}
        source = cfg.get("source", "function")
        if source == "mcp":
            return f"{tref}_mcp"
        if source == "http":
            wrapper = http_wrappers_by_id.get(tref)
            if wrapper:
                return (
                    f"Function.from_callable({wrapper['func_name']}, "
                    f"name={q(wrapper['func_name'])})"
                )
            return "None"
        # source == "function" (default)
        funcs = cfg.get("functions") or []
        if not funcs:
            return "[]"
        parts = [
            f"Function.from_callable({safe_ident(fn.get('name') or 'tool')}, "
            f"name={q(fn.get('name') or 'tool')})"
            for fn in funcs
        ]
        return "[" + ", ".join(parts) + "]"
    return "None"

def tools_list(
    tools_ref: list[str],
    nodes_by_id: dict[str, dict],
    http_wrappers_by_id: dict[str, dict],
) -> str:
    """Render the full `tools=[...]` list expression for one Agent.

    Splices sub-lists (so we don't end up with `[[a], [b]]`).
    """
    parts: list[str] = []
    for tref in tools_ref:
        expr = tools_expr(tref, nodes_by_id, http_wrappers_by_id)
        if expr.startswith("[") and expr.endswith("]"):
            inner = expr[1:-1].strip()
            if inner:
                parts.append(inner)
        elif expr != "None":
            parts.append(expr)
    return "[" + ", ".join(parts) + "]" if parts else "[]"

# ─────────────────────────────────────────────────────────────────
# Hooks (phase.1 / P1.1 Agent )
# ─────────────────────────────────────────────────────────────────
def hooks_expr(
    hooks_ref: list[str],
    nodes_by_id: dict[str, dict],
) -> str:
    """Render an `Agent(pre_hooks=[...], post_hooks=[...])` list expression.

    Different from `tools_expr` in two ways:

      1. **Plain callables only** — agno's `pre_hooks` / `post_hooks`
         want `Callable[..., Any]`, not `Function` wrappers. So we emit
         the inner function name (e.g. `my_tool`), not
         `Function.from_callable(my_tool, ...)`.
      2. **`tool` with `source='function'` only** — MCP servers /
         HTTP wrappers aren't simple callables; the Plan ()
         restricts hooks to function-mode tool refs. Non-matching
         refs are skipped (silently — the schema allows the value
         but the semantics don't).

    : filter narrowed from `n["type"] == "tools"`
    to `n["type"] == "tool" and cfg.source == "function"`.

    Returns `"[]"` when no refs resolve (so we emit clean output even
    for an empty / dangling list).
    """
    parts: list[str] = []
    for tref in hooks_ref:
        node = nodes_by_id.get(tref)
        if not node:
            continue
        if node["type"] != "tool":
            continue
        cfg = (node.get("data") or {}).get("config") or {}
        if cfg.get("source", "function") != "function":
            continue
        for fn in cfg.get("functions") or []:
            name = safe_ident(fn.get("name") or "tool")
            if name and name != "tool":
                parts.append(name)
    return "[" + ", ".join(parts) + "]" if parts else "[]"

# Re-export so `http_wrappers` and `mcp_target_for_export` can be called from
# the package root without callers reaching into sub-modules.
__all__ = [
    "iter_tool_function_blocks",
    "tools_expr",
    "tools_list",
    "hooks_expr",
    "http_wrappers_metadata",
    "mcp_target_for_export",
]