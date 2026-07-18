"""Context-assembly kernel for the chat builder.

The chat builder's LLM needs a JSON-ish description of the current
workflow so it can plan edits. The naive approach — dump the full
`WorkflowIR` — ensures correctness but blows up the prompt once
the workflow grows past ~30 nodes. The Anthropic Sonnet context
window is 200K tokens, but cost / latency are sensitive to prompt
size, and we hit a 32K-token hard cap on the LiteLLM gateway in
production, where the LiteLLM gateway enforces a hard cap and
tripping it turns the chat builder into an infinite retry loop.

This module is the single source of truth for context size. Any
caller that needs to "show the LLM the workflow" should go through
`render_workflow_context` here, not reimplement the truncation.

Centralising the size rules means the cap can be tuned in one
place — the chat builder's system prompt example, the
`run_workflow` tool's payload echo, and any future debug /
explanation tool all share the same limit.
"""
from __future__ import annotations

import json
from typing import Any

# Tuned : empirically a 20-node workflow with ~500 char
# configs fits inside ~24K tokens of LLM context, leaving room for
# the system prompt + user message + tool definitions. Above that,
# the LLM provider gateway starts rejecting with "context length
# 32768" and the chat builder turns into an infinite retry loop.
MAX_NODES_IN_CONTEXT = 20
MAX_EDGES_IN_CONTEXT = 40
MAX_CONFIG_CHARS = 500
MAX_TOTAL_CONTEXT_CHARS = 28_000

def _truncate_config(cfg: Any, max_chars: int = MAX_CONFIG_CHARS) -> str:
    """Render a node config as compact JSON, truncating raw text past
    `max_chars` with a `... [+N chars]` suffix so the LLM can still
    see the shape without blowing the context window on a long
    `instructions` field or `query_params` blob."""
    if cfg is None:
        return "{}"
    text = json.dumps(cfg, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [+{len(text) - max_chars} chars]"

def _render_nodes(nodes: list[dict]) -> str:
    """Render node list with the id/type/label/config-one-liner shape.

    If node count exceeds `MAX_NODES_IN_CONTEXT`, show the first
    `(N-5)` plus a `[... N more omitted ...]` line plus the last 5.
    That keeps the LLM seeing both the workflow entry-point and the
    sink nodes, which is the minimum useful slice for planning edits."""
    if not nodes:
        return "(workflow has no nodes)"
    if len(nodes) <= MAX_NODES_IN_CONTEXT:
        lines = []
        for n in nodes:
            cfg_str = _truncate_config(n.get("config"))
            lines.append(
                f"- id={n.get('id')!r} type={n.get('type')!r} "
                f"label={n.get('label', '')!r} config={cfg_str}"
            )
        return "\n".join(lines)
    head = nodes[: MAX_NODES_IN_CONTEXT - 5]
    tail = nodes[-5:]
    omitted = len(nodes) - MAX_NODES_IN_CONTEXT
    lines = []
    for n in head:
        cfg_str = _truncate_config(n.get("config"))
        lines.append(
            f"- id={n.get('id')!r} type={n.get('type')!r} "
            f"label={n.get('label', '')!r} config={cfg_str}"
        )
    lines.append(f"... [{omitted} more nodes omitted — too many to show] ...")
    for n in tail:
        cfg_str = _truncate_config(n.get("config"))
        lines.append(
            f"- id={n.get('id')!r} type={n.get('type')!r} "
            f"label={n.get('label', '')!r} config={cfg_str}"
        )
    return "\n".join(lines)

def _render_edges(edges: list[dict]) -> str:
    if not edges:
        return "(workflow has no edges)"
    if len(edges) <= MAX_EDGES_IN_CONTEXT:
        lines = []
        for e in edges:
            sh = e.get("sourceHandle") or ""
            sh_str = f" sourceHandle={sh!r}" if sh else ""
            lines.append(
                f"- {e.get('source')!r} -> {e.get('target')!r} "
                f"kind={e.get('kind', 'dataflow')!r}{sh_str}"
            )
        return "\n".join(lines)
    head = edges[: MAX_EDGES_IN_CONTEXT - 5]
    tail = edges[-5:]
    omitted = len(edges) - MAX_EDGES_IN_CONTEXT
    lines = [
        f"- {e.get('source')!r} -> {e.get('target')!r} "
        f"kind={e.get('kind', 'dataflow')!r}"
        for e in head
    ]
    lines.append(f"... [{omitted} more edges omitted] ...")
    lines.extend(
        f"- {e.get('source')!r} -> {e.get('target')!r} "
        f"kind={e.get('kind', 'dataflow')!r}"
        for e in tail
    )
    return "\n".join(lines)

def render_workflow_context(
    nodes: list[dict],
    edges: list[dict],
    *,
    max_total_chars: int = MAX_TOTAL_CONTEXT_CHARS,
) -> str:
    """Render the workflow as a compact text block for the LLM context.

    Returns a string starting with `Current nodes:` / `Current edges:`
    headers (matching the previous inline format so the system prompt
    examples still parse). If the rendered output exceeds
    `max_total_chars`, the workflow section is replaced with a
    summary count so the LLM gets a useful "this is too large to
    show — use `plan_workflow` to replace it whole" hint instead of
    a truncated JSON blob that misleads the model.
    """
    body = (
        f"Current nodes ({len(nodes)} total):\n{_render_nodes(nodes)}\n"
        f"Current edges ({len(edges)} total):\n{_render_edges(edges)}"
    )
    if len(body) <= max_total_chars:
        return body
    return (
        f"(workflow has {len(nodes)} nodes and {len(edges)} edges — "
        f"too large to show in context. Use `plan_workflow(plan={{nodes: [...], "
        f"edges: [...]}})` to replace it whole, or `get_workflow` to read "
        f"specific nodes by id before editing.)"
    )
