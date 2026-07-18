"""Legacy node-type migration layer.

When a workflow envelope is loaded and its nodes carry legacy type
strings (`parallel`, `steps`), this module rewrites them to the
merged `flow` type in-place, preserving config semantics.

The migration runs:
  - on `WorkflowNode._validate_node_type` (fail-open for aliases,
    rewrite `self.type` in place before the registry check)
  - on `workflow_io.parse` for envelope imports (so even a
    `schemaVersion: "1.0"` legacy envelope gets upgraded)
  - on `_seed_templates` for old DB rows (via template_service)

This is the same pattern as `_migrate_legacy_condition` in
`services/node_configs.py` (legacy DSL string → evaluator dict) — a
small in-place dict mutation that runs on the read path so old
data keeps working without a DB migration.

The tuple shape extends to `(new_type, default_mode, default_preset)`
— preset aliases inject `config.preset = "<name>"` after rewriting
the type to `tool`. The prior `(new_type, default_mode)` shape is
preserved semantically for non-preset rows (default_preset=None).
"""
from __future__ import annotations

from typing import Any, Optional

# Maps legacy type → (new_type, default_mode_or_None, default_preset_or_None).
#
# `default_mode`    injects `config.mode = default_mode`    if not already set.
# `default_preset`  injects `config.preset = default_preset` if not already set.
#
# Node-type collapses (mode / source / kind discriminators):
#   - flow / branch: mode discriminator
#   - tool: source discriminator (rewritten to `config.source`
#     by ToolNodeConfig's `_migrate_legacy_mode_to_source` validator)
#   - ask: human_input (no mode/preset)
#   - preset tools: 5 presets → tool + preset discriminator
LEGACY_NODE_ALIASES: dict[str, tuple[str, Optional[str], Optional[str]]] = {
    # `parallel` + `steps` → `flow`
    "parallel": ("flow", "parallel",  None),
    "steps":    ("flow", "sequential", None),
    # `router` + `condition` → `branch`
    "router":    ("branch", "switch",   None),
    "condition": ("branch", "if-else",  None),
    # `http` + `mcp` + `tools` → `tool`
    "http":  ("tool", "http",    None),
    "mcp":   ("tool", "mcp",     None),
    "tools": ("tool", "function", None),
    # `human_input` → `ask` (kind=control_flow).
    # No `default_mode` / `default_preset` — `ask` has no mode/preset discriminator.
    "human_input": ("ask", None, None),
    # 5 presets → `tool` + `preset` discriminator.
    "wikipedia":    ("tool", None, "wikipedia"),
    "tavily_search": ("tool", None, "tavily_search"),
    "duckduckgo":   ("tool", None, "duckduckgo"),
    "calculator":   ("tool", None, "calculator"),
    "arxiv_search": ("tool", None, "arxiv_search"),
}

def is_legacy_type(node_type: str) -> bool:
    """True if `node_type` is a known legacy alias."""
    return node_type in LEGACY_NODE_ALIASES

def migrate_node_dict(node: dict[str, Any]) -> dict[str, Any]:
    """If `node['type']` is a legacy alias, rewrite it to the new type
    and inject the appropriate `mode` / `preset` sub-fields. Mutates
    in place.

    Returns the same dict object (caller convenience).

    Extended from 2-tuple `(new_type, default_mode)` to 3-tuple
    `(new_type, default_mode, default_preset)`. `default_preset` is
    used by the 5 preset aliases to inject `config.preset` so the
    runtime can dispatch to the correct `PRESET_REGISTRY` entry.
    """
    nt = node.get("type")
    if isinstance(nt, str):
        alias = LEGACY_NODE_ALIASES.get(nt)
        if alias is not None:
            new_type, default_mode, default_preset = alias
            node["type"] = new_type
            if default_mode is not None or default_preset is not None:
                data = node.get("data")
                if not isinstance(data, dict):
                    data = {}
                    node["data"] = data
                cfg = data.get("config")
                if not isinstance(cfg, dict):
                    cfg = {}
                    data["config"] = cfg
                if default_mode is not None:
                    cfg.setdefault("mode", default_mode)
                if default_preset is not None:
                    cfg.setdefault("preset", default_preset)
    return node

def migrate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Walk an envelope's `nodes` and apply `migrate_node_dict` to each.

    Mutates in place. Returns the envelope.
    """
    nodes = envelope.get("nodes")
    if isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict):
                migrate_node_dict(n)
    return envelope

__all__ = [
    "LEGACY_NODE_ALIASES",
    "is_legacy_type",
    "migrate_node_dict",
    "migrate_envelope",
]
