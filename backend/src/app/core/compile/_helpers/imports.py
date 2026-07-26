"""Import-block builder for the generated Python file.

Only emit what the workflow actually uses:

  - Provider classes (`OpenAIChat` / `Claude` / `Ollama` / `Gemini`) — one
    per provider present in an Agent's model config.
  - `Function` — needed when any tool-source node (`tool` with any
    `source` discriminator / `ask`) is in the graph.
  - `MCPTools` — only when a `tool` node with `config.source == 'mcp'`
    exists.
  - `Knowledge` + `LanceDb` / `PgVector` / `ChromaDb` + matching
    embedders — only when a `knowledge` node is in the graph (see
    [[gleaming-munching-grove]]).
  - `Parallel` / `Router` / `Condition` / `Loop` — only when those
    compound types appear.

`http` + `mcp` + `tools` collapse to `tool` with a
`source: mcp | http | function` discriminator. The 3 prior
type-string filters become one `tool` filter; `MCPTools` is gated on
`config.source == 'mcp'` (only emitted when an MCP-mode tool is wired).
"""
from __future__ import annotations

def collect_imports(nodes_by_id: dict[str, dict], has_http: bool) -> list[str]:
    """Return the list of `from agno... import ...` lines to emit."""
    providers = sorted({
        (n["data"]["config"]["model"]["provider"] or "openai").lower()
        for n in nodes_by_id.values()
        if n["type"] == "agent"
    })
    imports: list[str] = []
    if not providers or "openai" in providers:
        imports.append("from agno.models.openai import OpenAIChat")
    if "anthropic" in providers:
        imports.append("from agno.models.anthropic import Claude")
    if "ollama" in providers:
        imports.append("from agno.models.ollama import Ollama")
    if "google" in providers:
        imports.append("from agno.models.google import Gemini")

    # Single `tool` filter (replaces the prior `has_tools` + `has_mcp` +
    # `has_http_nodes`). MCPTools import is gated on
    # `config.source == 'mcp'` so we don't import it for function-mode
    # or http-mode tool nodes.
    tool_nodes = [n for n in nodes_by_id.values() if n["type"] == "tool"]
    has_tool = bool(tool_nodes)
    has_mcp = any(
        ((n.get("data") or {}).get("config") or {}).get("source") == "mcp"
        for n in tool_nodes
    )
    has_ask = any(n["type"] == "ask" for n in nodes_by_id.values())
    # `parallel` + `steps` collapsed to `flow`; the runtime primitive
    # (`Parallel` vs `Steps`) is chosen by `config.mode`, so we look
    # at each `flow` node's config to decide which agno class to
    # import.
    flow_modes = {
        (n.get("data") or {}).get("config", {}).get("mode") or "parallel"
        for n in nodes_by_id.values()
        if n["type"] == "flow"
    }
    has_parallel = "parallel" in flow_modes
    has_steps = "sequential" in flow_modes
    has_loop = any(n["type"] == "loop" for n in nodes_by_id.values())
    # `router` + `condition` collapsed to `branch` with a `mode`
    # discriminator. The agno class to import depends on the mode —
    # `Router` for `switch`, `Condition` for `if-else`.
    branch_modes = {
        (n.get("data") or {}).get("config", {}).get("mode") or "switch"
        for n in nodes_by_id.values()
        if n["type"] == "branch"
    }
    has_router = "switch" in branch_modes
    has_condition = "if-else" in branch_modes

    # `Function` is needed when any of: tool (any source), ask exists
    if has_tool or has_ask:
        imports.append("from agno.tools.function import Function")
    if has_mcp:
        imports.append("# To run MCP-backed nodes you must: pip install mcp")
        imports.append("from agno.tools.mcp import MCPTools")
    if has_parallel:
        imports.append("from agno.workflow.parallel import Parallel")
    if has_steps:
        imports.append("from agno.workflow.steps import Steps")
    if has_router:
        imports.append("from agno.workflow.router import Router")
    if has_condition:
        imports.append("from agno.workflow.condition import Condition")
    if has_loop:
        imports.append("from agno.workflow.loop import Loop")
    # RAG / knowledge — gate on `vectorDb` / `embedder` discriminators
    # in each `knowledge` node's config so we don't pull in deps the
    # user didn't pick. `knowledge_expr.required_imports` knows the
    # rules, so we delegate rather than re-implement them here.
    from .knowledge_expr import required_imports
    imports.extend(required_imports(nodes_by_id))
    return imports