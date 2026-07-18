"""F2  — manifest-driven schema documentation for the LLM.

Two pieces:

  1. `summarise_node_types()` — walks `app.core.node_types.NODE_TYPES`
     and produces a small JSON-serialisable description of every
     node type: name, display name, kind, default config, and the
     Pydantic field list for the config schema. The LLM can read
     this output to figure out which fields a type accepts — far
     more reliable than guessing from the tool's JSON schema.

  2. `get_node_types()` — the F2.1 tool wrapper. Returns the full
     summary as a string so agno can hand it back to the agent.
     LLM-friendly call: `get_node_types()` → JSON string.

Why this exists. Before F2 the LLM had to rely on the JSON schema
that `Function.from_callable` derives from the wrapper function's
type hints. That schema is:
  * missing the per-type config shape (the wrapper accepts `config:
    dict`, no per-type schema),
  * missing the default config (so the LLM doesn't know what
    `add_node(type='router')` produces without a `config` arg),
  * missing field aliases (Pydantic's `populate_by_name=True`
    aliases — the wrapper doesn't see them).

The fix is to give the LLM a way to ASK for the schema
documentation on demand. `get_node_types` is the read-side
counterpart to `plan_workflow`: the LLM looks up the type
metadata, then issues a plan with the right shape.

The summary is computed lazily once per process — `NODE_TYPES` is
frozen at startup, so per-call regeneration is wasted work.
"""
from __future__ import annotations

import functools
import json
from typing import Any, Optional

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from app.core.node_types import NODE_TYPES

# ─────────────────────────────────────────────────────────────────
# Per-type config schema summariser
# ─────────────────────────────────────────────────────────────────
def _summarise_field(name: str, info: FieldInfo) -> dict[str, Any]:
    """One Pydantic field → JSON-friendly dict.

    The LLM gets the alias (camelCase key the platform actually
    accepts on input) as the primary name. We also surface the
    python name so the LLM can disambiguate when a name has no
    alias. `required` is the only flag the LLM needs to know
    about — type and default are inferred from the JSON-schema
    output the LLM is already trained on.

    `description` is the Pydantic field's docstring summary
    (extracted from `Field(..., description="…")`). When absent
    we fall back to an empty string; the LLM can ask follow-up
    questions if it needs more context.
    """
    alias = info.alias
    required = info.is_required()
    description = info.description or ""
    out: dict[str, Any] = {
        "name": name,
        "alias": alias,
        "required": required,
        "description": description,
    }
    # Surface the JSON-schema type when it's available. Pydantic
    # may not have materialised `json_schema_extra` yet for some
    # unusual field types (custom validators) — in which case we
    # just omit the field. The LLM can read `default` from the
    # schema-derived shape anyway.
    #
    # We skip `PydanticUndefined` (Pydantic's `...`) — it's the
    # sentinel for "no default supplied" and isn't JSON-serialisable.
    # `is_required()` already captures the same signal.
    default = getattr(info, "default", None)
    if default is not None and not isinstance(default, type(...)):
        try:
            # `default` may still be a non-trivial object (e.g. a
            # list with a nested PydanticUndefined inside). Use a
            # JSON round-trip to surface any unhandled sentinel —
            # if the round-trip fails, just omit the default.
            json.dumps(default)
            out["default"] = default
        except (TypeError, ValueError):
            pass
    return out

def _summarise_config_schema(model: type[BaseModel]) -> list[dict[str, Any]]:
    """Walk the per-type Pydantic config model and emit one entry
    per field. Returns the list of dicts the LLM sees."""
    return [
        _summarise_field(name, info)
        for name, info in model.model_fields.items()
    ]

def _summarise_one_type(name: str, spec: Any) -> dict[str, Any]:
    """Build the JSON-friendly summary for one manifest entry.

    `default_config` is the resolved `NodeTypeSpec.default_config`
    (post preset-inheritance). The LLM uses it to know what an
    `add_node(type=X)` without a `config` arg produces — handy
    for "add a router, then I'll patch its branches" flows.

    `fields` is the per-type config schema summary. The LLM
    matches field names (alias first, python name as fallback)
    when constructing the `config` dict for `plan_workflow` /
    `add_node`.
    """
    return {
        "type": name,
        "display_name": spec.display_name,
        "kind": spec.kind,
        "category": spec.category,
        "description": spec.i18n_key,
        "default_config": spec.default_config,
        "fields": _summarise_config_schema(spec.config_schema),
        # Surface whether this type allows multiple outgoing edges
        # — useful for the LLM to know up-front whether its plan
        # needs a Router / Parallel in front of it.
        "extends": spec.extends,
    }

@functools.lru_cache(maxsize=1)
def summarise_node_types() -> list[dict[str, Any]]:
    """Public: list of per-type summaries, ordered by `palette_order`.

    Cached for process lifetime — the manifest is frozen at
    startup. The LLM can call this on every chat turn without
    worrying about cost.
    """
    return [
        _summarise_one_type(name, spec)
        for name, spec in sorted(
            NODE_TYPES.items(),
            key=lambda kv: (kv[1].palette_order, kv[0]),
        )
    ]

# ─────────────────────────────────────────────────────────────────
# Tool wrapper — the LLM's "tell me the schema" hook
# ─────────────────────────────────────────────────────────────────
def get_node_types_tool() -> str:
    """Read-only: return the per-type schema documentation as a
    JSON string.

    Use this BEFORE issuing a `plan_workflow` when:
      * the workflow has types the LLM hasn't used before,
      * the LLM isn't sure of a field name,
      * the LLM wants to know what defaults the platform
        applies when a field is omitted.

    Returns:
        A JSON object: `{"node_types": [...]}`. Each entry carries
        `type`, `display_name`, `kind`, `default_config`, `fields`.
        Field entries carry `name`, `alias` (camelCase — this is
        the key the platform accepts on input), `required`, and
        `description`.
    """
    import json
    return json.dumps(
        {"node_types": summarise_node_types()},
        ensure_ascii=False,
    )

def node_types_for_prompt() -> str:
    """Compact per-type summary for the system prompt.

    NOT a full JSON dump — just `type`, `display_name`, and a
    one-line "use this for X" hint. The full schema lives in
    `get_node_types()` which the LLM can call on demand. This
    avoids burning system-prompt tokens on fields the LLM rarely
    needs.
    """
    rows: list[str] = []
    for entry in summarise_node_types():
        hint = _USAGE_HINT.get(entry["type"], "")
        rows.append(
            f"  - {entry['type']} ({entry['display_name']})"
            + (f" — {hint}" if hint else "")
        )
    return "\n".join(rows)

# Short per-type usage hints embedded in the system prompt. Kept
# terse — the LLM can call `get_node_types` for the full schema.
_USAGE_HINT: dict[str, str] = {
    "agent": "single-step LLM agent; default config has empty instructions + a default model.",
    # +N2 : the prior `router` / `condition` /
    # `parallel` / `steps` rows collapsed to `branch` (mode=
    # switch|if-else) and `flow` (mode=parallel|sequential). The
    # HITL hint that used to live on `router` now lives on `branch`
    # — the `selector_mode='hitl'` primitive is the one the LLM
    # should reach for when the user picks the branch.
    "branch": "conditional fan-out or if/else split, picked by `mode` (default `switch`). Needs `branches` listing the named paths (switch) or a then-edge + optional else-edge (if-else). Use `selector_mode='hitl'` when the user should pick which branch to take — that's the correct primitive for HITL branch decisions (see HITL section in the system prompt).",
    "flow": "fan-out or sequential pipeline, picked by `mode` (default `parallel`). Needs `branches` listing concurrent (parallel) or ordered (sequential) paths.",
    "loop": "loop body; needs `bodyTarget` (id of the loop-body node) + iteration control.",
    "ask": "ask the user for input — pauses the pipeline to COLLECT a value (text / yes-no confirmation / single-choice pick), then injects the answer downstream. NOT a branch decider — if the answer needs to route the flow to different downstream nodes, use `branch` with `selector_mode='hitl'` instead. : renamed from `human_input`.",
    "tool": "tool source with `source` discriminator (mcp | http | function). MCP needs `serverId`; HTTP needs `url`; function mode needs `toolName`. Wire to an agent with kind='tool_attachment'. : also accepts a `preset` discriminator (wikipedia / tavily_search / duckduckgo / calculator / arxiv_search) — preset pre-fills defaults via PRESET_REGISTRY and overrides `source` (wikipedia forces http; toolkit presets route through build_toolkit_for_preset). Set `preset: '<name>'` (NOT a separate type) when the user asks for any of these.",
}

__all__ = [
    "summarise_node_types",
    "get_node_types_tool",
    "node_types_for_prompt",
]