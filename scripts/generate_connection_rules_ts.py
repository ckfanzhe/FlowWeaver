#!/usr/bin/env python3
"""Generate `frontend/src/lib/connectionRules.generated.ts` from the
shared connection-rules JSON.

The rules table used to be hand-loaded by
`connectionValidation.ts` (via Vite's JSON import), then
re-expanded by both the Python loader and the TS loader. Drift
was possible because two language runtimes both had to read the
same file and agree on `@group` resolution. The fix: codegen at
build time. A single Python pass reads the JSON, expands `@group`
references, and emits a frozen TS module. The TS module imports
nothing from the JSON at runtime.

Output shape (sorted alphabetically, deterministic — `check_node_types_consistency.py`
and `check_connection_rules_consistency.py` both compare against this
string):

    // ─── DO NOT EDIT — regenerate with scripts/generate_connection_rules_ts.py
    import type { NodeType } from '../types/workflow'
    export const GROUPS = { ... } as const
    export const EXECUTABLE_TYPES: ReadonlySet<NodeType> = new Set([...])
    export const TOOL_SOURCE_TYPES: ReadonlySet<NodeType> = new Set([...])
    export interface ConnectionRule { ... }
    export const CONNECTION_RULES: Record<NodeType, ConnectionRule> = { ... }
    export const TOOL_ATTACHMENT_RULES: Record<NodeType, ConnectionRule> = { ... }

Run:
    python scripts/generate_connection_rules_ts.py

Companion CI check:
    python scripts/check_connection_rules_consistency.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "shared" / "connection_rules.json"
OUT_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "connectionRules.generated.ts"


def _load_rules() -> dict:
    with RULES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _is_doc_key(name: str) -> bool:
    """JSON keys prefixed with `_` are inline docs (e.g. `_doc_`), not
    rule entries. Skipped silently — matches the Python loader."""
    return name.startswith("_")


def _resolve(value, groups: dict[str, list[str]]) -> list[str]:
    """Expand a rule value (string / list) to a sorted list. Mirrors
    `connection_rules._resolve_value` and
    `check_connection_rules_consistency._resolve` so all three agree.
    """
    if value is None:
        return []
    if isinstance(value, str):
        if value.startswith("@"):
            name = value[1:]
            if name not in groups:
                raise ValueError(f"unknown group reference {value!r}")
            return sorted(groups[name])
        return [value]
    if isinstance(value, list):
        out: set[str] = set()
        for item in value:
            if isinstance(item, str) and item.startswith("@"):
                name = item[1:]
                if name not in groups:
                    raise ValueError(f"unknown group reference {item!r}")
                out.update(groups[name])
            else:
                out.add(str(item))
        return sorted(out)
    raise ValueError(f"unsupported set value: {value!r}")


def _resolve_group_spec(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    """Resolve each group definition. Returns `{name: sorted_members}`.
    Groups themselves are plain string lists in the JSON, so this is a
    pass-through + sort for determinism."""
    return {name: sorted(members) for name, members in groups.items()}


def _render_rule(spec: dict, groups: dict[str, list[str]]) -> str:
    """Render one rule row as a TS object literal."""
    src = _resolve(spec.get("allowed_source_types"), groups)
    tgt = _resolve(spec.get("allowed_target_types"), groups)
    parts = [
        f"    allowed_source_types: new Set<NodeType>({json.dumps(src)})",
        f"    allowed_target_types: new Set<NodeType>({json.dumps(tgt)})",
        f"    max_outgoing: {_render_nullable(spec.get('max_outgoing'))}",
        f"    min_outgoing: {int(spec.get('min_outgoing', 0))}",
        f"    min_incoming: {int(spec.get('min_incoming', 0))}",
        f"    max_incoming: {_render_nullable(spec.get('max_incoming'))}",
    ]
    return "{\n" + ",\n".join(parts) + ",\n}"


def _render_nullable(value) -> str:
    """Render a JSON value as a TS literal; null stays null, numbers
    stay numbers, strings stay strings (quoted)."""
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def generate(raw: dict) -> str:
    groups_raw = raw.get("groups") or {}
    rules_raw = raw.get("rules") or {}
    edge_kinds_raw = raw.get("edge_kinds") or {}

    groups = _resolve_group_spec(groups_raw)
    executable = groups.get("executable", [])
    tool_source = groups.get("tool_source", [])
    # RAG / knowledge sources — sets the kind=knowledge_attachment on
    # outgoing edges so the IR builder + agent wiring pick the right
    # path. New in [[gleaming-munching-grove]].
    knowledge_source = groups.get("knowledge_source", [])

    # Sort every collection for byte-stable output.
    rule_keys = sorted(k for k in rules_raw if not _is_doc_key(k))
    kind_keys = sorted(k for k in edge_kinds_raw if not _is_doc_key(k))

    lines = [
        "/**",
        " * GENERATED FILE — DO NOT EDIT.",
        " *",
        " * Source of truth: shared/connection_rules.json.",
        " * Regenerate with:  python scripts/generate_connection_rules_ts.py",
        " * CI check:         python scripts/check_connection_rules_consistency.py",
        " *",
        " * The rules table used to be loaded by both the Python",
        " * backend (connection_rules.py) and the TS frontend",
        " * (connectionValidation.ts), each expanding @group refs",
        " * independently. Drift was possible. This codegen pass",
        " * runs once, expanding the JSON's @group refs and emitting a",
        " * frozen TS module — the runtime imports nothing from the JSON.",
        " */",
        "import type { NodeType } from '../types/workflow';",
        "",
    ]

    # GROUPS — frozen record.
    lines.append("export const GROUPS: Readonly<Record<string, ReadonlyArray<NodeType>>> = {")
    for name in sorted(groups.keys()):
        members = groups[name]
        lines.append(f"  {json.dumps(name)}: {json.dumps(members)},")
    lines.append("};")
    lines.append("")

    # EXECUTABLE_TYPES / TOOL_SOURCE_TYPES / KNOWLEDGE_SOURCE_TYPES — derived convenience sets.
    lines.append(
        f"export const EXECUTABLE_TYPES: ReadonlySet<NodeType> = "
        f"new Set<NodeType>({json.dumps(executable)});"
    )
    lines.append(
        f"export const TOOL_SOURCE_TYPES: ReadonlySet<NodeType> = "
        f"new Set<NodeType>({json.dumps(tool_source)});"
    )
    # RAG / knowledge sources — the canvas uses this set to tag outgoing
    # edges as `kind: 'knowledge_attachment'` so the IR builder routes
    # them through the knowledge_attachment walker (parallel to how
    # TOOL_SOURCE_TYPES routes through tool_attachment).
    lines.append(
        f"export const KNOWLEDGE_SOURCE_TYPES: ReadonlySet<NodeType> = "
        f"new Set<NodeType>({json.dumps(knowledge_source)});"
    )
    lines.append("")

    # ConnectionRule type.
    lines.extend([
        "export interface ConnectionRule {",
        "  /** Who is allowed to have an outgoing edge INTO this node. */",
        "  allowed_source_types: ReadonlySet<NodeType>;",
        "  /** Which targets this node may connect to via outgoing edges. */",
        "  allowed_target_types: ReadonlySet<NodeType>;",
        "  max_outgoing: number | null;",
        "  min_outgoing: number;",
        "  min_incoming: number;",
        "  max_incoming: number | null;",
        "}",
        "",
    ])

    # CONNECTION_RULES (dataflow).
    lines.append(
        "/** Dataflow rule table — top-level `rules` block in the JSON. "
        "Default kind. */"
    )
    lines.append(
        "export const CONNECTION_RULES: Readonly<Record<NodeType, ConnectionRule>> = {"
    )
    for type_name in rule_keys:
        spec = rules_raw[type_name]
        lines.append(f"  {json.dumps(type_name)}: {_render_rule(spec, groups_raw)},")
    lines.append("};")
    lines.append("")

    # Per-edge-kind rule tables — one frozen `*_RULES` constant per
    # entry under `edge_kinds` in the JSON. Generalized from the prior
    # hardcoded `TOOL_ATTACHMENT_RULES` so adding a new edge kind (e.g.
    # `knowledge_attachment`) requires zero generator edits — just an
    # entry in `shared/connection_rules.json`. The 2nd-caller kernel
    # trap (tool_attachment → knowledge_attachment) flipped the
    # switch.
    rules_per_kind: list[tuple[str, str, dict]] = []
    for kind_name in kind_keys:
        block = edge_kinds_raw.get(kind_name) or {}
        nested = block.get("rules") or {}
        if not nested:
            continue
        rules_per_kind.append((kind_name, kind_name.upper(), nested))

    for kind_name, kind_const, nested in rules_per_kind:
        nested_keys = sorted(k for k in nested if not _is_doc_key(k))
        lines.append(
            f"/** {kind_name} rule table — `edge_kinds.{kind_name}.rules` "
            "in the JSON. */"
        )
        lines.append(
            f"export const {kind_const}_RULES: "
            f"Readonly<Record<NodeType, ConnectionRule>> = {{"
        )
        for type_name in nested_keys:
            spec = nested[type_name]
            lines.append(f"  {json.dumps(type_name)}: {_render_rule(spec, groups_raw)},")
        lines.append("};")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    if not RULES_PATH.exists():
        print(f"FAIL: rules JSON missing at {RULES_PATH}", file=sys.stderr)
        return 1
    try:
        raw = _load_rules()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot parse {RULES_PATH}: {e}", file=sys.stderr)
        return 1

    body = generate(raw)

    # Atomic write — temp file + rename, so a crash mid-write doesn't
    # leave a half-written generated.ts on disk.
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)

    # Summary line — count rules per edge kind so adding a new edge
    # kind shows up in the generator output. Format:
    # `wrote … (N dataflow rules, M tool_attachment rules, K knowledge_attachment rules)`.
    raw_rules = raw.get("rules", {})
    raw_edge_kinds = raw.get("edge_kinds") or {}
    summary_parts = [f"{len(raw_rules)} dataflow rules"]
    for kind_name in sorted(k for k in raw_edge_kinds if not _is_doc_key(k)):
        block = raw_edge_kinds.get(kind_name) or {}
        nested = block.get("rules") or {}
        summary_parts.append(f"{len(nested)} {kind_name} rules")
    print(
        f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
        f"({', '.join(summary_parts)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())